"""Small, durable SQLite store for the prediction-market execution boundary."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping
from zoneinfo import ZoneInfo

from open_trader.prediction_arbitrage import MAX_CROSS_UNSETTLED_PRINCIPAL


StoreHistoryKind = Literal["signals", "executions", "incidents"]
SignalHistoryWindow = Literal["24h", "7d", "30d", "all"]

_BUSY_TIMEOUT_MS = 5_000
_LLM_USAGE_RETENTION = timedelta(days=7)
_PREVIEW_TTL = timedelta(seconds=10)
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
        if (
            (token_name and not (allow_public_token_ids and public_token))
            or sensitive_name
            or any(part in normalized for part in _PRIVATE_FIELD_PARTS)
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


class PredictionArbitrageStore:
    """Direct sqlite3 persistence with one short-lived connection per action."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._read_connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            self._create_schema(connection)
        self._cache_hits: dict[str, int] = {}
        self._cache_hits_lock = threading.Lock()
        self.prune_llm_usage()

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
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime (
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
                'directional_incident', 'merge_incident'
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

    def write_runtime(self, payload: Mapping[str, object]) -> None:
        now = _utc_now()
        encoded = _dump_payload(payload)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime(singleton, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (encoded, now),
            )

    def load_runtime(self) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM runtime WHERE singleton=1"
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

    @staticmethod
    def _llm_usage_payload(
        usage: Mapping[str, object],
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
        return payload

    def record_llm_call(
        self, *, status: str, usage: Mapping[str, object]
    ) -> None:
        if status not in {"success", "failed"}:
            raise ValueError("unsupported llm call status")
        payload = self._llm_usage_payload(usage)
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
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.reasoning_output_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS reasoning_output_tokens
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
                    COALESCE(SUM(CASE WHEN kind='call' THEN CAST(COALESCE(json_extract(payload, '$.reasoning_output_tokens'), 0) AS INTEGER) ELSE 0 END), 0) AS reasoning_output_tokens
                FROM llm_usage
                WHERE created_at >= ?
                GROUP BY provider
                ORDER BY provider
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
        result = {
            str(row["provider"]): {field: int(row[field]) for field in fields}
            for row in rows
        }
        _, memory_hits = self._cache_hit_snapshot()
        for provider, count in memory_hits.items():
            counts = result.setdefault(provider, {field: 0 for field in fields})
            counts["cache_hits"] = count
        return result

    def create_preview(self, payload: Mapping[str, object], *, expires_at: str) -> str:
        encoded = _dump_execution_payload(payload)
        created = _parse_timestamp(_utc_now())
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

    def n_leg_create_batch(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Atomically claim a lineage and the single active N-leg batch."""
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
            control = self._n_leg_control_row(
                connection.execute("SELECT * FROM n_leg_controls WHERE singleton=1").fetchone()
            )
            if control["breaker_open"]:
                raise ValueError("N_LEG_BREAKER_OPEN")
            if control["active_batch_id"] is not None:
                raise ValueError("N_LEG_ACTIVE_BATCH_EXISTS")
            if connection.execute(
                "SELECT 1 FROM n_leg_lineage_claims WHERE episode_lineage_id=?", (lineage_id,)
            ).fetchone() is not None:
                raise ValueError("N_LEG_LINEAGE_ALREADY_CLAIMED")
            stored_payload = dict(payload)
            stored_payload["prior_unsettled_capital_units"] = int(control["total_unsettled_capital_units"])
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
                "INSERT INTO n_leg_controls(singleton, mode, breaker_open, breaker_reason, active_batch_id, total_unsettled_capital_units, contract_generation, qualification_policy_version, safety_config_version, enabled_execution_scope_version, updated_at) VALUES (1, ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, active_batch_id=excluded.active_batch_id, total_unsettled_capital_units=excluded.total_unsettled_capital_units, contract_generation=excluded.contract_generation, qualification_policy_version=excluded.qualification_policy_version, safety_config_version=excluded.safety_config_version, enabled_execution_scope_version=excluded.enabled_execution_scope_version, updated_at=excluded.updated_at",
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
                "INSERT INTO n_leg_controls(singleton, mode, breaker_open, breaker_reason, active_batch_id, total_unsettled_capital_units, contract_generation, qualification_policy_version, safety_config_version, enabled_execution_scope_version, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, breaker_open=excluded.breaker_open, breaker_reason=excluded.breaker_reason, active_batch_id=excluded.active_batch_id, total_unsettled_capital_units=excluded.total_unsettled_capital_units, contract_generation=excluded.contract_generation, qualification_policy_version=excluded.qualification_policy_version, safety_config_version=excluded.safety_config_version, enabled_execution_scope_version=excluded.enabled_execution_scope_version, updated_at=excluded.updated_at",
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
