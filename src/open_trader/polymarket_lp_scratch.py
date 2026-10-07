"""Process-local LP read copies, spilled to SQLite instead of retained objects.

This is disposable scratch space, never the durable trading/history store.
Pickle preserves Decimal, datetime, tuples and UNKNOWN exactly. Only values
written by this process are decoded; no external file can be opened here.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from datetime import date, datetime
from io import BytesIO
import pickle
import sqlite3
import threading
import weakref


class _ValuePickler(pickle.Pickler):
    def reducer_override(self, value):
        # Cache the timestamp value, not a process-local SDK/clock subclass.
        # The builtin reduction retains timezone, microseconds and fold.
        kind = datetime if isinstance(value, datetime) else date if isinstance(value, date) else None
        if kind is not None and type(value) is not kind:
            return kind, kind.__reduce_ex__(value, 5)[1]
        return NotImplemented


def _encode(value):
    stream = BytesIO()
    _ValuePickler(stream, protocol=5).dump(value)
    return stream.getvalue()


class LPReadScratch(MutableMapping):
    def __init__(self, values=()):
        # An empty filename gives SQLite a private, automatically removed
        # temporary database. Bound its native page cache as well as Python data.
        self._db = sqlite3.connect("", check_same_thread=False)
        self._lock = threading.RLock()
        weakref.finalize(self, self._db.close)
        self._db.execute("PRAGMA temp_store=FILE")
        self._db.execute("PRAGMA cache_size=-1024")
        self._db.execute("PRAGMA mmap_size=0")
        self._db.execute("CREATE TABLE entries (key TEXT PRIMARY KEY, value BLOB)")
        self.update(values)

    def __getitem__(self, key):
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM entries WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return pickle.loads(row[0])

    def __setitem__(self, key, value):
        self.update(((key, value),))

    def __delitem__(self, key):
        with self._lock, self._db:
            if not self._db.execute("DELETE FROM entries WHERE key=?", (key,)).rowcount:
                raise KeyError(key)

    def __iter__(self):
        with self._lock:
            # Only identifiers are materialized; values stay on disk until read.
            keys = tuple(row[0] for row in self._db.execute(
                "SELECT key FROM entries ORDER BY rowid"
            ))
        return iter(keys)

    def __len__(self):
        with self._lock:
            return self._db.execute("SELECT count(*) FROM entries").fetchone()[0]

    def update(self, values=(), **kwargs):
        items = values.items() if isinstance(values, Mapping) else values
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO entries VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ((key, _encode(value)) for key, value in items),
            )
        if kwargs:
            self.update(kwargs)

    def clear(self):
        with self._lock, self._db:
            self._db.execute("DELETE FROM entries")

    def __deepcopy__(self, memo):
        clone = type(self)()
        with self._lock:
            self._db.backup(clone._db)
        memo[id(self)] = clone
        return clone


class LPDirectionScratch(Mapping):
    """Direction overrides over one immutable published metadata generation.

    The metadata mapping owns its private scratch snapshot. Retaining it keeps
    old readers valid across publication without copying or closing that source.
    Each lookup decodes fresh base and override values for caller isolation.
    """

    def __init__(self, metadata):
        self._metadata = metadata
        self._values = LPReadScratch()

    def __setitem__(self, key, direction):
        self._values[key] = direction

    def __getitem__(self, key):
        direction = self._values[key]
        overrides = direction["market"]
        direction["market"] = {
            **self._metadata[overrides["condition_id"]],
            **overrides,
        }
        return direction

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class LPReadRows(Sequence):
    """Immutable, repeatable rows; iteration decodes one row at a time."""

    def __init__(self, rows):
        self._values = LPReadScratch((str(index), row) for index, row in enumerate(rows))
        self._length = len(self._values)

    def __len__(self):
        return self._length

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self._values[str(index)]
