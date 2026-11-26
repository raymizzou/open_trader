"""Private SQLite ledger for confirmed Kelly round evidence."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

_APPLICATION_ID = 0x4B4C4447
_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE rounds (
    round_id TEXT PRIMARY KEY NOT NULL,
    account_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    source_group TEXT NOT NULL,
    report_ref TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    currency TEXT NOT NULL,
    net_pnl TEXT,
    return_base TEXT
) STRICT;
CREATE UNIQUE INDEX one_open_position
ON rounds (account_key, environment, instrument_key) WHERE closed_at IS NULL;

CREATE TABLE trade_events (
    event_key TEXT PRIMARY KEY NOT NULL,
    round_id TEXT NOT NULL REFERENCES rounds (round_id),
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    quantity TEXT,
    notional TEXT,
    cash_adjustment TEXT,
    receipt_ref TEXT NOT NULL
) STRICT;
CREATE INDEX events_by_round ON trade_events (round_id);
"""
_SCHEMA_DEFINITIONS = frozenset(
    sql.strip() for sql in _SCHEMA.split(";") if sql.strip()
)
_ISO_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_IDENTITY_COLUMNS = """
account_key, environment, instrument_key, direction, source_group, report_ref, currency
"""


def _required(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")


@dataclass(frozen=True)
class RoundIdentity:
    """Immutable attribution from the verified opening evidence."""

    account_key: str
    environment: str
    instrument_key: str
    direction: str
    source_group: str
    report_ref: str
    currency: str

    def __post_init__(self) -> None:
        for field, value in vars(self).items():
            _required(value, field)
        if self.environment not in {"SIMULATE", "REAL"}:
            raise ValueError("environment must be SIMULATE or REAL")
        if self.direction not in {"long", "short"}:
            raise ValueError("direction must be long or short")

    @property
    def entry_side(self) -> str:
        return "buy" if self.direction == "long" else "sell"


@dataclass(frozen=True)
class Fill:
    """One verified fill with an ISO timestamp and exact quantity and notional."""

    event_key: str
    occurred_at: str
    side: str
    quantity: str | Decimal
    notional: str | Decimal
    receipt_ref: str


def _amount(value: str | Decimal) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("use exact decimal strings or Decimal amounts")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal amount") from exc
    if (
        not number.is_finite()
        or number.as_tuple().exponent < -18
        or number.adjusted() > 17
    ):
        raise ValueError("amount must fit 18 whole and 18 fractional decimal digits")
    return number


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or _ISO_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("occurred_at requires an ISO timestamp with UTC offset")
    return (
        datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="microseconds")
    )


@dataclass(frozen=True)
class _Event:
    event_key: str
    occurred_at: str
    event_type: str
    quantity: str | None
    notional: str | None
    cash_adjustment: str | None
    receipt_ref: str

    def __post_init__(self) -> None:
        _required(self.event_key, "event_key")
        _required(self.receipt_ref, "receipt_ref")

    def replay_round(self, db: sqlite3.Connection) -> str | None:
        previous = db.execute(
            "SELECT * FROM trade_events WHERE event_key = ?",
            (self.event_key,),
        ).fetchone()
        if previous is None:
            return None
        if any(previous[field] != value for field, value in vars(self).items()):
            raise ValueError("event key conflict")
        return previous["round_id"]

    def insert(self, db: sqlite3.Connection, round_id: str) -> None:
        db.execute(
            """
            INSERT INTO trade_events (
                round_id, event_key, occurred_at, event_type, quantity, notional,
                cash_adjustment, receipt_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (round_id, *astuple(self)),
        )


def _require_identity(round_row: sqlite3.Row, identity: RoundIdentity) -> None:
    if any(round_row[field] != value for field, value in vars(identity).items()):
        raise ValueError("fill identity does not match the round")


def _refresh_round(db: sqlite3.Connection, round_id: str, entry_side: str) -> None:
    entries = exits = adjustment = position = Decimal(0)
    last_fill_at = None
    settled = False
    events = db.execute(
        """
        SELECT event_type, quantity, notional, cash_adjustment, occurred_at
        FROM trade_events WHERE round_id = ? ORDER BY rowid
        """,
        (round_id,),
    )
    # Preserve the supported 18 decimal places even when many fills are summed.
    with localcontext(Context(prec=64)):
        for event_type, quantity, notional, cash, occurred_at in events:
            if event_type == "settle":
                settled = True
            elif event_type == "adjustment":
                adjustment += Decimal(cash)
            elif event_type == entry_side:
                entries += Decimal(notional)
                position += Decimal(quantity)
                last_fill_at = occurred_at
            else:
                exits += Decimal(notional)
                position -= Decimal(quantity)
                last_fill_at = occurred_at
        if position < 0:
            raise ValueError("fill exceeds open quantity")
        if position > 0:
            return
        gross = exits - entries if entry_side == "buy" else entries - exits
        net = _decimal_text(gross + adjustment) if settled else None
    db.execute(
        """
        UPDATE rounds SET closed_at = ?, return_base = ?, net_pnl = ?
        WHERE round_id = ?
        """,
        (last_fill_at, _decimal_text(entries), net, round_id),
    )


def _private_path(path: Path) -> Path:
    path = Path(path).absolute()
    parent = path.parent
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise ValueError("Kelly ledger requires a directory you own with mode 0700")
    if path.suffix != ".sqlite":
        raise ValueError("Kelly ledger filename must end in .sqlite")
    return parent.resolve() / path.name


def _check_private_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise ValueError("Kelly ledger requires a private regular file with mode 0600")


class KellyLedger:
    """Record verified fills in a caller-selected local database.

    Amounts are stored as exact decimal text. SQL consumers can cast values
    for presentation or parse them with Decimal for exact arithmetic.
    """

    def __init__(self, path: Path):
        self.path = _private_path(path)

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        populate: Callable[[KellyLedger], None] | None = None,
    ) -> KellyLedger:
        """Publish a new private ledger once initialization succeeds."""
        ledger = cls(path)
        if os.path.lexists(ledger.path):
            raise FileExistsError(ledger.path)
        if any(os.path.lexists(sidecar) for sidecar in ledger._sidecars()):
            raise ValueError("new Kelly ledger destination has a SQLite sidecar")

        with TemporaryDirectory(dir=ledger.path.parent, prefix=".kelly-") as staging:
            pending = cls(Path(staging) / "new.sqlite")
            pending.path.touch(mode=0o600, exist_ok=False)
            with closing(sqlite3.connect(pending.path)) as db:
                db.executescript(
                    f"PRAGMA application_id = {_APPLICATION_ID};"
                    f"PRAGMA user_version = {_SCHEMA_VERSION};" + _SCHEMA
                )
            if populate is not None:
                populate(pending)
            # Linking fails if another creation has already claimed the destination.
            os.link(pending.path, ledger.path)
            pending.path.unlink()
        return ledger

    def _sidecars(self) -> Iterator[Path]:
        for suffix in _SIDECAR_SUFFIXES:
            yield self.path.with_name(self.path.name + suffix)

    def _validate(self) -> None:
        _check_private_file(self.path)
        for sidecar in self._sidecars():
            try:
                _check_private_file(sidecar)
            except FileNotFoundError:
                continue
        # Inspect the format without allowing journal recovery on a foreign file.
        try:
            with closing(
                sqlite3.connect(
                    f"{self.path.as_uri()}?mode=ro&immutable=1",
                    uri=True,
                )
            ) as db:
                (application_id,) = db.execute("PRAGMA application_id").fetchone()
                (version,) = db.execute("PRAGMA user_version").fetchone()
                definitions = frozenset(
                    sql
                    for (sql,) in db.execute(
                        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL"
                    )
                )
        except sqlite3.DatabaseError as exc:
            raise ValueError("not a Kelly ledger database") from exc
        if (application_id, version, definitions) != (
            _APPLICATION_ID,
            _SCHEMA_VERSION,
            _SCHEMA_DEFINITIONS,
        ):
            raise ValueError("not a Kelly ledger database")

    def _connect(self, mode: str) -> sqlite3.Connection:
        self._validate()
        db = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA trusted_schema = OFF")
        return db

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Read a consistent SQLite snapshot without write access."""
        with closing(self._connect("ro")) as db, db:
            db.execute("BEGIN")
            yield db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect("rw")) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def record_fill(self, identity: RoundIdentity, fill: Fill) -> str:
        """Record fills in time order for each position and return the round ID.

        Replaying the same event within the same identity returns its original
        round ID even after that round has closed.
        """
        with self._write() as db:
            return self._save_fill(db, identity, fill)

    @staticmethod
    def _save_fill(db: sqlite3.Connection, identity: RoundIdentity, fill: Fill) -> str:
        quantity, notional = _amount(fill.quantity), _amount(fill.notional)
        if fill.side not in {"buy", "sell"} or quantity <= 0 or notional <= 0:
            raise ValueError(
                "fill needs a valid side, positive quantity and positive notional"
            )
        event = _Event(
            fill.event_key,
            _timestamp(fill.occurred_at),
            fill.side,
            _decimal_text(quantity),
            _decimal_text(notional),
            None,
            fill.receipt_ref,
        )
        replay = event.replay_round(db)
        if replay is not None:
            round_row = db.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (replay,),
            ).fetchone()
            _require_identity(round_row, identity)
            return replay

        latest = db.execute(
            """
            SELECT * FROM rounds
            WHERE account_key = ? AND environment = ? AND instrument_key = ?
            ORDER BY opened_at DESC, rowid DESC LIMIT 1
            """,
            (identity.account_key, identity.environment, identity.instrument_key),
        ).fetchone()
        if latest is None or latest["closed_at"] is not None:
            if latest is not None and event.occurred_at < latest["closed_at"]:
                raise ValueError("fill precedes the previous close")
            if fill.side != identity.entry_side:
                raise ValueError("first fill must enter the position")
            round_id = str(uuid4())
            db.execute(
                f"""
                INSERT INTO rounds (round_id, {_IDENTITY_COLUMNS}, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (round_id, *astuple(identity), event.occurred_at),
            )
        else:
            _require_identity(latest, identity)
            round_id = latest["round_id"]
            (last_fill_at,) = db.execute(
                """
                SELECT MAX(occurred_at) FROM trade_events
                WHERE round_id = ? AND event_type IN ('buy', 'sell')
                """,
                (round_id,),
            ).fetchone()
            if event.occurred_at < last_fill_at:
                raise ValueError("fill precedes the last recorded fill")

        event.insert(db, round_id)
        _refresh_round(db, round_id, identity.entry_side)
        return round_id

    def record_adjustment(
        self,
        round_id: str,
        *,
        event_key: str,
        occurred_at: str,
        cash_adjustment: str | Decimal,
        receipt_ref: str,
    ) -> None:
        """Record a confirmed signed cash adjustment, including late costs.

        A negative amount is a charge. Adjustments after cost confirmation
        update the same finalized round.
        """
        event = _Event(
            event_key,
            _timestamp(occurred_at),
            "adjustment",
            None,
            None,
            _decimal_text(_amount(cash_adjustment)),
            receipt_ref,
        )
        self._record_cash_event(round_id, event)

    def settle(
        self,
        round_id: str,
        *,
        event_key: str,
        occurred_at: str,
        receipt_ref: str,
    ) -> None:
        """Confirm complete costs for a closed round, including confirmed zero."""
        event = _Event(
            event_key,
            _timestamp(occurred_at),
            "settle",
            None,
            None,
            None,
            receipt_ref,
        )
        self._record_cash_event(round_id, event)

    def _record_cash_event(self, round_id: str, event: _Event) -> None:
        with self._write() as db:
            self._save_cash_event(db, round_id, event)

    @staticmethod
    def _save_cash_event(db: sqlite3.Connection, round_id: str, event: _Event) -> None:
        replay = event.replay_round(db)
        if replay is not None:
            if replay != round_id:
                raise ValueError("event key conflict")
            return
        round_row = db.execute(
            "SELECT opened_at, closed_at, direction FROM rounds WHERE round_id = ?",
            (round_id,),
        ).fetchone()
        if round_row is None:
            raise ValueError("cash event requires a known round")
        if event.occurred_at < round_row["opened_at"]:
            raise ValueError("cash event precedes the opening fill")
        if event.event_type == "settle":
            closed_at = round_row["closed_at"]
            if closed_at is None or event.occurred_at < closed_at:
                raise ValueError("settlement requires a closed round")
        event.insert(db, round_id)
        entry_side = "buy" if round_row["direction"] == "long" else "sell"
        _refresh_round(db, round_id, entry_side)
