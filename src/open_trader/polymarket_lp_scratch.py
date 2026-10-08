"""Process-local LP read copies, spilled to SQLite instead of retained objects.

This is disposable scratch space, never the durable trading/history store.
Pickle preserves Decimal, datetime, tuples and UNKNOWN exactly. Only values
written by this process are decoded; no external file can be opened here.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from copy import deepcopy
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


class LPReadOverlay(MutableMapping):
    """Pinned immutable source with disk-backed updates and identifier-only removal."""

    def __init__(self, source, keys=None):
        self._source = source
        self._keys = dict.fromkeys(source if keys is None else keys)
        self._updates = LPReadScratch()

    def __getitem__(self, key):
        with self._updates._lock:
            if key not in self._keys:
                raise KeyError(key)
            try:
                return self._updates[key]
            except KeyError:
                return self._source[key]

    def __setitem__(self, key, value):
        with self._updates._lock:
            self._updates[key] = value
            self._keys[key] = None

    def __delitem__(self, key):
        with self._updates._lock:
            del self._keys[key]
            self._updates.pop(key, None)

    def __iter__(self):
        with self._updates._lock:
            return iter(tuple(self._keys))

    def __len__(self):
        return len(self._keys)

    def __deepcopy__(self, memo):
        with self._updates._lock:
            clone = type(self)(self._source, self._keys)
            clone._updates = deepcopy(self._updates, memo)
            memo[id(self)] = clone
            return clone


class LPDirectionIndex(MutableMapping):
    """Queue-owned direction keys; only one condition is expanded per lookup."""

    def __init__(self, source, keys):
        self._source = source
        self._keys = keys
        retained = {key for group in keys.values() for key in group}
        for key in tuple(source):
            if key not in retained:
                del source._values[key]
        source._metadata._keys = dict.fromkeys(keys)

    def __getitem__(self, condition):
        with self._source._values._lock:
            rows = []
            for key in self._keys[condition]:
                row = self._source._values[key]
                row["market"] = {**self._source._metadata[condition], **row["market"]}
                rows.append(row)
            return rows

    def __setitem__(self, condition, directions):
        with self._source._values._lock:
            # Renewals share one new market base, never one full base per direction.
            base = dict(directions[0]["market"]) if directions else {}
            base = {key: value for key, value in base.items()
                    if all(key in row["market"] and row["market"][key] == value for row in directions)}
            old_keys = self._keys.get(condition, ())
            keys = [f"{len(condition)}:{condition}:{index}" for index in range(len(directions))]
            self._source._metadata[condition] = base
            for key, row in zip(keys, directions):
                overrides = {name: value for name, value in row["market"].items()
                             if name not in base or base[name] != value}
                # Keep identity available to key-only exclusion consumers.
                for name in ("condition_id", "token_id", "outcome"):
                    overrides[name] = row["market"][name]
                self._source[key] = {**row, "market": overrides}
            self._keys[condition] = keys
            for key in set(old_keys) - set(keys):
                del self._source._values[key]

    def __delitem__(self, condition):
        with self._source._values._lock:
            for key in self._keys.pop(condition):
                del self._source._values[key]
            del self._source._metadata[condition]

    def filter_tokens(self, condition, allowed):
        with self._source._values._lock:
            if condition not in self._keys:
                return
            surviving = []
            for key in self._keys[condition]:
                token = self._source._values[key]["market"]["token_id"]
                if allowed(token):
                    surviving.append(key)
                else:
                    del self._source._values[key]
            self._keys[condition] = surviving

    def __iter__(self):
        with self._source._values._lock:
            return iter(tuple(self._keys))

    def __contains__(self, condition):
        with self._source._values._lock:
            return condition in self._keys

    def __len__(self):
        return len(self._keys)

    def __deepcopy__(self, memo):
        with self._source._values._lock:
            clone = type(self).__new__(type(self))
            memo[id(self)] = clone
            clone._source = deepcopy(self._source, memo)
            clone._keys = deepcopy(self._keys, memo)
            return clone


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
