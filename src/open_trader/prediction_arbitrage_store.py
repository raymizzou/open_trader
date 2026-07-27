"""Small, durable SQLite store for the prediction-market execution boundary."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping


StoreHistoryKind = Literal["signals", "executions", "incidents"]
SignalHistoryWindow = Literal["24h", "7d", "all"]

_BUSY_TIMEOUT_MS = 5_000
_PREVIEW_TTL = timedelta(seconds=10)
_TERMINAL_EXECUTION_STATES = (
    "both_rejected",
    "complete",
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


def _safe_value(value: Any, *, key: str | None = None) -> Any:
    """Return JSON-safe data while dropping credential/tick-shaped fields."""

    if key is not None:
        normalized = _normalise_field_name(key)
        sensitive_name = normalized in {"token", "auth", "authorization", "bearer"}
        if sensitive_name or any(part in normalized for part in _PRIVATE_FIELD_PARTS):
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
            child = _safe_value(raw_value, key=child_key)
            if child is not _DROPPED:
                cleaned[child_key] = child
        return cleaned
    if isinstance(value, (list, tuple)):
        cleaned_list = []
        for item in value:
            child = _safe_value(item)
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
        connection.execute("PRAGMA journal_mode=WAL")
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

            CREATE UNIQUE INDEX IF NOT EXISTS one_open_signal_per_market
            ON signals(market_id) WHERE ended_at IS NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS one_nonterminal_execution
            ON executions(singleton)
            WHERE state NOT IN (
                'both_rejected', 'complete', 'neutralized_incident',
                'directional_incident', 'merge_incident'
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_execution_per_idempotency_key
            ON executions(idempotency_key);
            """
        )
        if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
            connection.execute("PRAGMA user_version=1")

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
        encoded = _dump_payload(payload)
        clean = _load_payload(encoded)
        market_id = str(clean.get("market_id", "")).strip()
        if not market_id:
            raise ValueError("signal market_id is required")
        started_at = self._signal_time(clean)
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
                previous.update(clean)
                connection.execute(
                    "UPDATE signals SET payload=?, updated_at=? WHERE signal_id=?",
                    (_dump_payload(previous), now, str(row["signal_id"])),
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

    def close_signal(self, market_id: str, *, ended_at: str, reason: str) -> None:
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
            payload["ended_at"] = ended
            payload["ended_reason"] = str(reason)
            connection.execute(
                "UPDATE signals SET payload=?, ended_at=?, updated_at=? WHERE signal_id=?",
                (_dump_payload(payload), ended, now, str(row["signal_id"])),
            )

    def signal_history(self, window: SignalHistoryWindow) -> list[dict[str, object]]:
        if window not in {"24h", "7d", "all"}:
            raise ValueError("window must be 24h, 7d, or all")
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM signals ORDER BY started_at DESC, signal_id DESC"
            ).fetchall()
        cutoff: datetime | None = None
        if window != "all":
            delta = timedelta(hours=24) if window == "24h" else timedelta(days=7)
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

    def create_preview(self, payload: Mapping[str, object], *, expires_at: str) -> str:
        encoded = _dump_payload(payload)
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

    def consume_preview_and_create_execution(
        self, preview_id: str, idempotency_key: str
    ) -> dict[str, object]:
        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("idempotency_key is required")
        now = _parse_timestamp(_utc_now())
        with self._transaction() as connection:
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
            if now >= _parse_timestamp(preview["expires_at"]):
                raise ValueError("preview_expired")
            execution_id = _new_id()
            created = _canonical_timestamp(now)
            payload = str(preview["payload"])
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
            connection.execute(
                "UPDATE previews SET consumed_at=? WHERE preview_id=? AND consumed_at IS NULL",
                (created, preview_id),
            )
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            assert row is not None
            return self._execution_result(row)

    def transition_execution(
        self, execution_id: str, *, state: str, evidence: Mapping[str, object]
    ) -> None:
        encoded_evidence = _dump_payload(evidence)
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
        encoded = _dump_payload(payload)
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
        encoded = _dump_payload(payload)
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
        encoded = _dump_payload(payload)
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
