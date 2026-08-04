"""Small, durable SQLite store for the prediction-market execution boundary."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

from open_trader.prediction_arbitrage import MAX_CROSS_UNSETTLED_PRINCIPAL


StoreHistoryKind = Literal["signals", "executions", "incidents"]
SignalHistoryWindow = Literal["24h", "7d", "30d", "all"]

_BUSY_TIMEOUT_MS = 5_000
_PREVIEW_TTL = timedelta(seconds=10)
_TERMINAL_EXECUTION_STATES = (
    "both_rejected",
    "complete",
    "holding_to_resolution",
    "neutralized_incident",
    "directional_incident",
    "merge_incident",
)

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


class PredictionArbitrageStore:
    """Direct sqlite3 persistence with one short-lived connection per action."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._read_connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            self._create_schema(connection)

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
        max_attempts: int = 3,
        lease_seconds: float = 60.0,
        order_ready_at: str | None = None,
    ) -> dict[str, object]:
        """Atomically reserve one open signal notification attempt."""

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
            state = str(payload.get("notification_state", "pending"))
            if state == "sent":
                return {"state": "sent", "signal_id": str(signal_id)}
            current_lease = payload.get("notification_lease_expires_at")
            lease_active = False
            if current_lease not in (None, ""):
                try:
                    lease_active = _parse_timestamp(current_lease) > now
                except ValueError:
                    lease_active = False
            if lease_active:
                return {"state": "in_flight", "signal_id": str(signal_id)}
            try:
                attempts = int(payload.get("notification_attempts", 0) or 0)
            except (TypeError, ValueError):
                attempts = 0
            if attempts >= max_attempts:
                return {
                    "state": "exhausted",
                    "signal_id": str(signal_id),
                    "notification_attempts": attempts,
                }
            payload.update(
                {
                    "notification_state": "pending",
                    "notification_attempts": attempts + 1,
                    "notification_lease_id": lease_id,
                    "notification_lease_expires_at": lease_expires,
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
                "notification_attempts": attempts + 1,
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
        success: bool,
        error_code: str = "delivery_failed",
    ) -> dict[str, object]:
        """Persist a reserved attempt's final pending/sent/failed state."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE signal_id=?", (str(signal_id),)
            ).fetchone()
            if row is None:
                return {"state": "missing", "signal_id": str(signal_id)}
            payload = _load_payload(str(row["payload"]))
            if payload.get("notification_lease_id") != str(lease_id):
                return {"state": "stale", "signal_id": str(signal_id)}
            if row["ended_at"] is not None:
                return {"state": "closed", "signal_id": str(signal_id)}
            payload["notification_state"] = "sent" if success else "failed"
            payload.pop("notification_lease_id", None)
            payload.pop("notification_lease_expires_at", None)
            if success:
                payload["notification_sent_at"] = _utc_now()
                payload.pop("notification_error_code", None)
            else:
                payload["notification_error_code"] = str(error_code)
            updated_at = _utc_now()
            connection.execute(
                "UPDATE signals SET payload=?, updated_at=? WHERE signal_id=?",
                (_dump_relation_payload(payload), updated_at, str(signal_id)),
            )
            return {
                "state": payload["notification_state"],
                "signal_id": str(signal_id),
                "notification_attempts": payload.get("notification_attempts", 0),
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
            rows = connection.execute(
                "SELECT * FROM signals ORDER BY started_at DESC, signal_id DESC"
            ).fetchall()
        cutoff: datetime | None = None
        if window != "all":
            deltas = {
                "24h": timedelta(hours=24),
                "7d": timedelta(days=7),
                "30d": timedelta(days=30),
            }
            delta = deltas[window]
            cutoff = _parse_timestamp(_utc_now()) - delta
        result = []
        for row in rows:
            started = _parse_timestamp(row["started_at"])
            if cutoff is not None and started < cutoff:
                continue
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
    ) -> bool:
        """Return whether this market has a successful delivery at or after since."""

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
            sent_at = payload.get("notification_sent_at")
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

    def record_llm_cache_hit(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO llm_usage(
                    usage_id, kind, status, payload, created_at
                ) VALUES (?, 'cache_hit', 'success', '{}', ?)
                """,
                (_new_id(), _utc_now()),
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
        return {
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

    @staticmethod
    def _reserved_cross_principal(connection: sqlite3.Connection) -> Decimal:
        rows = connection.execute(
            "SELECT amount FROM cross_execution_reservations WHERE state='reserved'"
        ).fetchall()
        return sum((Decimal(str(row["amount"])) for row in rows), Decimal("0"))

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
            if preview_payload.get("market_type") == "cross_venue_yes_no":
                if not self._valid_cross_preview_payload(preview_payload):
                    if now >= _parse_timestamp(preview["expires_at"]):
                        raise ValueError("preview_expired")
                    raise ValueError("cross_preview_invalid")
                cross_amount = self._cross_reservation_amount(preview_payload)
                if (
                    self._reserved_cross_principal(connection) + cross_amount
                    > MAX_CROSS_UNSETTLED_PRINCIPAL
                ):
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
