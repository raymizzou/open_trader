"""Small, durable SQLite store for the prediction-market execution boundary."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
import threading
import uuid
import zlib
from contextlib import contextmanager, nullcontext
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping
from zoneinfo import ZoneInfo

from .polymarket_lp_errors import LpObservationWait
from .polymarket_lp_notification_batches import matching_batch_channels
from open_trader.llm_providers import DEFAULT_PROVIDER, PROVIDER_IDS
from open_trader.prediction_arbitrage import MAX_CROSS_UNSETTLED_PRINCIPAL
from open_trader.prediction_n_leg import fingerprint as canonical_fingerprint
from open_trader.prediction_n_leg_episodes import CLOSE_NO_QUALIFIED_OPPORTUNITY
from open_trader.polymarket_lp_risk import (
    LP_QUEUE_PROTECTION_THRESHOLD,
    TERMINAL_ORDER_STATES,
    _items,
    _maybe_decimal,
)

StoreHistoryKind = Literal["signals", "executions", "incidents"]
SignalHistoryWindow = Literal["24h", "7d", "30d", "all"]

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 5_000
_SLOW_TRANSACTION_SECONDS = 1.0
# Reserved lp_sessions.session_id holding manual-cancel audit anchor rows.
# Never returned as a "latest" session and skipped by daily-report assembly;
# direct reads (lp_session) still work.
LP_RESERVED_MANUAL_SESSION_ID = "manual"
_LLM_USAGE_RETENTION = timedelta(days=7)
_PREVIEW_TTL = timedelta(seconds=10)
_LP_BOOK_SAMPLE_RETENTION = timedelta(minutes=65)
_LP_PRICE_HISTORY_VALIDITY = timedelta(hours=24)
_LP_PREPARATION_RETRY_DELAYS_SECONDS = (300, 600, 1200, 1800, 1800)
_CROSS_AUTO_DAILY_PRINCIPAL_CAP = Decimal("100")
_CROSS_AUTO_MODES = frozenset({"observe_only", "manual_confirm", "auto_submit"})
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_TERMINAL_EXECUTION_STATES = (
    "both_rejected",
    "complete",
    "holding_to_resolution",
    "neutralized_incident",
    "directional_incident",
    "merge_incident",
    "submit_failed_cleared",
)

_LP_OPERATOR_ERROR_MARKERS = (
    "auth",
    "cert",
    "ssl",
    "credential",
    "config",
    "schema",
    "forbidden",
    "unauthor",
    "requestrejected",
    "invalid",
)

_NOTIFICATION_KINDS = {
    "order_ready": {
        "state": "notification_state",
        "attempts": "notification_attempts",
        "lease_id": "notification_lease_id",
        "lease_expires_at": "notification_lease_expires_at",
        "sent_at": "notification_sent_at",
        "error_code": "notification_error_code",
    },
    "observation": {
        "state": "observation_state",
        "attempts": "observation_attempts",
        "lease_id": "observation_lease_id",
        "lease_expires_at": "observation_lease_expires_at",
        "sent_at": "observation_sent_at",
        "error_code": "observation_error_code",
    },
}

# These are deliberately field-name based: the store is an audit ledger, not a
# credential vault. Values belonging to these fields never cross the SQLite
# boundary, even when a caller accidentally includes them in a larger payload.
_PRIVATE_FIELD_PARTS = (
    "api_key",
    "apikey",
    "api_token",
    "access_token",
    "refresh_token",
    "auth_token",
    "jwt",
    "session_token",
    "builder_key",
    "builder_secret",
    "builder_passphrase",
    "credential",
    "private_key",
    "privatekey",
    "password",
    "passphrase",
    "secret",
    "signature",
    "signed",
    "raw_",
    "raw_tick",
    "ticks",
    "websocket",
    "order_payload",
)
_PUBLIC_RELATION_TOKEN_FIELDS = frozenset(
    {
        "token_id",
        "yes_token_id",
        "no_token_id",
        "predict_yes_token_id",
        "predict_no_token_id",
        "polymarket_yes_token_id",
        "polymarket_no_token_id",
    }
)
# Preflight 公开状态键：值只有 "pass"/"fail" 等短状态，名字含 "signed" 但
# 不携带任何签名材料。精确匹配该键时仅豁免子串丢弃规则，其余过滤器语义
# （token 名、sensitive 名、其他含 signed 子串的键）不变。
_PUBLIC_PREFLIGHT_STATUS_FIELDS = frozenset(
    {
        "fok_pair_signed_not_submitted",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid timestamp: {value!r}") from exc
    else:
        raise ValueError(f"invalid timestamp: {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _canonical_timestamp(value: object) -> str:
    return _parse_timestamp(value).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _decimal_string(value: Any) -> str:
    # Decimal is intentionally imported lazily so ordinary payloads remain
    # lightweight; Decimal's fixed-point formatter avoids exponent notation.
    from decimal import Decimal

    if not value.is_finite():
        raise ValueError("non-finite decimal cannot be persisted")
    return format(value, "f")


def _safe_value(
    value: Any,
    *,
    key: str | None = None,
    allow_public_token_ids: bool = False,
) -> Any:
    """Return JSON-safe data while dropping credential/tick-shaped fields."""

    if key is not None:
        normalized = _normalise_field_name(key)
        token_name = (
            normalized == "token"
            or normalized.endswith("_token")
            or normalized.endswith("_token_id")
            or normalized.endswith("_token_ids")
            or normalized in {"token_id", "token_ids"}
        )
        sensitive_name = normalized in {"auth", "authorization", "bearer"}
        public_token = normalized in _PUBLIC_RELATION_TOKEN_FIELDS
        public_status = normalized in _PUBLIC_PREFLIGHT_STATUS_FIELDS
        if (
            (token_name and not (allow_public_token_ids and public_token))
            or sensitive_name
            or (
                any(part in normalized for part in _PRIVATE_FIELD_PARTS)
                and not public_status
            )
        ):
            return _DROPPED
    from decimal import Decimal

    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            child_key = str(raw_key)
            child = _safe_value(
                raw_value,
                key=child_key,
                allow_public_token_ids=allow_public_token_ids,
            )
            if child is not _DROPPED:
                cleaned[child_key] = child
        return cleaned
    if isinstance(value, (list, tuple)):
        cleaned_list = []
        for item in value:
            child = _safe_value(item, allow_public_token_ids=allow_public_token_ids)
            if child is not _DROPPED:
                cleaned_list.append(child)
        return cleaned_list
    if isinstance(value, float):
        # json.dumps allows NaN by default; reject it at this trust boundary.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite number cannot be persisted")
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    raise TypeError(f"unsupported payload value: {type(value).__name__}")


class _Dropped:
    pass


_DROPPED = _Dropped()

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalise_field_name(key: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", key).lower().replace("-", "_")


def _dump_payload(payload: Mapping[str, object]) -> str:
    cleaned = _safe_value(payload)
    if cleaned is _DROPPED or not isinstance(cleaned, dict):
        raise TypeError("payload must be a mapping")
    return json.dumps(
        cleaned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dump_relation_payload(payload: Mapping[str, object]) -> str:
    cleaned = _safe_value(payload, allow_public_token_ids=True)
    if cleaned is _DROPPED or not isinstance(cleaned, dict):
        raise TypeError("payload must be a mapping")
    return json.dumps(
        cleaned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dump_execution_payload(payload: Mapping[str, object]) -> str:
    """Keep public outcome IDs needed to reconcile durable executions."""

    cleaned = _safe_value(payload, allow_public_token_ids=True)
    if cleaned is _DROPPED or not isinstance(cleaned, dict):
        raise TypeError("payload must be a mapping")
    return json.dumps(
        cleaned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_payload(raw: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("stored payload is not an object")
    return value


def _new_id() -> str:
    return uuid.uuid4().hex


def _row_payload(row: sqlite3.Row, *, fields: Mapping[str, object]) -> dict[str, object]:
    result = _load_payload(str(row["payload"]))
    result.update(fields)
    return result


def _n_leg_enabled_scopes(raw: object) -> list[dict[str, object]]:
    """Decode the enabled-execution list, failing closed on malformed storage."""
    if raw is None:
        return []
    try:
        value = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    enabled = []
    for item in value:
        if not isinstance(item, dict):
            return []
        scope_id = item.get("scope_id")
        scope_version = item.get("scope_version")
        if not isinstance(scope_id, str) or not scope_id:
            return []
        if type(scope_version) is not int or scope_version < 1:
            return []
        enabled.append({"scope_id": scope_id, "scope_version": scope_version})
    return enabled


#: Reader fence for issue #60's N_LEG cutover: once the migration publishes
#: N_LEG capital units, readers below this generation would misread the
#: ledger. Lives beside the schema_metadata DDL (seeded at 1) and only ever
#: moves up via `advance_minimum_reader_generation`.
N_LEG_READER_GENERATION = 2


def read_minimum_reader_generation(data_dir: Path) -> int:
    path = Path(data_dir) / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    if not path.exists():
        return 1
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_metadata'"
        ).fetchone()
        if table is None:
            return 1
        row = connection.execute(
            "SELECT minimum_reader_generation FROM schema_metadata WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ValueError("prediction minimum reader generation is missing")
        generation = row[0]
        if type(generation) is not int or generation < 1:
            raise ValueError("prediction minimum reader generation is invalid")
        return generation
    finally:
        connection.close()


def load_relation_state_readonly(data_dir: Path) -> dict[str, object] | None:
    path = Path(data_dir) / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT payload FROM relation_state WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _load_payload(str(row[0]))


class PredictionArbitrageStore:
    """Direct sqlite3 persistence with one short-lived connection per action."""

    def __init__(self, data_dir: Path, *, initialize: bool = True) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._read_connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                self._create_schema(connection)
        elif not self.path.is_file():
            raise ValueError("existing recovery database required")
        self._cache_hits: dict[str, int] = {}
        self._cache_hits_lock = threading.Lock()
        self._lp_preparation_owner_handle: Any | None = None
        self._lp_preparation_owner_mutex = threading.Lock()
        self._lp_metadata_cache_ready = False
        self._lp_metadata_cache_schema_lock = threading.Lock()
        self._lp_trade_change_listener: Callable[[str], None] | None = None
        self._lp_trade_change_listener_lock = threading.Lock()
        if initialize:
            self.prune_llm_usage()
            self._truncate_wal()

    def _truncate_wal(self) -> None:
        """Best-effort startup `wal_checkpoint(TRUNCATE)`.

        The retired runtime snapshot table used to grow the WAL by several
        MB every second; a long-lived process could otherwise keep a huge
        WAL alive forever.  A failed checkpoint must never block startup.
        """

        try:
            with self._read_connection() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.warning("prediction_arbitrage_wal_checkpoint_failed", exc_info=True)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = True, prepared_at: float | None = None,
                     diagnostics: dict | None = None, prepare_seconds: float = 0.0) -> Iterator[sqlite3.Connection]:
        started = monotonic()
        connection = self._connection()
        operation = sys._getframe(2).f_code.co_qualname
        begin_at = monotonic()
        acquired = body_end = commit_end = None
        try:
            changes_before = connection.total_changes
        except Exception:
            changes_before = None
        phase = "begin"
        failed = False
        wait_failure = False
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            acquired = monotonic()
            phase = "body"
            yield connection
            body_end = monotonic()
            phase = "commit"
            connection.execute("COMMIT")
            commit_end = monotonic()
            phase = "complete"
        except BaseException as error:
            failed = True
            wait_failure = isinstance(error, LpObservationWait) and str(error) in {
                'session_changed', 'account_round_invalid', 'account_snapshot_superseded', 'account_snapshot_conflict',
            }
            if phase == "body":
                body_end = monotonic()
            if phase == "commit":
                commit_end = monotonic()
            if isinstance(error, sqlite3.Error):
                try:
                    logger.warning(
                        "prediction_store_transaction_failed operation=%s phase=%s sqlite_error=%s thread=%s",
                        operation, phase, getattr(error, "sqlite_errorname", type(error).__name__),
                        threading.get_ident(),
                    )
                except Exception:
                    pass
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error as rollback_error:
                    error.add_note(f"Rollback failed: {type(rollback_error).__name__}")
                    try:
                        logger.error("prediction_store_rollback_failed operation=%s sqlite_error=%s thread=%s",
                                     operation, getattr(rollback_error, 'sqlite_errorname', type(rollback_error).__name__),
                                     threading.get_ident())
                    except Exception:
                        pass
            raise
        finally:
            finished = monotonic()
            try:
                # SQLite counts attempted changes, including triggers and
                # writes later rolled back; this is not committed-row count.
                changes = connection.total_changes - changes_before if changes_before is not None else 'unknown'
            except Exception:
                changes = 'unknown'
            connection.close()
            try:
                if finished - begin_at >= _SLOW_TRANSACTION_SECONDS:
                    logger.warning(
                        "prediction_store_transaction_slow operation=%s phase=%s wait_seconds=%.3f hold_seconds=%.3f thread=%s",
                        operation, phase,
                        (acquired if acquired is not None else finished) - begin_at,
                        finished - acquired if acquired is not None else 0.0,
                        threading.get_ident(),
                    )
                if failed or prepare_seconds + finished - (prepared_at if prepared_at is not None else started) >= _SLOW_TRANSACTION_SECONDS:
                    metrics = {key: value for key, value in (diagnostics or {}).items()
                        if key in {'input_rows', 'account_input_rows', 'registration_input_rows', 'publish_input_buys',
                                   'session_scan_calls', 'session_rows_scanned', 'actions_query_calls', 'apply_lock_wait_seconds'}
                        and type(value) in {int, float} and math.isfinite(value) and value >= 0}
                    emit = logger.info if wait_failure else logger.warning
                    emit(
                        "prediction_store_transaction_timing operation=%s phase=%s prepare_seconds=%.3f wait_seconds=%.3f body_seconds=%.3f commit_seconds=%.3f sqlite_changes=%s sqlite_changes_scope=attempted_including_triggers scan_scope=instrumented_calls metrics=%s thread=%s",
                        operation, phase, prepare_seconds + begin_at - (prepared_at if prepared_at is not None else started),
                        (acquired if acquired is not None else finished) - begin_at,
                        (body_end - acquired) if acquired is not None and body_end is not None else 0.0,
                        (commit_end - body_end) if commit_end is not None and body_end is not None else 0.0,
                        changes, metrics, threading.get_ident(),
                    )
            except Exception:
                pass

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS lp_auto_pool (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_flags (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                minimum_reader_generation INTEGER NOT NULL
                    CHECK (minimum_reader_generation >= 1)
            );

            INSERT INTO schema_metadata(singleton, minimum_reader_generation)
            VALUES (1, 1)
            ON CONFLICT(singleton) DO NOTHING;

            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS previews (
                preview_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                preview_id TEXT NOT NULL REFERENCES previews(preview_id),
                idempotency_key TEXT NOT NULL,
                singleton INTEGER NOT NULL DEFAULT 1 CHECK (singleton = 1),
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                evidence TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cross_execution_reservations (
                execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id),
                amount TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('reserved', 'released')),
                created_at TEXT NOT NULL,
                released_at TEXT,
                release_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS execution_legs (
                leg_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                leg_label TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (execution_id, leg_label)
            );

            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                payload TEXT NOT NULL,
                acknowledgement TEXT,
                acknowledged_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_usage (
                usage_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relation_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL,
                full_scanned_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS observation_pool_members (
                identity TEXT PRIMARY KEY,
                relation_type TEXT NOT NULL,
                version_id TEXT NOT NULL,
                version_fingerprint TEXT NOT NULL,
                rules_fingerprint TEXT NOT NULL,
                entered_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS observation_pool_members_entered_at
            ON observation_pool_members(entered_at, identity);

            CREATE TABLE IF NOT EXISTS relation_scan_runs (
                scan_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL CHECK (scope IN ('full', 'event', 'activity')),
                event_id TEXT,
                status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
                payload TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_open_signal_per_market
            ON signals(market_id) WHERE ended_at IS NULL;

            CREATE INDEX IF NOT EXISTS signals_market_started_at
            ON signals(market_id, started_at DESC);

            CREATE INDEX IF NOT EXISTS signals_started_at
            ON signals(started_at DESC, signal_id DESC);

            CREATE INDEX IF NOT EXISTS signals_open_started_at
            ON signals(started_at DESC, signal_id DESC) WHERE ended_at IS NULL;

            DROP INDEX IF EXISTS one_nonterminal_execution;

            CREATE UNIQUE INDEX IF NOT EXISTS one_nonterminal_execution
            ON executions(singleton)
            WHERE state NOT IN (
                'both_rejected', 'complete', 'holding_to_resolution', 'neutralized_incident',
                'directional_incident', 'merge_incident', 'submit_failed_cleared'
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_execution_per_idempotency_key
            ON executions(idempotency_key);

            CREATE INDEX IF NOT EXISTS llm_usage_created_at
            ON llm_usage(created_at);

            CREATE TABLE IF NOT EXISTS validation_mode (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                mode TEXT NOT NULL CHECK (mode IN ('observe_only', 'manual', 'auto')),
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_provider_selection (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                provider TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_eat_attempts (
                attempt_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                preview_id TEXT,
                execution_id TEXT,
                total_cost TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS auto_eat_attempts_created_at
            ON auto_eat_attempts(created_at);

            CREATE INDEX IF NOT EXISTS auto_eat_attempts_signal
            ON auto_eat_attempts(signal_id, decision);

            CREATE INDEX IF NOT EXISTS auto_eat_attempts_market
            ON auto_eat_attempts(market_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS cross_auto_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                configured_mode TEXT NOT NULL DEFAULT 'observe_only'
                    CHECK (configured_mode IN ('observe_only', 'manual_confirm', 'auto_submit')),
                armed INTEGER NOT NULL CHECK (armed IN (0, 1)),
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cross_auto_attempts (
                signal_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload TEXT NOT NULL,
                preview_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                total_cost TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS cross_auto_attempts_created_at
            ON cross_auto_attempts(created_at DESC, signal_id DESC);

            CREATE TABLE IF NOT EXISTS safety_policy (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                fingerprint TEXT NOT NULL,
                policy TEXT NOT NULL,
                git_sha TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS control_events (
                event_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                outcome TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS control_events_action_target
            ON control_events(action, target, created_at DESC);

            CREATE TABLE IF NOT EXISTS n_leg_controls (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                mode TEXT NOT NULL CHECK (mode IN ('MANUAL', 'AUTO')),
                breaker_open INTEGER NOT NULL CHECK (breaker_open IN (0, 1)),
                breaker_reason TEXT,
                active_batch_id TEXT,
                total_unsettled_capital_units INTEGER NOT NULL CHECK (total_unsettled_capital_units >= 0),
                total_unsettled_capital_version INTEGER NOT NULL DEFAULT 0,
                contract_generation INTEGER NOT NULL DEFAULT 1
                    CHECK (contract_generation >= 1),
                qualification_policy_version INTEGER NOT NULL DEFAULT 1
                    CHECK (qualification_policy_version >= 1),
                safety_config_version INTEGER NOT NULL DEFAULT 1
                    CHECK (safety_config_version >= 1),
                enabled_execution_scope_version TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS n_leg_qualification_policy (
                version INTEGER PRIMARY KEY CHECK (version >= 1),
                policy TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS n_leg_safety_config (
                version INTEGER PRIMARY KEY CHECK (version >= 1),
                config TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS n_leg_execution_scopes (
                scope_id TEXT PRIMARY KEY,
                capability TEXT NOT NULL
                    CHECK (capability IN ('OBSERVE_ONLY', 'MANUAL_CANARY', 'AUTO_ELIGIBLE')),
                scope_version INTEGER NOT NULL CHECK (scope_version >= 1),
                members TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS n_leg_lineage_claims (
                episode_lineage_id TEXT PRIMARY KEY,
                opportunity_episode_id TEXT NOT NULL,
                execution_batch_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS n_leg_batches (
                execution_batch_id TEXT PRIMARY KEY,
                opportunity_episode_id TEXT NOT NULL,
                episode_lineage_id TEXT NOT NULL UNIQUE REFERENCES n_leg_lineage_claims(episode_lineage_id),
                state TEXT NOT NULL,
                submission_enabled INTEGER NOT NULL CHECK (submission_enabled IN (0, 1)),
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS n_leg_transitions (
                transition_id TEXT PRIMARY KEY,
                execution_batch_id TEXT NOT NULL REFERENCES n_leg_batches(execution_batch_id),
                kind TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (execution_batch_id, idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS n_leg_transitions_batch_created
            ON n_leg_transitions(execution_batch_id, created_at DESC, transition_id DESC);

            CREATE TABLE IF NOT EXISTS partial_fill_proofs (
                proof_fingerprint TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS n_leg_execution_requests (
                fifo_index INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                component_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL
                    CHECK (state IN ('PENDING', 'ADMITTED', 'ABANDONED', 'SUBMITTED')),
                abandon_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS n_leg_execution_requests_state
            ON n_leg_execution_requests(state, fifo_index);

            CREATE TABLE IF NOT EXISTS lp_sessions (
                session_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lp_trade_generation (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                generation INTEGER NOT NULL CHECK (generation >= 0)
            );

            INSERT INTO lp_trade_generation(singleton, generation)
            VALUES (1, 0)
            ON CONFLICT(singleton) DO NOTHING;

            CREATE TABLE IF NOT EXISTS lp_daily_reports (
                report_date TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                generated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lp_auto_daily_reports (
                account_id TEXT NOT NULL,
                auto_run_id TEXT NOT NULL,
                report_date TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(account_id, auto_run_id, report_date)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_active_lp_session_account_token
            ON lp_sessions(lower(trim(coalesce(json_extract(payload,'$.account_id'),''))), trim(json_extract(payload,'$.token_id')))
            WHERE state NOT IN ('complete', 'entry_rejected');

            DROP INDEX IF EXISTS one_active_lp_session;
            DROP INDEX IF EXISTS one_active_lp_session_market;

            CREATE TABLE IF NOT EXISTS lp_actions (
                action_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES lp_sessions(session_id),
                action_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS lp_actions_session
            ON lp_actions(session_id, created_at, action_id);

            CREATE TABLE IF NOT EXISTS lp_first_seen_episodes (
                episode_id TEXT PRIMARY KEY,
                token_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_active_first_seen_episode
            ON lp_first_seen_episodes(token_id)
            WHERE state IN ('monitoring', 'canceling', 'blocked');

            CREATE TABLE IF NOT EXISTS lp_book_samples (
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(condition_id, token_id, received_at)
            );

            CREATE INDEX IF NOT EXISTS lp_book_samples_received
            ON lp_book_samples(received_at);

            CREATE TABLE IF NOT EXISTS lp_price_history_cache (
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                samples BLOB NOT NULL,
                summary TEXT NOT NULL,
                PRIMARY KEY(condition_id, token_id)
            );

            CREATE TABLE IF NOT EXISTS lp_market_exclusions (
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL DEFAULT '',
                reason_code TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                cooldown_until TEXT NOT NULL,
                PRIMARY KEY(condition_id, token_id)
            );
            CREATE INDEX IF NOT EXISTS lp_market_exclusions_expiry
            ON lp_market_exclusions(cooldown_until);

            CREATE TABLE IF NOT EXISTS lp_screening_snapshot (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lp_preparation (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lp_preparation_items (
                condition_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                retry_used INTEGER NOT NULL DEFAULT 0 CHECK (retry_used IN (0,1)),
                failure_count INTEGER NOT NULL DEFAULT 1 CHECK (failure_count >= 1),
                state TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
                stage TEXT NOT NULL,
                direction TEXT,
                token_id TEXT,
                error TEXT,
                failed_at TEXT,
                next_retry_at TEXT,
                retry_started_at TEXT,
                alert_attempted INTEGER NOT NULL DEFAULT 0 CHECK (alert_attempted IN (0,1)),
                alert_state TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lp_market_observations (
                account_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, condition_id)
            );

            CREATE TABLE IF NOT EXISTS lp_market_competitiveness (
                condition_id TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            """
        )
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            connection.execute("PRAGMA user_version=1")
            version = 1
        if version < 2:
            connection.execute("PRAGMA user_version=2")
            version = 2
        if version < 3:
            connection.execute("PRAGMA user_version=3")
            version = 3
        if version < 4:
            connection.execute("PRAGMA user_version=4")
            version = 4
        if version < 5:
            columns = {
                str(column[1])
                for column in connection.execute("PRAGMA table_info(cross_auto_state)")
            }
            if "configured_mode" not in columns:
                connection.execute(
                    """
                    ALTER TABLE cross_auto_state ADD COLUMN configured_mode
                    TEXT NOT NULL DEFAULT 'observe_only'
                    CHECK (configured_mode IN ('observe_only', 'manual_confirm', 'auto_submit'))
                    """
                )
            connection.execute(
                """
                UPDATE cross_auto_state
                SET configured_mode='observe_only', armed=0, reason='migration_fail_closed'
                """
            )
            connection.execute("PRAGMA user_version=5")
            version = 5
        if version < 6:
            connection.execute("PRAGMA user_version=6")
            version = 6
        if version < 7:
            connection.execute("PRAGMA user_version=7")
            version = 7
        if version < 8:
            connection.execute("PRAGMA user_version=8")
            version = 8
        if version < 9:
            columns = {
                str(column[1])
                for column in connection.execute("PRAGMA table_info(n_leg_controls)")
            }
            for name, definition in (
                (
                    "contract_generation",
                    "INTEGER NOT NULL DEFAULT 1 CHECK (contract_generation >= 1)",
                ),
                (
                    "qualification_policy_version",
                    "INTEGER NOT NULL DEFAULT 1 CHECK (qualification_policy_version >= 1)",
                ),
                (
                    "safety_config_version",
                    "INTEGER NOT NULL DEFAULT 1 CHECK (safety_config_version >= 1)",
                ),
                (
                    "enabled_execution_scope_version",
                    "TEXT NOT NULL DEFAULT '[]'",
                ),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE n_leg_controls ADD COLUMN {name} {definition}"
                    )
            connection.execute("PRAGMA user_version=9")
            version = 9
        if version < 10:
            connection.execute("PRAGMA user_version=10")
            version = 10
        if version < 11:
            # Issue #64: manual-confirm FIFO execution requests (expand-only;
            # the table itself is CREATE IF NOT EXISTS above).
            connection.execute("PRAGMA user_version=11")
            version = 11
        if version < 12:
            # Issue #64 Slice 4: atomic-admission CAS counter for the
            # unsettled-capital ledger; every unit-writing transaction bumps
            # it in the same transaction.
            columns = {
                str(column[1])
                for column in connection.execute("PRAGMA table_info(n_leg_controls)")
            }
            if "total_unsettled_capital_version" not in columns:
                connection.execute(
                    "ALTER TABLE n_leg_controls ADD COLUMN total_unsettled_capital_version INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute("PRAGMA user_version=12")
            version = 12
        if version < 13:
            # Issue #130: durable BBO history and the previous screening result.
            connection.execute("PRAGMA user_version=13")
            version = 13
        if version < 14:
            # retire-runtime-snapshot-20260917: the every-second full-snapshot
            # rewrite of `runtime` (≈5 MB/s WAL growth) is retired.  The only
            # durable fact it carried — the first-live-order flag — lives in
            # service_flags now; drop the legacy table on upgrade.
            connection.execute("DROP TABLE IF EXISTS runtime")
            connection.execute("PRAGMA user_version=14")
            version = 14
        if version < 15:
            # Issue #159: first-seen baseline protection episodes for
            # web-manual BUYs (expand-only; the table itself is CREATE IF NOT
            # EXISTS above).
            connection.execute("PRAGMA user_version=15")
            version = 15
        if version < 16:
            # LP same-round reuse needs a durable trade-generation fence so a
            # cached account bundle cannot be published across a mutation.
            connection.execute("PRAGMA user_version=16")
            version = 16

    @staticmethod
    def _execution_fields(row: sqlite3.Row) -> dict[str, object]:
        evidence = json.loads(str(row["evidence"]))
        if not isinstance(evidence, list):
            evidence = []
        return {
            "execution_id": str(row["execution_id"]),
            "preview_id": str(row["preview_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "state": str(row["state"]),
            "evidence": evidence,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _execution_result(cls, row: sqlite3.Row) -> dict[str, object]:
        return _row_payload(row, fields=cls._execution_fields(row))

    @staticmethod
    def _incident_result(row: sqlite3.Row) -> dict[str, object]:
        acknowledgement = row["acknowledgement"]
        return _row_payload(
            row,
            fields={
                "incident_id": str(row["incident_id"]),
                "execution_id": str(row["execution_id"]),
                "acknowledged": row["acknowledged_at"] is not None,
                "acknowledged_at": row["acknowledged_at"],
                "acknowledgement": (
                    json.loads(str(acknowledgement)) if acknowledgement is not None else None
                ),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            },
        )

    @staticmethod
    def _signal_result(row: sqlite3.Row) -> dict[str, object]:
        return _row_payload(
            row,
            fields={
                "signal_id": str(row["signal_id"]),
                "market_id": str(row["market_id"]),
                "started_at": str(row["started_at"]),
                "ended_at": row["ended_at"],
                "updated_at": str(row["updated_at"]),
            },
        )

    @staticmethod
    def _canonical_signal_payload(payload: dict[str, object]) -> dict[str, object]:
        for field in ("started_at", "first_positive_at", "ended_at"):
            if field in payload and payload[field] is not None:
                payload[field] = _canonical_timestamp(payload[field])
        return payload

    def save_relation_state(
        self, payload: Mapping[str, object], *, full_scanned_at: str
    ) -> None:
        encoded = _dump_relation_payload(payload)
        scanned = _canonical_timestamp(full_scanned_at)
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO relation_state(singleton, payload, full_scanned_at, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    payload=excluded.payload,
                    full_scanned_at=excluded.full_scanned_at,
                    updated_at=excluded.updated_at
                """,
                (encoded, scanned, now),
            )

    def load_relation_state(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM relation_state WHERE singleton=1"
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    def load_observation_pool_members(self) -> dict[str, dict[str, str]]:
        """Read the small durable observation membership set."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT identity, relation_type, version_id, "
                "version_fingerprint, rules_fingerprint, entered_at "
                "FROM observation_pool_members ORDER BY identity"
            ).fetchall()
        return {
            str(row["identity"]): {
                "identity": str(row["identity"]),
                "relation_type": str(row["relation_type"]),
                "version_id": str(row["version_id"]),
                "version_fingerprint": str(row["version_fingerprint"]),
                "rules_fingerprint": str(row["rules_fingerprint"]),
                "entered_at": str(row["entered_at"]),
            }
            for row in rows
        }

    def save_observation_pool_members(
        self, members: Mapping[str, Mapping[str, object]]
    ) -> None:
        """Atomically replace observation membership metadata.

        The observation component stores identities and input fingerprints only;
        quotes and result history deliberately stay in its in-memory snapshot.
        """

        normalized: list[tuple[str, str, str, str, str, str]] = []
        for identity, member in members.items():
            if not isinstance(member, Mapping):
                raise ValueError("observation member must be a mapping")
            key = str(identity).strip()
            if not key:
                raise ValueError("observation member identity is required")
            values = (
                key,
                str(member.get("relation_type") or "").strip(),
                str(member.get("version_id") or "").strip(),
                str(member.get("version_fingerprint") or "").strip(),
                str(member.get("rules_fingerprint") or "").strip(),
                _canonical_timestamp(member.get("entered_at")),
            )
            if not all(values[:5]):
                raise ValueError("observation member metadata is incomplete")
            normalized.append(values)
        if len(normalized) > 10:
            raise ValueError("observation pool cannot exceed ten members")
        with self._transaction() as connection:
            identities = {row[0] for row in normalized}
            existing = {
                str(row[0])
                for row in connection.execute(
                    "SELECT identity FROM observation_pool_members"
                )
            }
            removed = existing - identities
            if removed:
                placeholders = ",".join("?" for _ in removed)
                connection.execute(
                    "DELETE FROM observation_pool_members WHERE identity IN ("
                    + placeholders
                    + ")",
                    tuple(sorted(removed)),
                )
            connection.executemany(
                """
                INSERT INTO observation_pool_members(
                    identity, relation_type, version_id, version_fingerprint,
                    rules_fingerprint, entered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET
                    relation_type=excluded.relation_type,
                    version_id=excluded.version_id,
                    version_fingerprint=excluded.version_fingerprint,
                    rules_fingerprint=excluded.rules_fingerprint,
                    entered_at=excluded.entered_at
                """,
                normalized,
            )

    def record_relation_scan(
        self,
        *,
        scope: Literal["full", "event", "activity"],
        status: Literal["completed", "failed"],
        started_at: str,
        completed_at: str,
        payload: Mapping[str, object],
        event_id: str | None = None,
    ) -> str:
        if scope not in {"full", "event", "activity"}:
            raise ValueError("unsupported relation scan scope")
        if status not in {"completed", "failed"}:
            raise ValueError("unsupported relation scan status")
        started = _canonical_timestamp(started_at)
        completed = _canonical_timestamp(completed_at)
        encoded = _dump_payload(payload)
        scan_id = _new_id()
        with self._transaction() as connection:
            cutoff = _canonical_timestamp(
                _parse_timestamp(_utc_now()) - timedelta(days=7)
            )
            connection.execute(
                """
                DELETE FROM relation_scan_runs
                WHERE scope='activity' AND completed_at < ?
                """,
                (cutoff,),
            )
            connection.execute(
                """
                INSERT INTO relation_scan_runs(
                    scan_id, scope, event_id, status, payload, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (scan_id, scope, event_id, status, encoded, started, completed),
            )
        return scan_id

    def relation_scan_history(
        self, *, scope: str | None = None, limit: int = 20
    ) -> list[dict[str, object]]:
        if scope is not None and scope not in {"full", "event", "activity"}:
            raise ValueError("unsupported relation scan scope")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        if limit == 0:
            return []
        query = "SELECT * FROM relation_scan_runs"
        parameters: tuple[object, ...] = ()
        if scope is not None:
            query += " WHERE scope=?"
            parameters = (scope,)
        query += (
            " ORDER BY completed_at DESC, scope ASC, scan_id DESC LIMIT ?"
        )
        parameters += (limit,)
        with self._read_connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            _row_payload(
                row,
                fields={
                    "scan_id": str(row["scan_id"]),
                    "scope": str(row["scope"]),
                    "event_id": row["event_id"],
                    "status": str(row["status"]),
                    "started_at": str(row["started_at"]),
                    "completed_at": str(row["completed_at"]),
                },
            )
            for row in rows
        ]

    def set_first_live_order_validated(self, validated_at: str) -> None:
        """Persist the durable first-live-order flag in service_flags."""

        encoded = _dump_payload(
            {"status": "validated", "validated_at": validated_at}
        )
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO service_flags(singleton, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (encoded, now),
            )

    def first_live_order_state(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM service_flags WHERE singleton=1"
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    @staticmethod
    def _insert_control_event(
        connection: sqlite3.Connection,
        *,
        action: str,
        target: str,
        outcome: str,
        payload: Mapping[str, object],
        event_id: str | None = None,
        now: str | None = None,
    ) -> str:
        identifier = event_id or _new_id()
        timestamp = now or _utc_now()
        connection.execute(
            """
            INSERT INTO control_events(
                event_id, action, target, outcome, payload, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                str(action),
                str(target),
                str(outcome),
                _dump_payload(payload),
                timestamp,
                timestamp,
            ),
        )
        return identifier

    def latest_control_event(
        self, action: str, target: str
    ) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT event_id, action, target, outcome, payload, created_at, updated_at
                FROM control_events
                WHERE action=? AND target=?
                ORDER BY created_at DESC, event_id DESC
                LIMIT 1
                """,
                (str(action), str(target)),
            ).fetchone()
        if row is None:
            return None
        return self._control_event_result(row)

    @staticmethod
    def _control_event_result(row: sqlite3.Row) -> dict[str, object]:
        return {
            "event_id": str(row["event_id"]),
            "action": str(row["action"]),
            "target": str(row["target"]),
            "outcome": str(row["outcome"]),
            "payload": _load_payload(str(row["payload"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def begin_control_event(
        self, *, action: str, target: str, payload: Mapping[str, object]
    ) -> str:
        with self._transaction() as connection:
            return self._insert_control_event(
                connection,
                action=action,
                target=target,
                outcome="started",
                payload=payload,
            )

    def finish_control_event(
        self,
        event_id: str,
        *,
        outcome: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if outcome not in {"succeeded", "rejected", "failed"}:
            raise ValueError("invalid terminal control outcome")
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT outcome, payload FROM control_events WHERE event_id=?",
                (str(event_id),),
            ).fetchone()
            if row is None:
                raise ValueError("control event does not exist")
            if str(row["outcome"]) != "started":
                raise ValueError("control event is already terminal")
            merged = _load_payload(str(row["payload"]))
            merged.update(dict(payload))
            connection.execute(
                """
                UPDATE control_events
                SET outcome=?, payload=?, updated_at=?
                WHERE event_id=? AND outcome='started'
                """,
                (outcome, _dump_payload(merged), now, str(event_id)),
            )
            finished = connection.execute(
                """
                SELECT event_id, action, target, outcome, payload, created_at, updated_at
                FROM control_events WHERE event_id=?
                """,
                (str(event_id),),
            ).fetchone()
            assert finished is not None
            return self._control_event_result(finished)

    def safety_policy(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT fingerprint, policy, git_sha, updated_at
                FROM safety_policy WHERE singleton=1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "fingerprint": str(row["fingerprint"]),
            "policy": _load_payload(str(row["policy"])),
            "git_sha": str(row["git_sha"]),
            "updated_at": str(row["updated_at"]),
        }

    def apply_safety_policy(
        self, policy: Mapping[str, object], *, git_sha: str
    ) -> dict[str, object]:
        encoded = _dump_payload(policy)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT fingerprint FROM safety_policy WHERE singleton=1"
            ).fetchone()
            previous = None if existing is None else str(existing["fingerprint"])
            if previous == fingerprint:
                return {
                    "state": "unchanged",
                    "fingerprint": fingerprint,
                    "previous_fingerprint": previous,
                    "downgraded": False,
                }

            downgraded = False
            if previous is not None:
                mode = connection.execute(
                    "SELECT mode FROM validation_mode WHERE singleton=1"
                ).fetchone()
                if mode is not None and str(mode["mode"]) == "auto":
                    connection.execute(
                        "UPDATE validation_mode SET mode='manual', updated_at=? WHERE singleton=1",
                        (now,),
                    )
                    downgraded = True
                cross = self._cross_auto_state_from_connection(connection)
                if cross["configured_mode"] == "auto_submit" or cross["armed"] is True:
                    connection.execute(
                        """
                        INSERT INTO cross_auto_state(
                            singleton, configured_mode, armed, reason, updated_at
                        ) VALUES (1, 'manual_confirm', 0, 'safety_policy_changed', ?)
                        ON CONFLICT(singleton) DO UPDATE SET
                            configured_mode='manual_confirm',
                            armed=0,
                            reason='safety_policy_changed',
                            updated_at=excluded.updated_at
                        """,
                        (now,),
                    )
                    downgraded = True

            connection.execute(
                """
                INSERT INTO safety_policy(singleton, fingerprint, policy, git_sha, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    policy=excluded.policy,
                    git_sha=excluded.git_sha,
                    updated_at=excluded.updated_at
                """,
                (fingerprint, encoded, str(git_sha), now),
            )
            outcome = (
                "baseline_enrolled" if previous is None else "safety_policy_changed"
            )
            self._insert_control_event(
                connection,
                action="safety_policy",
                target="production",
                outcome=outcome,
                payload={
                    "actor": "system",
                    "before_fingerprint": previous,
                    "after_fingerprint": fingerprint,
                    "downgraded": downgraded,
                    "git_sha": str(git_sha),
                },
                now=now,
            )
        return {
            "state": (
                "baseline_enrolled"
                if previous is None
                else "downgraded" if downgraded else "updated"
            ),
            "fingerprint": fingerprint,
            "previous_fingerprint": previous,
            "downgraded": downgraded,
        }

    @staticmethod
    def _cross_auto_state_from_connection(
        connection: sqlite3.Connection,
    ) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT configured_mode, armed, reason, updated_at
            FROM cross_auto_state WHERE singleton=1
            """
        ).fetchone()
        if (
            row is None
            or row["configured_mode"] not in _CROSS_AUTO_MODES
            or row["armed"] not in (0, 1)
            or (row["armed"] == 1 and row["configured_mode"] != "auto_submit")
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
            or not isinstance(row["updated_at"], str)
            or not row["updated_at"].strip()
        ):
            return {
                "configured_mode": "observe_only",
                "armed": False,
                "reason": "not_armed",
                "updated_at": None,
            }
        return {
            "configured_mode": str(row["configured_mode"]),
            "armed": bool(row["armed"]),
            "reason": str(row["reason"]),
            "updated_at": str(row["updated_at"]),
        }

    def cross_auto_state(self) -> dict[str, object]:
        try:
            with self._read_connection() as connection:
                return self._cross_auto_state_from_connection(connection)
        except Exception:
            return {
                "configured_mode": "observe_only",
                "armed": False,
                "reason": "not_armed",
                "updated_at": None,
            }

    def _set_cross_auto_state(
        self,
        *,
        armed: bool,
        reason: str,
        configured_mode: str | None = None,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cross auto state reason is required")
        if configured_mode is not None and configured_mode not in _CROSS_AUTO_MODES:
            raise ValueError("invalid cross auto mode")
        updated_at = _utc_now()
        with self._transaction() as connection:
            current = self._cross_auto_state_from_connection(connection)
            target_mode = (
                current["configured_mode"] if configured_mode is None else configured_mode
            )
            changed = not (
                current["configured_mode"] == target_mode
                and current["armed"] is armed
                and current["reason"] == reason
            )
            if changed:
                connection.execute(
                    """
                    INSERT INTO cross_auto_state(singleton, configured_mode, armed, reason, updated_at)
                    VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        configured_mode=excluded.configured_mode,
                        armed=excluded.armed,
                        reason=excluded.reason,
                        updated_at=excluded.updated_at
                    """,
                    (target_mode, int(armed), reason, updated_at),
                )
            result = self._cross_auto_state_from_connection(connection)
            if audit is not None:
                self._insert_control_event(
                    connection,
                    action="pause_cross_auto",
                    target="cross_auto",
                    outcome="succeeded" if changed else "no_op",
                    payload={**dict(audit), "before": current, "after": result},
                )
            return result

    def set_cross_auto_mode(self, mode: str, reason: str) -> dict[str, object]:
        if not isinstance(mode, str) or mode not in _CROSS_AUTO_MODES:
            raise ValueError("invalid cross auto mode")
        return self._set_cross_auto_state(
            configured_mode=mode,
            armed=False,
            reason=reason,
        )

    def pause_cross_auto(
        self, reason: str, *, audit: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        return self._set_cross_auto_state(armed=False, reason=reason, audit=audit)

    def arm_cross_auto(self) -> dict[str, object]:
        return self._set_cross_auto_state(
            configured_mode="auto_submit",
            armed=True,
            reason="armed",
        )

    @staticmethod
    def _cross_auto_attempt_payload(
        *,
        reason: str,
        reason_zh: str,
        current: object,
        limit: object,
        venue: str,
        operator_action_required: bool,
        operator_action: str,
        signal_id: str,
        opportunity_id: str,
    ) -> str:
        return _dump_payload(
            {
                "reason_code": reason,
                "reason_zh": reason_zh,
                "current": current,
                "limit": limit,
                "venue": venue,
                "operator_action_required": operator_action_required,
                "operator_action": operator_action,
                "signal_id": signal_id,
                "opportunity_id": opportunity_id,
            }
        )

    def _claim_cross_auto_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        signal: str,
        opportunity: str,
        now: str,
    ) -> dict[str, str]:
        """Insert the one-shot attempt and gate it on durable authority."""

        payload = self._cross_auto_attempt_payload(
            reason="claimed",
            reason_zh="",
            current=None,
            limit=None,
            venue="",
            operator_action_required=False,
            operator_action="",
            signal_id=signal,
            opportunity_id=opportunity,
        )
        try:
            connection.execute(
                """
                INSERT INTO cross_auto_attempts(
                    signal_id, opportunity_id, decision, reason, payload,
                    preview_id, execution_id, total_cost, created_at, updated_at
                ) VALUES (?, ?, 'claimed', 'claimed', ?, '', '', NULL, ?, ?)
                """,
                (signal, opportunity, payload, now, now),
            )
        except sqlite3.IntegrityError:
            return {"state": "signal_already_attempted"}
        state = self._cross_auto_state_from_connection(connection)
        if state["configured_mode"] != "auto_submit":
            return {
                "state": "rejected",
                "reason": "configured_mode_not_auto_submit",
                "current": str(state["configured_mode"]),
            }
        if state["armed"] is not True:
            return {"state": "rejected", "reason": "cross_auto_paused"}
        return {"state": "claimed"}

    def claim_cross_auto_attempt(
        self, signal_id: str, opportunity_id: str
    ) -> dict[str, str]:
        signal = str(signal_id).strip()
        opportunity = str(opportunity_id).strip()
        if not signal or not opportunity:
            raise ValueError("signal_id and opportunity_id are required")
        now = _utc_now()
        with self._transaction() as connection:
            return self._claim_cross_auto_attempt(
                connection,
                signal=signal,
                opportunity=opportunity,
                now=now,
            )

    def finish_cross_auto_attempt(
        self,
        signal_id: str,
        *,
        decision: str,
        reason: str,
        reason_zh: str,
        current: object = None,
        limit: object = None,
        venue: str = "",
        operator_action_required: bool = False,
        operator_action: str = "",
        preview_id: str = "",
        execution_id: str = "",
        total_cost: object = None,
    ) -> dict[str, object]:
        signal = str(signal_id).strip()
        if not signal or not str(decision).strip() or not str(reason).strip():
            raise ValueError("signal_id, decision, and reason are required")
        if not all(
            isinstance(value, str) for value in (reason_zh, venue, operator_action)
        ):
            raise ValueError("reason_zh, venue, and operator_action must be strings")
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT opportunity_id FROM cross_auto_attempts WHERE signal_id=? AND decision='claimed'",
                (signal,),
            ).fetchone()
            if row is None:
                raise KeyError(signal)
            opportunity = str(row["opportunity_id"])
            payload = self._cross_auto_attempt_payload(
                reason=str(reason),
                reason_zh=reason_zh,
                current=current,
                limit=limit,
                venue=venue,
                operator_action_required=bool(operator_action_required),
                operator_action=operator_action,
                signal_id=signal,
                opportunity_id=opportunity,
            )
            connection.execute(
                """
                UPDATE cross_auto_attempts
                SET decision=?, reason=?, payload=?, preview_id=?, execution_id=?, total_cost=?, updated_at=?
                WHERE signal_id=? AND decision='claimed'
                """,
                (
                    str(decision),
                    str(reason),
                    payload,
                    str(preview_id),
                    str(execution_id),
                    None if total_cost is None else format(Decimal(str(total_cost)), "f"),
                    now,
                    signal,
                ),
            )
        return self.cross_auto_attempts(limit=1, signal_id=signal)[0]

    def cross_auto_attempts(
        self, limit: int = 100, *, signal_id: str | None = None
    ) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        if limit == 0:
            return []
        query = "SELECT * FROM cross_auto_attempts"
        parameters: tuple[object, ...] = ()
        if signal_id is not None:
            query += " WHERE signal_id=?"
            parameters = (str(signal_id),)
        query += " ORDER BY created_at DESC, signal_id DESC LIMIT ?"
        parameters += (limit,)
        with self._read_connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                **_load_payload(str(row["payload"])),
                "decision": str(row["decision"]),
                "reason": str(row["reason"]),
                "preview_id": str(row["preview_id"]),
                "execution_id": str(row["execution_id"]),
                "total_cost": row["total_cost"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    VALIDATION_MODES = frozenset({"observe_only", "manual", "auto"})
    LLM_PROVIDERS = frozenset(PROVIDER_IDS)

    def get_llm_provider(self, *, default: str = DEFAULT_PROVIDER) -> str:
        fallback = default if default in self.LLM_PROVIDERS else DEFAULT_PROVIDER
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT provider FROM llm_provider_selection WHERE singleton=1"
            ).fetchone()
        if row is None or str(row["provider"]) not in self.LLM_PROVIDERS:
            return fallback
        return str(row["provider"])

    def set_llm_provider(
        self,
        provider: str,
        *,
        default: str = DEFAULT_PROVIDER,
        audit: Mapping[str, object] | None = None,
    ) -> str:
        if provider not in self.LLM_PROVIDERS:
            raise ValueError(f"invalid llm provider: {provider}")
        fallback = default if default in self.LLM_PROVIDERS else DEFAULT_PROVIDER
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT provider FROM llm_provider_selection WHERE singleton=1"
            ).fetchone()
            before = (
                str(row["provider"])
                if row is not None and str(row["provider"]) in self.LLM_PROVIDERS
                else fallback
            )
            if before != provider:
                connection.execute(
                    """
                    INSERT INTO llm_provider_selection(singleton, provider, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        provider=excluded.provider,
                        updated_at=excluded.updated_at
                    """,
                    (provider, _utc_now()),
                )
            if audit is not None:
                self._insert_control_event(
                    connection,
                    action="set_llm_provider",
                    target="llm_provider_selection",
                    outcome="succeeded" if before != provider else "no_op",
                    payload={**dict(audit), "before": before, "after": provider},
                )
        return provider

    def get_validation_mode(self) -> str:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT mode FROM validation_mode WHERE singleton=1"
            ).fetchone()
        if row is None or str(row["mode"]) not in self.VALIDATION_MODES:
            return "observe_only"
        return str(row["mode"])

    def set_validation_mode(
        self, mode: str, *, audit: Mapping[str, object] | None = None
    ) -> str:
        if mode not in self.VALIDATION_MODES:
            raise ValueError(f"invalid validation mode: {mode}")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT mode FROM validation_mode WHERE singleton=1"
            ).fetchone()
            before = (
                str(row["mode"])
                if row is not None and str(row["mode"]) in self.VALIDATION_MODES
                else "observe_only"
            )
            if before != mode:
                connection.execute(
                    """
                    INSERT INTO validation_mode(singleton, mode, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        mode=excluded.mode,
                        updated_at=excluded.updated_at
                    """,
                    (mode, _utc_now()),
                )
            if audit is not None:
                self._insert_control_event(
                    connection,
                    action="set_validation_mode",
                    target="validation_mode",
                    outcome="succeeded" if before != mode else "no_op",
                    payload={**dict(audit), "before": before, "after": mode},
                )
        return mode

    def record_auto_eat_attempt(
        self,
        *,
        signal_id: str,
        market_id: str,
        decision: str,
        reason: str = "",
        preview_id: str = "",
        execution_id: str = "",
        total_cost: Decimal | None = None,
    ) -> str:
        attempt_id = _new_id()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO auto_eat_attempts(
                    attempt_id, signal_id, market_id, decision, reason,
                    preview_id, execution_id, total_cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, str(signal_id), str(market_id), str(decision), str(reason),
                    str(preview_id), str(execution_id),
                    _decimal_string(total_cost) if total_cost is not None else None,
                    _utc_now(),
                ),
            )
        return attempt_id

    def auto_eat_attempt_exists(self, signal_id: str, decision: str) -> bool:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM auto_eat_attempts WHERE signal_id=? AND decision=? LIMIT 1",
                (str(signal_id), str(decision)),
            ).fetchone()
        return row is not None

    def last_submitted_auto_eat(self, market_id: str) -> str | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT created_at FROM auto_eat_attempts
                WHERE market_id=? AND decision='submitted'
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(market_id),),
            ).fetchone()
        return None if row is None else str(row["created_at"])

    def execution_payload(self, execution_id: str) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM executions WHERE execution_id=?",
                (str(execution_id),),
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    def auto_eat_stats(self, *, now: datetime | None = None) -> dict[str, object]:
        current = now or _parse_timestamp(_utc_now())
        day_start = (
            current.astimezone(ZoneInfo("Asia/Shanghai"))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
            .isoformat(timespec="seconds")
        )
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT count(*),
                       coalesce(sum(CASE WHEN decision='submitted' THEN 1 ELSE 0 END), 0),
                       coalesce(sum(CASE WHEN decision='submitted' AND total_cost IS NOT NULL
                                         THEN CAST(total_cost AS REAL) ELSE 0 END), 0)
                FROM auto_eat_attempts WHERE created_at >= ?
                """,
                (day_start,),
            ).fetchone()
            rejected = connection.execute(
                """
                SELECT reason, count(*) FROM auto_eat_attempts
                WHERE decision='rejected' AND created_at >= ? GROUP BY reason
                """,
                (day_start,),
            ).fetchall()
            realized = connection.execute(
                """
                SELECT coalesce(sum(
                    CAST(json_extract(e.payload, '$.minimum_profit') AS REAL)
                ), 0)
                FROM auto_eat_attempts a
                JOIN executions e ON e.execution_id = a.execution_id
                WHERE a.decision = 'submitted'
                  AND e.state = 'holding_to_resolution'
                  AND e.created_at >= ?
                """,
                (day_start,),
            ).fetchone()
        return {
            "mode": self.get_validation_mode(),
            "today_attempts": int(row[0]),
            "today_submitted": int(row[1]),
            "today_cost": float(row[2] or 0.0),
            "realized_pnl": float(realized[0] or 0.0),
            "rejected_by_reason": {str(item[0]): int(item[1]) for item in rejected},
        }

    @staticmethod
    def _signal_time(payload: Mapping[str, object]) -> str:
        for key in ("started_at", "detected_at", "created_at", "updated_at"):
            if key in payload:
                return _canonical_timestamp(payload[key])
        return _utc_now()

    def upsert_signal(self, payload: Mapping[str, object]) -> str:
        clean = self._canonical_signal_payload(
            _load_payload(_dump_relation_payload(payload))
        )
        encoded = _dump_relation_payload(clean)
        market_id = str(clean.get("market_id", "")).strip()
        if not market_id:
            raise ValueError("signal market_id is required")
        started_at = self._signal_time(clean)
        clean.setdefault("started_at", started_at)
        encoded = _dump_relation_payload(clean)
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM signals
                WHERE market_id=? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1
                """,
                (market_id,),
            ).fetchone()
            if row is not None:
                previous = _load_payload(str(row["payload"]))
                immutable_fields = ["started_at", "first_positive_at", "initial_profit"]
                if previous.get("market_type") == "cross_venue_yes_no":
                    immutable_fields.extend(
                        ("trigger_total_max_cost", "trigger_minimum_profit")
                    )
                for immutable in immutable_fields:
                    if immutable in previous:
                        clean[immutable] = previous[immutable]
                previous.update(clean)
                connection.execute(
                    "UPDATE signals SET payload=?, updated_at=? WHERE signal_id=?",
                    (_dump_relation_payload(previous), now, str(row["signal_id"])),
                )
                return str(row["signal_id"])
            signal_id = _new_id()
            try:
                connection.execute(
                    """
                    INSERT INTO signals(signal_id, market_id, payload, started_at, ended_at, updated_at)
                    VALUES (?, ?, ?, ?, NULL, ?)
                    """,
                    (signal_id, market_id, encoded, started_at, now),
                )
            except sqlite3.IntegrityError as exc:
                # A separate writer may have opened the same market between the
                # read and insert. Return that durable episode rather than
                # leaking a backend-specific constraint error.
                if "one_open_signal_per_market" not in str(exc):
                    raise
                row = connection.execute(
                    "SELECT signal_id FROM signals WHERE market_id=? AND ended_at IS NULL",
                    (market_id,),
                ).fetchone()
                if row is None:
                    raise
                return str(row["signal_id"])
            return signal_id

    def signal(self, signal_id: str) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return None if row is None else self._signal_result(row)

    def update_signal(
        self, signal_id: str, changes: Mapping[str, object]
    ) -> dict[str, object]:
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(signal_id)
            previous = _load_payload(str(row["payload"]))
            clean = self._canonical_signal_payload(
                _load_payload(_dump_relation_payload(changes))
            )
            immutable_fields = ["started_at", "first_positive_at", "initial_profit"]
            if previous.get("market_type") == "cross_venue_yes_no":
                immutable_fields.extend(
                    ("trigger_total_max_cost", "trigger_minimum_profit")
                )
            for immutable in immutable_fields:
                if immutable in previous:
                    clean[immutable] = previous[immutable]
            previous.update(clean)
            encoded = _dump_relation_payload(previous)
            connection.execute(
                "UPDATE signals SET payload=?, updated_at=? WHERE signal_id=?",
                (encoded, now, signal_id),
            )
            refreshed = connection.execute(
                "SELECT * FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
            assert refreshed is not None
            return self._signal_result(refreshed)

    def reserve_notification_attempt(
        self,
        signal_id: str,
        *,
        kind: str = "order_ready",
        max_attempts: int = 3,
        lease_seconds: float = 60.0,
        order_ready_at: str | None = None,
    ) -> dict[str, object]:
        """Atomically reserve one open signal notification attempt."""

        fields = _NOTIFICATION_KINDS.get(kind)
        if fields is None:
            raise ValueError(f"unknown notification kind: {kind}")
        if isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if isinstance(lease_seconds, bool) or lease_seconds < 0:
            raise ValueError("lease_seconds must be non-negative")
        now_text = _utc_now()
        now = _parse_timestamp(now_text)
        lease_expires = _canonical_timestamp(
            now + timedelta(seconds=float(lease_seconds))
        )
        lease_id = _new_id()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE signal_id=?", (str(signal_id),)
            ).fetchone()
            if row is None:
                return {"state": "missing", "signal_id": str(signal_id)}
            payload = _load_payload(str(row["payload"]))
            if row["ended_at"] is not None or payload.get("ended_at") is not None:
                return {"state": "closed", "signal_id": str(signal_id)}
            state = str(payload.get(fields["state"], "pending"))
            if state == "sent":
                return {"state": "sent", "signal_id": str(signal_id)}
            current_lease = payload.get(fields["lease_expires_at"])
            lease_active = False
            if current_lease not in (None, ""):
                try:
                    lease_active = _parse_timestamp(current_lease) > now
                except ValueError:
                    lease_active = False
            if lease_active:
                return {"state": "in_flight", "signal_id": str(signal_id)}
            try:
                attempts = int(payload.get(fields["attempts"], 0) or 0)
            except (TypeError, ValueError):
                attempts = 0
            if attempts >= max_attempts:
                return {
                    "state": "exhausted",
                    "signal_id": str(signal_id),
                    fields["attempts"]: attempts,
                }
            payload.update(
                {
                    fields["state"]: "pending",
                    fields["attempts"]: attempts + 1,
                    fields["lease_id"]: lease_id,
                    fields["lease_expires_at"]: lease_expires,
                }
            )
            if order_ready_at is not None:
                payload["order_ready_at"] = _canonical_timestamp(order_ready_at)
            connection.execute(
                "UPDATE signals SET payload=?, updated_at=? WHERE signal_id=?",
                (_dump_relation_payload(payload), now_text, str(signal_id)),
            )
            return {
                "state": "reserved",
                "signal_id": str(signal_id),
                "lease_id": lease_id,
                fields["attempts"]: attempts + 1,
                "signal": {
                    **payload,
                    "signal_id": str(signal_id),
                    "market_id": str(row["market_id"]),
                    "started_at": str(row["started_at"]),
                    "ended_at": row["ended_at"],
                    "updated_at": now_text,
                },
            }

    def complete_notification_attempt(
        self,
        signal_id: str,
        lease_id: str,
        *,
        kind: str = "order_ready",
        success: bool,
        error_code: str = "delivery_failed",
    ) -> dict[str, object]:
        """Persist a reserved attempt's final pending/sent/failed state."""

        fields = _NOTIFICATION_KINDS.get(kind)
        if fields is None:
            raise ValueError(f"unknown notification kind: {kind}")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE signal_id=?", (str(signal_id),)
            ).fetchone()
            if row is None:
                return {"state": "missing", "signal_id": str(signal_id)}
            payload = _load_payload(str(row["payload"]))
            if payload.get(fields["lease_id"]) != str(lease_id):
                return {"state": "stale", "signal_id": str(signal_id)}
            if row["ended_at"] is not None and kind != "observation":
                return {"state": "closed", "signal_id": str(signal_id)}
            payload[fields["state"]] = "sent" if success else "failed"
            payload.pop(fields["lease_id"], None)
            payload.pop(fields["lease_expires_at"], None)
            if success:
                payload[fields["sent_at"]] = _utc_now()
                payload.pop(fields["error_code"], None)
            else:
                payload[fields["error_code"]] = str(error_code)
            updated_at = _utc_now()
            connection.execute(
                "UPDATE signals SET payload=?, updated_at=? WHERE signal_id=?",
                (_dump_relation_payload(payload), updated_at, str(signal_id)),
            )
            return {
                "state": payload[fields["state"]],
                "signal_id": str(signal_id),
                fields["attempts"]: payload.get(fields["attempts"], 0),
            }

    def close_signal(
        self,
        market_id: str,
        *,
        ended_at: str,
        reason: str,
        updates: Mapping[str, object] | None = None,
    ) -> None:
        ended = _canonical_timestamp(ended_at)
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE market_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
                (market_id,),
            ).fetchone()
            if row is None:
                return
            payload = _load_payload(str(row["payload"]))
            if updates is not None:
                clean = self._canonical_signal_payload(
                    _load_payload(_dump_relation_payload(updates))
                )
                immutable_fields = ["started_at", "first_positive_at", "initial_profit"]
                if payload.get("market_type") == "cross_venue_yes_no":
                    immutable_fields.extend(
                        ("trigger_total_max_cost", "trigger_minimum_profit")
                    )
                for immutable in immutable_fields:
                    if immutable in payload:
                        clean[immutable] = payload[immutable]
                payload.update(clean)
            payload["ended_at"] = ended
            payload["ended_reason"] = str(reason)
            connection.execute(
                "UPDATE signals SET payload=?, ended_at=?, updated_at=? WHERE signal_id=?",
                (_dump_relation_payload(payload), ended, now, str(row["signal_id"])),
            )

    def signal_history(self, window: SignalHistoryWindow) -> list[dict[str, object]]:
        if window not in {"24h", "7d", "30d", "all"}:
            raise ValueError("window must be 24h, 7d, 30d, or all")
        with self._read_connection() as connection:
            if window == "all":
                rows = connection.execute(
                    "SELECT * FROM signals ORDER BY started_at DESC, signal_id DESC"
                ).fetchall()
            else:
                deltas = {
                    "24h": timedelta(hours=24),
                    "7d": timedelta(days=7),
                    "30d": timedelta(days=30),
                }
                cutoff = _canonical_timestamp(
                    _parse_timestamp(_utc_now()) - deltas[window]
                )
                rows = connection.execute(
                    "SELECT * FROM signals WHERE started_at >= ? "
                    "ORDER BY started_at DESC, signal_id DESC",
                    (cutoff,),
                ).fetchall()
        result = []
        for row in rows:
            result.append(
                _row_payload(
                    row,
                    fields={
                        "signal_id": str(row["signal_id"]),
                        "market_id": str(row["market_id"]),
                        "started_at": str(row["started_at"]),
                        "ended_at": row["ended_at"],
                        "updated_at": str(row["updated_at"]),
                    },
                )
            )
        return result

    def signal_metric_summary(self) -> dict[str, object]:
        """Read only the fields needed for cached monitor metrics."""

        now = _parse_timestamp(_utc_now())
        cutoff_24h = _canonical_timestamp(now - timedelta(hours=24))
        cutoff_7d = _canonical_timestamp(now - timedelta(days=7))
        cutoff_30d = _canonical_timestamp(now - timedelta(days=30))
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT started_at, "
                "json_extract(payload, '$.annualized_yield') AS annualized_yield "
                "FROM signals WHERE started_at >= ? "
                "ORDER BY started_at DESC, signal_id DESC",
                (cutoff_30d,),
            ).fetchall()
        annualized_7d: list[object] = []
        annualized_30d: list[object] = []
        signals_24h = 0
        for row in rows:
            started_at = str(row["started_at"])
            value = row["annualized_yield"]
            if started_at >= cutoff_24h:
                signals_24h += 1
            if started_at >= cutoff_7d:
                annualized_7d.append(value)
            annualized_30d.append(value)
        return {
            "signals_24h": signals_24h,
            "annualized_yields": {"7d": annualized_7d, "30d": annualized_30d},
        }

    def open_signal_history(self) -> list[dict[str, object]]:
        """Return only currently open signal episodes, newest first."""

        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM signals
                WHERE ended_at IS NULL
                ORDER BY started_at DESC, signal_id DESC
                """
            ).fetchall()
        return [self._signal_result(row) for row in rows]

    def notification_sent_since(
        self,
        market_id: str,
        since: datetime,
        *,
        kind: str = "order_ready",
    ) -> bool:
        """Return whether this market has a successful delivery at or after since."""

        fields = _NOTIFICATION_KINDS.get(kind)
        if fields is None:
            raise ValueError(f"unknown notification kind: {kind}")
        cutoff = _parse_timestamp(since)
        # ponytail: scan per-market episode payloads; add a notification_sent_at
        # index only if measured history makes this check material.
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM signals WHERE market_id=? ORDER BY started_at DESC",
                (str(market_id),),
            ).fetchall()
        for row in rows:
            payload = _load_payload(str(row["payload"]))
            sent_at = payload.get(fields["sent_at"])
            if sent_at in (None, ""):
                continue
            try:
                if _parse_timestamp(sent_at) >= cutoff:
                    return True
            except ValueError:
                continue
        return False

    def save_llm_cache(
        self, cache_key: str, payload: Mapping[str, object]
    ) -> None:
        key = str(cache_key).strip()
        if not key:
            raise ValueError("llm cache_key is required")
        encoded = _dump_payload(payload)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO llm_cache(cache_key, payload, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload=excluded.payload,
                    created_at=excluded.created_at
                """,
                (key, encoded, _utc_now()),
            )

    def load_llm_cache(self, cache_key: str) -> dict[str, object] | None:
        key = str(cache_key).strip()
        if not key:
            raise ValueError("llm cache_key is required")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM llm_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    def load_llm_cache_entries(
        self, cache_keys: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        """Batch-load multiple cache keys in a single DB connection.

        Returns a dict mapping *hit* cache_key -> parsed payload dict.
        Opens zero connections when *cache_keys* is empty.
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in cache_keys:
            key = str(raw).strip()
            if key and key not in seen:
                cleaned.append(key)
                seen.add(key)
        if not cleaned:
            return {}
        result: dict[str, dict[str, object]] = {}
        CHUNK = 900
        with self._read_connection() as connection:
            for i in range(0, len(cleaned), CHUNK):
                chunk = cleaned[i : i + CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT cache_key, payload FROM llm_cache WHERE cache_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[str(row["cache_key"])] = _load_payload(str(row["payload"]))
        return result

    @staticmethod
    def _llm_usage_label(value: object, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > 64:
            raise ValueError(
                f"{name} must be a non-empty string of at most 64 characters"
            )
        return value

    @staticmethod
    def _llm_usage_payload(
        usage: Mapping[str, object],
        violation: str | None = None,
        reason: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            value = usage.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
            payload[field] = value
        provider = usage.get("provider", "codex")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        payload["provider"] = provider.strip()
        violation_label = PredictionArbitrageStore._llm_usage_label(
            violation, "violation"
        )
        if violation_label is not None:
            payload["violation"] = violation_label
        reason_label = PredictionArbitrageStore._llm_usage_label(reason, "reason")
        if reason_label is not None:
            payload["reason"] = reason_label
        return payload

    def record_llm_call(
        self,
        *,
        status: str,
        usage: Mapping[str, object],
        violation: str | None = None,
        reason: str | None = None,
    ) -> None:
        if status not in {"success", "failed"}:
            raise ValueError("unsupported llm call status")
        payload = self._llm_usage_payload(usage, violation, reason)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO llm_usage(
                    usage_id, kind, status, payload, created_at
                ) VALUES (?, 'call', ?, ?, ?)
                """,
                (_new_id(), status, _dump_payload(payload), _utc_now()),
            )

    def record_llm_cache_hit(self, *, provider: str = "codex") -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        with self._cache_hits_lock:
            self._cache_hits[provider.strip()] = (
                self._cache_hits.get(provider.strip(), 0) + 1
            )

    def _cache_hit_snapshot(self) -> tuple[int, dict[str, int]]:
        with self._cache_hits_lock:
            return sum(self._cache_hits.values()), dict(self._cache_hits)

    def prune_llm_usage(
        self, *, retention: timedelta = _LLM_USAGE_RETENTION
    ) -> None:
        cutoff = _canonical_timestamp(_parse_timestamp(_utc_now()) - retention)
        with self._read_connection() as connection:
            connection.execute(
                "DELETE FROM llm_usage WHERE kind='cache_hit' OR created_at < ?",
                (cutoff,),
            )

    def llm_usage_24h(self) -> dict[str, int]:
        cutoff = _canonical_timestamp(
            _parse_timestamp(_utc_now()) - timedelta(hours=24)
        )
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN kind='call' THEN 1 ELSE 0 END), 0) AS calls,
                    COALESCE(SUM(CASE WHEN kind='call' AND status='success' THEN 1 ELSE 0 END), 0) AS successes,
                    COALESCE(SUM(CASE WHEN kind='call' AND status!='success' THEN 1 ELSE 0 END), 0) AS failures,
                    COALESCE(SUM(CASE WHEN kind='cache_hit' THEN 1 ELSE 0 END), 0) AS cache_hits,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.input_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS input_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.cached_input_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS cached_input_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.output_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS output_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.reasoning_output_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS reasoning_output_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' AND status='failed' AND json_extract(payload, '$.violation') IS NOT NULL THEN 1 ELSE 0 END), 0) AS invalid_outputs
                FROM llm_usage
                WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
        assert row is not None
        result = {
            field: int(row[field])
            for field in (
                "calls",
                "successes",
                "failures",
                "cache_hits",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
        }
        if int(row["invalid_outputs"]):
            result["invalid_outputs"] = int(row["invalid_outputs"])
        cache_hits, _ = self._cache_hit_snapshot()
        result["cache_hits"] = cache_hits
        return result

    def llm_usage_24h_by_provider(self) -> dict[str, dict[str, int]]:
        cutoff = _canonical_timestamp(
            _parse_timestamp(_utc_now()) - timedelta(hours=24)
        )
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    COALESCE(json_extract(payload, '$.provider'), 'codex') AS provider,
                    COALESCE(SUM(CASE WHEN kind='call' THEN 1 ELSE 0 END), 0) AS calls,
                    COALESCE(SUM(CASE WHEN kind='call' AND status='success' THEN 1 ELSE 0 END), 0) AS successes,
                    COALESCE(SUM(CASE WHEN kind='call' AND status!='success' THEN 1 ELSE 0 END), 0) AS failures,
                    COALESCE(SUM(CASE WHEN kind='cache_hit' THEN 1 ELSE 0 END), 0) AS cache_hits,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.input_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS input_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.cached_input_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS cached_input_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.output_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS output_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.reasoning_output_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS reasoning_output_tokens,
                    COALESCE(SUM(CASE WHEN kind='call' AND status='failed' AND json_extract(payload, '$.violation') IS NOT NULL THEN 1 ELSE 0 END), 0) AS invalid_outputs
                FROM llm_usage
                WHERE created_at >= ?
                GROUP BY provider
                ORDER BY provider
                """,
                (cutoff,),
            ).fetchall()
            violation_rows = connection.execute(
                """
                SELECT
                    COALESCE(json_extract(payload, '$.provider'), 'codex') AS provider,
                    json_extract(payload, '$.violation') AS label,
                    COUNT(*) AS hits
                FROM llm_usage
                WHERE created_at >= ?
                  AND kind='call' AND status='failed'
                  AND json_extract(payload, '$.violation') IS NOT NULL
                GROUP BY provider, label
                """,
                (cutoff,),
            ).fetchall()
            reason_rows = connection.execute(
                """
                SELECT
                    COALESCE(json_extract(payload, '$.provider'), 'codex') AS provider,
                    json_extract(payload, '$.reason') AS label,
                    COUNT(*) AS hits
                FROM llm_usage
                WHERE created_at >= ?
                  AND kind='call' AND status='failed'
                  AND json_extract(payload, '$.reason') IS NOT NULL
                GROUP BY provider, label
                """,
                (cutoff,),
            ).fetchall()
        fields = (
            "calls",
            "successes",
            "failures",
            "cache_hits",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            counts = {field: int(row[field]) for field in fields}
            if int(row["invalid_outputs"]):
                counts["invalid_outputs"] = int(row["invalid_outputs"])
            result[str(row["provider"])] = counts
        for row in violation_rows:
            violations = result.setdefault(str(row["provider"]), {}).setdefault(
                "violations", {}
            )
            violations[str(row["label"])] = int(row["hits"])
        for row in reason_rows:
            failure_reasons = result.setdefault(str(row["provider"]), {}).setdefault(
                "failure_reasons", {}
            )
            failure_reasons[str(row["label"])] = int(row["hits"])
        _, memory_hits = self._cache_hit_snapshot()
        for provider, count in memory_hits.items():
            counts = result.setdefault(provider, {field: 0 for field in fields})
            counts["cache_hits"] = count
        return result

    def create_preview(
        self,
        payload: Mapping[str, object],
        *,
        expires_at: str,
        created_at: str | None = None,
    ) -> str:
        encoded = _dump_execution_payload(payload)
        created = _parse_timestamp(created_at or _utc_now())
        requested_expiry = _parse_timestamp(expires_at)
        # The caller supplies the displayed deadline; cap accidental longer
        # lifetimes so every preview is at most the fixed ten-second window.
        expiry = min(requested_expiry, created + _PREVIEW_TTL)
        preview_id = _new_id()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO previews(preview_id, payload, created_at, expires_at, consumed_at) VALUES (?, ?, ?, ?, NULL)",
                (preview_id, encoded, _canonical_timestamp(created), _canonical_timestamp(expiry)),
            )
        return preview_id

    def lp_preview(self, preview_id: str) -> dict[str, object] | None:
        """Load one LP preview without consuming it."""

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM previews WHERE preview_id=?", (str(preview_id),)
            ).fetchone()
        if row is None:
            return None
        payload = _load_payload(str(row["payload"]))
        payload.update(
            {
                "preview_id": str(row["preview_id"]),
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "consumed_at": row["consumed_at"],
            }
        )
        return payload

    def consume_lp_preview(self, preview_id: str) -> None:
        now = _utc_now()
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE previews SET consumed_at=? WHERE preview_id=? AND consumed_at IS NULL",
                (now, str(preview_id)),
            )
            if updated.rowcount != 1:
                raise ValueError("preview_consumed")

    @staticmethod
    def _lp_row_result(row: sqlite3.Row) -> dict[str, object]:
        payload = _load_payload(str(row["payload"]))
        # The LP write revision is an internal compare-and-observe fence for
        # concurrent monitor/submit updates.  It never belongs in the public
        # session payload returned to callers.
        payload.pop("_lp_revision", None)
        payload.pop("_lp_trade_revision", None)
        payload.update(
            {
                "session_id": str(row["session_id"]),
                "idempotency_key": str(row["idempotency_key"]),
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        return payload

    def lp_session_by_idempotency(self, idempotency_key: str) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
        return None if row is None else self._lp_row_result(row)

    def lp_session_revision(self, session_id: str, *, trading: bool = False) -> int:
        """Return the durable internal revision for one LP session."""

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM lp_sessions WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        if row is None:
            raise ValueError("lp_session_not_found")
        payload = _load_payload(str(row["payload"]))
        value = payload.get("_lp_trade_revision" if trading else "_lp_revision", 0)
        try:
            revision = int(value)
        except (TypeError, ValueError):
            return 0
        return max(revision, 0)

    @staticmethod
    def _lp_payload_revision(payload: Mapping[str, object]) -> int:
        value = payload.get("_lp_revision", 0)
        try:
            revision = int(value)
        except (TypeError, ValueError):
            return 0
        return max(revision, 0)

    def lp_session_with_revision(
        self, session_id: str, *, trading: bool = False
    ) -> tuple[dict[str, object], int] | None:
        """Read one LP session image and its fence from one SQLite snapshot."""

        with self._read_connection() as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(
                    "SELECT * FROM lp_sessions WHERE session_id=?",
                    (str(session_id),),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                payload = _load_payload(str(row["payload"]))
                result = self._lp_row_result(row)
                revision = int(payload.get("_lp_trade_revision", 0)) if trading else self._lp_payload_revision(payload)
                connection.execute("COMMIT")
                return result, revision
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def lp_active_sessions_with_revisions(
        self,
    ) -> list[tuple[dict[str, object], int]]:
        """Read active LP session images and fences from one SQLite snapshot."""

        with self._read_connection() as connection:
            connection.execute("BEGIN")
            try:
                rows = connection.execute(
                    "SELECT * FROM lp_sessions WHERE state NOT IN ('complete','entry_rejected') ORDER BY created_at DESC"
                ).fetchall()
                result: list[tuple[dict[str, object], int]] = []
                for row in rows:
                    payload = _load_payload(str(row["payload"]))
                    result.append(
                        (self._lp_row_result(row), self._lp_payload_revision(payload))
                    )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def lp_session(self, session_id: str) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
        return None if row is None else self._lp_row_result(row)

    def lp_active_session(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE state NOT IN ('complete','entry_rejected') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._lp_row_result(row)

    def lp_active_sessions(self) -> list[dict[str, object]]:
        """Return all non-terminal LP sessions, newest first (issue 165)."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM lp_sessions WHERE state NOT IN ('complete','entry_rejected') ORDER BY created_at DESC"
            ).fetchall()
        return [self._lp_row_result(row) for row in rows]

    def lp_latest_session(self) -> dict[str, object] | None:
        """Return the most recently created LP session for read-only status.

        The reserved manual-cancel anchor session is never returned here so
        it cannot leak into "latest session" fallbacks or daily reports.
        """

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id != ? ORDER BY created_at DESC LIMIT 1",
                (LP_RESERVED_MANUAL_SESSION_ID,),
            ).fetchone()
        return None if row is None else self._lp_row_result(row)

    def lp_sessions(self) -> list[dict[str, object]]:
        """Return stored LP sessions for a read-only daily report snapshot."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM lp_sessions ORDER BY created_at, session_id"
            ).fetchall()
        return [self._lp_row_result(row) for row in rows]

    def lp_record_book_samples(
        self, samples: Iterable[Mapping[str, object]], *, now: datetime
    ) -> int:
        """Persist valid BBO receipts and prune samples older than 65 minutes."""

        if isinstance(samples, (str, bytes, Mapping)) or not isinstance(
            samples, Iterable
        ):
            raise ValueError("lp_book_samples_invalid")
        current = _parse_timestamp(now)
        cutoff = _canonical_timestamp(current - _LP_BOOK_SAMPLE_RETENTION)
        rows: list[tuple[str, str, str, str]] = []
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            condition_id = sample.get("condition_id")
            token_id = sample.get("token_id")
            if not isinstance(condition_id, str) or not condition_id.strip():
                continue
            if not isinstance(token_id, str) or not token_id.strip():
                continue
            try:
                received_at = _canonical_timestamp(sample.get("received_at"))
                received = _parse_timestamp(received_at)
            except (TypeError, ValueError):
                continue
            if received > current:
                continue

            values: dict[str, Decimal] = {}
            for name in (
                "best_bid_price",
                "best_bid_size",
                "best_ask_price",
                "best_ask_size",
            ):
                raw = sample.get(name)
                if isinstance(raw, bool):
                    break
                try:
                    value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
                except (InvalidOperation, TypeError, ValueError):
                    break
                if not value.is_finite():
                    break
                values[name] = value
            if len(values) != 4:
                continue
            bid = values["best_bid_price"]
            ask = values["best_ask_price"]
            if (
                bid <= 0
                or ask > 1
                or bid >= ask
                or values["best_bid_size"] <= 0
                or values["best_ask_size"] <= 0
            ):
                continue

            payload: dict[str, object] = {
                "condition_id": condition_id.strip(),
                "token_id": token_id.strip(),
                "received_at": received_at,
                "source_timestamp": sample.get("source_timestamp"),
                **values,
            }
            try:
                encoded = _dump_relation_payload(payload)
            except (TypeError, ValueError):
                continue
            rows.append(
                (
                    condition_id.strip(),
                    token_id.strip(),
                    received_at,
                    encoded,
                )
            )

        with self._transaction() as connection:
            if rows:
                connection.executemany(
                    """
                    INSERT INTO lp_book_samples(condition_id,token_id,received_at,payload)
                    VALUES (?,?,?,?)
                    ON CONFLICT(condition_id,token_id,received_at)
                    DO UPDATE SET payload=excluded.payload
                    """,
                    rows,
                )
            connection.execute(
                "DELETE FROM lp_book_samples WHERE received_at < ?", (cutoff,)
            )
        return len(rows)

    def lp_book_samples(
        self,
        condition_id: str,
        token_id: str,
        *,
        since: datetime | str,
        until: datetime | str,
    ) -> list[dict[str, object]]:
        """Read one direction's window plus its closest preceding anchor."""

        condition = str(condition_id).strip()
        token = str(token_id).strip()
        if not condition or not token:
            raise ValueError("lp_book_sample_identity_invalid")
        start = _canonical_timestamp(since)
        end = _canonical_timestamp(until)
        if end < start:
            return []
        with self._read_connection() as connection:
            anchor = connection.execute(
                """
                SELECT payload FROM lp_book_samples
                WHERE condition_id=? AND token_id=? AND received_at < ?
                ORDER BY received_at DESC LIMIT 1
                """,
                (condition, token, start),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT payload FROM lp_book_samples
                WHERE condition_id=? AND token_id=? AND received_at >= ? AND received_at <= ?
                ORDER BY received_at
                """,
                (condition, token, start, end),
            ).fetchall()
        payloads = ([] if anchor is None else [_load_payload(str(anchor["payload"]))])
        payloads.extend(_load_payload(str(row["payload"])) for row in rows)
        return sorted(payloads, key=lambda row: str(row.get("received_at") or ""))

    def lp_save_price_history(
        self,
        condition_id: str,
        token_id: str,
        samples: Iterable[Mapping[str, object]],
        summary: Mapping[str, object],
    ) -> None:
        """Persist compressed price samples and their independently readable summary."""
        self.lp_save_price_history_batch(
            (
                {
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "samples": samples,
                    "summary": summary,
                },
            )
        )

    def lp_save_price_history_batch(
        self,
        rows: Iterable[Mapping[str, object]],
        *,
        generation: int | None = None,
    ) -> int:
        """Write a small batch without opening one transaction per token."""

        encoded: list[tuple[str, str, sqlite3.Binary, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            condition = str(row.get("condition_id") or "").strip()
            token = str(row.get("token_id") or "").strip()
            summary = row.get("summary")
            if not condition or not token or not isinstance(summary, Mapping):
                raise ValueError("lp_price_history_identity_invalid")
            samples = row.get("samples")
            sample_rows = (
                [dict(sample) for sample in samples if isinstance(sample, Mapping)]
                if isinstance(samples, Iterable) and not isinstance(samples, (str, bytes, Mapping))
                else []
            )
            samples_payload = _dump_relation_payload({"samples": sample_rows}).encode(
                "utf-8"
            )
            summary_payload = _dump_relation_payload(summary)
            encoded.append(
                (
                    condition,
                    token,
                    sqlite3.Binary(zlib.compress(samples_payload)),
                    summary_payload,
                )
            )
        if not encoded:
            return 0
        expected_generation = (
            generation if type(generation) is int and generation >= 1 else None
        )
        with self._transaction() as connection:
            persisted = 0
            singleton = connection.execute(
                "SELECT payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            global_fence = _load_payload(str(singleton["payload"])).get("global_recovery_generation") if singleton is not None else None
            if type(global_fence) is int and expected_generation is not None and global_fence > expected_generation:
                return 0
            for row in encoded:
                if expected_generation is not None:
                    fence = connection.execute(
                        """
                        SELECT generation FROM lp_preparation_items
                        WHERE condition_id=?
                        """,
                        (row[0],),
                    ).fetchone()
                    if (
                        fence is not None
                        and int(fence["generation"]) > expected_generation
                    ):
                        continue
                    current = connection.execute(
                        """
                        SELECT summary FROM lp_price_history_cache
                        WHERE condition_id=? AND token_id=?
                        """,
                        (row[0], row[1]),
                    ).fetchone()
                    if current is not None:
                        try:
                            current_summary = _load_payload(str(current["summary"]))
                            current_generation = current_summary.get(
                                "preparation_generation"
                            )
                        except (TypeError, ValueError):
                            current_generation = None
                        if (
                            type(current_generation) is int
                            and current_generation > expected_generation
                        ):
                            continue
                connection.execute(
                    """
                    INSERT INTO lp_price_history_cache(condition_id,token_id,samples,summary)
                    VALUES (?,?,?,?)
                    ON CONFLICT(condition_id,token_id) DO UPDATE SET
                        samples=excluded.samples, summary=excluded.summary
                    """,
                    row,
                )
                persisted += 1
        return persisted

    @staticmethod
    def _lp_expire_price_history_summary(
        summary: Mapping[str, object], *, now: datetime | None
    ) -> dict[str, object]:
        result = dict(summary)
        if isinstance(now, datetime) and now.tzinfo is not None:
            checked_at = result.get("checked_at")
            try:
                checked = _parse_timestamp(checked_at)
                current = _parse_timestamp(now)
                if checked > current:
                    result["state"] = "unknown"
                    result.setdefault("reason", "summary_time_unknown")
                    return result
                expiry = checked + _LP_PRICE_HISTORY_VALIDITY
                valid_until = result.get("valid_until")
                if valid_until is not None:
                    declared_expiry = _parse_timestamp(valid_until)
                    if declared_expiry < expiry:
                        expiry = declared_expiry
                if current >= expiry:
                    result["state"] = "expired"
                    result.setdefault("reason", "summary_expired")
            except ValueError:
                result["state"] = "unknown"
                result.setdefault("reason", "summary_time_unknown")
        return result

    def lp_price_history_summary(
        self,
        condition_id: str,
        token_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, object] | None:
        """Read a cached range summary without decompressing historical samples."""

        condition = str(condition_id).strip()
        token = str(token_id).strip()
        if not condition or not token:
            return None
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT summary FROM lp_price_history_cache WHERE condition_id=? AND token_id=?",
                (condition, token),
            ).fetchone()
        if row is None:
            return None
        summary = _load_payload(str(row["summary"]))
        return self._lp_expire_price_history_summary(summary, now=now)

    def lp_price_history_summaries(
        self,
        identities: Iterable[tuple[str, str]],
        *,
        now: datetime | None = None,
    ) -> dict[tuple[str, str], dict[str, object]]:
        """Read only summary columns for many directions in one read batch."""

        unique = tuple(
            dict.fromkeys(
                (str(condition).strip(), str(token).strip())
                for condition, token in identities
                if str(condition).strip() and str(token).strip()
            )
        )
        if not unique:
            return {}
        result: dict[tuple[str, str], dict[str, object]] = {}
        with self._read_connection() as connection:
            for offset in range(0, len(unique), 400):
                batch = unique[offset : offset + 400]
                where = " OR ".join("(condition_id=? AND token_id=?)" for _ in batch)
                params = tuple(value for pair in batch for value in pair)
                rows = connection.execute(
                    f"SELECT condition_id,token_id,summary FROM lp_price_history_cache WHERE {where}",
                    params,
                ).fetchall()
                for row in rows:
                    key = (str(row["condition_id"]), str(row["token_id"]))
                    result[key] = self._lp_expire_price_history_summary(
                        _load_payload(str(row["summary"])), now=now
                    )
        return result

    def lp_price_history_samples(
        self, condition_id: str, token_id: str
    ) -> list[dict[str, object]]:
        """Read and decompress one token's cached samples."""

        condition = str(condition_id).strip()
        token = str(token_id).strip()
        if not condition or not token:
            return []
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT samples FROM lp_price_history_cache WHERE condition_id=? AND token_id=?",
                (condition, token),
            ).fetchone()
        if row is None:
            return []
        try:
            payload = _load_payload(zlib.decompress(bytes(row["samples"])).decode("utf-8"))
        except (OSError, TypeError, ValueError, zlib.error):
            return []
        samples = payload.get("samples")
        return [dict(item) for item in samples if isinstance(item, Mapping)] if isinstance(samples, list) else []

    def lp_price_history_samples_batch(
        self, identities: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], list[dict[str, object]]]:
        """Read compressed samples for one bounded batch in one connection."""

        unique = tuple(
            dict.fromkeys(
                (str(condition).strip(), str(token).strip())
                for condition, token in identities
                if str(condition).strip() and str(token).strip()
            )
        )
        if not unique:
            return {}
        result: dict[tuple[str, str], list[dict[str, object]]] = {}
        with self._read_connection() as connection:
            where = " OR ".join("(condition_id=? AND token_id=?)" for _ in unique)
            params = tuple(value for pair in unique for value in pair)
            rows = connection.execute(
                f"SELECT condition_id,token_id,samples FROM lp_price_history_cache WHERE {where}",
                params,
            ).fetchall()
        for row in rows:
            key = (str(row["condition_id"]), str(row["token_id"]))
            try:
                payload = _load_payload(zlib.decompress(bytes(row["samples"])).decode("utf-8"))
            except (OSError, TypeError, ValueError, zlib.error):
                result[key] = []
                continue
            samples = payload.get("samples")
            result[key] = (
                [dict(item) for item in samples if isinstance(item, Mapping)]
                if isinstance(samples, list)
                else []
            )
        return result

    def lp_record_market_exclusion(
        self, condition_id: str, token_id: str, reason_code: str, *,
        checked_at: datetime, cooldown_until: datetime, now: datetime,
    ) -> bool:
        """Insert one fixed deadline; an active row never changes or extends."""
        if not condition_id or reason_code not in {
            "reward_inactive", "reward_pool_empty", "market_not_accepting_orders",
            "competition_empty", "competition_too_thin", "competition_too_crowded",
            "history_amplitude_exceeded", "event_starting_soon", "event_in_progress",
            "event_recovery_pending",
        }:
            raise ValueError("lp_market_exclusion_invalid")
        checked, until, current = map(_canonical_timestamp, (checked_at, cooldown_until, now))
        if checked > current or until <= current or until <= checked:
            return False
        with self._transaction() as connection:
            return bool(connection.execute(
                """INSERT INTO lp_market_exclusions VALUES (?,?,?,?,?)
                ON CONFLICT(condition_id,token_id) DO UPDATE SET
                    reason_code=excluded.reason_code, checked_at=excluded.checked_at,
                    cooldown_until=excluded.cooldown_until
                WHERE lp_market_exclusions.cooldown_until <= excluded.checked_at
                  AND lp_market_exclusions.checked_at < excluded.checked_at""",
                (condition_id, token_id, reason_code, checked, until),
            ).rowcount)

    def lp_candidate_allowed(
        self, identities: Sequence[tuple[str, str]], *, now: datetime,
    ) -> tuple[tuple[str, str], ...]:
        """Indexed filtering of bounded caller batches, never a blacklist dump."""
        current = _canonical_timestamp(now)
        allowed = []
        with self._read_connection() as connection:
            for offset in range(0, len(identities), 300):
                batch = identities[offset:offset + 300]
                values = ",".join("(?,?)" for _ in batch)
                rows = connection.execute(
                    f"""WITH targets(condition_id,token_id) AS (VALUES {values})
                    SELECT condition_id,token_id FROM targets t WHERE NOT EXISTS (
                        SELECT 1 FROM lp_market_exclusions e
                        WHERE e.condition_id=t.condition_id AND e.token_id IN ('',t.token_id)
                          AND e.cooldown_until > ?)
                    """, [part for pair in batch for part in pair] + [current],
                )
                allowed.extend((str(row[0]), str(row[1])) for row in rows)
        return tuple(allowed)

    def lp_candidate_conditions(
        self, condition_ids: Sequence[str], *, now: datetime,
    ) -> tuple[str, ...]:
        """Skip shared metadata when all current directions are cooling."""
        current = _canonical_timestamp(now)
        self._ensure_lp_metadata_cache_schema()
        result = []
        with self._read_connection() as connection:
            for condition in condition_ids:
                if connection.execute(
                    "SELECT 1 FROM lp_market_exclusions WHERE condition_id=? AND token_id='' AND cooldown_until>?",
                    (condition, current),
                ).fetchone():
                    continue
                if not connection.execute(
                    "SELECT 1 FROM lp_market_exclusions WHERE condition_id=? AND cooldown_until>? LIMIT 1",
                    (condition, current),
                ).fetchone():
                    result.append(condition)
                    continue
                cached = connection.execute(
                    "SELECT payload FROM lp_market_metadata_cache WHERE condition_id=? AND present=1",
                    (condition,),
                ).fetchone()
                tokens = ()
                if cached:
                    try:
                        payload = _load_payload(str(cached[0]))
                    except ValueError:
                        payload = {}
                    outcomes = payload.get("outcomes")
                    if isinstance(outcomes, Mapping):
                        tokens = tuple(str(value.get("token_id") or "") for key, value in outcomes.items()
                            if str(key).lower() in {"yes", "no"} and isinstance(value, Mapping) and value.get("token_id"))
                if tokens:
                    # Metadata gives the current token identities. An unrelated old
                    # history token must not wake a completely excluded market.
                    allowed = any(not connection.execute(
                        "SELECT 1 FROM lp_market_exclusions WHERE condition_id=? AND token_id=? AND cooldown_until>?",
                        (condition, token, current),
                    ).fetchone() for token in tokens)
                else:
                    allowed = not connection.execute(
                        "SELECT 1 FROM lp_price_history_cache WHERE condition_id=? LIMIT 1", (condition,),
                    ).fetchone() or connection.execute(
                        """SELECT 1 FROM lp_price_history_cache h WHERE h.condition_id=?
                        AND NOT EXISTS (SELECT 1 FROM lp_market_exclusions e
                          WHERE e.condition_id=h.condition_id AND e.token_id=h.token_id
                            AND e.cooldown_until>?) LIMIT 1""", (condition, current),
                    ).fetchone()
                if allowed:
                    result.append(condition)
        return tuple(result)

    def lp_next_market_exclusion_expiry(self, *, now: datetime) -> datetime | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT cooldown_until FROM lp_market_exclusions WHERE cooldown_until>? ORDER BY cooldown_until LIMIT 1",
                (_canonical_timestamp(now),),
            ).fetchone()
        return _parse_timestamp(row[0]) if row else None

    def lp_market_exclusion_counts(self, *, now: datetime) -> dict[str, int]:
        with self._read_connection() as connection:
            return {str(row[0]): int(row[1]) for row in connection.execute(
                "SELECT reason_code,count(*) FROM lp_market_exclusions WHERE cooldown_until>? GROUP BY reason_code",
                (_canonical_timestamp(now),),
            )}

    def lp_prune_market_exclusions(self, *, now: datetime, limit: int = 300) -> int:
        """Delete a due batch atomically; a concurrent replacement remains intact."""
        if type(limit) is not int or not 1 <= limit <= 300:
            raise ValueError("lp_market_exclusion_prune_limit_invalid")
        with self._transaction() as connection:
            return connection.execute(
                """DELETE FROM lp_market_exclusions WHERE rowid IN (
                    SELECT rowid FROM lp_market_exclusions WHERE cooldown_until<=?
                    ORDER BY cooldown_until LIMIT ?)""",
                (_canonical_timestamp(now), limit),
            ).rowcount

    def lp_clear_ended_market_exclusions(self, condition_id: str, *, checked_at: datetime) -> int:
        with self._transaction() as connection:
            return connection.execute(
                "DELETE FROM lp_market_exclusions WHERE condition_id=? AND checked_at<=?",
                (condition_id, _canonical_timestamp(checked_at)),
            ).rowcount

    def _ensure_lp_metadata_cache_schema(self) -> None:
        """Create `lp_market_metadata_cache` on first use.

        Expand-only companion of the LP price-history cache.  It is created
        lazily (CREATE TABLE IF NOT EXISTS on the first metadata-cache call)
        so a store open never mutates the pinned init-time table set.
        """

        if self._lp_metadata_cache_ready:
            return
        with self._lp_metadata_cache_schema_lock:
            if self._lp_metadata_cache_ready:
                return
            with self._transaction() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lp_market_metadata_cache(
                        condition_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        present INTEGER NOT NULL
                    )
                    """
                )
            self._lp_metadata_cache_ready = True

    @staticmethod
    def _lp_metadata_cache_horizon(now: datetime | None) -> float:
        moment = (
            _parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
        )
        return moment.timestamp()

    def lp_metadata_cache_entries(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, tuple[float, dict[str, object] | None]]:
        """Bulk-read non-expired rows with their persisted `expires_at`.

        Each value is ``(expires_at, payload)`` carrying the persisted
        expiry stamp; a ``None`` payload marks a confirmed-missing row.
        """

        return dict(self.lp_metadata_cache_items(now=now))

    def lp_metadata_cache_items(
        self, *, now: datetime | None = None, exclude_candidates: bool = False,
    ) -> Iterator[tuple[str, tuple[float, dict[str, object] | None]]]:
        """Stream warm-cache rows without materializing the entire universe."""
        self._ensure_lp_metadata_cache_schema()
        horizon = self._lp_metadata_cache_horizon(now)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT condition_id, payload, expires_at, present,
                  CASE WHEN ?=1 THEN EXISTS (
                    SELECT 1 FROM lp_market_exclusions e WHERE e.condition_id=m.condition_id
                      AND e.token_id<>'' AND e.cooldown_until>?) ELSE 0 END AS token_exclusion
                FROM lp_market_metadata_cache m
                WHERE expires_at > ? AND (?=0 OR NOT EXISTS (
                  SELECT 1 FROM lp_market_exclusions e WHERE e.condition_id=m.condition_id
                    AND e.token_id='' AND e.cooldown_until>?))
                """,
                (int(exclude_candidates), _canonical_timestamp(now or datetime.now(timezone.utc)),
                 horizon, int(exclude_candidates), _canonical_timestamp(now or datetime.now(timezone.utc))),
            )
            for row in rows:
                condition_id = str(row["condition_id"])
                if row["token_exclusion"] and not self.lp_candidate_conditions((condition_id,), now=now or datetime.now(timezone.utc)):
                    continue
                expires_at = float(row["expires_at"])
                try:
                    payload = _load_payload(str(row["payload"]))
                except ValueError:
                    continue
                if not int(row["present"]):
                    if payload.get("closed_checked") is True:
                        yield condition_id, (expires_at, None)
                    continue
                yield condition_id, (expires_at, payload)

    def lp_metadata_cache_store_entries(
        self,
        entries: Mapping[str, tuple[float, Mapping[str, object] | None]],
    ) -> None:
        """Upsert LP market metadata rows in bounded ~400-row transactions."""

        self._ensure_lp_metadata_cache_schema()
        encoded: list[tuple[str, str, float, int]] = []
        for raw_condition, raw_entry in entries.items():
            condition = str(raw_condition or "").strip()
            if not condition or not isinstance(raw_entry, tuple) or len(raw_entry) != 2:
                raise ValueError("lp_metadata_cache_entry_invalid")
            raw_expires_at, raw_payload = raw_entry
            if isinstance(raw_expires_at, bool) or not isinstance(
                raw_expires_at, (int, float)
            ):
                raise ValueError("lp_metadata_cache_entry_invalid")
            expires_at = float(raw_expires_at)
            if raw_payload is None:
                encoded.append((condition, '{"closed_checked":true}', expires_at, 0))
                continue
            if not isinstance(raw_payload, Mapping):
                raise ValueError("lp_metadata_cache_entry_invalid")
            encoded.append(
                (
                    condition,
                    _dump_relation_payload(dict(raw_payload)),
                    expires_at,
                    1,
                )
            )
        for offset in range(0, len(encoded), 400):
            chunk = encoded[offset : offset + 400]
            with self._transaction() as connection:
                connection.executemany(
                    """
                    INSERT INTO
                    lp_market_metadata_cache(condition_id,payload,expires_at,present)
                    VALUES (?,?,?,?)
                    ON CONFLICT(condition_id) DO UPDATE SET
                        payload=excluded.payload,
                        expires_at=excluded.expires_at,
                        present=excluded.present
                    """,
                    chunk,
                )

    def lp_metadata_cache_prune(self, *, now: datetime | None = None) -> None:
        """Delete expired LP market metadata rows."""

        self._ensure_lp_metadata_cache_schema()
        horizon = self._lp_metadata_cache_horizon(now)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM lp_market_metadata_cache WHERE expires_at <= ?",
                (horizon,),
            )

    def lp_save_screening_snapshot(
        self, payload: Mapping[str, object]
    ) -> dict[str, object]:
        """Save screening facts unless a later-started scan already won."""

        encoded = _dump_relation_payload(payload)
        updated_at = _utc_now()
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT payload FROM lp_screening_snapshot WHERE singleton=1"
            ).fetchone()
            if current is not None:
                previous = _load_payload(str(current["payload"]))
                previous_started = previous.get("scan_started_at")
                incoming_started = payload.get("scan_started_at")
                if (
                    isinstance(previous_started, str)
                    and isinstance(incoming_started, str)
                    and previous_started > incoming_started
                ):
                    return previous
            connection.execute(
                """
                INSERT INTO lp_screening_snapshot(singleton,payload,updated_at)
                VALUES (1,?,?)
                ON CONFLICT(singleton) DO UPDATE
                SET payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (encoded, updated_at),
            )
        return _load_payload(encoded)

    def lp_screening_snapshot(self) -> dict[str, object] | None:
        """Load the saved LP screening projection and event confirmations."""

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM lp_screening_snapshot WHERE singleton=1"
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    @staticmethod
    def _lp_preparation_item(row: sqlite3.Row) -> dict[str, object]:
        result = {key: row[key] for key in row.keys()}
        for key in ("retry_used", "paused", "alert_attempted"):
            result[key] = bool(result.get(key))
        return result

    def lp_preparation_items(self) -> list[dict[str, object]]:
        """Read durable per-market preparation failures and retry state."""

        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lp_preparation_items
                WHERE state != 'recovered'
                ORDER BY condition_id
                """
            ).fetchall()
        return [self._lp_preparation_item(row) for row in rows]

    @property
    def _lp_preparation_owner_path(self) -> Path:
        return self.path.with_name("lp-preparation.lock")

    def lp_try_acquire_preparation_owner(self) -> bool:
        """Acquire the process-wide preparation read/publish fence if free."""

        with self._lp_preparation_owner_mutex:
            if self._lp_preparation_owner_handle is not None:
                return True
            handle = self._lp_preparation_owner_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                handle.close()
                return False
            self._lp_preparation_owner_handle = handle
            return True

    def lp_release_preparation_owner(self) -> None:
        """Release the preparation fence held by this store instance."""

        with self._lp_preparation_owner_mutex:
            handle = self._lp_preparation_owner_handle
            if handle is None:
                return
            self._lp_preparation_owner_handle = None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _lp_preparation_owner_entered_here(self) -> bool:
        if self._lp_preparation_owner_handle is not None:
            return False
        return self.lp_try_acquire_preparation_owner()

    def lp_normalize_interrupted_preparation_items(self) -> int:
        """Requeue retries whose exclusive owner stopped before publishing."""

        entered_here = self._lp_preparation_owner_entered_here()
        if not entered_here and self._lp_preparation_owner_handle is None:
            return 0

        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE lp_preparation_items
                    SET state='waiting_retry',paused=0,retry_used=0,
                        next_retry_at=COALESCE(retry_started_at,updated_at),
                        retry_started_at=NULL,
                        error='retry_interrupted',alert_attempted=0,alert_state=NULL,
                        updated_at=?
                    WHERE state='retrying' AND retry_used=1 AND paused=0
                    """,
                    (_utc_now(),),
                )
            return int(cursor.rowcount)
        finally:
            if entered_here:
                self.lp_release_preparation_owner()

    def lp_migrate_legacy_preparation(self) -> dict[str, object] | None:
        """Preserve identifiable old history failures as per-market pauses."""

        with self._transaction() as connection:
            preparation_row = connection.execute(
                "SELECT generation,payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            if preparation_row is None:
                return None
            payload = _load_payload(str(preparation_row["payload"]))
            if (
                payload.get("state") != "paused"
                or payload.get("paused") is not True
                or payload.get("stage") != "history"
                or payload.get("attempt") != 2
                or payload.get("failure_count") != 2
                or payload.get("alert_attempted") is not True
                or payload.get("alert_state") != "sent"
            ):
                payload["generation"] = int(preparation_row["generation"])
                return payload
            try:
                attempt_at = _canonical_timestamp(payload.get("last_attempt_at"))
            except (TypeError, ValueError):
                payload["generation"] = int(preparation_row["generation"])
                return payload
            error = payload.get("last_error")
            if not isinstance(error, str) or not error.strip():
                payload["generation"] = int(preparation_row["generation"])
                return payload
            existing_item = connection.execute(
                """
                SELECT 1 FROM lp_preparation_items
                WHERE state != 'recovered'
                LIMIT 1
                """
            ).fetchone()
            if existing_item is not None:
                payload["generation"] = int(preparation_row["generation"])
                return payload

            matches: dict[str, str] = {}
            for row in connection.execute(
                "SELECT condition_id,token_id,summary FROM lp_price_history_cache"
            ).fetchall():
                try:
                    summary = _load_payload(str(row["summary"]))
                    if summary.get("last_error") != error:
                        continue
                    if _canonical_timestamp(summary.get("last_attempt_at")) != attempt_at:
                        continue
                except (TypeError, ValueError):
                    continue
                condition_id = str(row["condition_id"] or "").strip()
                token_id = str(row["token_id"] or "").strip()
                if condition_id and token_id:
                    matches.setdefault(condition_id, token_id)
            if not matches:
                payload["generation"] = int(preparation_row["generation"])
                return payload

            generation = int(preparation_row["generation"])
            normalized_error = re.sub(r"[^a-z0-9]", "", error.casefold())
            explicit_certificate = any(
                marker in normalized_error
                for marker in (
                    "sslcertverificationerror",
                    "certificateverify",
                    "certverification",
                    "explicitcert",
                )
            )
            generic_ssl = "genericssl" in normalized_error or (
                "sslerror" in normalized_error and not explicit_certificate
            )
            transient_legacy = generic_ssl or any(
                marker in normalized_error
                for marker in (
                    "globaltransporterror",
                    "incompleteread",
                    "retryinterrupted",
                    "timeout",
                    "proxyerror",
                    "connectionerror",
                    "network",
                )
            )
            updated_at = _utc_now()
            retry_at = _canonical_timestamp(
                _parse_timestamp(attempt_at)
                + timedelta(seconds=0 if generic_ssl or normalized_error == "retryinterrupted" else 300)
            ) if transient_legacy else None
            migrated_rows = tuple(
                (
                    condition_id,
                    token_id,
                    "waiting_retry" if transient_legacy else "paused",
                    0 if transient_legacy else 1,
                    0 if transient_legacy else 1,
                    retry_at,
                )
                for condition_id, token_id in sorted(matches.items())
            )
            connection.executemany(
                """
                INSERT INTO lp_preparation_items(
                    condition_id,generation,retry_used,failure_count,state,paused,
                    stage,direction,token_id,error,failed_at,next_retry_at,
                    retry_started_at,alert_attempted,alert_state,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    (
                        condition_id,
                        generation,
                        retry_used,
                        2,
                        state,
                        paused,
                        "history",
                        None,
                        token_id,
                        error,
                        attempt_at,
                        retry_at,
                        None,
                        1,
                        "sent",
                        updated_at,
                    )
                    for condition_id, _token_id, state, retry_used, paused, retry_at in migrated_rows
                    for token_id in (_token_id,)
                ),
            )
            migrated_retry_times = tuple(
                value[5] for value in migrated_rows if value[5] is not None
            )
            payload.update(
                {
                    "state": "partial",
                    "attempt": 0,
                    "failure_count": 0,
                    "paused": False,
                    "next_retry_at": min(migrated_retry_times)
                    if migrated_retry_times
                    else None,
                    "next_probe_at": attempt_at if transient_legacy else None,
                    "last_probe_stage": "history" if transient_legacy else None,
                    "last_error": error if not transient_legacy else None,
                    "generation": generation,
                }
            )
            connection.execute(
                "UPDATE lp_preparation SET payload=?,updated_at=? WHERE singleton=1",
                (_dump_payload(payload), updated_at),
            )
            return payload

    @staticmethod
    def _lp_preparation_item_summary_from_connection(
        connection: sqlite3.Connection,
        *,
        limit: int,
    ) -> dict[str, object]:
        bounded_limit = max(1, min(int(limit), 20))
        counts = connection.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                SUM(CASE WHEN state='waiting_retry' THEN 1 ELSE 0 END) AS waiting_count,
                SUM(CASE WHEN state='retrying' THEN 1 ELSE 0 END) AS retrying_count,
                SUM(CASE WHEN paused=1 THEN 1 ELSE 0 END) AS paused_count
            FROM lp_preparation_items
            WHERE state != 'recovered'
            """
        ).fetchone()
        paused_rows = connection.execute(
            """
            SELECT condition_id,stage,error,token_id
            FROM lp_preparation_items
            WHERE paused=1 AND state != 'recovered'
            ORDER BY condition_id
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
        waiting_rows = connection.execute(
            """
            SELECT condition_id,stage,error,token_id
            FROM lp_preparation_items
            WHERE state IN ('waiting_retry','retrying')
              AND state != 'recovered'
            ORDER BY condition_id
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()

        def samples(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
            return [
                {
                    "condition_id": str(row["condition_id"]),
                    "stage": str(row["stage"]),
                    "error": row["error"],
                    "token_id": row["token_id"],
                }
                for row in rows
            ]

        total_count = int(counts["total_count"] or 0) if counts is not None else 0
        waiting_count = int(counts["waiting_count"] or 0) if counts is not None else 0
        retrying_count = int(counts["retrying_count"] or 0) if counts is not None else 0
        paused_count = int(counts["paused_count"] or 0) if counts is not None else 0
        return {
            "preparation_item_total": total_count,
            "waiting_market_count": waiting_count,
            "retrying_market_count": retrying_count,
            "paused_market_count": paused_count,
            "failed_market_count": paused_count,
            "paused_error_samples": samples(paused_rows),
            "waiting_error_samples": samples(waiting_rows),
            "paused_error_samples_truncated": paused_count > bounded_limit,
            "waiting_error_samples_truncated": waiting_count + retrying_count > bounded_limit,
        }

    def lp_preparation_item_summary(self, *, limit: int = 5) -> dict[str, object]:
        """Read bounded per-market coverage counts and error samples."""

        with self._read_connection() as connection:
            return self._lp_preparation_item_summary_from_connection(
                connection, limit=limit
            )

    def lp_claim_preparation_item_alerts(self, *, limit: int = 5) -> dict[str, object]:
        """Claim one aggregate alert for newly paused markets."""

        bounded_limit = max(1, min(int(limit), 20))
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT condition_id
                FROM lp_preparation_items
                WHERE paused=1 AND alert_attempted=0
                ORDER BY condition_id
                """
            ).fetchall()
            condition_ids = tuple(str(row["condition_id"]) for row in rows)
            if condition_ids:
                connection.executemany(
                    """
                    UPDATE lp_preparation_items
                    SET alert_attempted=1,alert_state='claimed',updated_at=?
                    WHERE condition_id=? AND paused=1 AND alert_attempted=0
                    """,
                    ((_utc_now(), condition_id) for condition_id in condition_ids),
                )
            alert_rows = ()
            if condition_ids:
                placeholders = ",".join("?" for _ in condition_ids)
                alert_rows = connection.execute(
                    f"""
                    SELECT condition_id,stage,error,token_id
                    FROM lp_preparation_items
                    WHERE paused=1 AND alert_attempted=1 AND alert_state='claimed'
                      AND condition_id IN ({placeholders})
                    ORDER BY condition_id
                    """,
                    condition_ids,
                ).fetchall()

            def samples(rows: Iterable[sqlite3.Row]) -> list[dict[str, object]]:
                return [
                    {
                        "condition_id": str(row["condition_id"]),
                        "stage": str(row["stage"]),
                        "error": row["error"],
                        "token_id": row["token_id"],
                    }
                    for row in rows
                ]

            summary = self._lp_preparation_item_summary_from_connection(
                connection, limit=bounded_limit
            )
        summary["alert_pending"] = bool(condition_ids)
        summary["alert_condition_ids"] = list(condition_ids[:bounded_limit])
        summary["alert_condition_count"] = len(condition_ids)
        summary["alert_error_samples"] = samples(alert_rows[:bounded_limit])
        summary["alert_error_samples_truncated"] = len(condition_ids) > bounded_limit
        return summary

    def lp_finish_preparation_item_alerts(
        self,
        *,
        success: bool,
        condition_ids: Iterable[str] | None = None,
    ) -> int:
        """Record one completed aggregate alert attempt without rearming data."""

        identities = None if condition_ids is None else tuple(
            dict.fromkeys(
                str(value).strip() for value in condition_ids if str(value).strip()
            )
        )
        state = "sent" if success else "failed"
        with self._transaction() as connection:
            if identities is None:
                cursor = connection.execute(
                    """
                    UPDATE lp_preparation_items
                    SET alert_state=?,updated_at=?
                    WHERE paused=1 AND alert_attempted=1 AND alert_state='claimed'
                    """,
                    (state, _utc_now()),
                )
            elif identities:
                where = " OR ".join("condition_id=?" for _ in identities)
                cursor = connection.execute(
                    f"""
                    UPDATE lp_preparation_items
                    SET alert_state=?,updated_at=?
                    WHERE paused=1 AND alert_attempted=1 AND alert_state='claimed'
                      AND ({where})
                    """,
                    (state, _utc_now(), *identities),
                )
            else:
                return 0
        return int(cursor.rowcount)

    def lp_wake_preparation_retries(
        self,
        *,
        condition_ids: Iterable[str],
        stage: str | None = None,
        now: datetime,
        generation: int | None = None,
    ) -> int:
        """Wake waiting markets after their related dependency recovers."""

        identities = tuple(
            dict.fromkeys(
                str(value).strip() for value in condition_ids if str(value).strip()
            )
        )
        if not identities:
            return 0
        current = _canonical_timestamp(now)
        expected_generation = (
            generation if type(generation) is int and generation >= 1 else None
        )
        with self._transaction() as connection:
            changed = 0
            for condition_id in identities:
                row = connection.execute(
                    "SELECT generation,state,paused,stage FROM lp_preparation_items WHERE condition_id=?",
                    (condition_id,),
                ).fetchone()
                if row is None or row["state"] != "waiting_retry" or row["paused"]:
                    continue
                if expected_generation is not None and int(row["generation"]) > expected_generation:
                    continue
                if stage is not None and str(row["stage"] or "") != str(stage):
                    continue
                connection.execute(
                    """
                    UPDATE lp_preparation_items
                    SET retry_used=0,state='waiting_retry',paused=0,
                        next_retry_at=?,retry_started_at=NULL,updated_at=?
                    WHERE condition_id=?
                    """,
                    (current, _utc_now(), condition_id),
                )
                changed += 1
        return changed

    def lp_record_preparation_failure(
        self,
        condition_id: str,
        *,
        generation: int,
        stage: str,
        error: str,
        failed_at: datetime,
        direction: str | None = None,
        token_id: str | None = None,
        retry_after_seconds: int | float | None = None,
        retry_after_at: datetime | str | None = None,
    ) -> dict[str, object] | None:
        """Record one market failure while sharing one retry budget."""

        condition = str(condition_id or "").strip()
        if not condition:
            return None
        failed = _canonical_timestamp(failed_at)
        safe_stage = str(stage or "unknown").strip() or "unknown"
        safe_error = str(error or "unknown_error").strip() or "unknown_error"
        retry_started_at: str | None = None
        with self._transaction() as connection:
            request_generation = max(1, int(generation))
            singleton = connection.execute(
                "SELECT payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            global_fence = _load_payload(str(singleton["payload"])).get("global_recovery_generation") if singleton is not None else None
            if type(global_fence) is int and global_fence > request_generation:
                return None
            existing = connection.execute(
                "SELECT * FROM lp_preparation_items WHERE condition_id=?",
                (condition,),
            ).fetchone()
            if (
                existing is not None
                and int(existing["generation"]) > request_generation
            ):
                return None
            effective_generation = request_generation
            existing_is_fence = bool(existing and existing["state"] == "recovered")
            previous_paused = (
                False if existing_is_fence else bool(existing and existing["paused"])
            )
            previous_count = (
                0 if existing_is_fence else int(existing["failure_count"]) if existing else 0
            )
            normalized_error = str(safe_error).casefold()
            upstream_history_shape_error = (
                safe_stage == "history"
                and normalized_error
                in {
                    "history_values_invalid",
                    "history_values_unknown",
                    "history_missing",
                    "history_insufficient",
                    "history_window_incomplete",
                }
            )
            operator_error = (
                not upstream_history_shape_error
                and (
                    any(
                        marker in normalized_error
                        for marker in _LP_OPERATOR_ERROR_MARKERS
                    )
                    or any(
                        code in normalized_error
                        for code in ("http401", "http403", "status401", "status403")
                    )
                )
            )
            paused = previous_paused or operator_error
            # A transient market retry is a bounded, repeatable probe rather
            # than a one-shot allowance.  The claim lease still fences a live
            # attempt, while a failed attempt returns to waiting_retry.
            retry_used = 1 if paused else 0
            state = "paused" if paused else "waiting_retry"
            next_retry_at = None
            if not paused:
                delay_index = min(
                    max(previous_count, 0),
                    len(_LP_PREPARATION_RETRY_DELAYS_SECONDS) - 1,
                )
                retry_deadline = _parse_timestamp(failed) + timedelta(
                    seconds=_LP_PREPARATION_RETRY_DELAYS_SECONDS[delay_index]
                )
                if (
                    isinstance(retry_after_seconds, (int, float))
                    and not isinstance(retry_after_seconds, bool)
                    and math.isfinite(float(retry_after_seconds))
                    and retry_after_seconds >= 0
                ):
                    retry_deadline = max(
                        retry_deadline,
                        _parse_timestamp(failed)
                        + timedelta(seconds=float(retry_after_seconds)),
                    )
                elif retry_after_at is not None:
                    try:
                        retry_deadline = max(
                            retry_deadline, _parse_timestamp(retry_after_at)
                        )
                    except (TypeError, ValueError):
                        pass
                next_retry_at = _canonical_timestamp(retry_deadline)
            if existing is not None and not existing_is_fence:
                retry_started_at = existing["retry_started_at"]
            alert_attempted = (
                int(existing["alert_attempted"])
                if existing is not None and not existing_is_fence
                else 0
            )
            alert_state = (
                existing["alert_state"]
                if existing is not None and not existing_is_fence
                else None
            )
            if paused and not previous_paused:
                alert_attempted = 0
                alert_state = None
            connection.execute(
                """
                INSERT INTO lp_preparation_items(
                    condition_id,generation,retry_used,failure_count,state,paused,
                    stage,direction,token_id,error,failed_at,next_retry_at,
                    retry_started_at,alert_attempted,alert_state,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    generation=excluded.generation,
                    retry_used=excluded.retry_used,
                    failure_count=excluded.failure_count,
                    state=excluded.state,
                    paused=excluded.paused,
                    stage=excluded.stage,
                    direction=excluded.direction,
                    token_id=excluded.token_id,
                    error=excluded.error,
                    failed_at=excluded.failed_at,
                    next_retry_at=excluded.next_retry_at,
                    retry_started_at=excluded.retry_started_at,
                    alert_attempted=excluded.alert_attempted,
                    alert_state=excluded.alert_state,
                    updated_at=excluded.updated_at
                """,
                (
                    condition,
                    effective_generation,
                    int(retry_used),
                    max(1, previous_count + 1),
                    state,
                    int(paused),
                    safe_stage,
                    direction,
                    token_id,
                    safe_error,
                    failed,
                    next_retry_at,
                    retry_started_at,
                    alert_attempted,
                    alert_state,
                    _utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM lp_preparation_items WHERE condition_id=?",
                (condition,),
            ).fetchone()
        return None if row is None else self._lp_preparation_item(row)

    def lp_claim_preparation_retries(
        self,
        *,
        now: datetime,
        condition_ids: Iterable[str] | None = None,
    ) -> list[dict[str, object]]:
        """Atomically spend due retries that the caller is ready to dispatch."""

        current = _canonical_timestamp(now)
        now_moment = _parse_timestamp(current)
        claimed: list[dict[str, object]] = []
        identities = None if condition_ids is None else tuple(
            dict.fromkeys(
                str(value).strip() for value in condition_ids if str(value).strip()
            )
        )
        if identities == ():
            return claimed
        entered_here = self._lp_preparation_owner_entered_here()
        if not entered_here and self._lp_preparation_owner_handle is None:
            return claimed
        try:
            with self._transaction() as connection:
                if identities is None:
                    rows = connection.execute(
                        """
                        SELECT * FROM lp_preparation_items
                        WHERE state != 'recovered'
                          AND paused=0 AND retry_used=0 AND next_retry_at IS NOT NULL
                        ORDER BY next_retry_at, condition_id
                        """
                    ).fetchall()
                else:
                    # Older deployed SQLite builds allow only 999 parameters.
                    # Keep every batch and retry spend in this one transaction.
                    batch_size = max(1, connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
                    rows = []
                    for offset in range(0, len(identities), batch_size):
                        batch = identities[offset : offset + batch_size]
                        placeholders = ",".join("?" for _ in batch)
                        rows.extend(connection.execute(
                            f"""
                            SELECT * FROM lp_preparation_items
                            WHERE state != 'recovered'
                              AND paused=0 AND retry_used=0 AND next_retry_at IS NOT NULL
                              AND condition_id IN ({placeholders})
                            """,
                            batch,
                        ).fetchall())
                    rows.sort(key=lambda row: (row["next_retry_at"], row["condition_id"]))
                for row in rows:
                    try:
                        due = _parse_timestamp(row["next_retry_at"])
                    except ValueError:
                        continue
                    if due > now_moment:
                        continue
                    condition = str(row["condition_id"])
                    connection.execute(
                        """
                        UPDATE lp_preparation_items
                        SET retry_used=1,state='retrying',retry_started_at=?,
                            next_retry_at=NULL,updated_at=?
                        WHERE condition_id=? AND paused=0 AND retry_used=0
                        """,
                        (current, _utc_now(), condition),
                    )
                    updated = connection.execute(
                        "SELECT * FROM lp_preparation_items WHERE condition_id=?",
                        (condition,),
                    ).fetchone()
                    if updated is not None:
                        claimed.append(self._lp_preparation_item(updated))
        finally:
            if entered_here:
                self.lp_release_preparation_owner()
        return claimed

    def lp_clear_preparation_items(
        self,
        condition_ids: Iterable[str],
        *,
        generation: int | None = None,
    ) -> int:
        identities = tuple(
            dict.fromkeys(
                str(value).strip() for value in condition_ids if str(value).strip()
            )
        )
        if not identities:
            return 0
        with self._transaction() as connection:
            cleared = 0
            expected_generation = (
                generation if type(generation) is int and generation >= 1 else None
            )
            singleton = connection.execute(
                "SELECT payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            global_fence = _load_payload(str(singleton["payload"])).get("global_recovery_generation") if singleton is not None else None
            if type(global_fence) is int and expected_generation is not None and global_fence > expected_generation:
                return 0
            for condition_id in identities:
                row = connection.execute(
                    "SELECT generation,state FROM lp_preparation_items WHERE condition_id=?",
                    (condition_id,),
                ).fetchone()
                if row is None:
                    continue
                if (
                    expected_generation is not None
                    and int(row["generation"]) > expected_generation
                ):
                    continue
                if expected_generation is None:
                    if row["state"] == "recovered":
                        continue
                    connection.execute(
                        "DELETE FROM lp_preparation_items WHERE condition_id=?",
                        (condition_id,),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE lp_preparation_items
                        SET generation=?,retry_used=1,failure_count=1,
                            state='recovered',paused=0,error=NULL,failed_at=NULL,
                            next_retry_at=NULL,retry_started_at=NULL,
                            alert_attempted=0,alert_state=NULL,updated_at=?
                        WHERE condition_id=?
                        """,
                        (expected_generation, _utc_now(), condition_id),
                    )
                cleared += 1
        return cleared

    def lp_recover_preparation_items(
        self, condition_ids: Iterable[str] | None = None,
        *, expected_generation: int | None = None,
    ) -> list[dict[str, object]]:
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 1
        ):
            raise ValueError("positive expected preparation generation required")
        identities = None if condition_ids is None else tuple(
            dict.fromkeys(
                str(value).strip() for value in condition_ids if str(value).strip()
            )
        )
        with self._transaction() as connection:
            singleton = connection.execute(
                "SELECT generation,payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            if singleton is None:
                if expected_generation is not None:
                    raise ValueError("paused preparation generation mismatch")
                return []
            current_generation = int(singleton["generation"])
            payload = _load_payload(str(singleton["payload"]))
            if expected_generation is not None and (
                current_generation != expected_generation
                or payload.get("paused") is not True
                or payload.get("state") != "paused"
            ):
                raise ValueError("paused preparation generation mismatch")
            if expected_generation is not None:
                # Offline recovery cannot let triggers mutate trading/audit
                # tables. Default production recovery remains unchanged.
                def preparation_writes_only(action, table, column, database, trigger):
                    if trigger is not None and action in {
                        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_UPDATE
                    }:
                        return sqlite3.SQLITE_DENY
                    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
                        return sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_UPDATE and table not in {
                        "lp_preparation", "lp_preparation_items"
                    }:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(preparation_writes_only)
            if identities is None:
                rows = connection.execute(
                    """
                    SELECT condition_id FROM lp_preparation_items
                    WHERE paused=1 AND state != 'recovered'
                    """
                ).fetchall()
            elif not identities:
                rows = []
            else:
                where = " OR ".join("condition_id=?" for _ in identities)
                rows = connection.execute(
                    f"""
                    SELECT condition_id FROM lp_preparation_items
                    WHERE state != 'recovered'
                      AND (paused=1 OR state='waiting_retry')
                      AND ({where})
                    """,
                    identities,
                ).fetchall()
            selected = tuple(str(row["condition_id"]) for row in rows)
            should_advance = bool(selected) or payload.get("paused") is True
            if not should_advance:
                return []
            new_generation = current_generation + 1
            if selected:
                connection.executemany(
                    """
                    UPDATE lp_preparation_items
                    SET generation=?,retry_used=1,failure_count=1,
                        state='recovered',paused=0,error=NULL,failed_at=NULL,
                        next_retry_at=NULL,retry_started_at=NULL,
                        alert_attempted=0,alert_state=NULL,updated_at=?
                    WHERE condition_id=? AND state != 'recovered'
                      AND (paused=1 OR state='waiting_retry')
                    """,
                    (
                        (new_generation, _utc_now(), value)
                        for value in selected
                    ),
                )
            recovered_state = (
                "partial"
                if selected and payload.get("state") != "paused"
                else "ready"
            )
            if expected_generation is not None and identities == ():
                payload["global_recovery_generation"] = new_generation
                # The old error is unknown: retain its audit, re-arm only the
                # global task, and let the normal reader prove recovery.
                payload["recovery_history"] = [
                    *payload.get("recovery_history", []),
                    {
                        **{key: payload.get(key) for key in (
                            "state", "stage", "last_error", "last_error_chain",
                            "last_error_status", "last_error_category",
                            "last_sqlite_errorcode", "last_sqlite_errorname",
                            "last_failure_at", "attempt", "failure_count",
                        )},
                        "generation": current_generation,
                        "scope": "global",
                        "recovered_at": _utc_now(),
                    },
                ]
            payload.update(
                {
                    "state": recovered_state,
                    "stage": "catalog",
                    "attempt": 0,
                    "failure_count": 0,
                    "paused": False,
                    "alert_attempted": False,
                    "alert_state": None,
                    "last_error": None,
                    "next_retry_at": None,
                    "next_probe_at": None,
                    "last_probe_at": None,
                    "last_probe_state": None,
                    "retry_after_seconds": None,
                    "fault_started_at": None,
                    "fault_alert_attempts": 0,
                    "fault_alert_state": None,
                    "fault_alert_next_at": None,
                    "fault_alert_sent_at": None,
                    "recovery_alert_state": None,
                    "recovery_alert_attempts": 0,
                    "recovery_alert_next_at": None,
                    "recovery_alert_claimed_at": None,
                    "recovery_alert_sent_at": None,
                }
            )
            connection.execute(
                """
                UPDATE lp_preparation
                SET generation=?,payload=?,updated_at=?
                WHERE singleton=1 AND generation=?
                """,
                (
                    new_generation,
                    _dump_payload(payload),
                    _utc_now(),
                    current_generation,
                ),
            )
        return [{"condition_id": value, "recovered": True} for value in selected]

    def lp_preparation(self) -> dict[str, object] | None:
        """Load the durable singleton state for LP preparation."""

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT generation,payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
        if row is None:
            return None
        payload = _load_payload(str(row["payload"]))
        payload["generation"] = int(row["generation"])
        return payload

    def lp_save_preparation(
        self,
        payload: Mapping[str, object],
        *,
        expected_generation: int | None = None,
    ) -> dict[str, object] | None:
        """Persist one preparation snapshot with an optional cycle guard.

        ``generation`` identifies a manual recovery cycle.  Progress updates
        keep that generation; a stale worker cannot overwrite a newer cycle.
        """

        encoded = _dump_payload(payload)
        requested_generation = payload.get("generation")
        if type(requested_generation) is not int or requested_generation < 1:
            requested_generation = 1
        updated_at = _utc_now()
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT generation,payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            if current is None:
                if expected_generation is not None:
                    return None
                generation = requested_generation
                connection.execute(
                    "INSERT INTO lp_preparation(singleton,generation,payload,updated_at) VALUES (1,?,?,?)",
                    (generation, encoded, updated_at),
                )
            else:
                current_generation = int(current["generation"])
                if (
                    expected_generation is not None
                    and current_generation != expected_generation
                ):
                    saved = _load_payload(str(current["payload"]))
                    saved["generation"] = current_generation
                    return saved
                generation = requested_generation
                connection.execute(
                    "UPDATE lp_preparation SET generation=?,payload=?,updated_at=? WHERE singleton=1",
                    (generation, encoded, updated_at),
                )
        saved = _load_payload(encoded)
        saved["generation"] = generation
        return saved

    def lp_claim_preparation_alert(
        self, *, expected_generation: int | None = None
    ) -> dict[str, object] | None:
        """Claim the one alert attempt for a paused preparation cycle."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT generation,payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            if row is None:
                return None
            generation = int(row["generation"])
            if expected_generation is not None and generation != expected_generation:
                saved = _load_payload(str(row["payload"]))
                saved["generation"] = generation
                saved["alert_claimed_now"] = False
                return saved
            payload = _load_payload(str(row["payload"]))
            if payload.get("paused") is not True or payload.get("alert_attempted") is True:
                payload["generation"] = generation
                payload["alert_claimed_now"] = False
                return payload
            payload["alert_attempted"] = True
            payload["alert_state"] = "claimed"
            encoded = _dump_payload(payload)
            connection.execute(
                "UPDATE lp_preparation SET payload=?,updated_at=? WHERE singleton=1 AND generation=?",
                (encoded, _utc_now(), generation),
            )
        payload["generation"] = generation
        payload["alert_claimed_now"] = True
        return payload

    def lp_finish_preparation_alert(
        self,
        *,
        generation: int,
        success: bool,
    ) -> dict[str, object] | None:
        """Record the result of the claimed preparation alert attempt."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT generation,payload FROM lp_preparation WHERE singleton=1"
            ).fetchone()
            if row is None or int(row["generation"]) != generation:
                return None
            payload = _load_payload(str(row["payload"]))
            payload["alert_state"] = "sent" if success else "failed"
            encoded = _dump_payload(payload)
            connection.execute(
                "UPDATE lp_preparation SET payload=?,updated_at=? WHERE singleton=1 AND generation=?",
                (encoded, _utc_now(), generation),
            )
        payload["generation"] = generation
        return payload

    def save_lp_observation(
        self,
        account_id: str,
        condition_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist one account-scoped LP market observation and its alert state."""

        account_key = str(account_id).strip()
        market_key = str(condition_id).strip()
        if not account_key or not market_key:
            raise ValueError("LP observation identity is required")
        encoded = _dump_payload(payload)
        updated_at = _utc_now()
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT payload FROM lp_market_observations WHERE account_id=? AND condition_id=?",
                (account_key, market_key),
            ).fetchone()
            if current is not None:
                previous = _load_payload(str(current["payload"]))
                previous_share = previous.get("share_alert")
                if isinstance(previous_share, Mapping):
                    # Observation publishers may have read an older selection
                    # and an older alert episode.  They must not overwrite any
                    # part of the current share-watch state.  Explicit share
                    # updates use update_lp_observation(), which reads and
                    # merges the latest row in its own short transaction.
                    merged_payload = dict(payload)
                    merged_payload["share_alert"] = dict(previous_share)
                    encoded = _dump_payload(merged_payload)
            connection.execute(
                """
                INSERT INTO lp_market_observations(account_id, condition_id, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, condition_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (account_key, market_key, encoded, updated_at),
            )
        result = _load_payload(encoded)
        result.update({"condition_id": market_key, "updated_at": updated_at})
        return result

    def update_lp_observation(
        self,
        account_id: str,
        condition_id: str,
        updates: Mapping[str, object],
    ) -> dict[str, object]:
        """Atomically merge selected fields into one LP observation."""

        account_key = str(account_id).strip()
        market_key = str(condition_id).strip()
        if not account_key or not market_key:
            raise ValueError("LP observation identity is required")
        if not isinstance(updates, Mapping):
            raise ValueError("LP observation updates must be an object")
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT payload FROM lp_market_observations WHERE account_id=? AND condition_id=?",
                (account_key, market_key),
            ).fetchone()
            previous = _load_payload(str(current["payload"])) if current is not None else {}
            merged = dict(previous)
            for key, value in updates.items():
                old_value = merged.get(str(key))
                if isinstance(old_value, Mapping) and isinstance(value, Mapping):
                    merged[str(key)] = {**old_value, **value}
                else:
                    merged[str(key)] = value
            encoded = _dump_payload(merged)
            updated_at = _utc_now()
            connection.execute(
                """
                INSERT INTO lp_market_observations(account_id, condition_id, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, condition_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (account_key, market_key, encoded, updated_at),
            )
        result = _load_payload(encoded)
        result.update({"condition_id": market_key, "updated_at": updated_at})
        return result

    def lp_observations(self, account_id: str) -> dict[str, dict[str, object]]:
        """Load saved LP observations for one account without changing them."""

        account_key = str(account_id).strip()
        if not account_key:
            return {}
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT condition_id, payload, updated_at
                FROM lp_market_observations
                WHERE account_id=?
                ORDER BY condition_id
                """,
                (account_key,),
            ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            condition_id = str(row["condition_id"])
            payload = _load_payload(str(row["payload"]))
            payload.update(
                {"condition_id": condition_id, "updated_at": str(row["updated_at"])}
            )
            result[condition_id] = payload
        return result

    def lp_competitiveness_upsert(
        self,
        entries: Iterable[tuple[str, Decimal, datetime]],
    ) -> int:
        """Persist one full competition round in a single transaction (#181).

        Rows are keyed by condition_id; a re-read of the same market replaces
        its previous value and checked_at instead of stacking a second row.
        """

        prepared_at = monotonic()
        encoded: list[tuple[str, str, str]] = []
        for condition_id, value, checked_at in entries:
            condition = str(condition_id).strip()
            if not condition:
                raise ValueError("lp_competitiveness_identity_invalid")
            if not isinstance(value, Decimal):
                raise ValueError("lp_competitiveness_value_invalid")
            encoded.append(
                (
                    condition,
                    _decimal_string(value),
                    _canonical_timestamp(checked_at),
                )
            )
        if not encoded:
            return 0
        with self._transaction(prepared_at=prepared_at, diagnostics={"input_rows": len(encoded)}) as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO lp_market_competitiveness
                (condition_id, value, checked_at)
                VALUES (?, ?, ?)
                """,
                encoded,
            )
        return len(encoded)

    def lp_competitiveness_entry(self, condition_id: str):
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT value,checked_at FROM lp_market_competitiveness WHERE condition_id=?",
                (condition_id,),
            ).fetchone()
        return (Decimal(str(row[0])), _parse_timestamp(row[1])) if row else None

    def lp_competitiveness_map(self) -> dict[str, tuple[Decimal, datetime]]:
        """Read every persisted competition value back keyed by condition_id."""

        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT condition_id, value, checked_at
                FROM lp_market_competitiveness
                """
            ).fetchall()
        result: dict[str, tuple[Decimal, datetime]] = {}
        for row in rows:
            try:
                value = Decimal(str(row["value"]))
                checked_at = _parse_timestamp(row["checked_at"])
            except (ArithmeticError, TypeError, ValueError):
                continue
            result[str(row["condition_id"])] = (value, checked_at)
        return result

    def lp_competitiveness_count(self) -> int:
        """Count the persisted competition rows."""

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM lp_market_competitiveness"
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _lp_report_date(report_date: str) -> str:
        value = str(report_date)
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("lp_report_date_invalid") from exc
        if parsed.isoformat() != value:
            raise ValueError("lp_report_date_invalid")
        return value

    def lp_auto_daily_reports(
        self, account_id: str, auto_run_id: str, *, summaries: bool = False,
    ) -> list[dict[str, object]]:
        columns = (
            "report_date, json_extract(payload,'$.generated_at') AS generated_at, json_extract(payload,'$.late_generated') AS late_generated"
            if summaries else "payload"
        )
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM lp_auto_daily_reports WHERE account_id=? AND auto_run_id=? ORDER BY report_date DESC",
                (account_id, auto_run_id),
            ).fetchall()
        if summaries:
            return [dict(row) for row in rows]
        return [_load_payload(str(row["payload"])) for row in rows]

    def lp_auto_daily_report(
        self, account_id: str, auto_run_id: str, report_date: str,
    ) -> dict[str, object] | None:
        key = self._lp_report_date(report_date)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM lp_auto_daily_reports WHERE account_id=? AND auto_run_id=? AND report_date=?",
                (account_id, auto_run_id, key),
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    def lp_save_auto_daily_report(
        self, account_id: str, auto_run_id: str, report_date: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Freeze natural-day facts once; independent of legacy 08:00 reports."""
        key = self._lp_report_date(report_date)
        if not account_id.strip() or not auto_run_id.strip():
            raise ValueError("lp_auto_report_identity_required")
        _parse_timestamp(payload.get("generated_at"))
        value = {**payload, "account_id": account_id, "auto_run_id": auto_run_id, "report_date": key}
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO lp_auto_daily_reports(account_id,auto_run_id,report_date,payload) VALUES (?,?,?,?)",
                (account_id, auto_run_id, key, _dump_execution_payload(value)),
            )
            row = connection.execute(
                "SELECT payload FROM lp_auto_daily_reports WHERE account_id=? AND auto_run_id=? AND report_date=?",
                (account_id, auto_run_id, key),
            ).fetchone()
        assert row is not None
        return _load_payload(str(row["payload"]))

    def lp_daily_report(self, report_date: str) -> dict[str, object] | None:
        key = self._lp_report_date(report_date)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload, generated_at FROM lp_daily_reports WHERE report_date=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        payload = _load_payload(str(row["payload"]))
        payload.update(
            {"report_date": key, "generated_at": str(row["generated_at"])}
        )
        return payload

    def lp_latest_daily_report(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT report_date FROM lp_daily_reports ORDER BY report_date DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self.lp_daily_report(str(row["report_date"]))

    def lp_save_daily_report(
        self, report_date: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        """Insert one immutable report per Beijing report date."""

        key = self._lp_report_date(report_date)
        generated_at = payload.get("generated_at")
        if not isinstance(generated_at, str) or not generated_at.strip():
            raise ValueError("lp_report_generated_at_required")
        encoded = _dump_execution_payload(payload)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO lp_daily_reports(report_date,payload,generated_at) VALUES (?,?,?)",
                (key, encoded, generated_at),
            )
            row = connection.execute(
                "SELECT payload, generated_at FROM lp_daily_reports WHERE report_date=?",
                (key,),
            ).fetchone()
        assert row is not None
        saved = _load_payload(str(row["payload"]))
        saved.update(
            {"report_date": key, "generated_at": str(row["generated_at"])}
        )
        return saved

    def lp_auto_owns_session(self, session_id: str) -> bool:
        with self._read_connection() as connection:
            row = connection.execute("SELECT payload FROM lp_auto_pool WHERE singleton=1").fetchone()
        if row is None:
            return False
        return any(i.get("session_id") == session_id for i in json.loads(row[0]).get("intents", {}).values())

    def lp_create_session(
        self,
        session_id: str,
        idempotency_key: str,
        *,
        state: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        stored_payload = dict(payload)
        stored_payload["_lp_revision"] = 0
        encoded = _dump_execution_payload(stored_payload)
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM lp_sessions WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
            if existing is not None:
                return self._lp_row_result(existing)
            n_leg_control = connection.execute(
                "SELECT active_batch_id FROM n_leg_controls WHERE singleton=1"
            ).fetchone()
            if n_leg_control is not None and n_leg_control["active_batch_id"] is not None:
                raise ValueError("active_n_leg_batch")
            try:
                connection.execute(
                    "INSERT INTO lp_sessions(session_id,idempotency_key,state,payload,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (str(session_id), str(idempotency_key), str(state), encoded, now, now),
                )
            except sqlite3.IntegrityError as exc:
                # Preserve the public conflict code for canonical account/token groups.
                if "one_active_lp_session_account_token" in str(exc):
                    raise ValueError("lp_session_market_active") from exc
                raise
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            assert row is not None
            return self._lp_row_result(row)

    def _lp_manual_retirement_state(self, connection, session_id, payload, state):
        """A late local callback cannot reopen a waived empty request container."""
        from .polymarket_lp_accounting import reservation_is_manually_released
        retired = payload.get("manual_release_retired")
        account_retired = payload.get("account_coverage_retired")
        api_retired = (isinstance(account_retired, Mapping)
            and account_retired.get("reason") == "account_observation_no_exposure"
            and account_retired.get("snapshot_id") and account_retired.get("checked_at"))
        # Prior container retirement is durable. Current capital admission still
        # checks fresh account facts; stale local fill audit cannot reopen it.
        if (not api_retired and (not isinstance(retired, Mapping)
                                 or retired.get("reason") != "operator_waived_provisional_hold")
                or not reservation_is_manually_released({**payload, "session_id": str(session_id)})
                or self._lp_owned_session_ids(payload)
                or not api_retired and any((_maybe_decimal(payload.get(key)) or Decimal(0)) > 0
                       for key in ("residual_quantity", "buy_filled_quantity"))
                or any(payload.get(key) in {"pending", "unknown", "accepted_without_order_id"}
                       for key in ("passive_exit_attempt_state", "protected_exit_attempt_state"))):
            return state
        if any(action.get("state") in {"pending", "unknown", "accepted_without_order_id"}
               and ("cancel" in str(action.get("action_key") or "")
                    or action.get("role") != "entry"
                    and not str(action.get("action_key") or "").endswith("entry-submit"))
               for action in self.lp_actions(str(session_id), connection=connection)):
            return state
        return "complete"

    def lp_update_session(
        self,
        session_id: str,
        *,
        state: str | None = None,
        patch: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError("lp_session_not_found")
            payload = _load_payload(str(row["payload"]))
            if patch:
                payload.update(patch)
            next_state = self._lp_manual_retirement_state(connection, session_id, payload, str(state or row["state"]))
            try:
                revision = int(payload.get("_lp_revision", 0))
            except (TypeError, ValueError):
                revision = 0
            payload["_lp_revision"] = max(revision, 0) + 1
            connection.execute(
                "UPDATE lp_sessions SET state=?,payload=?,updated_at=? WHERE session_id=?",
                (next_state, _dump_execution_payload(payload), now, str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            assert updated is not None
        return self._lp_row_result(updated)

    @staticmethod
    def _lp_order_state_rank(status: object) -> int:
        value = str(status or "").upper()
        if value in TERMINAL_ORDER_STATES:
            return 3
        if value == "PARTIALLY_FILLED":
            return 2
        if value in {"LIVE", "OPEN", "ACCEPTED", "PENDING"}:
            return 1
        return 0

    @classmethod
    def _lp_register_order_record(cls, raw: Mapping[str, object]) -> dict[str, object]:
        order_id = str(raw.get("order_id") or raw.get("id") or "").strip()
        token_id = str(raw.get("token_id") or raw.get("asset_id") or "").strip()
        side = str(raw.get("side") or "").strip().upper()
        if not order_id or not token_id or side not in {"BUY", "SELL"}:
            raise ValueError("order_identity_unknown")

        def decimal(name: str) -> Decimal | None:
            value = raw.get(name)
            if value is None or value == "":
                return None
            try:
                parsed = value if isinstance(value, Decimal) else Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{name}_invalid") from exc
            if not parsed.is_finite() or parsed < 0:
                raise ValueError(f"{name}_invalid")
            return parsed

        return {
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "status": str(raw.get("status") or "UNKNOWN").upper(),
            "price": decimal("price"),
            "quantity": decimal("original_size") if "original_size" in raw else decimal("quantity"),
            "size_matched": decimal("size_matched"),
            "average_price": decimal("average_price"),
            "order_type": raw.get("order_type"),
            "expiration": raw.get("expiration", raw.get("expires_at")),
            "created_at": raw.get("created_at"),
            "fills": raw.get("fills", ()),
            "remaining_size": decimal("remaining_size"),
            "queue_baseline": raw.get("_queue_baseline", raw.get("queue_baseline")),
        }

    @staticmethod
    def _lp_owned_session_ids(payload: Mapping[str, object]) -> set[str]:
        history = payload.get("order_history")
        ids = {
            str(key)
            for key in (history.keys() if isinstance(history, Mapping) else ())
        }
        for field in (
            "owned_order_ids",
            "augment_order_ids",
            "augment_order_id",
            "entry_order_id",
            "passive_exit_order_id",
            "protected_exit_order_id",
        ):
            value = payload.get(field)
            if isinstance(value, (list, tuple)):
                ids.update(str(item) for item in value if item)
            elif value:
                ids.add(str(value))
        return ids

    @classmethod
    def _lp_merge_order_record(
        cls, current: object, incoming: Mapping[str, object]
    ) -> dict[str, object]:
        base = dict(current) if isinstance(current, Mapping) else {}
        if base and base.get("order_id") != incoming["order_id"]:
            raise ValueError("order_identity_conflict")
        for field in ("token_id", "side"):
            known = str(base.get(field) or "").strip()
            value = str(incoming.get(field) or "").strip()
            if known and value and known.upper() != value.upper():
                raise ValueError("order_identity_conflict")
            base[field] = known or incoming[field]
        current_rank = cls._lp_order_state_rank(base.get("status"))
        incoming_rank = cls._lp_order_state_rank(incoming.get("status"))
        if current_rank >= incoming_rank:
            base["status"] = base.get("status") or incoming["status"]
        else:
            base["status"] = incoming["status"]
        # SQLite JSON reads money/quantity fields as strings.  Normalize
        # before any comparison or a restarted process would let a stale
        # lower cumulative fill overwrite the durable higher value.
        old_price = _maybe_decimal(base.get("price"))
        new_price = _maybe_decimal(incoming.get("price"))
        old_quantity = _maybe_decimal(base.get("quantity"))
        new_quantity = _maybe_decimal(incoming.get("quantity"))
        if old_quantity is not None and new_quantity is not None:
            if old_quantity != new_quantity:
                raise ValueError("order_original_quantity_conflict")
            if old_price is not None and new_price is not None and old_price != new_price:
                raise ValueError("order_limit_price_conflict")
        # Trade-only discovery uses execution average in price while original
        # size is unknown. An actual order may establish its distinct limit;
        # the execution average remains in average_price. Conversely a later
        # fill-only row must never replace a known (or missing) original limit.
        if old_quantity is None and new_quantity is not None:
            base["price"] = new_price
        elif old_quantity is not None and new_quantity is None:
            base["price"] = old_price
        else:
            base["price"] = old_price if old_price is not None else new_price
        base["quantity"] = old_quantity if old_quantity is not None else new_quantity
        old_matched = _maybe_decimal(base.get("size_matched"))
        new_matched = _maybe_decimal(incoming.get("size_matched"))
        matched = max(
            value for value in (old_matched, new_matched) if value is not None
        ) if old_matched is not None or new_matched is not None else None
        base["size_matched"] = matched
        old_average = _maybe_decimal(base.get("average_price"))
        new_average = _maybe_decimal(incoming.get("average_price"))
        if new_average is not None and (
            old_average is None
            or (matched is not None and old_matched is not None and matched > old_matched)
            or (matched is not None and old_matched is None)
        ):
            base["average_price"] = new_average
        else:
            base["average_price"] = old_average if old_average is not None else new_average
        for field in ("order_type", "expiration", "created_at"):
            if base.get(field) in (None, "") and incoming.get(field) not in (None, ""):
                base[field] = incoming.get(field)
        remaining = [value for value in (
            _maybe_decimal(base.get("remaining_size")),
            _maybe_decimal(incoming.get("remaining_size")),
        ) if value is not None]
        if base["quantity"] is not None and matched is not None:
            if matched > base["quantity"]:
                raise ValueError("order_fill_exceeds_quantity")
            remaining.append(base["quantity"] - matched)
        base["remaining_size"] = min(remaining) if remaining else None
        if base.get("queue_baseline") in (None, ""):
            base["queue_baseline"] = incoming.get("queue_baseline")
        base["order_id"] = incoming["order_id"]
        old_fills = base.get("fills", ())
        new_fills = incoming.get("fills", ())
        if isinstance(old_fills, (list, tuple)) and isinstance(new_fills, (list, tuple)):
            fills = {
                str(item.get("trade_id")): dict(item)
                for item in old_fills
                if isinstance(item, Mapping) and item.get("trade_id")
            }
            for item in new_fills:
                if not isinstance(item, Mapping) or not item.get("trade_id"):
                    continue
                key = str(item["trade_id"])
                existing = fills.get(key)
                if existing is not None and not cls._lp_fill_facts_equal(existing, item):
                    raise ValueError("trade_fill_conflict")
                fills[key] = dict(item)
            base["fills"] = list(fills.values())
        return base

    @staticmethod
    def _lp_fill_facts_equal(left: object, right: object) -> bool:
        """Compare persisted fill numbers without JSON Decimal/string drift."""

        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return left == right
        for key in set(left) | set(right):
            old = left.get(key)
            new = right.get(key)
            if key in {"quantity", "price", "notional", "size"}:
                old_value = _maybe_decimal(old)
                new_value = _maybe_decimal(new)
                if old_value != new_value:
                    return False
            elif old != new:
                return False
        return True

    def lp_register_exchange_orders(
        self,
        account_id: str,
        token_id: str,
        orders: object,
        *,
        session: Mapping[str, object] | None = None,
        connection: sqlite3.Connection | None = None,
        expected_generation: int | None = None,
        diagnostics: dict | None = None,
    ) -> dict[str, object]:
        """Atomically adopt exchange IDs into their canonical LP owners.

        Ownership is rechecked from the latest database value inside this
        transaction.  Known IDs return to their original sessions; only IDs
        absent from every account-owned session may create or join the active
        token group.  When ``connection`` is supplied the caller owns commit
        and rollback.
        """

        prepared_at = monotonic()
        diagnostics = {} if diagnostics is None else diagnostics
        by_id: dict[str, dict[str, object]] = {}
        for raw in (orders if isinstance(orders, (list, tuple)) else []):
            if not isinstance(raw, Mapping):
                continue
            record = self._lp_register_order_record(raw)
            previous = by_id.get(record["order_id"])
            by_id[record["order_id"]] = (
                self._lp_merge_order_record(previous, record)
                if previous is not None
                else record
            )
        records = list(by_id.values())
        diagnostics["registration_input_rows"] = diagnostics.get("registration_input_rows", 0) + len(records)
        if not records:
            raise ValueError("orders_empty")
        canonical_account = str(account_id or "").strip().casefold()
        canonical_token = str(token_id or "").strip()
        if not canonical_account or not canonical_token:
            raise ValueError("account_identity_unknown")
        if any(record["token_id"] != canonical_token for record in records):
            raise ValueError("order_identity_conflict")

        cm = self._transaction(prepared_at=prepared_at, diagnostics=diagnostics) if connection is None else nullcontext(connection)
        with cm as tx:
            if expected_generation is not None:
                generation = tx.execute(
                    "SELECT generation FROM lp_trade_generation WHERE singleton=1"
                ).fetchone()
                if generation is None or int(generation["generation"]) != int(expected_generation):
                    raise LpObservationWait("account_round_invalid")

            loaded = [
                (row, _load_payload(str(row["payload"])))
                for row in tx.execute(
                    "SELECT * FROM lp_sessions ORDER BY updated_at,session_id"
                ).fetchall()
            ]
            diagnostics["session_scan_calls"] = diagnostics.get("session_scan_calls", 0) + 1
            diagnostics["session_rows_scanned"] = diagnostics.get("session_rows_scanned", 0) + len(loaded)
            explicit_owners: dict[tuple[str, str], tuple[sqlite3.Row, dict[str, object]]] = {}
            legacy_owners: dict[str, tuple[sqlite3.Row, dict[str, object]]] = {}
            for row, payload in loaded:
                account = str(payload.get("account_id") or "").strip().casefold()
                if account:
                    for order_id in self._lp_owned_session_ids(payload):
                        previous = explicit_owners.get((account, order_id))
                        if previous is not None and previous[0]["session_id"] != row["session_id"]:
                            raise ValueError("order_identity_conflict")
                        explicit_owners[(account, order_id)] = (row, payload)
                else:
                    for order_id in self._lp_owned_session_ids(payload):
                        previous = legacy_owners.get(order_id)
                        if previous is not None and previous[0]["session_id"] != row["session_id"]:
                            raise ValueError("legacy_order_ambiguous")
                        legacy_owners[order_id] = (row, payload)

            routed: dict[str, list[dict[str, object]]] = {}
            routed_rows: dict[str, sqlite3.Row] = {}
            legacy_takeovers: set[str] = set()
            unknown: list[dict[str, object]] = []
            first_owner_id: str | None = None
            for record in records:
                owner = explicit_owners.get((canonical_account, record["order_id"]))
                if owner is None:
                    owner = legacy_owners.get(record["order_id"])
                    if owner is not None:
                        legacy_takeovers.add(str(owner[0]["session_id"]))
                if owner is None:
                    unknown.append(record)
                    continue
                owner_payload = owner[1]
                owner_token = str(owner_payload.get("token_id") or "").strip()
                if not owner_token:
                    raise ValueError("order_identity_unknown")
                if owner_token != record["token_id"]:
                    raise ValueError("order_identity_conflict")
                order_id = record["order_id"]
                roles = set()
                buy_ids = {
                    str(owner_payload.get("entry_order_id") or ""),
                    str(owner_payload.get("augment_order_id") or ""),
                    *(str(value) for value in _items(owner_payload.get("augment_order_ids"))),
                }
                if order_id in buy_ids:
                    roles.add("BUY")
                if order_id in {
                    str(owner_payload.get("passive_exit_order_id") or ""),
                    str(owner_payload.get("protected_exit_order_id") or ""),
                }:
                    roles.add("SELL")
                if roles and roles != {record["side"]}:
                    raise ValueError("order_identity_conflict")
                session_id = str(owner[0]["session_id"])
                first_owner_id = first_owner_id or session_id
                routed.setdefault(session_id, []).append(record)
                routed_rows[session_id] = owner[0]

            active_candidates = [
                item
                for item in loaded
                if str(item[1].get("token_id") or "").strip() == canonical_token
                and (
                    str(item[1].get("account_id") or "").strip().casefold() == canonical_account
                    or str(item[0]["session_id"]) in legacy_takeovers
                )
                and str(item[0]["state"]) not in {"complete", "entry_rejected"}
            ]
            if len(active_candidates) > 1:
                raise ValueError("lp_session_group_ambiguous")
            active = active_candidates[-1] if active_candidates else None
            active_id = str(active[0]["session_id"]) if active is not None else None
            candidate_id = str((session or {}).get("session_id") or "")
            candidate_item = next(
                (item for item in loaded if str(item[0]["session_id"]) == candidate_id),
                None,
            )
            if candidate_item is not None:
                candidate_payload = candidate_item[1]
                candidate_account = str(candidate_payload.get("account_id") or "").strip().casefold()
                if (
                    str(candidate_payload.get("token_id") or "") != canonical_token
                    or (
                        candidate_account
                        and candidate_account != canonical_account
                    )
                ):
                    raise ValueError("account_order_conflict")
                candidate_is_active = str(candidate_item[0]["state"]) not in {
                    "complete", "entry_rejected"
                }
                if unknown and candidate_is_active:
                    active_id = candidate_id
                    active = candidate_item
                elif unknown and active_id is None and expected_generation is None:
                    from .polymarket_lp_accounting import reservation_is_released
                    retired = candidate_payload.get("account_coverage_retired") or candidate_payload.get("manual_release_retired")
                    covered_candidate = {**candidate_payload, "session_id": candidate_id}
                    if (str(candidate_item[0]["state"]) == "complete"
                            and reservation_is_released(covered_candidate, canonical_account)
                            and isinstance(retired, Mapping)
                            and retired.get("reason") in {"account_observation_no_exposure", "operator_waived_provisional_hold"}
                            and not self._lp_owned_session_ids(candidate_payload)):
                        # A late explicit exchange receipt may arrive after a
                        # covered empty container retired. Reuse that container
                        # only when no newer active token owner exists; keep
                        # the original audit and one-way coverage intact.
                        candidate_payload["account_id"] = canonical_account
                        candidate_payload["resume_state"] = "entry_open"
                        candidate_payload["account_coverage_reactivated"] = dict(
                            reason="late_exchange_receipt", order_ids=sorted(record["order_id"] for record in unknown),
                            observed_at=_utc_now())
                        tx.execute("UPDATE lp_sessions SET state='entry_open' WHERE session_id=?", (candidate_id,))
                        active_id = candidate_id
                        active = candidate_item
            if unknown and active_id is not None:
                routed.setdefault(active_id, []).extend(unknown)
                routed_rows[active_id] = active[0]
            elif unknown and routed:
                # A completed historical owner must never absorb a new
                # lifecycle.  New IDs require the caller to supply a candidate
                # when this account/token has no active group.
                if session is None:
                    raise ValueError("lp_session_candidate_required")

            updated: list[dict[str, object]] = []
            changed_any = False
            fallback_owner_id = next(iter(routed), None)
            for session_id, session_records in routed.items():
                row = routed_rows[session_id]
                payload = next(item[1] for item in loaded if str(item[0]["session_id"]) == session_id)
                changed = self._lp_merge_registered_orders(payload, session_records)
                if session_id in legacy_takeovers and str(payload.get("account_id") or "").casefold() != canonical_account:
                    payload["account_id"] = canonical_account
                    changed = True
                if not changed:
                    continue
                changed_any = True
                # Account-sync observations are not fresh-enough financial
                # facts: they only establish identity, so previously verified
                # economics must wait for reconciliation. A direct receipt
                # follows a durable accepted action; its existing trade fence
                # already blocks spend/resend, so identity merge preserves the
                # prior as-of financial flags instead of fabricating new ones.
                if (
                    expected_generation is not None
                    and payload.get("position_reconciled") is True
                ):
                    payload["position_reconciled"] = False
                    payload["facts_error"] = "trade_change_pending"
                payload["_lp_trade_revision"] = int(payload.get("_lp_trade_revision", 0) or 0) + 1
                payload["_lp_revision"] = self._lp_payload_revision(payload) + 1
                tx.execute(
                    "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                    (_dump_execution_payload(payload), _utc_now(), session_id),
                )
                refreshed = tx.execute(
                    "SELECT * FROM lp_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                assert refreshed is not None
                updated.append(self._lp_row_result(refreshed))

            episode_id = str((session or {}).get("_first_seen_episode_id") or "")
            episode_target_id = active_id or next(iter(routed), None)
            if episode_id and episode_target_id:
                episode_row = tx.execute(
                    "SELECT * FROM lp_first_seen_episodes WHERE episode_id=?",
                    (episode_id,),
                ).fetchone()
                if episode_row is not None and episode_row["state"] in {
                    "monitoring", "canceling", "blocked"
                }:
                    target_item = next(
                        (
                            item for item in loaded
                            if str(item[0]["session_id"]) == episode_target_id
                        ),
                        None,
                    )
                    if target_item is None:
                        raise ValueError("lp_first_seen_episode_not_found")
                    target_payload = target_item[1]
                    episode_payload = _load_payload(str(episode_row["payload"]))
                    if self._lp_merge_first_seen_protection(target_payload, episode_payload):
                        target_payload["_lp_trade_revision"] = int(
                            target_payload.get("_lp_trade_revision", 0) or 0
                        ) + 1
                        target_payload["_lp_revision"] = self._lp_payload_revision(
                            target_payload
                        ) + 1
                        tx.execute(
                            "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                            (
                                _dump_execution_payload(target_payload),
                                _utc_now(),
                                episode_target_id,
                            ),
                        )
                        refreshed = tx.execute(
                            "SELECT * FROM lp_sessions WHERE session_id=?",
                            (episode_target_id,),
                        ).fetchone()
                        assert refreshed is not None
                        updated[:] = [
                            item for item in updated
                            if item.get("session_id") != episode_target_id
                        ]
                        updated.append(self._lp_row_result(refreshed))
                        changed_any = True
                    converted = tx.execute(
                        "UPDATE lp_first_seen_episodes "
                        "SET state='converted',payload=json_set(payload,'$.converted_reason','exchange_registration'),updated_at=? "
                        "WHERE episode_id=? AND state IN ('monitoring','canceling','blocked')",
                        (_utc_now(), episode_id),
                    )
                    if converted.rowcount != 1:
                        raise ValueError("lp_first_seen_episode_not_found")

            created_session = None
            if unknown and active_id is None:
                prepared = dict(session or {})
                if not prepared:
                    raise ValueError("lp_session_candidate_required")
                episode_id = str(prepared.pop("_first_seen_episode_id", "") or "")
                prepared["account_id"] = canonical_account
                prepared["token_id"] = canonical_token
                unknown_ids = {record["order_id"] for record in unknown}
                candidate_history = (
                    prepared.get("order_history")
                    if isinstance(prepared.get("order_history"), Mapping)
                    else {}
                )
                prepared["order_history"] = {
                    order_id: record
                    for order_id, record in candidate_history.items()
                    if str(order_id) in unknown_ids
                }
                prepared["owned_order_ids"] = [
                    order_id
                    for order_id in prepared.get("owned_order_ids", ())
                    if str(order_id) in unknown_ids
                ]
                if prepared.get("entry_order_id") not in unknown_ids:
                    prepared["entry_order_id"] = None
                if prepared.get("passive_exit_order_id") not in unknown_ids:
                    prepared["passive_exit_order_id"] = None
                prepared["augment_order_ids"] = []
                self._lp_merge_registered_orders(prepared, unknown)
                session_id = str(prepared.get("session_id") or uuid.uuid4().hex)
                prepared["session_id"] = session_id
                prepared.pop("_lp_revision", None)
                prepared.pop("_lp_trade_revision", None)
                state = str(prepared.pop("state", "entry_open") or "entry_open")
                idempotency_key = str(
                    prepared.pop("idempotency_key", f"lp-sync:{session_id}")
                    or f"lp-sync:{session_id}"
                )
                try:
                    tx.execute(
                        "INSERT INTO lp_sessions(session_id,idempotency_key,state,payload,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            session_id,
                            idempotency_key,
                            state,
                            _dump_execution_payload({**prepared, "_lp_revision": 0}),
                            _utc_now(),
                            _utc_now(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    if "one_active_lp_session_account_token" in str(exc):
                        raise ValueError("lp_session_market_active") from exc
                    raise
                if episode_id:
                    converted = tx.execute(
                        "UPDATE lp_first_seen_episodes "
                        "SET state='converted',payload=json_set(payload,'$.converted_reason','exchange_registration'),updated_at=? "
                        "WHERE episode_id=? AND state NOT IN ('converted','cancelled')",
                        (_utc_now(), episode_id),
                    )
                    if converted.rowcount != 1:
                        raise ValueError("lp_first_seen_episode_not_found")
                result = tx.execute(
                    "SELECT * FROM lp_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                assert result is not None
                created_session = self._lp_row_result(result)

            primary = created_session
            if primary is None and not unknown and first_owner_id is not None:
                primary = next(
                    (item for item in updated if item.get("session_id") == first_owner_id),
                    None,
                )
                if primary is None:
                    row = tx.execute(
                        "SELECT * FROM lp_sessions WHERE session_id=?", (first_owner_id,)
                    ).fetchone()
                    primary = None if row is None else self._lp_row_result(row)
            if primary is None and unknown and active_id is None and fallback_owner_id is not None:
                primary = next(
                    (item for item in updated if item.get("session_id") == fallback_owner_id),
                    None,
                )
                if primary is None:
                    row = tx.execute(
                        "SELECT * FROM lp_sessions WHERE session_id=?", (fallback_owner_id,)
                    ).fetchone()
                    primary = None if row is None else self._lp_row_result(row)
            if primary is None and active_id is not None:
                primary = next(
                    (item for item in updated if item.get("session_id") == active_id),
                    None,
                )
                if primary is None:
                    row = tx.execute(
                        "SELECT * FROM lp_sessions WHERE session_id=?", (active_id,)
                    ).fetchone()
                    primary = None if row is None else self._lp_row_result(row)
            elif primary is None and updated:
                primary = updated[-1]
            if primary is None:
                raise ValueError("lp_session_not_found")
        return {
            "session": primary,
            "updated_sessions": updated,
            "created": created_session is not None,
            "changed": changed_any or created_session is not None,
        }

    @staticmethod
    def _lp_merge_first_seen_protection(
        payload: dict[str, object], episode_payload: Mapping[str, object]
    ) -> bool:
        """Fill only missing/UNKNOWN buckets from a legacy first-seen anchor."""

        price = _maybe_decimal(episode_payload.get("baseline_price"))
        if price is None:
            return False
        anchors = episode_payload.get("anchor_order_ids")
        anchor = (
            episode_payload.get("order_id")
            or (str(anchors[0]) if isinstance(anchors, (list, tuple)) and anchors else "")
            or payload.get("entry_order_id")
            or ""
        )
        bucket = dict(episode_payload)
        for group_field in ("version", "levels", "data_failures"):
            bucket.pop(group_field, None)
        bucket["order_id"] = str(anchor)
        key = format(price, "f")
        current = payload.get("queue_protection")
        current_levels = current.get("levels") if isinstance(current, Mapping) else None
        if isinstance(current_levels, Mapping):
            merged = dict(current_levels)
            existing = merged.get(key)
            if isinstance(existing, Mapping) and str(existing.get("state") or "") not in {
                "", "unknown"
            }:
                return False
            merged[key] = bucket
            payload["queue_protection"] = {
                **(current if isinstance(current, Mapping) else {}),
                "version": 2,
                "levels": merged,
            }
            return True
        if isinstance(current, Mapping) and str(current.get("state") or "") not in {
            "", "unknown"
        }:
            return False
        payload["queue_protection"] = {
            "version": 2,
            "data_failures": _queue_group_failures_int(
                current.get("data_failures") if isinstance(current, Mapping) else None
            ),
            "levels": {key: bucket},
        }
        return True

    @classmethod
    def _lp_merge_registered_orders(
        cls, payload: dict[str, object], records: list[dict[str, object]]
    ) -> bool:
        """Merge exact-ID facts into the latest payload and derive group roles."""

        changed = False
        old_ids = cls._lp_owned_session_ids(payload)
        def amount(record: Mapping[str, object], field: str) -> Decimal:
            return _maybe_decimal(record.get(field)) or Decimal("0")

        history = dict(
            payload.get("order_history")
            if isinstance(payload.get("order_history"), Mapping)
            else {}
        )
        for record in records:
            record["token_id"] = record["token_id"] or str(payload.get("token_id") or "")
            existing = history.get(record["order_id"])
            history[record["order_id"]] = cls._lp_merge_order_record(
                existing, record
            )
            if cls._lp_order_record_changed(existing, history[record["order_id"]]):
                changed = True
            if record["order_id"] not in old_ids:
                payload.setdefault("owned_order_ids", []).append(record["order_id"])
                changed = True
        payload["order_history"] = history
        buys = {
            order_id: record
            for order_id, record in history.items()
            if str(record.get("side") or "").upper() == "BUY"
        }
        sells = {
            order_id: record
            for order_id, record in history.items()
            if str(record.get("side") or "").upper() == "SELL"
        }
        entry = str(payload.get("entry_order_id") or "")
        if entry not in buys:
            entry = next(iter(buys), "")
        if entry and payload.get("entry_order_id") != entry:
            payload["entry_order_id"] = entry
            changed = True
        passive = str(payload.get("passive_exit_order_id") or "")
        if passive not in sells:
            passive = next(iter(sells), "")
        if passive and payload.get("passive_exit_order_id") != passive:
            payload["passive_exit_order_id"] = passive
            changed = True
        augment = [order_id for order_id in buys if order_id != entry]
        if payload.get("augment_order_ids") != augment:
            payload["augment_order_ids"] = augment
            changed = True
        protection = payload.get("queue_protection")
        if isinstance(protection, Mapping):
            levels = protection.get("levels")
            try:
                baseline_price = Decimal(str(protection.get("baseline_price")))
            except (InvalidOperation, ValueError, TypeError):
                baseline_price = None
            if not isinstance(levels, Mapping) and baseline_price is not None:
                price = baseline_price
                bucket = dict(protection)
                for group_field in ("version", "levels", "data_failures"):
                    bucket.pop(group_field, None)
                anchors = _items(protection.get("anchor_order_ids"))
                anchor = (
                    protection.get("order_id")
                    or (str(anchors[0]) if anchors else "")
                    or payload.get("entry_order_id")
                    or ""
                )
                bucket.setdefault("order_id", str(anchor or ""))
                levels = {
                    format(price, "f") if price is not None else "unknown": bucket
                }
                protection = {
                    **protection,
                    "version": 2,
                    "levels": levels,
                }
            if isinstance(levels, Mapping):
                merged_levels = {
                    str(key): dict(value) if isinstance(value, Mapping) else value
                    for key, value in levels.items()
                }
                for order_id, record in buys.items():
                    if (
                        str(record.get("status") or "").upper() in TERMINAL_ORDER_STATES
                        or amount(record, "remaining_size") <= 0
                    ):
                        continue
                    price = _maybe_decimal(record.get("price"))
                    key = format(price, "f") if price is not None else "unknown"
                    baseline = record.get("queue_baseline")
                    if not (
                        isinstance(baseline, Mapping)
                        and baseline.get("state") == "known"
                    ):
                        if key in merged_levels and isinstance(
                            merged_levels[key], Mapping
                        ):
                            existing_bucket = merged_levels[key]
                            if str(existing_bucket.get("state") or "") != "unknown":
                                continue
                        continue
                    if key in merged_levels and isinstance(merged_levels[key], Mapping):
                        existing_bucket = merged_levels[key]
                        if str(existing_bucket.get("state") or "") != "unknown":
                            continue
                        # A fresh first observation can complete an explicitly
                        # UNKNOWN bucket. Terminal, canceling, notification,
                        # and baseline evidence never enter this branch.
                        merged_levels[key] = {
                            **existing_bucket,
                            **baseline,
                            "order_id": str(
                                existing_bucket.get("order_id") or order_id
                            ),
                            "baseline_source": "first_observation",
                            "baseline_version": 1,
                            "threshold": LP_QUEUE_PROTECTION_THRESHOLD,
                            "state": "registered",
                            "notification_sent": existing_bucket.get(
                                "notification_sent", False
                            ),
                            "blocked_notified": existing_bucket.get(
                                "blocked_notified", False
                            ),
                            "cancel_scope": existing_bucket.get(
                                "cancel_scope", "own_buys_at_level"
                            ),
                        }
                        changed = True
                        continue
                    bucket = (
                        dict(baseline)
                        if isinstance(baseline, Mapping) and baseline.get("state") == "known"
                        else {
                            "state": "unknown",
                            "baseline_price": price,
                            "baseline_source": "account_sync",
                            "reason_codes": ["baseline_unknown"],
                        }
                    )
                    own_remaining = sum(
                        amount(level_buy, "remaining_size")
                        for level_order_id, level_buy in buys.items()
                        if level_order_id == order_id or _maybe_decimal(level_buy.get("price")) == price
                    )
                    merged_levels[key] = {
                        **bucket,
                        "order_id": order_id,
                        "own_remaining": own_remaining,
                        "baseline_version": 1,
                        "threshold": LP_QUEUE_PROTECTION_THRESHOLD,
                        "data_failures": 0,
                        "notification_sent": False,
                        "blocked_notified": False,
                        "cancel_scope": "own_buys_at_level",
                        "cancel_targets": [],
                        "canceled_order_ids": [],
                        "cancel_target_remaining": {},
                        "canceled_remaining": None,
                        "partially_filled_quantity": None,
                        "reason_codes": [],
                    }
                    changed = True
                payload["queue_protection"] = {**protection, "levels": merged_levels}

        group_quantity = sum(
            amount(record, "quantity") or amount(record, "size_matched")
            for record in buys.values()
        )
        if group_quantity > 0:
            payload["quantity"] = group_quantity
            payload["group_buy_quantity"] = group_quantity
        payload["orders_terminal"] = all(
            str(record.get("status") or "").upper() in TERMINAL_ORDER_STATES
            for record in history.values()
        )
        return changed

    @classmethod
    def _lp_order_record_changed(cls, left: object, right: object) -> bool:
        """Return whether a merge added or semantically changed durable facts.

        Fields present only in the older durable record are preserved and do
        not make every later sparse replay look like a new revision.
        """

        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return left != right
        if any(right.get(key) not in (None, "", (), []) for key in set(right) - set(left)):
            return True
        for key in set(left) & set(right):
            old = left.get(key)
            new = right.get(key)
            if key == "fills":
                old_items = old if isinstance(old, (list, tuple)) else ()
                new_items = new if isinstance(new, (list, tuple)) else ()
                if len(old_items) != len(new_items):
                    return True
                if any(
                    not cls._lp_fill_facts_equal(old_item, new_item)
                    for old_item, new_item in zip(old_items, new_items)
                ):
                    return True
            elif key in {"price", "quantity", "size_matched", "average_price", "remaining_size"}:
                if _maybe_decimal(old) != _maybe_decimal(new):
                    return True
            elif str(old or "") != str(new or ""):
                return True
        return False

    @staticmethod
    def _merge_lp_protection(
        current: object, incoming: object
    ) -> object:
        """Merge one queue-protection write without losing sibling buckets."""

        if not isinstance(current, Mapping) or not isinstance(incoming, Mapping):
            return incoming
        current_levels = current.get("levels")
        incoming_levels = incoming.get("levels")
        if not isinstance(current_levels, Mapping) or not isinstance(incoming_levels, Mapping):
            return incoming
        merged = dict(current)
        merged.update(
            {
                key: value
                for key, value in incoming.items()
                if key != "levels"
            }
        )
        state_rank = {
            "registered": 0,
            "monitoring": 0,
            "unknown": 0,
            "triggered": 1,
            "canceling": 2,
            "canceled": 3,
            "partially_filled": 3,
        }
        merged_levels: dict[str, object] = {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in current_levels.items()
        }
        for raw_key, raw_value in incoming_levels.items():
            key = str(raw_key)
            if not isinstance(raw_value, Mapping):
                merged_levels[key] = raw_value
                continue
            existing = merged_levels.get(key)
            if not isinstance(existing, Mapping):
                merged_levels[key] = dict(raw_value)
                continue
            existing_state = str(existing.get("state") or "")
            incoming_state = str(raw_value.get("state") or "")
            existing_rank = state_rank.get(existing_state, 0)
            incoming_rank = state_rank.get(incoming_state, 0)
            cancellation_states = {
                "canceling",
                "canceled",
                "partially_filled",
            }
            same_cancel_episode = (
                existing_state in cancellation_states
                and incoming_state in cancellation_states
            )
            bucket = dict(existing)
            bucket.update(raw_value)
            # A stale submit/monitoring write cannot reopen a cancel episode
            # or clear its durable receipt fields.  A newer retry/convergence
            # state still replaces the bucket normally.
            if incoming_rank < existing_rank:
                bucket = dict(existing)
            elif incoming_rank == existing_rank and existing_state in {
                "canceling",
                "canceled",
                "partially_filled",
            } and incoming_state not in {
                "canceling",
                "canceled",
                "partially_filled",
            }:
                bucket = dict(existing)
            for list_key in ("cancel_targets", "canceled_order_ids"):
                old_items = existing.get(list_key)
                new_items = raw_value.get(list_key)
                if isinstance(old_items, (list, tuple)) or isinstance(new_items, (list, tuple)):
                    bucket[list_key] = list(
                        dict.fromkeys(
                            [str(item) for item in (old_items or []) if str(item)]
                            + [str(item) for item in (new_items or []) if str(item)]
                        )
                    )
            if same_cancel_episode:
                old_failed = existing.get("cancel_failed")
                new_failed = raw_value.get("cancel_failed")
                if isinstance(old_failed, (list, tuple)) or isinstance(
                    new_failed, (list, tuple)
                ):
                    failed = list(
                        dict.fromkeys(
                            [str(item) for item in (old_failed or []) if str(item)]
                            + [str(item) for item in (new_failed or []) if str(item)]
                        )
                    )
                    canceled = {
                        str(item)
                        for item in bucket.get("canceled_order_ids", [])
                        if str(item)
                    }
                    bucket["cancel_failed"] = [
                        order_id for order_id in failed if order_id not in canceled
                    ]

                # Cancellation evidence and its one-shot notice are
                # monotonic: a stale same-episode image cannot undo either.
                bucket["notification_sent"] = bool(
                    existing.get("notification_sent")
                ) or bool(raw_value.get("notification_sent"))
                if "blocked_notified" in existing or "blocked_notified" in raw_value:
                    bucket["blocked_notified"] = bool(
                        existing.get("blocked_notified")
                    ) or bool(raw_value.get("blocked_notified"))

                # Preserve the first durable timing/evidence value while
                # allowing a newer retry to add evidence for another target.
                for mapping_key in (
                    "cancel_target_remaining",
                    "order_placement_times",
                    "order_cancel_confirmed_at",
                ):
                    old_mapping = existing.get(mapping_key)
                    new_mapping = raw_value.get(mapping_key)
                    if not isinstance(old_mapping, Mapping) and not isinstance(
                        new_mapping, Mapping
                    ):
                        continue
                    merged_mapping = (
                        dict(old_mapping) if isinstance(old_mapping, Mapping) else {}
                    )
                    if isinstance(new_mapping, Mapping):
                        for map_key, map_value in new_mapping.items():
                            if map_key not in merged_mapping:
                                merged_mapping[map_key] = map_value
                            elif (
                                mapping_key != "cancel_target_remaining"
                                and merged_mapping[map_key] is None
                                and map_value is not None
                            ):
                                merged_mapping[map_key] = map_value
                    bucket[mapping_key] = merged_mapping

                # Keep episode request metadata from regressing to an older
                # image.  The canceled total is monotonic across one episode
                # because it is the sum of the request-time target amounts.
                if "cancel_requested_at" in existing:
                    bucket["cancel_requested_at"] = existing.get(
                        "cancel_requested_at"
                    )
                incoming_failed = raw_value.get("cancel_failed")
                canceled_ids = bucket.get("canceled_order_ids")
                canceled_set = (
                    {
                        str(item)
                        for item in canceled_ids
                        if str(item)
                    }
                    if isinstance(canceled_ids, (list, tuple))
                    else set()
                )
                incoming_failed_unsettled = (
                    {
                        str(item)
                        for item in incoming_failed
                        if str(item) and str(item) not in canceled_set
                    }
                    if isinstance(incoming_failed, (list, tuple))
                    else set()
                )
                if incoming_failed_unsettled and raw_value.get("cancel_failure"):
                    bucket["cancel_failure"] = raw_value.get("cancel_failure")
                elif "cancel_failure" in existing:
                    bucket["cancel_failure"] = existing.get("cancel_failure")
                if "canceled_remaining" in existing:
                    old_remaining = existing.get("canceled_remaining")
                    new_remaining = raw_value.get("canceled_remaining")
                    existing_canceled_ids = {
                        str(item)
                        for item in existing.get("canceled_order_ids", [])
                        if str(item)
                    }
                    incoming_canceled_ids = {
                        str(item)
                        for item in raw_value.get("canceled_order_ids", [])
                        if str(item)
                    }
                    newly_canceled_ids = incoming_canceled_ids - existing_canceled_ids
                    merged_remaining = bucket.get("cancel_target_remaining")
                    new_canceled_remaining_unknown = (
                        incoming_rank == existing_rank
                        and bool(newly_canceled_ids)
                        and (
                            not isinstance(merged_remaining, Mapping)
                            or any(
                                order_id not in merged_remaining
                                or merged_remaining.get(order_id) is None
                                for order_id in newly_canceled_ids
                            )
                        )
                    )
                    if new_canceled_remaining_unknown:
                        bucket["canceled_remaining"] = None
                    elif incoming_rank <= existing_rank:
                        try:
                            old_amount = (
                                None
                                if old_remaining is None
                                else Decimal(str(old_remaining))
                            )
                            new_amount = (
                                None
                                if new_remaining is None
                                else Decimal(str(new_remaining))
                            )
                        except (InvalidOperation, TypeError, ValueError):
                            old_amount = None
                            new_amount = None
                        if old_amount is None or new_amount is None:
                            bucket["canceled_remaining"] = old_remaining
                        elif new_amount > old_amount:
                            bucket["canceled_remaining"] = new_remaining
                        else:
                            bucket["canceled_remaining"] = old_remaining
            merged_levels[key] = bucket
        merged["levels"] = merged_levels
        return merged

    def lp_merge_queue_protection(
        self,
        session_id: str,
        *,
        queue_protection: Mapping[str, object],
        patch: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Atomically merge a protection write with concurrent session state."""

        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError("lp_session_not_found")
            payload = _load_payload(str(row["payload"]))
            if patch:
                for key, value in patch.items():
                    if key == "augment_cancel_requested":
                        current_values = payload.get(key)
                        merged_values = (
                            [str(item) for item in current_values if str(item)]
                            if isinstance(current_values, (list, tuple))
                            else []
                        )
                        for item in value if isinstance(value, (list, tuple)) else []:
                            item_text = str(item)
                            if item_text and item_text not in merged_values:
                                merged_values.append(item_text)
                        payload[key] = sorted(merged_values)
                    elif key == "entry_cancel_requested":
                        payload[key] = bool(payload.get(key)) or bool(value)
                    else:
                        payload[key] = value
            payload["queue_protection"] = self._merge_lp_protection(
                payload.get("queue_protection"), queue_protection
            )
            try:
                revision = int(payload.get("_lp_revision", 0))
            except (TypeError, ValueError):
                revision = 0
            payload["_lp_revision"] = max(revision, 0) + 1
            connection.execute(
                "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                (_dump_execution_payload(payload), now, str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            assert updated is not None
            return self._lp_row_result(updated)

    def lp_merge_queue_protection_bucket(
        self,
        session_id: str,
        *,
        level_key: str,
        bucket: Mapping[str, object],
        patch: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Atomically merge one protection bucket and session cancel flags.

        The caller may have evaluated the bucket against an older session
        image.  The transaction preserves every current sibling bucket and
        applies the state-aware merge only to ``level_key``.
        """

        now = _utc_now()
        key = str(level_key)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError("lp_session_not_found")
            payload = _load_payload(str(row["payload"]))
            if patch:
                for patch_key, value in patch.items():
                    if patch_key == "augment_cancel_requested":
                        current_values = payload.get(patch_key)
                        merged_values = (
                            [str(item) for item in current_values if str(item)]
                            if isinstance(current_values, (list, tuple))
                            else []
                        )
                        for item in value if isinstance(value, (list, tuple)) else []:
                            item_text = str(item)
                            if item_text and item_text not in merged_values:
                                merged_values.append(item_text)
                        payload[patch_key] = sorted(merged_values)
                    elif patch_key == "entry_cancel_requested":
                        payload[patch_key] = bool(payload.get(patch_key)) or bool(value)
                    else:
                        payload[patch_key] = value

            current = payload.get("queue_protection")
            if isinstance(current, Mapping):
                current_levels = current.get("levels")
            else:
                current_levels = None
            if isinstance(current, Mapping) and isinstance(current_levels, Mapping):
                incoming = {"levels": {key: dict(bucket)}}
                merged_protection = self._merge_lp_protection(current, incoming)
            else:
                merged_protection = dict(current) if isinstance(current, Mapping) else {}
                levels = dict(
                    current_levels if isinstance(current_levels, Mapping) else {}
                )
                levels[key] = dict(bucket)
                merged_protection["version"] = 2
                merged_protection["levels"] = levels
            payload["queue_protection"] = merged_protection
            revision = self._lp_payload_revision(payload)
            payload["_lp_revision"] = revision + 1
            connection.execute(
                "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                (_dump_execution_payload(payload), now, str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            assert updated is not None
            return self._lp_row_result(updated)

    def lp_mark_queue_protection_notified(
        self,
        session_id: str,
        *,
        level_key: str,
        field: str,
        episode: str,
        order_id: str,
    ) -> dict[str, object]:
        """Mark exactly the still-present bucket that armed this episode."""
        now = _utc_now()
        key = str(level_key)
        field = str(field)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError("lp_session_not_found")
            payload = _load_payload(str(row["payload"]))
            current = payload.get("queue_protection")
            levels = current.get("levels") if isinstance(current, Mapping) else None
            if not isinstance(current, Mapping) or not isinstance(levels, Mapping):
                return self._lp_row_result(row)
            bucket = levels.get(key)
            if not isinstance(bucket, Mapping):
                return self._lp_row_result(row)
            if (
                bucket.get(field) is True
                or str(bucket.get("notification_episode") or "") != str(episode)
                or str(bucket.get("order_id") or "") != str(order_id)
            ):
                return self._lp_row_result(row)
            merged_levels = dict(levels)
            merged_bucket = dict(bucket)
            merged_bucket[field] = True
            merged_levels[key] = merged_bucket
            payload["queue_protection"] = {**current, "levels": merged_levels}
            payload["_lp_revision"] = self._lp_payload_revision(payload) + 1
            connection.execute(
                "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                (_dump_execution_payload(payload), now, str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            assert updated is not None
            return self._lp_row_result(updated)

    def lp_arm_queue_protection_notification(
        self,
        session_id: str,
        *,
        level_key: str,
        episode: str,
        order_id: str,
    ) -> bool:
        """Persist a bucket notice identity before any network send."""
        now = _utc_now()
        key = str(level_key)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                return False
            payload = _load_payload(str(row["payload"]))
            current = payload.get("queue_protection")
            levels = current.get("levels") if isinstance(current, Mapping) else None
            if isinstance(current, Mapping) and "levels" not in current:
                # Existing single-price sessions are normalized on their first
                # write; an explicitly empty/deleted levels map stays empty.
                if str(current.get("baseline_price")) != key:
                    return False
                legacy = dict(current)
                legacy.setdefault("order_id", payload.get("entry_order_id"))
                levels = {key: legacy}
            if not isinstance(current, Mapping) or not isinstance(levels, Mapping):
                return False
            bucket = levels.get(key)
            if not isinstance(bucket, Mapping):
                return False
            if bucket.get("notification_sent") is True:
                return True
            if (
                str(bucket.get("notification_episode") or "") not in {"", str(episode)}
                or str(bucket.get("order_id") or "") != str(order_id)
            ):
                return False
            merged_levels = dict(levels)
            merged_bucket = dict(bucket)
            merged_bucket["notification_episode"] = str(episode)
            merged_levels[key] = merged_bucket
            payload["queue_protection"] = {**current, "version": 2, "levels": merged_levels}
            payload["_lp_revision"] = self._lp_payload_revision(payload) + 1
            connection.execute(
                "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                (_dump_execution_payload(payload), now, str(session_id)),
            )
            return True

    @staticmethod
    def _lp_first_seen_row_result(row: sqlite3.Row) -> dict[str, object]:
        payload = _load_payload(str(row["payload"]))
        payload.update(
            {
                "episode_id": str(row["episode_id"]),
                "token_id": str(row["token_id"]),
                "condition_id": str(row["condition_id"]),
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        return payload

    def lp_create_first_seen_episode(
        self,
        episode_id: str,
        *,
        token_id: str,
        condition_id: str,
        state: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        encoded = _dump_execution_payload(payload)
        now = _utc_now()
        with self._transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO lp_first_seen_episodes(episode_id,token_id,condition_id,state,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        str(episode_id),
                        str(token_id),
                        str(condition_id),
                        str(state),
                        encoded,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "one_active_first_seen_episode" in str(exc):
                    raise ValueError("active_first_seen_episode") from exc
                raise
            row = connection.execute(
                "SELECT * FROM lp_first_seen_episodes WHERE episode_id=?",
                (str(episode_id),),
            ).fetchone()
            assert row is not None
            return self._lp_first_seen_row_result(row)

    def lp_active_first_seen_episodes(self) -> list[dict[str, object]]:
        """Return every first-seen episode in the active domain, per token."""

        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lp_first_seen_episodes
                WHERE state IN ('monitoring','canceling','blocked')
                ORDER BY created_at, episode_id
                """
            ).fetchall()
        return [self._lp_first_seen_row_result(row) for row in rows]

    def lp_first_seen_episode(self, episode_id: str) -> dict[str, object] | None:
        """Return one first-seen episode by id, terminal states included."""

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM lp_first_seen_episodes WHERE episode_id=?",
                (str(episode_id),),
            ).fetchone()
        return None if row is None else self._lp_first_seen_row_result(row)

    def lp_update_first_seen_episode(
        self,
        episode_id: str,
        *,
        state: str | None = None,
        patch: Mapping[str, object] | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, object]:
        now = _utc_now()
        with self._transaction() as connection:
            if expected_generation is not None:
                generation = connection.execute(
                    "SELECT generation FROM lp_trade_generation WHERE singleton=1"
                ).fetchone()
                if generation is None or int(generation["generation"]) != int(
                    expected_generation
                ):
                    raise LpObservationWait("account_round_invalid")
            row = connection.execute(
                "SELECT * FROM lp_first_seen_episodes WHERE episode_id=?",
                (str(episode_id),),
            ).fetchone()
            if row is None:
                raise ValueError("lp_first_seen_episode_not_found")
            payload = _load_payload(str(row["payload"]))
            if patch:
                payload.update(patch)
            next_state = str(state or row["state"])
            # The payload doubles as the projected UI summary, so the column
            # state and payload["state"] always travel together.
            payload["state"] = next_state
            connection.execute(
                "UPDATE lp_first_seen_episodes SET state=?,payload=?,updated_at=? WHERE episode_id=?",
                (
                    next_state,
                    _dump_execution_payload(payload),
                    now,
                    str(episode_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM lp_first_seen_episodes WHERE episode_id=?",
                (str(episode_id),),
            ).fetchone()
            assert updated is not None
            return self._lp_first_seen_row_result(updated)

    @staticmethod
    def _lp_register_trade_change(connection: sqlite3.Connection, session_id: str) -> None:
        connection.execute(
            "UPDATE lp_trade_generation SET generation=generation+1 WHERE singleton=1"
        )
        row = connection.execute(
            "SELECT payload FROM lp_sessions WHERE session_id=?", (str(session_id),)
        ).fetchone()
        if row is None:
            raise ValueError("lp_session_not_found")
        payload = _load_payload(str(row["payload"]))
        payload["_lp_trade_revision"] = int(payload.get("_lp_trade_revision", 0)) + 1
        payload["facts_error"] = payload.get("facts_error") or "trade_change_pending"
        pool = connection.execute("SELECT payload FROM lp_auto_pool WHERE singleton=1").fetchone()
        if pool:
            from .polymarket_lp_accounting import reservation_is_released
            document = _load_payload(str(pool[0]))
            changed = False
            for intent in document.get("intents", {}).values():
                if (intent.get("session_id") == session_id and intent.get("state") != "reserved"
                        and not reservation_is_released(intent, document.get("account_id"))):
                    intent.update(financial_status="unknown", reconcile_reason="trade_change_pending", settled=False)
                    changed = True
            if changed:
                connection.execute("UPDATE lp_auto_pool SET payload=? WHERE singleton=1",
                                   (_dump_execution_payload(document),))
        connection.execute(
            "UPDATE lp_sessions SET payload=? WHERE session_id=?",
            (_dump_execution_payload(payload), str(session_id)),
        )

    def lp_register_trade_change(self, session_id: str) -> None:
        """Fence in-flight facts before a durable trading intent is executed."""
        with self._transaction() as connection:
            self._lp_register_trade_change(connection, session_id)
        self._notify_lp_trade_change(session_id)

    def lp_advance_trade_generation(self, expected: int) -> bool:
        """Advance the shared fence only from the caller's observed generation."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT generation FROM lp_trade_generation WHERE singleton=1"
            ).fetchone()
            if row is None or int(row["generation"]) != int(expected):
                return False
            connection.execute(
                "UPDATE lp_trade_generation SET generation=generation+1 "
                "WHERE singleton=1 AND generation=?",
                (int(expected),),
            )
        return True

    def lp_trade_generation(self) -> int:
        """Authoritative durable fence shared by every LP account consumer."""

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT generation FROM lp_trade_generation WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("lp_trade_generation_unknown")
        return int(row["generation"])

    def set_lp_trade_change_listener(self, callback: Callable[[str], None] | None) -> None:
        """Receive one callback after each durable trade-generation fence."""

        with self._lp_trade_change_listener_lock:
            self._lp_trade_change_listener = callback

    def _notify_lp_trade_change(self, session_id: str) -> None:
        with self._lp_trade_change_listener_lock:
            listener = self._lp_trade_change_listener
        if listener is None:
            return
        listener(session_id)

    def lp_publish_account_financial_facts(
        self, facts: Mapping[str, object], *, connection: sqlite3.Connection | None = None,
        expected_generation: int | None = None,
        diagnostics: dict | None = None,
    ) -> dict[str, object]:
        """Publish one account snapshot and its ended-send coverage atomically.

        Callers registering exchange IDs pass the same transaction. Facts do
        not rewrite request outcomes, infer request/order identity, or advance
        the trade generation. The supplied generation is the current fence.
        """
        from .polymarket_lp_accounting import (
            account_cancel_is_pending, account_position_quantity,
            can_resume_covered_management, default_account_pool_document,
            ended_reservation_evidence, reservation_is_covered, reservation_is_released,
        )
        prepared_at = monotonic()
        diagnostics = {} if diagnostics is None else diagnostics
        value = _load_payload(_dump_execution_payload(facts))
        account = str(value.get("account_id") or "").strip().casefold()
        pool_account = hashlib.sha256(account.encode()).hexdigest()
        if not account or value.get("pool_account_id") != pool_account:
            raise ValueError("account_identity_mismatch")
        started = _parse_timestamp(value.get("read_started_at"))
        ended = _parse_timestamp(value.get("read_ended_at"))
        checked = _parse_timestamp(value.get("checked_at"))
        if not started <= checked <= ended or not value.get("snapshot_id"):
            raise ValueError("account_read_order_invalid")
        if value.get("financial_status") not in {"known", "unknown"}:
            raise ValueError("account_financial_facts_unknown")
        with (self._transaction(prepared_at=prepared_at, diagnostics=diagnostics) if connection is None else nullcontext(connection)) as tx:
            generation_row = tx.execute(
                "SELECT generation FROM lp_trade_generation WHERE singleton=1"
            ).fetchone()
            generation = int(generation_row["generation"]) if generation_row is not None else None
            expected = value.get("trade_generation") if expected_generation is None else expected_generation
            if type(expected) is not int or generation != expected:
                raise LpObservationWait("account_round_invalid")
            row = tx.execute("SELECT payload FROM lp_auto_pool WHERE singleton=1").fetchone()
            document = (_load_payload(str(row["payload"])) if row else
                default_account_pool_document(self.path, pool_account, checked))
            if document.get("account_id") != pool_account:
                raise ValueError("account_identity_mismatch")
            previous = document.get("account_financial_facts")
            if isinstance(previous, Mapping):
                if previous.get("account_id") != account:
                    raise ValueError("account_identity_mismatch")
                if any(_parse_timestamp(value.get(key)) < _parse_timestamp(previous.get(key))
                       for key in ("read_started_at", "read_ended_at", "checked_at")):
                    raise LpObservationWait("account_snapshot_superseded")
                # Reapply the same observation against current exact-ID
                # terminal receipts without refreshing any source timestamp.
                if (previous.get("snapshot_id") != value["snapshot_id"]
                        and previous.get("read_started_at") == value.get("read_started_at")
                        and previous.get("read_ended_at") == value.get("read_ended_at")):
                    raise LpObservationWait("account_snapshot_conflict")
            # A complete-list flag does not prove the API retained its full
            # historical window. Preserve reporting uncertainty when a known
            # fill disappears or changes, without overriding current exposure.
            frontier = {(item["trade_id"], item["order_id"]): item["fingerprint"]
                for item in document.get("account_confirmed_fill_facts", ())}
            current_confirmed = {(item["trade_id"], item["order_id"]): item["fingerprint"]
                for item in value.get("confirmed_fill_facts", ())}
            missing = set(frontier) - set(current_confirmed)
            conflicts = {key for key in set(frontier) & set(current_confirmed)
                         if frontier[key] != current_confirmed[key]}
            if missing or conflicts:
                value["report_status"] = "unknown"
                history_reasons = set(value.get("report_reason_codes") or ())
                if missing:
                    history_reasons.add("account_trade_history_incomplete")
                if conflicts:
                    history_reasons.add("account_trade_history_conflict")
                value["report_reason_codes"] = sorted(history_reasons)
                value["realized_pnl_usd"] = None
            for identity, fingerprint in current_confirmed.items():
                frontier.setdefault(identity, fingerprint)
            document["account_confirmed_fill_facts"] = [dict(trade_id=identity[0], order_id=identity[1],
                fingerprint=fingerprint) for identity, fingerprint in sorted(frontier.items())]
            loaded = [(row, _load_payload(str(row["payload"]))) for row in tx.execute(
                "SELECT * FROM lp_sessions ORDER BY session_id"
            ).fetchall()]
            diagnostics["session_scan_calls"] = diagnostics.get("session_scan_calls", 0) + 1
            diagnostics["session_rows_scanned"] = diagnostics.get("session_rows_scanned", 0) + len(loaded)
            diagnostics["publish_input_buys"] = len(value.get("buys", ()))
            sessions = {str(row["session_id"]): (row, payload) for row, payload in loaded}
            owners = {}
            for row, payload in loaded:
                if str(payload.get("account_id") or "").strip().casefold() != account:
                    continue
                for oid in self._lp_owned_session_ids(payload):
                    if oid in owners and owners[oid][0]["session_id"] != row["session_id"]:
                        raise ValueError("order_identity_conflict")
                    owners[oid] = (row, payload)
            # The raw trade window must also cover cumulative fills proven
            # by exact order receipts, including receipts whose execution fee
            # was not yet known and therefore never entered the fill frontier.
            # Generic receipt merges remain monotonic; this affects reporting,
            # while the current API positions still determine exposure.
            observed_fills = value.get("order_fills") or {}
            if any((_maybe_decimal((payload.get("order_history") or {}).get(oid, {}).get("size_matched")) or Decimal(0))
                   > (_maybe_decimal(observed_fills.get(oid)) or Decimal(0))
                   for oid, (_row, payload) in owners.items()):
                value["report_status"] = "unknown"
                value["report_reason_codes"] = sorted(set(value.get("report_reason_codes") or ()) | {"account_order_fill_history_incomplete"})
                value["realized_pnl_usd"] = None
            buys = [dict(buy) for buy in value.get("buys", ())]
            actions_by_session = {}

            def session_actions(sid):
                if sid not in actions_by_session:
                    diagnostics["actions_query_calls"] = diagnostics.get("actions_query_calls", 0) + 1
                    actions_by_session[sid] = self.lp_actions(sid, connection=tx)
                return actions_by_session[sid]
            for buy in buys:
                owner = owners.get(str(buy.get("order_id") or ""))
                if owner is None:
                    raise ValueError("account_order_owner_unknown")
                row, payload = owner
                sid = str(row["session_id"])
                buy["session_id"] = sid
                for key in ("condition_id", "market_id", "outcome"):
                    if not buy.get(key):
                        buy[key] = payload.get(key)
                actions = session_actions(sid)
                if account_cancel_is_pending(payload, actions, buy["order_id"]):
                    buy["state"] = "canceling"
            # Current complete open-order facts determine occupancy. Historical
            # cancel audit is retained, but never invents an absent BUY slot.
            value["buys"] = sorted(buys, key=lambda buy: buy["order_id"])
            value["trade_generation"] = generation
            intents = document.get("intents") or {}
            session_bindings = {}
            for intent in intents.values():
                session_bindings.setdefault(str(intent.get("session_id") or ""), []).append(intent)
            # A round begun during a separate BUY submission cannot price
            # its future exposure. Keep account admission blocked even when
            # this session's original entry reservation was already covered.
            for sid, (row, payload) in sessions.items():
                explicit = str(payload.get("account_id") or "").strip().casefold()
                bindings = session_bindings.get(sid, ())
                legacy_owned = (not explicit and len(bindings) == 1 and
                    row["idempotency_key"] == "lp-auto:" + str(bindings[0].get("intent_id") or ""))
                if explicit != account and not legacy_owned:
                    continue
                actions = session_actions(sid)
                for action in actions:
                    is_entry = action.get("role") == "entry" or str(action.get("action_key") or "").endswith("entry-submit")
                    if (action.get("side") != "BUY" or (is_entry and bindings)
                            or "cancel" in str(action.get("action_key") or "")
                            or action.get("state") not in {"pending", "unknown"}):
                        continue
                    end = action.get("submit_finished_at") or action.get("submit_receipt_at") or action.get("updated_at")
                    try:
                        ended_before = _parse_timestamp(end) < started
                    except ValueError:
                        ended_before = False
                    if (action.get("state") == "pending" or not ended_before
                            or action.get("submit_stage") in {"preparing", "sending"}):
                        value["financial_status"] = "unknown"
                        value["reason_codes"] = sorted(set(value.get("reason_codes") or ()) | {"account_send_inflight"})
            retained = []
            if value["financial_status"] == "known":
                for intent_id, intent in intents.items():
                    if reservation_is_released(intent, pool_account):
                        continue
                    sid = str(intent.get("session_id") or "")
                    entry = sessions.get(sid)
                    if entry is None or len(session_bindings[sid]) != 1:
                        retained.append(intent_id)
                        continue
                    row, payload = entry
                    session = {**payload, "session_id": sid, "idempotency_key": row["idempotency_key"]}
                    actions = session_actions(sid)
                    evidence = ended_reservation_evidence(intent, session, actions, account_id=account,
                        pool_account_id=pool_account, read_started_at=started)
                    if evidence is None:
                        retained.append(intent_id)
                        continue
                    marker = dict(version=1, state="covered", account_id=account, pool_account_id=pool_account,
                        snapshot_id=value["snapshot_id"], session_id=sid, intent_id=intent_id,
                        read_started_at=value["read_started_at"], read_ended_at=value["read_ended_at"],
                        checked_at=value["checked_at"], original_reserved_usd=intent.get("reserved_usd"), **evidence)
                    intent["reservation_coverage"] = marker
                    payload["reservation_coverage"] = marker
                    payload["_lp_revision"] = self._lp_payload_revision(payload) + 1
                    tx.execute("UPDATE lp_sessions SET payload=? WHERE session_id=?",
                        (_dump_execution_payload(payload), sid))
            else:
                retained = [key for key, intent in intents.items() if not reservation_is_released(intent, pool_account)]
            value["retained_reservation_ids"] = sorted(retained)
            if value["financial_status"] == "known":
                for intent in intents.values():
                    if not reservation_is_released(intent, pool_account):
                        continue
                    sid = str(intent.get("session_id") or "")
                    entry = sessions.get(sid)
                    if entry is None:
                        continue
                    row, payload = entry
                    actions = session_actions(sid)
                    managed = {**payload, "session_id": sid, "state": row["state"]}
                    if can_resume_covered_management(managed, actions, value):
                        payload["resume_state"] = "entry_open"
                        payload["account_coverage_management"] = dict(reason="account_observation_owned_exposure",
                            snapshot_id=value["snapshot_id"], checked_at=value["checked_at"])
                        payload["_lp_revision"] = self._lp_payload_revision(payload) + 1
                        tx.execute("UPDATE lp_sessions SET state='entry_open',payload=? WHERE session_id=?",
                            (_dump_execution_payload(payload), sid))
                        continue
                    token = str(payload.get("token_id") or "")
                    if (not token or self._lp_owned_session_ids(payload)
                            or token in value.get("open_order_tokens", ())
                            or account_position_quantity(value, token) != 0):
                        continue
                    actions = session_actions(sid)
                    if any(a.get("state") in {"pending", "unknown"}
                           and a.get("role") != "entry"
                           and not str(a.get("action_key") or "").endswith("entry-submit")
                           for a in actions):
                        continue
                    if row["state"] == "complete":
                        continue
                    # Retire only the empty management container. The original
                    # request/action remains UNKNOWN and no order is assigned.
                    payload["account_coverage_retired"] = dict(reason="account_observation_no_exposure",
                        snapshot_id=value["snapshot_id"], checked_at=value["checked_at"])
                    payload["_lp_revision"] = self._lp_payload_revision(payload) + 1
                    tx.execute("UPDATE lp_sessions SET state='complete',payload=? WHERE session_id=?",
                        (_dump_execution_payload(payload), sid))
            if value["financial_status"] == "known" and value.get("realized_pnl_usd") is not None:
                lifetime_pnl = _maybe_decimal(value.get("realized_pnl_usd"))
                if lifetime_pnl is None:
                    raise ValueError("account_realized_pnl_unknown")
                baseline = _maybe_decimal(document.get("account_realized_pnl_baseline_usd"))
                if baseline is None:
                    carried_pnl = Decimal(0)
                    if document.get("allocations") or document.get("ever_enabled"):
                        for intent in intents.values():
                            if not reservation_is_released(intent, pool_account):
                                continue
                            pnl = _maybe_decimal(intent.get("realized_pnl_usd"))
                            if pnl is not None and (pnl < 0 or reservation_is_covered(intent, pool_account) and intent.get("financial_status") == "known"):
                                carried_pnl += pnl
                    # The first observation must not turn historical account
                    # profits into new allocation. Preserve already-accounted
                    # automatic PnL when its request hold is replaced.
                    baseline = lifetime_pnl - carried_pnl
                    document["account_realized_pnl_baseline_usd"] = str(baseline)
                value["lifetime_realized_pnl_usd"] = str(lifetime_pnl)
                value["realized_pnl_usd"] = str(lifetime_pnl - baseline)
                value["realized_pnl_basis"] = "account_allocation_boundary"
            document["account_financial_facts"] = value
            tx.execute("INSERT INTO lp_auto_pool(singleton,payload) VALUES(1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
                (_dump_execution_payload(document),))
            return value

    def lp_publish_facts(self, session_id, revision, *, patch=None, state=None,
                         publish=None, error=None, resolved_cancels=(),
                         trade_generation=None):
        """Commit one fenced observation and its automatic ledger atomically.

        The publisher receives this connection; it must not open a nested
        write transaction or perform external I/O.
        """
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError("lp_session_not_found")
            payload = _load_payload(str(row["payload"]))
            if int(payload.get("_lp_trade_revision", 0)) != revision:
                raise LpObservationWait("session_changed")
            if trade_generation is not None:
                observed = connection.execute(
                    "SELECT generation FROM lp_trade_generation WHERE singleton=1"
                ).fetchone()
                if observed is None or int(observed["generation"]) != int(trade_generation):
                    raise LpObservationWait("account_round_invalid")
            for action_id in resolved_cancels:
                connection.execute("UPDATE lp_actions SET state='accepted',updated_at=? WHERE action_id=? AND session_id=? AND state IN ('pending','unknown')",
                                   (_utc_now(), action_id, str(session_id)))
            payload.update(patch or {})
            payload["_lp_revision"] = self._lp_payload_revision(payload) + 1
            connection.execute(
                "UPDATE lp_sessions SET state=?,payload=?,updated_at=? WHERE session_id=?",
                (self._lp_manual_retirement_state(connection, session_id, payload, state or row["state"]),
                 _dump_execution_payload(payload), _utc_now(), str(session_id)),
            )
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            session = self._lp_row_result(row)
            changed = publish(session, connection=connection, error=error) if publish else False
        return session, changed

    def lp_register_fenced_actions(
        self,
        session_id: str,
        actions: list[Mapping[str, object]],
        *,
        expected_generation: int | None = None,
        expected_trade_revision: int | None = None,
        patch: Mapping[str, object] | None = None,
        state: str | None = None,
        reject_existing: bool = False,
    ) -> list[dict[str, object]]:
        """Authorize trading actions and optionally patch their session atomically.

        A shared-bundle caller must pass its observed generation. Pass a
        captured session revision where that action's contract requires it;
        each supplied fence is checked. The checks, session patch, and every
        action insert share one SQLite transaction, so a lost race cannot leave
        one action authorized while another is rejected. ``reject_existing``
        is the claim boundary for result-unknown BUY/SELL intents; the default
        preserves the existing exact-ID retry/upsert contract.
        """

        if not actions:
            return []
        encoded: list[tuple[str, str]] = []
        seen_keys: set[str] = set()
        for action in actions:
            action_key = str(action.get("action_key") or "")
            if not action_key or action_key in seen_keys:
                raise ValueError("action_key_invalid")
            seen_keys.add(action_key)
            payload: dict[str, object] = dict(action.get("payload") or {})
            if "targets" in action:
                payload["targets"] = list(action["targets"] or ())
            if not str(payload.get("role") or ""):
                raise ValueError("action_role_invalid")
            encoded.append((action_key, _dump_execution_payload(payload)))
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise ValueError("lp_session_not_found")
            session_payload = _load_payload(str(row["payload"]))
            trade_revision = int(session_payload.get("_lp_trade_revision", 0))
            stale = (
                expected_trade_revision is not None
                and trade_revision != int(expected_trade_revision)
            )
            if not stale and expected_generation is not None:
                generation_row = connection.execute(
                    "SELECT generation FROM lp_trade_generation WHERE singleton=1"
                ).fetchone()
                stale = (
                    generation_row is None
                    or int(generation_row["generation"]) != int(expected_generation)
                )
            if stale:
                if reject_existing:
                    return []
                raise LpObservationWait("account_round_invalid")
            if reject_existing:
                placeholders = ",".join("?" for _ in encoded)
                existing = connection.execute(
                    f"SELECT 1 FROM lp_actions WHERE action_key IN ({placeholders})",
                    tuple(key for key, _ in encoded),
                ).fetchone()
                if existing is not None:
                    return []
            self._lp_register_trade_change(connection, str(session_id))
            if patch is not None or state is not None:
                fenced = connection.execute(
                    "SELECT payload FROM lp_sessions WHERE session_id=?",
                    (str(session_id),),
                ).fetchone()
                assert fenced is not None
                fenced_payload = _load_payload(str(fenced["payload"]))
                fenced_payload.update(patch or {})
                connection.execute(
                    "UPDATE lp_sessions SET payload=?,state=COALESCE(?,state) WHERE session_id=?",
                    (_dump_execution_payload(fenced_payload), state, str(session_id)),
                )
            registered: list[dict[str, object]] = []
            for action_key, encoded_payload in encoded:
                connection.execute(
                    "INSERT INTO lp_actions(action_id,session_id,action_key,state,payload,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(action_key) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated_at=excluded.updated_at",
                    (
                        _new_id(), str(session_id), action_key, "pending", encoded_payload,
                        now, now,
                    ),
                )
                stored = connection.execute(
                    "SELECT * FROM lp_actions WHERE action_key=?", (action_key,)
                ).fetchone()
                assert stored is not None
                result = _load_payload(str(stored["payload"]))
                result.update({
                    "action_id": str(stored["action_id"]),
                    "session_id": str(stored["session_id"]),
                    "action_key": action_key,
                    "state": str(stored["state"]),
                    "created_at": str(stored["created_at"]),
                    "updated_at": str(stored["updated_at"]),
                })
                registered.append(result)
        self._notify_lp_trade_change(str(session_id))
        return registered

    def lp_claim_trade_actions(
        self,
        session_id: str,
        *,
        trade_revision: int,
        trade_generation: int,
        actions: Iterable[tuple[str, Mapping[str, object]]],
        patch: Mapping[str, object],
        state: str | None = None,
    ) -> list[tuple[str, str, Mapping[str, object]]]:
        """Thin claim entry over the one fenced-action transaction."""
        rows = [
            {"action_key": str(key), "payload": dict(payload)}
            for key, payload in actions
        ]
        try:
            registered = self.lp_register_fenced_actions(
                str(session_id),
                rows,
                expected_generation=int(trade_generation),
                expected_trade_revision=int(trade_revision),
                patch=patch,
                state=state,
                reject_existing=True,
            )
        except ValueError as exc:
            if str(exc) == "account_round_invalid":
                return []
            raise
        metadata = {
            "action_id", "session_id", "action_key", "state",
            "created_at", "updated_at",
        }
        return [
            (
                str(row["session_id"]),
                str(row["action_key"]),
                {key: value for key, value in row.items() if key not in metadata},
            )
            for row in registered
        ]

    def lp_claim_attention_notification(
        self,
        session_id: str,
        *,
        recovery: bool,
        episode: str,
        revision: int,
        trade_generation: int,
        patch: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Claim the same unarchived session image before notification I/O."""
        prefix = "needs_attention_recovery" if recovery else "needs_attention"
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                return None
            payload = _load_payload(str(row["payload"]))
            generation = connection.execute(
                "SELECT generation FROM lp_trade_generation WHERE singleton=1"
            ).fetchone()
            if (payload.get("account_baseline_archive")
                    or str(row["state"]) == "account_baseline_archived"
                    or self._lp_payload_revision(payload) != revision
                    or generation is None or int(generation[0]) != trade_generation
                    or str(payload.get(prefix + "_episode") or "") != episode
                    or not payload.get(prefix + "_due")
                    or (not recovery and str(row["state"]) != "needs_attention")
                    or (recovery and str(row["state"]) == "needs_attention")):
                return None
            payload.update(patch)
            payload["_lp_revision"] = revision + 1
            connection.execute(
                "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                (_dump_execution_payload(payload), _utc_now(), str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            assert updated is not None
            return self._lp_row_result(updated)

    def lp_finalize_attention_notification_batch(
        self,
        *,
        recovery: bool,
        claims: Mapping[str, tuple[str, int]],
        trade_generation: int,
        patches: Mapping[str, Mapping[str, object]],
        guards: Mapping[str, int],
    ) -> bool:
        """Persist immutable notice bodies only while every member is fenced."""
        if not claims or not set(patches).issubset(set(guards) | set(claims)):
            return False
        if any(
            not str(key).startswith("needs_attention_")
            for patch in patches.values() for key in patch
        ):
            raise ValueError("attention_notification_patch_invalid")
        prefix = "needs_attention_recovery" if recovery else "needs_attention"
        with self._transaction() as connection:
            generation = connection.execute(
                "SELECT generation FROM lp_trade_generation WHERE singleton=1"
            ).fetchone()
            if generation is None or int(generation[0]) != trade_generation:
                return False
            pool = connection.execute(
                "SELECT payload FROM lp_auto_pool WHERE singleton=1"
            ).fetchone()
            owned = {
                str(intent.get("session_id") or "")
                for intent in (json.loads(pool[0]).get("intents", {}).values() if pool else ())
            }
            checked = {}
            for sid in set(guards) | set(claims):
                row = connection.execute(
                    "SELECT * FROM lp_sessions WHERE session_id=?", (str(sid),)
                ).fetchone()
                if row is None or sid in owned:
                    return False
                payload = _load_payload(str(row["payload"]))
                revision = self._lp_payload_revision(payload)
                if (payload.get("account_baseline_archive")
                        or str(row["state"]) == "account_baseline_archived"
                        or (sid in guards and revision != guards[sid])):
                    return False
                if sid in claims:
                    episode, expected_revision = claims[sid]
                    if (revision != expected_revision
                            or str(payload.get(prefix + "_episode") or "") != episode
                            or not payload.get(prefix + "_due")
                            or (not recovery and str(row["state"]) != "needs_attention")
                            or (recovery and str(row["state"]) == "needs_attention")):
                        return False
                checked[sid] = payload, revision
            now = _utc_now()
            for sid, patch in patches.items():
                payload, revision = checked[sid]
                payload.update(patch)
                payload["_lp_revision"] = revision + 1
                connection.execute(
                    "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                    (_dump_execution_payload(payload), now, str(sid)),
                )
            return True

    def lp_finish_attention_notification(
        self,
        session_id: str,
        *,
        recovery: bool,
        episode: str,
        results: Mapping[str, bool],
    ) -> dict[str, object] | None:
        """Complete the exact durable notification episode after I/O."""
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if row is None:
                return None
            payload = _load_payload(str(row["payload"]))
            if row["state"] == "account_baseline_archived" or payload.get("account_baseline_archive"):
                return self._lp_row_result(row)
            batch_key = ("needs_attention_recovery" if recovery else "needs_attention") + "_delivery_batches"
            batches = payload.get(batch_key)
            matching = matching_batch_channels(results, batches if isinstance(batches, Mapping) else {})
            incoming = {str(key): bool(value) for key, value in results.items()
                        if matching is None or key in matching}
            if not incoming:
                return self._lp_row_result(row)
            if recovery:
                if str(payload.get("needs_attention_recovery_episode") or "") != str(episode):
                    return self._lp_row_result(row)
                if not payload.get("needs_attention_recovery_due"):
                    return self._lp_row_result(row)
                status_key = "needs_attention_recovery_channel_status"
                due_key = "needs_attention_recovery_due"
                channels_key = "needs_attention_recovery_channels"
            else:
                if str(payload.get("needs_attention_episode") or "") != str(episode):
                    return self._lp_row_result(row)
                status_key = "needs_attention_channel_status"
                channels_key = None
                if str(row["state"]) != "needs_attention" or not payload.get("needs_attention_due"):
                    current = payload.get(status_key)
                    status = {
                        **{"feishu": False, "xiaoai": False},
                        **(
                            {str(key): bool(value) for key, value in current.items()}
                            if isinstance(current, Mapping) else {}
                        ),
                        **incoming,
                    }
                    payload["needs_attention_sending"] = False
                    payload[status_key] = status
                    succeeded = sorted(key for key, value in status.items() if value)
                    if succeeded and payload.get("needs_attention_verified_recovery_episode") == str(episode):
                        payload.update(
                            needs_attention_recovery_episode=str(episode),
                            needs_attention_recovery_due=True,
                            needs_attention_recovery_channels=succeeded,
                            needs_attention_recovery_channel_status={},
                            needs_attention_recovery_retry_at=None,
                        )
                    connection.execute(
                        "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                        (_dump_execution_payload(payload), now, str(session_id)),
                    )
                    updated = connection.execute(
                        "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
                    ).fetchone()
                    assert updated is not None
                    return self._lp_row_result(updated)
                if not payload.get("needs_attention_due"):
                    return self._lp_row_result(row)
                due_key = "needs_attention_due"
            current = payload.get(status_key)
            status = {
                **{"feishu": False, "xiaoai": False},
                **(
                    {str(key): bool(value) for key, value in current.items()}
                    if isinstance(current, Mapping) else {}
                ),
                **incoming,
            }
            raw_channels = payload.get(channels_key) if channels_key else None
            channels = (
                [str(value) for value in raw_channels if str(value) in {"feishu", "xiaoai"}]
                if raw_channels is not None else ["feishu", "xiaoai"]
            ) or ["feishu", "xiaoai"]
            delivered = all(status[channel] for channel in channels)
            payload["needs_attention_sending"] = False
            payload[status_key] = status
            payload[due_key] = not delivered
            if not recovery:
                payload["needs_attention_notified"] = delivered
            payload["needs_attention_send_error"] = (
                None if delivered else "notification_delivery_failed"
            )
            connection.execute(
                "UPDATE lp_sessions SET payload=?,updated_at=? WHERE session_id=?",
                (_dump_execution_payload(payload), now, str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM lp_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            assert updated is not None
            return self._lp_row_result(updated)

    def lp_upsert_action(
        self,
        session_id: str,
        action_key: str,
        *,
        state: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        encoded = _dump_execution_payload(payload)
        now = _utc_now()
        action_id = _new_id()
        with self._transaction() as connection:
            # Trading actions own this sequence; ordinary observations never
            # advance it, regardless of which display fields are added later.
            self._lp_register_trade_change(connection, session_id)
            connection.execute(
                "INSERT INTO lp_actions(action_id,session_id,action_key,state,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(action_key) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated_at=excluded.updated_at",
                (action_id, str(session_id), str(action_key), str(state), encoded, now, now),
            )
            row = connection.execute(
                "SELECT * FROM lp_actions WHERE action_key=?", (str(action_key),)
            ).fetchone()
        assert row is not None
        result = _load_payload(str(row["payload"]))
        result.update(
            {
                "action_id": str(row["action_id"]),
                "session_id": str(row["session_id"]),
                "action_key": str(row["action_key"]),
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        self._notify_lp_trade_change(session_id)
        return result

    def lp_finish_cancel(
        self, session_id: str, action_key: str, *, state: str,
        payload: Mapping[str, object],
    ) -> None:
        """Record a cancel receipt without downgrading terminal account proof."""
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE lp_actions SET state=?,payload=?,updated_at=? "
                "WHERE session_id=? AND action_key=? "
                "AND (state!='accepted' OR ?='accepted')",
                (state, _dump_execution_payload(payload), _utc_now(),
                 str(session_id), str(action_key), state),
            ).rowcount
            if changed:
                self._lp_register_trade_change(connection, session_id)
        if changed:
            self._notify_lp_trade_change(session_id)

    def lp_reject_pending_cancel(self, session_id: str, action_key: str) -> None:
        """Reject a definitely unsent cancel without overwriting receipt facts."""
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE lp_actions SET state='rejected',updated_at=? "
                "WHERE session_id=? AND action_key=? AND state='pending'",
                (_utc_now(), str(session_id), str(action_key)),
            ).rowcount
            if changed:
                self._lp_register_trade_change(connection, session_id)
        if changed:
            self._notify_lp_trade_change(session_id)

    def lp_actions(self, session_id: str, *, connection=None) -> list[dict[str, object]]:
        with (self._read_connection() if connection is None else nullcontext(connection)) as connection:
            rows = connection.execute(
                "SELECT * FROM lp_actions WHERE session_id=? ORDER BY created_at,action_id",
                (str(session_id),),
            ).fetchall()
        result = []
        for row in rows:
            payload = _load_payload(str(row["payload"]))
            payload.update(
                {
                    "action_id": str(row["action_id"]),
                    "session_id": str(row["session_id"]),
                    "action_key": str(row["action_key"]),
                    "state": str(row["state"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
            result.append(payload)
        return result

    # N-leg execution owns separate tables: legacy execution rows deliberately
    # remain untouched while the future Adapter and this no-submit state machine
    # share one durable boundary.
    @staticmethod
    def _n_leg_control_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {
                "mode": "MANUAL",
                "breaker_open": False,
                "breaker_reason": None,
                "active_batch_id": None,
                "total_unsettled_capital_units": 0,
                "contract_generation": 1,
                "qualification_policy_version": 1,
                "safety_config_version": 1,
                "enabled_execution_scope_version": [],
            }
        return {
            "mode": str(row["mode"]),
            "breaker_open": bool(row["breaker_open"]),
            "breaker_reason": row["breaker_reason"],
            "active_batch_id": row["active_batch_id"],
            "total_unsettled_capital_units": int(row["total_unsettled_capital_units"]),
            "contract_generation": int(row["contract_generation"]),
            "qualification_policy_version": int(row["qualification_policy_version"]),
            "safety_config_version": int(row["safety_config_version"]),
            "enabled_execution_scope_version": _n_leg_enabled_scopes(
                row["enabled_execution_scope_version"]
            ),
        }

    def n_leg_control(self) -> dict[str, object]:
        with self._read_connection() as connection:
            row = connection.execute("SELECT * FROM n_leg_controls WHERE singleton=1").fetchone()
        return self._n_leg_control_row(row)

    def advance_minimum_reader_generation(
        self, target: int, *, connection: sqlite3.Connection | None = None
    ) -> int:
        """Move the schema_metadata reader fence up to ``target``.

        Idempotent when ``target`` equals the current fence, succeeds only for
        ``target > current``, and refuses to lower the fence. Runs inside the
        caller's transaction when ``connection`` is supplied (the N_LEG cutover
        composes it into its single migration transaction); otherwise the
        advance gets its own transaction.
        """
        if type(target) is not int or target < 1:
            raise ValueError("minimum reader generation target must be a positive integer")

        def _advance(sqlite_connection: sqlite3.Connection) -> int:
            row = sqlite_connection.execute(
                "SELECT minimum_reader_generation FROM schema_metadata WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ValueError("prediction minimum reader generation is missing")
            current = int(row[0])
            if target == current:
                return current
            if target < current:
                raise ValueError("minimum reader generation cannot be lowered")
            sqlite_connection.execute(
                "UPDATE schema_metadata SET minimum_reader_generation=? WHERE singleton=1",
                (target,),
            )
            return target

        if connection is not None:
            return _advance(connection)
        with self._transaction() as owned_connection:
            return _advance(owned_connection)

    def n_leg_qualification_policy_latest(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT version, policy FROM n_leg_qualification_policy ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "version": int(row["version"]),
            "policy": _load_payload(str(row["policy"])),
        }

    def n_leg_qualification_policy_write(
        self, version: int, policy: Mapping[str, object]
    ) -> None:
        if type(version) is not int or version < 1:
            raise ValueError("qualification policy version must be a positive integer")
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO n_leg_qualification_policy(version, policy, updated_at) VALUES (?, ?, ?) ON CONFLICT(version) DO UPDATE SET policy=excluded.policy, updated_at=excluded.updated_at",
                (version, _dump_payload(policy), now),
            )

    def n_leg_safety_config_latest(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT version, config FROM n_leg_safety_config ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "version": int(row["version"]),
            "config": _load_payload(str(row["config"])),
        }

    def n_leg_safety_config_write(self, version: int, config: Mapping[str, object]) -> None:
        if type(version) is not int or version < 1:
            raise ValueError("safety config version must be a positive integer")
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO n_leg_safety_config(version, config, updated_at) VALUES (?, ?, ?) ON CONFLICT(version) DO UPDATE SET config=excluded.config, updated_at=excluded.updated_at",
                (version, _dump_payload(config), now),
            )

    @staticmethod
    def _n_leg_request_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "request_id": str(row["request_id"]),
            "fifo_index": int(row["fifo_index"]),
            "idempotency_key": str(row["idempotency_key"]),
            "component_id": str(row["component_id"]),
            "state": str(row["state"]),
            "abandon_reason": row["abandon_reason"],
            "payload": _load_payload(str(row["payload"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def n_leg_request_enqueue(
        self,
        *,
        component_id: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        max_pending: int = 5,
    ) -> dict[str, object]:
        """Insert one FIFO execution request; the idempotency key is the
        replay identity, so a repeated key returns the existing row unchanged
        and never enqueues a second entry.

        Review round 2 (ruling 3): the FIFO queue rules are enforced inside
        this BEGIN IMMEDIATE transaction — one PENDING row per component
        (``QUEUE_DUPLICATE``) and at most ``max_pending`` PENDING rows in
        total (``QUEUE_FULL``) — so concurrent confirms with different
        idempotency keys cannot both insert."""
        if not isinstance(component_id, str) or not component_id:
            raise ValueError("component id must be non-empty text")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency key must be non-empty text")
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        encoded = _dump_payload(dict(payload))
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM n_leg_execution_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._n_leg_request_row(existing)
            pending_rows = connection.execute(
                "SELECT component_id FROM n_leg_execution_requests WHERE state='PENDING'"
            ).fetchall()
            if any(
                str(row["component_id"]) == component_id
                for row in pending_rows
            ):
                raise ValueError("QUEUE_DUPLICATE")
            if len(pending_rows) >= max_pending:
                raise ValueError("QUEUE_FULL")
            request_id = _new_id()
            connection.execute(
                "INSERT INTO n_leg_execution_requests(request_id, idempotency_key, component_id, payload, state, abandon_reason, created_at, updated_at) VALUES (?, ?, ?, ?, 'PENDING', NULL, ?, ?)",
                (request_id, idempotency_key, component_id, encoded, now, now),
            )
            row = connection.execute(
                "SELECT * FROM n_leg_execution_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            return self._n_leg_request_row(row)

    def n_leg_request_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM n_leg_execution_requests WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
        return None if row is None else self._n_leg_request_row(row)

    def n_leg_requests(self) -> list[dict[str, object]]:
        """All execution request rows in FIFO order (queue display)."""
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM n_leg_execution_requests ORDER BY fifo_index"
            ).fetchall()
        return [self._n_leg_request_row(row) for row in rows]

    def n_leg_request_head(self) -> dict[str, object] | None:
        """The lowest FIFO-index PENDING request, or None when idle."""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM n_leg_execution_requests WHERE state='PENDING' ORDER BY fifo_index LIMIT 1"
            ).fetchone()
        return None if row is None else self._n_leg_request_row(row)

    def n_leg_request_update(
        self,
        request_id: str,
        *,
        state: str | None = None,
        abandon_reason: str | None = None,
        payload_merge: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Transition one execution request row (state machine guarded)."""
        if state is not None and state not in {
            "PENDING",
            "ADMITTED",
            "ABANDONED",
            "SUBMITTED",
        }:
            raise ValueError("invalid n-leg execution request state")
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM n_leg_execution_requests WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                raise ValueError("N_LEG_REQUEST_NOT_FOUND")
            current = self._n_leg_request_row(row)
            next_state = current["state"] if state is None else state
            next_reason = (
                current["abandon_reason"]
                if abandon_reason is None
                else abandon_reason
            )
            payload = current["payload"]
            if payload_merge:
                payload = {**payload, **dict(payload_merge)}
            connection.execute(
                "UPDATE n_leg_execution_requests SET state=?, abandon_reason=?, payload=?, updated_at=? WHERE request_id=?",
                (
                    str(next_state),
                    next_reason,
                    _dump_payload(payload),
                    now,
                    str(request_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM n_leg_execution_requests WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
            return self._n_leg_request_row(updated)

    def n_leg_requests_abandon_pending(self, reason: str) -> int:
        """Abandon every PENDING request (incident stop-the-world) and return
        how many rows changed."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("abandon reason must be non-empty text")
        now = _utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE n_leg_execution_requests SET state='ABANDONED', abandon_reason=?, updated_at=? WHERE state='PENDING'",
                (reason, now),
            )
            return int(cursor.rowcount)

    def partial_fill_proof_save(
        self,
        proof: object,
        unsafe_counterexample: Mapping[str, object] | None = None,
        *,
        proof_fingerprint: str | None = None,
    ) -> None:
        """Upsert one #74 partial-fill proof record by its cache fingerprint.

        The record is duck-typed (``to_payload``/``status``/``fingerprint``)
        so this store module never imports the execution module (which itself
        imports this store).  The UNSAFE counterexample is persisted alongside
        the proof payload.

        The row key defaults to the record's own fingerprint; the live
        resolver passes the stable adversary fingerprint instead, because a
        proof can only be replayed from storage before solving when the key
        is the same stable per-snapshot fingerprint (read-before-solve).
        """
        payload = proof.to_payload()
        if not isinstance(payload, dict):
            raise TypeError("proof payload must be a mapping")
        status = str(proof.status)
        key = proof_fingerprint if proof_fingerprint is not None else str(proof.fingerprint)
        if not isinstance(key, str) or not key:
            raise ValueError("proof fingerprint must be a non-empty string")
        stored = {
            "proof": payload,
            "proof_fingerprint": key,
            "unsafe_counterexample": (
                dict(unsafe_counterexample)
                if unsafe_counterexample is not None
                else None
            ),
        }
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO partial_fill_proofs(
                    proof_fingerprint, status, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(proof_fingerprint) DO UPDATE SET
                    status=excluded.status,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (key, status, _dump_payload(stored), now, now),
            )

    def partial_fill_proof(self, proof_fingerprint: str) -> dict[str, object] | None:
        """Read one #74 proof record payload, or None when never persisted."""
        if not isinstance(proof_fingerprint, str) or not proof_fingerprint:
            raise ValueError("proof fingerprint must be a non-empty string")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM partial_fill_proofs WHERE proof_fingerprint = ?",
                (proof_fingerprint,),
            ).fetchone()
        if row is None:
            return None
        return _load_payload(str(row["payload"]))

    def n_leg_scopes(self) -> dict[str, dict[str, object]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT scope_id, capability, scope_version, members FROM n_leg_execution_scopes ORDER BY scope_id"
            ).fetchall()
        return {
            str(row["scope_id"]): {
                "scope_id": str(row["scope_id"]),
                "capability": str(row["capability"]),
                "scope_version": int(row["scope_version"]),
                "members": _load_payload(str(row["members"])),
            }
            for row in rows
        }

    def n_leg_scope(self, scope_id: str) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT scope_id, capability, scope_version, members FROM n_leg_execution_scopes WHERE scope_id=?",
                (str(scope_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "scope_id": str(row["scope_id"]),
            "capability": str(row["capability"]),
            "scope_version": int(row["scope_version"]),
            "members": _load_payload(str(row["members"])),
        }

    def n_leg_scope_write(
        self,
        scope_id: str,
        *,
        capability: str,
        scope_version: int,
        members: Mapping[str, object],
    ) -> None:
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError("scope id must be non-empty text")
        if capability not in {"OBSERVE_ONLY", "MANUAL_CANARY", "AUTO_ELIGIBLE"}:
            raise ValueError("scope capability is invalid")
        if type(scope_version) is not int or scope_version < 1:
            raise ValueError("scope version must be a positive integer")
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO n_leg_execution_scopes(scope_id, capability, scope_version, members, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(scope_id) DO UPDATE SET capability=excluded.capability, scope_version=excluded.scope_version, members=excluded.members, updated_at=excluded.updated_at",
                (scope_id, capability, scope_version, _dump_payload(members), now),
            )

    def n_leg_mode_control_write(
        self,
        *,
        mode: str | None = None,
        contract_generation: int | None = None,
        qualification_policy_version: int | None = None,
        safety_config_version: int | None = None,
        enabled_execution_scope_version: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Atomically update the versioned N-leg mode contract fields."""
        if mode is not None and mode not in {"MANUAL", "AUTO"}:
            raise ValueError("n-leg mode must be MANUAL or AUTO")
        for name, value in (
            ("contract_generation", contract_generation),
            ("qualification_policy_version", qualification_policy_version),
            ("safety_config_version", safety_config_version),
        ):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        now = _utc_now()
        with self._transaction() as connection:
            current = self._n_leg_control_row(
                connection.execute("SELECT * FROM n_leg_controls WHERE singleton=1").fetchone()
            )
            next_control = dict(current)
            if mode is not None:
                next_control["mode"] = mode
            if contract_generation is not None:
                next_control["contract_generation"] = contract_generation
            if qualification_policy_version is not None:
                next_control["qualification_policy_version"] = qualification_policy_version
            if safety_config_version is not None:
                next_control["safety_config_version"] = safety_config_version
            if enabled_execution_scope_version is not None:
                if not isinstance(enabled_execution_scope_version, list):
                    raise ValueError("enabled execution scope version must be a list")
                if any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("scope_id"), str)
                    or not item["scope_id"]
                    or type(item.get("scope_version")) is not int
                    or item["scope_version"] < 1
                    for item in enabled_execution_scope_version
                ):
                    raise ValueError("enabled execution scope version entries are invalid")
                next_control["enabled_execution_scope_version"] = enabled_execution_scope_version
            connection.execute(
                "INSERT INTO n_leg_controls(singleton, mode, breaker_open, breaker_reason, active_batch_id, total_unsettled_capital_units, contract_generation, qualification_policy_version, safety_config_version, enabled_execution_scope_version, updated_at) VALUES (1, ?, 0, NULL, NULL, 0, ?, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, contract_generation=excluded.contract_generation, qualification_policy_version=excluded.qualification_policy_version, safety_config_version=excluded.safety_config_version, enabled_execution_scope_version=excluded.enabled_execution_scope_version, updated_at=excluded.updated_at",
                (
                    str(next_control["mode"]),
                    int(next_control["contract_generation"]),
                    int(next_control["qualification_policy_version"]),
                    int(next_control["safety_config_version"]),
                    json.dumps(next_control["enabled_execution_scope_version"]),
                    now,
                ),
            )
        return self.n_leg_control()

    def record_control_event(
        self,
        *,
        action: str,
        target: str,
        outcome: str,
        payload: Mapping[str, object],
    ) -> str:
        if outcome not in {"succeeded", "rejected", "failed"}:
            raise ValueError("invalid terminal control outcome")
        with self._transaction() as connection:
            return self._insert_control_event(
                connection,
                action=action,
                target=target,
                outcome=outcome,
                payload=payload,
            )

    def n_leg_batch(self, execution_batch_id: str) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM n_leg_batches WHERE execution_batch_id=?",
                (str(execution_batch_id),),
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    # -- issue #122: the inherited N_LEG executed-lock -----------------------

    @staticmethod
    def _n_leg_lineage_ancestors(
        connection: sqlite3.Connection, lineage_id: str
    ) -> set[str]:
        """The lineage closure over ``predecessor_lineage_ids`` (R2): read
        live from the graph rows, CLOSED predecessors included, any status."""
        ancestors: set[str] = set()
        frontier = [lineage_id]
        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            rows = connection.execute(
                "SELECT predecessor_lineage_ids FROM n_leg_episode_lineage"
                f" WHERE lineage_id IN ({placeholders})",
                tuple(frontier),
            ).fetchall()
            frontier = []
            for row in rows:
                for predecessor in json.loads(str(row["predecessor_lineage_ids"])):
                    predecessor = str(predecessor)
                    if predecessor not in ancestors:
                        ancestors.add(predecessor)
                        frontier.append(predecessor)
        ancestors.discard(lineage_id)
        return ancestors

    def _n_leg_lineage_rearm_evidence(
        self,
        connection: sqlite3.Connection,
        current_lineage: str,
        claimed_at: str,
    ) -> bool:
        """R3: the successor re-arms only through its OWN graph lineage's
        ``NO_QUALIFIED_OPPORTUNITY`` episode close, stamped after the newest
        hit ancestor claim. COMPONENT_RETIRED closes never count, and both
        timestamp families are parsed (never string-compared)."""
        boundary = _parse_timestamp(claimed_at)
        # The episodes table belongs to the EpisodeStore DDL and may not exist
        # yet in a store that never recorded an episode: no table means no
        # re-arm evidence (fail-closed), not a migration.
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='opportunity_episodes'"
        ).fetchone()
        if table is None:
            return False
        rows = connection.execute(
            "SELECT closed_at FROM opportunity_episodes"
            " WHERE episode_lineage_id=? AND close_reason=?"
            " AND closed_at IS NOT NULL",
            (current_lineage, CLOSE_NO_QUALIFIED_OPPORTUNITY),
        ).fetchall()
        for row in rows:
            if _parse_timestamp(row["closed_at"]) > boundary:
                return True
        return False

    def _n_leg_lineage_lock_decision(
        self,
        connection: sqlite3.Connection,
        *,
        component_id: object,
        frozen_lineage_id: object,
    ) -> tuple[str | None, str | None]:
        """Resolve the batch's component in the runtime graph and enforce the
        executed-lock over the whole family (issue #122 R1-R4).

        Returns ``(claim_lineage, reason)``: ``claim_lineage`` is the resolved
        graph lineage the batch must claim (the caller records it as the
        claim key and overwrites the stored payload's display field), or None
        to keep the legacy frozen-string behavior; ``reason`` is a stable
        rejection literal. The frozen payload string is never trusted as the
        identity: when the payload carries a ``component_id`` the graph row
        for it decides, and an unresolvable identity fails closed.
        """
        # The graph tables belong to the RuntimeGraphStore DDL and may not
        # exist yet in a store that never built the runtime graph: an
        # explicit component identity then fails closed, while callers that
        # freeze only a display string keep the legacy behavior.
        graph_available = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='n_leg_episode_lineage'"
            ).fetchone()
            is not None
        )
        has_component_id = isinstance(component_id, str) and bool(component_id)
        row = None
        if has_component_id:
            if not graph_available:
                return None, "N_LEG_LINEAGE_UNKNOWN"
            row = connection.execute(
                "SELECT component_id, lineage_id FROM n_leg_episode_lineage"
                " WHERE component_id=?",
                (component_id,),
            ).fetchone()
            if row is None:
                return None, "N_LEG_LINEAGE_UNKNOWN"
            identity = component_id
        elif graph_available:
            # Callers that freeze only the display lineage string (the queue
            # path) are still resolved through the GRAPH, never trusted: a
            # legacy ``lineage:{component_id}`` string resolves that component
            # row, any other string resolves the row currently carrying it as
            # its lineage. Unresolvable strings keep the legacy behavior.
            if isinstance(frozen_lineage_id, str) and frozen_lineage_id:
                if frozen_lineage_id.startswith("lineage:"):
                    legacy_component_id = frozen_lineage_id[len("lineage:") :]
                    if legacy_component_id:
                        row = connection.execute(
                            "SELECT component_id, lineage_id"
                            " FROM n_leg_episode_lineage WHERE component_id=?",
                            (legacy_component_id,),
                        ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT component_id, lineage_id FROM n_leg_episode_lineage"
                        " WHERE lineage_id=?"
                        " ORDER BY (status='ACTIVE') DESC, generation DESC LIMIT 1",
                        (frozen_lineage_id,),
                    ).fetchone()
            if row is None:
                return None, None
            identity = str(row["component_id"])
        else:
            return None, None
        current_lineage = str(row["lineage_id"])
        blocked = {current_lineage}
        blocked |= self._n_leg_lineage_ancestors(connection, current_lineage)
        # Legacy-format claim rows (``lineage:{component_id}``) written
        # before #122 keep blocking their resolved families; an unknown
        # legacy component id falls back to the exact component-string
        # comparison (no weaker than the pre-#122 behavior).
        legacy_exact: set[str] = set()
        for claim_row in connection.execute(
            "SELECT episode_lineage_id FROM n_leg_lineage_claims"
            " WHERE episode_lineage_id LIKE 'lineage:%'"
        ).fetchall():
            legacy_component_id = str(claim_row["episode_lineage_id"])[
                len("lineage:") :
            ]
            legacy_row = connection.execute(
                "SELECT lineage_id FROM n_leg_episode_lineage WHERE component_id=?",
                (legacy_component_id,),
            ).fetchone()
            if legacy_row is None:
                legacy_exact.add(legacy_component_id)
            else:
                blocked.add(str(legacy_row["lineage_id"]))
        placeholders = ",".join("?" for _ in blocked)
        hits = {
            str(hit["episode_lineage_id"]): str(hit["created_at"])
            for hit in connection.execute(
                "SELECT episode_lineage_id, created_at FROM n_leg_lineage_claims"
                f" WHERE episode_lineage_id IN ({placeholders})",
                tuple(sorted(blocked)),
            ).fetchall()
        }
        if current_lineage in hits or identity in legacy_exact:
            return None, "N_LEG_LINEAGE_ALREADY_CLAIMED"
        if hits:
            newest_claim = max(hits.values())
            if not self._n_leg_lineage_rearm_evidence(
                connection, current_lineage, newest_claim
            ):
                return None, "N_LEG_LINEAGE_INHERITED_CLAIMED"
        return current_lineage, None

    def n_leg_lineage_admission_check(self, component_id: str) -> dict[str, object]:
        """Read-only executed-lock precheck for one frozen lineage identity
        (issue #122 R5).

        ``component_id`` is the frozen lineage identity the confirm seam is
        about to freeze — the resolver entry's graph lineage (digest), the
        legacy ``lineage:{component}`` form, or a bare component string. It is
        decided exactly like the admission transaction decides a queue
        payload's frozen string (``component_id=None`` path), so the graph's
        digest-keyed rows block through it whatever literal the caller holds.
        When the graph cannot resolve the string, the precheck mirrors the
        admission caller's fallback: an exact ``n_leg_lineage_claims`` hit on
        the string itself blocks with ``N_LEG_LINEAGE_ALREADY_CLAIMED`` (a
        pre-#122 claim row keeps blocking its family). Returns
        ``{"lineage", "blocked", "reason"}``; the transactional check stays
        the authority. A store whose graph tables were never built has no
        lineage to precheck — the authoritative admission check still fails
        unknown identities closed.
        """
        identity = str(component_id)
        with self._read_connection() as connection:
            # The graph tables belong to the RuntimeGraphStore DDL and may not
            # exist yet in a store that never built the runtime graph; there
            # is no lineage to precheck then, and the authoritative admission
            # check still fails unknown identities closed.
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='n_leg_episode_lineage'"
            ).fetchone()
            if table is None:
                return {"lineage": None, "blocked": False, "reason": None}
            claim_lineage, reason = self._n_leg_lineage_lock_decision(
                connection, component_id=None, frozen_lineage_id=identity
            )
            if claim_lineage is None and reason is None:
                # The string is unresolvable for the graph (an unknown
                # family): mirror the admission caller's exact-match fallback
                # so a pre-#122 claim on this very string still blocks.
                if connection.execute(
                    "SELECT 1 FROM n_leg_lineage_claims WHERE episode_lineage_id=?",
                    (identity,),
                ).fetchone() is not None:
                    reason = "N_LEG_LINEAGE_ALREADY_CLAIMED"
            return {
                "lineage": claim_lineage,
                "blocked": reason is not None,
                "reason": reason,
            }

    def n_leg_create_batch(
        self,
        payload: Mapping[str, object],
        *,
        expected_versions: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Atomically claim a lineage and the single active N-leg batch.

        Issue #64 Slice 4: when ``expected_versions`` is provided, every
        listed fact is compared against the CURRENT state inside this one
        transaction (contract generation, policy/mode/scope versions,
        breaker, solution/account fingerprints, caps fingerprint). Any
        mismatch raises ``N_LEG_ADMISSION_VERSION_STALE`` and the unsettled
        cap is enforced as ``N_LEG_ADMISSION_UNSETTLED_CAP`` — both before
        any write, so a rejection leaves no batch row, no lineage claim and
        an untouched ledger.
        """
        batch_id = payload.get("execution_batch_id")
        opportunity_id = payload.get("opportunity_episode_id")
        lineage_id = payload.get("episode_lineage_id")
        mode = payload.get("mode")
        reservation = payload.get("total_unsettled_capital_units")
        if (
            not all(isinstance(value, str) and value for value in (batch_id, opportunity_id, lineage_id))
            or mode not in {"MANUAL", "AUTO"}
            or type(reservation) is not int
            or reservation < 0
        ):
            raise ValueError("invalid n-leg batch")
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT payload FROM n_leg_batches WHERE execution_batch_id=?", (batch_id,)
            ).fetchone()
            if existing is not None:
                result = _load_payload(str(existing["payload"]))
                # Batches evolve after Entry. Retry identity is the immutable
                # entry fingerprint, not the mutable receipt/reconciliation state.
                if result.get("entry_fingerprint") != payload.get("entry_fingerprint"):
                    raise ValueError("N_LEG_BATCH_ID_CONFLICT")
                return result
            active_lp = connection.execute(
                "SELECT 1 FROM lp_sessions WHERE state NOT IN ('complete','entry_rejected') LIMIT 1"
            ).fetchone()
            if active_lp is not None:
                raise ValueError("N_LEG_ACTIVE_LP_SESSION")
            control = self._n_leg_control_row(
                connection.execute("SELECT * FROM n_leg_controls WHERE singleton=1").fetchone()
            )
            if control["breaker_open"]:
                raise ValueError("N_LEG_BREAKER_OPEN")
            if control["active_batch_id"] is not None:
                raise ValueError("N_LEG_ACTIVE_BATCH_EXISTS")
            claim_lineage, lineage_reason = self._n_leg_lineage_lock_decision(
                connection,
                component_id=payload.get("component_id"),
                frozen_lineage_id=lineage_id,
            )
            if lineage_reason is not None:
                raise ValueError(lineage_reason)
            if claim_lineage is None:
                if connection.execute(
                    "SELECT 1 FROM n_leg_lineage_claims WHERE episode_lineage_id=?",
                    (lineage_id,),
                ).fetchone() is not None:
                    raise ValueError("N_LEG_LINEAGE_ALREADY_CLAIMED")
            else:
                lineage_id = claim_lineage
            if expected_versions is not None and self._admission_version_mismatch(
                connection, control, payload, expected_versions
            ):
                raise ValueError("N_LEG_ADMISSION_VERSION_STALE")
            unsettled_cap = self._n_leg_unsettled_cap(connection)
            if (
                unsettled_cap > 0
                and int(control["total_unsettled_capital_units"]) + int(reservation)
                > unsettled_cap
            ):
                raise ValueError("N_LEG_ADMISSION_UNSETTLED_CAP")
            stored_payload = dict(payload)
            stored_payload["prior_unsettled_capital_units"] = int(control["total_unsettled_capital_units"])
            if claim_lineage is not None:
                # R4: the stored lineage identity is the graph's resolved
                # current lineage, never the frozen confirm display string.
                stored_payload["episode_lineage_id"] = lineage_id
            encoded = _dump_execution_payload(stored_payload)
            connection.execute(
                "INSERT INTO n_leg_lineage_claims(episode_lineage_id, opportunity_episode_id, execution_batch_id, created_at) VALUES (?, ?, ?, ?)",
                (lineage_id, opportunity_id, batch_id, now),
            )
            connection.execute(
                "INSERT INTO n_leg_batches(execution_batch_id, opportunity_episode_id, episode_lineage_id, state, submission_enabled, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, opportunity_id, lineage_id, str(payload.get("state")), 0, encoded, now, now),
            )
            connection.execute(
                "INSERT INTO n_leg_controls(singleton, mode, breaker_open, breaker_reason, active_batch_id, total_unsettled_capital_units, total_unsettled_capital_version, contract_generation, qualification_policy_version, safety_config_version, enabled_execution_scope_version, updated_at) VALUES (1, ?, 0, NULL, ?, ?, 1, ?, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, active_batch_id=excluded.active_batch_id, total_unsettled_capital_units=excluded.total_unsettled_capital_units, total_unsettled_capital_version=total_unsettled_capital_version+1, contract_generation=excluded.contract_generation, qualification_policy_version=excluded.qualification_policy_version, safety_config_version=excluded.safety_config_version, enabled_execution_scope_version=excluded.enabled_execution_scope_version, updated_at=excluded.updated_at",
                (
                    mode,
                    batch_id,
                    int(control["total_unsettled_capital_units"]) + reservation,
                    int(control["contract_generation"]),
                    int(control["qualification_policy_version"]),
                    int(control["safety_config_version"]),
                    json.dumps(control["enabled_execution_scope_version"]),
                    now,
                ),
            )
        return _load_payload(encoded)

    @staticmethod
    def _n_leg_unsettled_cap(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT config FROM n_leg_safety_config ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0
        try:
            config = _load_payload(str(row["config"]))
            return max(0, int(config.get("max_total_unsettled_capital_units") or 0))
        except (ValueError, TypeError):
            return 0

    def _admission_version_mismatch(
        self,
        connection: sqlite3.Connection,
        control: Mapping[str, object],
        payload: Mapping[str, object],
        expected: Mapping[str, object],
    ) -> bool:
        """Compare each expected fact against current in-transaction state."""
        scope_id = str(expected.get("scope_id") or "")
        scope = None
        if scope_id:
            scope = connection.execute(
                "SELECT capability, scope_version FROM n_leg_execution_scopes WHERE scope_id=?",
                (scope_id,),
            ).fetchone()
        caps_fingerprint = None
        caps_row = connection.execute(
            "SELECT config FROM n_leg_safety_config ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if caps_row is not None:
            try:
                config = _load_payload(str(caps_row["config"]))
                caps_fingerprint = canonical_fingerprint(
                    {
                        key: int(config.get(key) or 0)
                        for key in (
                            "max_per_trade_cost_units",
                            "max_total_unsettled_capital_units",
                            "max_partial_fill_loss_units",
                            "max_auto_repair_loss_units",
                        )
                    }
                )
            except (ValueError, TypeError):
                caps_fingerprint = None
        comparisons = (
            ("contract_generation", int(control["contract_generation"])),
            ("qualification_policy_version", int(control["qualification_policy_version"])),
            ("mode", str(control["mode"])),
            ("breaker_closed", not bool(control["breaker_open"])),
            (
                "enabled_execution_scope_version",
                control["enabled_execution_scope_version"],
            ),
            (
                "execution_solution_fingerprint",
                payload.get("execution_solution_fingerprint"),
            ),
            ("account_snapshot_fingerprint", payload.get("account_fingerprint")),
            (
                "capability",
                str(scope["capability"]) if scope is not None else None,
            ),
            (
                "scope_version",
                int(scope["scope_version"]) if scope is not None else None,
            ),
            ("caps_fingerprint", caps_fingerprint),
        )
        for key, current in comparisons:
            if key not in expected:
                continue
            if expected.get(key) != current:
                return True
        return False

    def n_leg_incident_batch(self) -> dict[str, object] | None:
        """The unacknowledged N_LEG execution-incident batch, if any."""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM n_leg_batches WHERE state='INCIDENT' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _load_payload(str(row["payload"]))

    def n_leg_acknowledge_incident(
        self,
        execution_batch_id: str,
        *,
        acknowledgement: Mapping[str, object],
    ) -> dict[str, object]:
        """Issue #64 Slice 6: the four-step atomic incident unlock.

        (1) every leg receipt is terminal and consistent — no UNKNOWN state,
        no unresolved conflicts; (2) the ledger recomputes to the stored
        total; (3) the acknowledgement (actor/time/batch/reason) is recorded
        as an immutable transition; (4) the gate releases by leaving the
        INCIDENT state. Any violated step raises before any write.
        """
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM n_leg_batches WHERE execution_batch_id=?",
                (str(execution_batch_id),),
            ).fetchone()
            if row is None:
                raise ValueError("N_LEG_BATCH_NOT_FOUND")
            batch = _load_payload(str(row["payload"]))
            if batch.get("state") != "INCIDENT":
                raise ValueError("N_LEG_INCIDENT_NOT_ACTIVE")
            legs = batch.get("legs")
            if not isinstance(legs, list) or not legs:
                raise ValueError("N_LEG_INCIDENT_UNKNOWABLE")
            for leg in legs:
                receipt = (
                    leg.get("receipt")
                    if isinstance(leg, dict) and isinstance(leg.get("receipt"), dict)
                    else None
                )
                if not isinstance(receipt, dict) or receipt.get("state") not in {
                    "FILLED",
                    "REJECTED",
                    "CANCELLED",
                }:
                    raise ValueError("N_LEG_INCIDENT_UNKNOWN_RECEIPT")
            unresolved = batch.get("unresolved_conflicts")
            if isinstance(unresolved, list) and unresolved:
                raise ValueError("N_LEG_INCIDENT_UNRESOLVED_CONFLICT")
            reservations = batch.get("reservations")
            if isinstance(reservations, list):
                occupancy = sum(
                    int(row.get("remaining_units", 0))
                    + int(row.get("holding_units", 0))
                    for row in reservations
                    if isinstance(row, dict)
                )
                stored = int(batch.get("total_unsettled_capital_units", -1))
                if occupancy != stored:
                    raise ValueError("N_LEG_INCIDENT_LEDGER_MISMATCH")
            acknowledged = dict(batch.get("incident") or {})
            acknowledged["acknowledgement"] = dict(acknowledgement)
            acknowledged["acknowledged_at"] = now
            acknowledged["repair_status"] = "ACKNOWLEDGED"
            batch["incident"] = acknowledged
            batch["state"] = "INCIDENT_ACKNOWLEDGED"
            encoded = _dump_execution_payload(batch)
            connection.execute(
                "UPDATE n_leg_batches SET state=?, payload=?, updated_at=? WHERE execution_batch_id=?",
                ("INCIDENT_ACKNOWLEDGED", encoded, now, str(execution_batch_id)),
            )
            # Gate release: a batch that held the single-active-batch slot
            # from the control releases it here (step 4).
            connection.execute(
                "UPDATE n_leg_controls SET active_batch_id=NULL, updated_at=? WHERE singleton=1 AND active_batch_id=?",
                (now, str(execution_batch_id)),
            )
            connection.execute(
                "INSERT OR IGNORE INTO n_leg_transitions(transition_id, execution_batch_id, kind, idempotency_key, payload, created_at) VALUES (?, ?, 'INCIDENT_ACKNOWLEDGED', ?, ?, ?)",
                (
                    _new_id(),
                    str(execution_batch_id),
                    f"incident-acknowledge:{execution_batch_id}",
                    _dump_execution_payload(
                        {
                            "batch": str(execution_batch_id),
                            **dict(acknowledgement),
                            "acknowledged_at": now,
                        }
                    ),
                    now,
                ),
            )
        return batch

    def n_leg_breaker_reset(self, *, audit: Mapping[str, object] | None = None) -> dict[str, object]:
        """Issue #64 Slice 6: clear the N_LEG global breaker only when the
        latest incident acknowledgement records a fresh_clean reconciliation
        (mirrors the legacy reset_breaker discipline)."""
        with self._transaction() as connection:
            control = self._n_leg_control_row(
                connection.execute("SELECT * FROM n_leg_controls WHERE singleton=1").fetchone()
            )
            if not control["breaker_open"]:
                return {"state": "ready", "reason": "breaker_not_open"}
            row = connection.execute(
                "SELECT payload FROM n_leg_transitions WHERE kind='INCIDENT_ACKNOWLEDGED' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError("N_LEG_BREAKER_INCIDENT_NOT_ACKNOWLEDGED")
            payload = _load_payload(str(row["payload"]))
            if payload.get("reconciliation") != "fresh_clean":
                raise ValueError("N_LEG_BREAKER_RECONCILIATION_REQUIRED")
            connection.execute(
                "UPDATE n_leg_controls SET breaker_open=0, breaker_reason=NULL, updated_at=? WHERE singleton=1",
                (_utc_now(),),
            )
        return {"state": "ready", "reason": "reset_confirmed"}

    def n_leg_transition_append(
        self,
        execution_batch_id: str,
        *,
        kind: str,
        idempotency_key: str,
        payload: Mapping[str, object],
    ) -> bool:
        """Append one N-leg transition; returns False when the exact
        (batch, idempotency key) pair was already recorded (no double write)."""
        if not isinstance(kind, str) or not kind:
            raise ValueError("invalid n-leg transition kind")
        now = _utc_now()
        with self._transaction() as connection:
            batch = connection.execute(
                "SELECT 1 FROM n_leg_batches WHERE execution_batch_id=?",
                (str(execution_batch_id),),
            ).fetchone()
            if batch is None:
                raise ValueError("N_LEG_BATCH_NOT_FOUND")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO n_leg_transitions(transition_id, execution_batch_id, kind, idempotency_key, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _new_id(),
                    str(execution_batch_id),
                    kind,
                    str(idempotency_key),
                    _dump_execution_payload(dict(payload)),
                    now,
                ),
            )
            return int(cursor.rowcount) > 0

    def n_leg_reduce(
        self,
        execution_batch_id: str,
        *,
        transition_kind: str,
        idempotency_key: str,
        reducer: Callable[[dict[str, object], dict[str, object]], tuple[dict[str, object], dict[str, object], bool]],
    ) -> dict[str, object]:
        """Read, reduce, transition-log, and write one N-leg receipt atomically."""
        if not isinstance(transition_kind, str) or not transition_kind:
            raise ValueError("invalid n-leg transition kind")
        with self._transaction() as connection:
            prior_transition = connection.execute(
                "SELECT payload FROM n_leg_transitions WHERE execution_batch_id=? AND idempotency_key=?",
                (str(execution_batch_id), str(idempotency_key)),
            ).fetchone()
            if prior_transition is not None:
                current = connection.execute(
                    "SELECT payload FROM n_leg_batches WHERE execution_batch_id=?", (str(execution_batch_id),)
                ).fetchone()
                if current is None:
                    raise ValueError("N_LEG_BATCH_NOT_FOUND")
                return _load_payload(str(current["payload"]))
            row = connection.execute(
                "SELECT payload FROM n_leg_batches WHERE execution_batch_id=?", (str(execution_batch_id),)
            ).fetchone()
            if row is None:
                raise ValueError("N_LEG_BATCH_NOT_FOUND")
            batch = _load_payload(str(row["payload"]))
            control = self._n_leg_control_row(
                connection.execute("SELECT * FROM n_leg_controls WHERE singleton=1").fetchone()
            )
            next_batch, next_control, changed = reducer(batch, control)
            if not changed:
                return batch
            encoded = _dump_execution_payload(next_batch)
            now = _utc_now()
            connection.execute(
                "UPDATE n_leg_batches SET state=?, payload=?, updated_at=? WHERE execution_batch_id=?",
                (str(next_batch.get("state")), encoded, now, str(execution_batch_id)),
            )
            connection.execute(
                "INSERT INTO n_leg_controls(singleton, mode, breaker_open, breaker_reason, active_batch_id, total_unsettled_capital_units, contract_generation, qualification_policy_version, safety_config_version, enabled_execution_scope_version, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, breaker_open=excluded.breaker_open, breaker_reason=excluded.breaker_reason, active_batch_id=excluded.active_batch_id, total_unsettled_capital_units=excluded.total_unsettled_capital_units, total_unsettled_capital_version=total_unsettled_capital_version+1, contract_generation=excluded.contract_generation, qualification_policy_version=excluded.qualification_policy_version, safety_config_version=excluded.safety_config_version, enabled_execution_scope_version=excluded.enabled_execution_scope_version, updated_at=excluded.updated_at",
                (
                    next_control["mode"],
                    int(bool(next_control["breaker_open"])),
                    next_control["breaker_reason"],
                    next_control["active_batch_id"],
                    next_control["total_unsettled_capital_units"],
                    int(next_control.get("contract_generation", 1)),
                    int(next_control.get("qualification_policy_version", 1)),
                    int(next_control.get("safety_config_version", 1)),
                    json.dumps(next_control.get("enabled_execution_scope_version", [])),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO n_leg_transitions(transition_id, execution_batch_id, kind, idempotency_key, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (_new_id(), str(execution_batch_id), transition_kind, str(idempotency_key), _dump_execution_payload({"payload": next_batch, "control": next_control}), now),
            )
        return _load_payload(encoded)

    @staticmethod
    def _reserved_cross_principal(connection: sqlite3.Connection) -> Decimal:
        rows = connection.execute(
            "SELECT amount FROM cross_execution_reservations WHERE state='reserved'"
        ).fetchall()
        return sum((Decimal(str(row["amount"])) for row in rows), Decimal("0"))

    @staticmethod
    def _cross_auto_daily_principal_for(
        connection: sqlite3.Connection, now: object
    ) -> Decimal:
        day = _parse_timestamp(now).astimezone(_SHANGHAI).date()
        rows = connection.execute(
            """
            SELECT reservation.amount, reservation.created_at
            FROM cross_execution_reservations AS reservation
            JOIN executions AS execution ON execution.execution_id=reservation.execution_id
            WHERE json_extract(execution.payload, '$.auto_submit') = 1
              AND NOT (reservation.state='released' AND reservation.release_reason='no_submit')
            """
        ).fetchall()
        return sum(
            (
                Decimal(str(row["amount"]))
                for row in rows
                if _parse_timestamp(row["created_at"]).astimezone(_SHANGHAI).date() == day
            ),
            Decimal("0"),
        )

    def cross_auto_daily_principal(self, now: object = None) -> Decimal:
        with self._read_connection() as connection:
            return self._cross_auto_daily_principal_for(
                connection, _utc_now() if now is None else now
            )

    @staticmethod
    def _cross_pair_unsettled(
        connection: sqlite3.Connection, pair_id: object
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1
                FROM cross_execution_reservations AS reservation
                JOIN executions AS execution ON execution.execution_id=reservation.execution_id
                WHERE reservation.state='reserved'
                  AND json_extract(execution.payload, '$.pair_id') = ?
                LIMIT 1
                """,
                (str(pair_id),),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _cross_reservation_amount(payload: Mapping[str, object]) -> Decimal:
        value = payload.get("total_max_cost")
        if isinstance(value, bool):
            raise ValueError("cross_unsettled_cost_invalid")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("cross_unsettled_cost_invalid") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("cross_unsettled_cost_invalid")
        return amount

    def cross_unsettled_principal(self) -> Decimal:
        with self._read_connection() as connection:
            return self._reserved_cross_principal(connection)

    @staticmethod
    def _nonempty_string(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _valid_decimal(
        value: object, *, allow_zero: bool = False, allow_negative: bool = False
    ) -> bool:
        if isinstance(value, bool):
            return False
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return False
        if not amount.is_finite():
            return False
        if allow_negative:
            return True
        if allow_zero:
            return amount >= 0
        return amount > 0

    @staticmethod
    def _valid_timestamp(value: object) -> bool:
        try:
            _parse_timestamp(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _decimal_values_match(left: object, right: object) -> bool:
        if isinstance(left, bool) or isinstance(right, bool):
            return False
        try:
            left_value = Decimal(str(left))
            right_value = Decimal(str(right))
        except (InvalidOperation, ValueError):
            return False
        return left_value.is_finite() and right_value.is_finite() and left_value == right_value

    @classmethod
    def _valid_cross_preview_candidate(cls, candidate: object) -> bool:
        return isinstance(candidate, Mapping) and all(
            cls._nonempty_string(candidate.get(field))
            for field in (
                "market_id",
                "condition_id",
                "yes_token_id",
                "no_token_id",
                "rules_fingerprint",
            )
        )

    @classmethod
    def _valid_cross_preview_leg(
        cls, leg: object, *, exchange: str, outcome: str
    ) -> bool:
        return (
            isinstance(leg, Mapping)
            and leg.get("exchange") == exchange
            and leg.get("outcome") == outcome
            and all(
                cls._nonempty_string(leg.get(field))
                for field in (
                    "market_id",
                    "condition_id",
                    "token_id",
                    "settlement_asset",
                    "fee_asset",
                )
            )
            and all(
                cls._valid_decimal(leg.get(field))
                for field in (
                    "requested_quantity",
                    "net_quantity",
                    "max_price",
                    "max_cost",
                )
            )
            and cls._valid_decimal(leg.get("maximum_fee"), allow_zero=True)
            and cls._valid_decimal(leg.get("minimum_order_size"), allow_zero=True)
            and cls._valid_timestamp(leg.get("book_timestamp"))
            and "settlement_at" in leg
            and (
                leg.get("settlement_at") is None
                or cls._valid_timestamp(leg.get("settlement_at"))
            )
        )

    @classmethod
    def _valid_cross_preview_payload(
        cls, payload: Mapping[str, object]
    ) -> bool:
        intent = payload.get("intent")
        if not (
            payload.get("market_type") == "cross_venue_yes_no"
            and all(
                cls._nonempty_string(payload.get(field))
                for field in (
                    "opportunity_id",
                    "execution_id",
                    "signal_episode_id",
                    "pair_id",
                    "direction",
                    "canonical_cutoff",
                )
            )
            and cls._valid_decimal(payload.get("total_max_cost"))
            and cls._valid_decimal(payload.get("minimum_payout"))
            and cls._valid_decimal(payload.get("minimum_profit"))
            and cls._valid_decimal(payload.get("annualized_yield"))
        ):
            return False
        if not cls._valid_timestamp(payload.get("canonical_cutoff")):
            return False
        if not isinstance(intent, Mapping) or intent.get("intent_type") != "cross_venue":
            return False
        if not (
            intent.get("pair_id") == payload.get("pair_id")
            and intent.get("direction") == payload.get("direction")
            and intent.get("canonical_cutoff") == payload.get("canonical_cutoff")
            and cls._valid_decimal(intent.get("quantity"))
            and cls._valid_decimal(intent.get("calculable_gas"), allow_zero=True)
            and cls._valid_decimal(intent.get("total_max_cost"))
            and cls._valid_decimal(intent.get("maximum_fee"), allow_zero=True)
            and cls._valid_decimal(intent.get("minimum_payout"))
            and cls._valid_decimal(intent.get("minimum_profit"))
            and cls._valid_decimal(intent.get("annualized_yield"))
            and cls._valid_timestamp(intent.get("canonical_cutoff"))
            and cls._valid_timestamp(intent.get("resolution_at"))
            and intent.get("actionable") is True
            and intent.get("quote_available") is True
            and cls._decimal_values_match(
                payload.get("total_max_cost"), intent.get("total_max_cost")
            )
            and cls._decimal_values_match(
                payload.get("minimum_payout"), intent.get("minimum_payout")
            )
            and cls._decimal_values_match(
                payload.get("minimum_profit"), intent.get("minimum_profit")
            )
            and cls._decimal_values_match(
                payload.get("annualized_yield"), intent.get("annualized_yield")
            )
        ):
            return False
        legs = intent.get("legs")
        if not isinstance(legs, list) or len(legs) != 2:
            return False
        if not (
            cls._valid_cross_preview_leg(legs[0], exchange="predict.fun", outcome="YES")
            and cls._valid_cross_preview_leg(legs[1], exchange="polymarket", outcome="NO")
        ) and not (
            cls._valid_cross_preview_leg(legs[0], exchange="predict.fun", outcome="NO")
            and cls._valid_cross_preview_leg(legs[1], exchange="polymarket", outcome="YES")
        ):
            return False
        rules_fingerprints = payload.get("rules_fingerprints")
        if not (
            isinstance(rules_fingerprints, Mapping)
            and all(
                cls._nonempty_string(rules_fingerprints.get(exchange))
                for exchange in ("predict.fun", "polymarket")
            )
        ):
            return False
        approved_candidates = payload.get("approved_candidates")
        if not (
            isinstance(approved_candidates, Mapping)
            and cls._valid_cross_preview_candidate(approved_candidates.get("predict.fun"))
            and cls._valid_cross_preview_candidate(approved_candidates.get("polymarket"))
        ):
            return False
        if any(
            approved_candidates[exchange].get("rules_fingerprint")
            != rules_fingerprints.get(exchange)
            for exchange in ("predict.fun", "polymarket")
        ):
            return False
        approval = payload.get("codex_approval")
        return bool(
            isinstance(approval, Mapping)
            and approval.get("decision") == "APPROVE"
            and cls._nonempty_string(approval.get("cache_key"))
            and isinstance(approval.get("direct_outcome_mapping"), Mapping)
            and isinstance(approval.get("evidence"), list)
            and approval.get("evidence")
        )

    @staticmethod
    def _has_zero_cross_positions(evidence: Mapping[str, object]) -> bool:
        positions = evidence.get("positions")
        if not isinstance(positions, Mapping):
            return False
        for venue in ("predict.fun", "polymarket"):
            value = positions.get(venue)
            if isinstance(value, bool):
                return False
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, ValueError):
                return False
            if not amount.is_finite() or amount != 0:
                return False
        return True

    @staticmethod
    def _winner_matches_cross_payload(
        winner: Mapping[str, object], payload: Mapping[str, object]
    ) -> bool:
        intent = payload.get("intent")
        if not isinstance(intent, Mapping) or intent.get("intent_type") != "cross_venue":
            return False
        venue = winner.get("venue")
        condition_id = winner.get("condition_id")
        outcome = winner.get("outcome")
        token_id = winner.get("token_id")
        quantity = winner.get("quantity")
        if (
            venue not in {"predict.fun", "polymarket"}
            or not all(isinstance(value, str) and value for value in (condition_id, outcome, token_id))
            or outcome not in {"YES", "NO"}
        ):
            return False
        try:
            amount = Decimal(str(quantity))
        except (InvalidOperation, ValueError):
            return False
        if not amount.is_finite() or amount <= 0:
            return False
        legs = intent.get("legs")
        if not isinstance(legs, list):
            return False
        matches = 0
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            if (
                leg.get("exchange") != venue
                or leg.get("condition_id") != condition_id
                or leg.get("outcome") != outcome
                or leg.get("token_id") != token_id
            ):
                continue
            try:
                maximum = Decimal(str(leg.get("net_quantity")))
            except (InvalidOperation, ValueError):
                continue
            if maximum.is_finite() and amount == maximum:
                matches += 1
        return matches == 1

    @classmethod
    def _has_observed_redeemed_collateral(
        cls,
        evidence: Mapping[str, object],
        payload: Mapping[str, object],
        settlement_baseline: Mapping[str, object],
    ) -> bool:
        redemption = evidence.get("redemption")
        if not isinstance(redemption, Mapping) or redemption.get("observed") is not True:
            return False
        winner = redemption.get("winner")
        if not isinstance(winner, Mapping) or not cls._winner_matches_cross_payload(winner, payload):
            return False
        collateral = redemption.get("redeemed_collateral")
        completed_baseline = evidence.get("settlement_baseline")
        if not isinstance(collateral, Mapping) or not isinstance(completed_baseline, Mapping):
            return False
        venue = str(winner["venue"])
        try:
            amount = Decimal(str(collateral.get(venue)))
            required = Decimal(str(winner["quantity"]))
            prior = Decimal(str(settlement_baseline.get(venue)))
            recorded = Decimal(str(completed_baseline.get(venue)))
        except (InvalidOperation, ValueError):
            return False
        return (
            amount.is_finite()
            and required.is_finite()
            and prior.is_finite()
            and recorded.is_finite()
            and prior >= 0
            and recorded == prior
            and amount >= required > 0
        )

    @staticmethod
    def _post_fill_settlement_baseline(evidence: list[object]) -> Mapping[str, object] | None:
        for item in reversed(evidence):
            if not isinstance(item, Mapping) or item.get("phase") != "holding_to_resolution":
                continue
            baseline = item.get("settlement_baseline")
            if not isinstance(baseline, Mapping):
                continue
            try:
                values = {
                    venue: Decimal(str(baseline.get(venue)))
                    for venue in ("polymarket", "predict.fun")
                }
            except (InvalidOperation, ValueError):
                continue
            if all(value.is_finite() and value >= 0 for value in values.values()):
                return baseline
        return None

    @classmethod
    def _cross_release_is_proven(
        cls, *, state: object, evidence: object, payload: Mapping[str, object], reason: str
    ) -> bool:
        if not isinstance(evidence, list):
            return False
        settlement_baseline = cls._post_fill_settlement_baseline(evidence)
        for item in reversed(evidence):
            if not isinstance(item, Mapping) or not cls._has_zero_cross_positions(item):
                continue
            if reason == "no_submit":
                if state == "both_rejected" and item.get("submitted") is False:
                    return True
            elif reason == "both_rejected":
                if state == "both_rejected" and item.get("no_position_observed") is True:
                    return True
            elif reason == "redeemed":
                if (
                    state == "complete"
                    and settlement_baseline is not None
                    and cls._has_observed_redeemed_collateral(
                        item, payload, settlement_baseline
                    )
                ):
                    return True
        return False

    def release_cross_reservation(self, execution_id: str, *, reason: str) -> None:
        if reason not in {"no_submit", "both_rejected", "redeemed"}:
            raise ValueError("unsupported cross reservation release reason")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT reservation.state AS reservation_state, execution.state, execution.evidence, execution.payload
                FROM cross_execution_reservations AS reservation
                JOIN executions AS execution ON execution.execution_id=reservation.execution_id
                WHERE reservation.execution_id=?
                """,
                (str(execution_id),),
            ).fetchone()
            if row is None or row["reservation_state"] == "released":
                return
            if not self._cross_release_is_proven(
                state=row["state"], evidence=json.loads(str(row["evidence"])),
                payload=_load_payload(str(row["payload"])), reason=reason,
            ):
                raise ValueError("cross reservation release proof missing")
            connection.execute(
                """
                UPDATE cross_execution_reservations
                SET state='released', released_at=?, release_reason=?
                WHERE execution_id=? AND state='reserved'
                """,
                (_utc_now(), reason, str(execution_id)),
            )

    def release_proven_cross_completions(self) -> tuple[str, ...]:
        """Recover only terminal cross reservations whose stored redemption proof is complete."""

        released: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT reservation.execution_id, execution.state, execution.evidence, execution.payload
                FROM cross_execution_reservations AS reservation
                JOIN executions AS execution ON execution.execution_id=reservation.execution_id
                WHERE reservation.state='reserved' AND execution.state='complete'
                """
            ).fetchall()
            for row in rows:
                if not self._cross_release_is_proven(
                    state=row["state"], evidence=json.loads(str(row["evidence"])),
                    payload=_load_payload(str(row["payload"])), reason="redeemed",
                ):
                    continue
                execution_id = str(row["execution_id"])
                updated = connection.execute(
                    """
                    UPDATE cross_execution_reservations
                    SET state='released', released_at=?, release_reason='redeemed'
                    WHERE execution_id=? AND state='reserved'
                    """,
                    (_utc_now(), execution_id),
                )
                if updated.rowcount == 1:
                    released.append(execution_id)
        return tuple(released)

    def consume_preview_and_create_execution(
        self, preview_id: str, idempotency_key: str
    ) -> dict[str, object]:
        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("idempotency_key is required")
        now = _parse_timestamp(_utc_now())
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM executions WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            if existing is not None:
                return self._execution_result(existing)
            existing = connection.execute(
                "SELECT * FROM executions WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                return self._execution_result(existing)
            preview = connection.execute(
                "SELECT * FROM previews WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            if preview is None:
                raise ValueError("preview_not_found")
            if preview["consumed_at"] is not None:
                raise ValueError("preview_consumed")
            payload = str(preview["payload"])
            preview_payload = _load_payload(payload)
            cross_amount: Decimal | None = None
            auto_claim: dict[str, str] | None = None
            if preview_payload.get("market_type") == "cross_venue_yes_no":
                if not self._valid_cross_preview_payload(preview_payload):
                    if now >= _parse_timestamp(preview["expires_at"]):
                        raise ValueError("preview_expired")
                    raise ValueError("cross_preview_invalid")
                cross_amount = self._cross_reservation_amount(preview_payload)
                if preview_payload.get("auto_submit") is True:
                    auto_claim = self._claim_cross_auto_attempt(
                        connection,
                        signal=key,
                        opportunity=str(preview_payload["opportunity_id"]),
                        now=_canonical_timestamp(now),
                    )
                    if auto_claim["state"] != "claimed":
                        return auto_claim
                if self._cross_pair_unsettled(connection, preview_payload["pair_id"]):
                    if auto_claim is not None:
                        return {"state": "rejected", "reason": "cross_pair_unsettled"}
                    raise ValueError("cross_pair_unsettled")
                if preview_payload.get("auto_submit") is True:
                    if (
                        self._cross_auto_daily_principal_for(connection, now) + cross_amount
                        > _CROSS_AUTO_DAILY_PRINCIPAL_CAP
                    ):
                        if auto_claim is not None:
                            return {
                                "state": "rejected",
                                "reason": "cross_auto_daily_principal_cap",
                            }
                        raise ValueError("cross_auto_daily_principal_cap")
                if (
                    self._reserved_cross_principal(connection) + cross_amount
                    > MAX_CROSS_UNSETTLED_PRINCIPAL
                ):
                    if auto_claim is not None:
                        return {"state": "rejected", "reason": "cross_unsettled_cap"}
                    raise ValueError("cross_unsettled_cap")
            elif now >= _parse_timestamp(preview["expires_at"]):
                raise ValueError("preview_expired")
            execution_id = (
                str(preview_payload["execution_id"])
                if cross_amount is not None
                and isinstance(preview_payload.get("execution_id"), str)
                and preview_payload["execution_id"].strip()
                else _new_id()
            )
            created = _canonical_timestamp(now)
            try:
                connection.execute(
                    """
                    INSERT INTO executions(
                        execution_id, preview_id, idempotency_key, singleton,
                        state, payload, evidence, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 'validating', ?, '[]', ?, ?)
                    """,
                    (execution_id, preview_id, key, payload, created, created),
                )
            except sqlite3.IntegrityError as exc:
                if "one_nonterminal_execution" in str(exc):
                    raise ValueError("active execution already exists") from exc
                raise
            if cross_amount is not None:
                connection.execute(
                    """
                    INSERT INTO cross_execution_reservations(
                        execution_id, amount, state, created_at, released_at, release_reason
                    ) VALUES (?, ?, 'reserved', ?, NULL, NULL)
                    """,
                    (execution_id, format(cross_amount, "f"), created),
                )
            connection.execute(
                "UPDATE previews SET consumed_at=? WHERE preview_id=? AND consumed_at IS NULL",
                (created, preview_id),
            )
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            assert row is not None
            return self._execution_result(row)

    def create_recovery_execution(
        self, payload: Mapping[str, object], *, idempotency_key: str
    ) -> dict[str, object]:
        """Persist a terminal, non-trading execution for external recovery state."""

        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("recovery idempotency_key is required")
        encoded = _dump_execution_payload(payload)
        now = _parse_timestamp(_utc_now())
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM executions WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                return self._execution_result(existing)
            preview_id = _new_id()
            execution_id = _new_id()
            created = _canonical_timestamp(now)
            connection.execute(
                "INSERT INTO previews(preview_id, payload, created_at, expires_at, consumed_at) VALUES (?, ?, ?, ?, ?)",
                (preview_id, encoded, created, created, created),
            )
            evidence = json.dumps(
                [_load_payload(encoded)],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO executions(
                    execution_id, preview_id, idempotency_key, singleton,
                    state, payload, evidence, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 'directional_incident', ?, ?, ?, ?)
                """,
                (execution_id, preview_id, key, encoded, evidence, created, created),
            )
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            assert row is not None
            return self._execution_result(row)

    def transition_execution(
        self, execution_id: str, *, state: str, evidence: Mapping[str, object]
    ) -> None:
        encoded_evidence = _dump_execution_payload(evidence)
        evidence_value = _load_payload(encoded_evidence)
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT evidence FROM executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            previous = json.loads(str(row["evidence"]))
            if not isinstance(previous, list):
                previous = []
            previous.append(evidence_value)
            # Keep evidence physically ahead of the state write in the same
            # transaction: a transition can never be observed without its fact.
            connection.execute(
                "UPDATE executions SET evidence=? WHERE execution_id=?",
                (
                    json.dumps(previous, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    execution_id,
                ),
            )
            connection.execute(
                "UPDATE executions SET state=?, updated_at=? WHERE execution_id=?",
                (str(state), now, execution_id),
            )

    def record_leg(self, execution_id: str, payload: Mapping[str, object]) -> None:
        encoded = _dump_execution_payload(payload)
        clean = _load_payload(encoded)
        label = str(
            clean.get(
                "label",
                clean.get("leg", clean.get("local_leg", clean.get("leg_label", ""))),
            )
        ).strip()
        if not label:
            raise ValueError("leg label is required")
        with self._transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO execution_legs(leg_id, execution_id, leg_label, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (f"{execution_id}:{label}", execution_id, label, encoded, _utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" in str(exc):
                    raise ValueError("leg label already exists") from exc
                raise

    def open_incident(self, execution_id: str, payload: Mapping[str, object]) -> str:
        encoded = _dump_execution_payload(payload)
        incident_id = _new_id()
        now = _utc_now()
        with self._transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO incidents(incident_id, execution_id, payload, acknowledgement, acknowledged_at, created_at, updated_at) VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                    (incident_id, execution_id, encoded, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("execution does not exist") from exc
        return incident_id

    def acknowledge_incident(self, incident_id: str, payload: Mapping[str, object]) -> None:
        encoded = _dump_execution_payload(payload)
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT incident_id FROM incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
            if row is None:
                raise KeyError(incident_id)
            connection.execute(
                "UPDATE incidents SET acknowledgement=?, acknowledged_at=?, updated_at=? WHERE incident_id=?",
                (encoded, now, now, incident_id),
            )

    def update_incident(self, incident_id: str, payload: Mapping[str, object]) -> None:
        """Append final incident facts without erasing its original evidence."""

        encoded = _dump_execution_payload(payload)
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
            if row is None:
                raise KeyError(incident_id)
            previous = _load_payload(str(row["payload"]))
            previous.update(_load_payload(encoded))
            connection.execute(
                "UPDATE incidents SET payload=?, updated_at=? WHERE incident_id=?",
                (_dump_execution_payload(previous), now, incident_id),
            )

    def active_execution(self) -> dict[str, object] | None:
        placeholders = ",".join("?" for _ in _TERMINAL_EXECUTION_STATES)
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT * FROM executions WHERE state NOT IN ({placeholders}) ORDER BY created_at DESC, execution_id DESC LIMIT 1",
                _TERMINAL_EXECUTION_STATES,
            ).fetchone()
        return None if row is None else self._execution_result(row)

    def unacknowledged_incident(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE acknowledged_at IS NULL ORDER BY created_at DESC, incident_id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._incident_result(row)

    def histories(self, kind: StoreHistoryKind) -> list[dict[str, object]]:
        if kind == "signals":
            return self.signal_history("all")
        if kind == "executions":
            with self._read_connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM executions ORDER BY created_at DESC, execution_id DESC"
                ).fetchall()
            return [self._execution_result(row) for row in rows]
        if kind == "incidents":
            with self._read_connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM incidents ORDER BY created_at DESC, incident_id DESC"
                ).fetchall()
            return [self._incident_result(row) for row in rows]
        raise ValueError("kind must be signals, executions, or incidents")
