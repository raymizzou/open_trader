"""Bounded, independent RAM snapshots for disposable current LP metadata."""
from collections import OrderedDict
from collections.abc import Mapping, MutableMapping
from copy import deepcopy
import sys
import threading

DISPLAY_FIELDS = frozenset(('market_title', 'market_url', 'price_change_24h',
                            'price_change_24h_source'))
CORE_FIELDS = frozenset((
    'market_id', 'condition_id', 'exchange_type', 'outcomes', 'accepting_orders',
    'closed', 'resolved', 'event_id', 'game_id', 'game_start_time', 'event_start_time',
    'event_ended', 'event_finished_at', 'tick_size', 'minimum_order_size', 'fee',
    'fees_enabled', 'fee_exponent', 'taker_fee_rate', 'reward_min_size', 'reward_max_spread',
    'metadata_checked_at', 'fees_checked_at',
))
DEFAULT_POLICY = dict(max_entries=3000, max_retained_value_bytes=16 * 1024 * 1024,
                      display_max_entries=3000, display_max_retained_value_bytes=4 * 1024 * 1024)


def cache_policy(policy=None):
    result = DEFAULT_POLICY | dict(policy or {})
    if set(result) != set(DEFAULT_POLICY) or any(type(v) is not int or v < 1 for v in result.values()):
        raise ValueError('invalid metadata cache policy')
    return result


def _retained_size(value):
    # Count every occurrence conservatively, including keys and containers.
    size = sys.getsizeof(value)
    if isinstance(value, Mapping):
        size += sum(_retained_size(k) + _retained_size(v) for k, v in value.items())
    elif isinstance(value, (tuple, list)):
        size += sum(_retained_size(v) for v in value)
    return size


class _Budget:
    def __init__(self, entries, size):
        self.entries, self.limit = entries, size
        self.rows = OrderedDict()
        self.bytes = 0

    def discard(self, key):
        previous = self.rows.pop(key, None)
        if previous is not None:
            self.bytes -= previous[1]

    def put(self, key, value):
        self.discard(key)
        size = _retained_size(key) + _retained_size(value)
        if size > self.limit:
            return
        while self.rows and (len(self.rows) >= self.entries or self.bytes + size > self.limit):
            _, (_, removed) = self.rows.popitem(last=False)
            self.bytes -= removed
        self.rows[key] = (value, size)
        self.bytes += size

    def get(self, key):
        value, _ = self.rows[key]
        self.rows.move_to_end(key)
        return value


class LPMetadataCache(MutableMapping):
    """Independent LRU core/detail budgets; oversize values are not retained."""

    def __init__(self, policy=None):
        self.policy = cache_policy(policy)
        self._core = _Budget(self.policy['max_entries'], self.policy['max_retained_value_bytes'])
        self._display = _Budget(self.policy['display_max_entries'], self.policy['display_max_retained_value_bytes'])
        self._lock = threading.RLock()

    def __getitem__(self, key):
        with self._lock:
            expiry, core = self._core.get(key)
            payload = deepcopy(core)
            if payload is not None and key in self._display.rows:
                detail_expiry, detail = self._display.get(key)
                if detail_expiry == expiry:
                    payload.update(deepcopy(detail))
            return expiry, payload

    def __setitem__(self, key, value):
        self.put(key, value)

    def get_core(self, key):
        with self._lock:
            return deepcopy(self._core.get(key))

    def put(self, key, value, *, planning_only=False):
        expiry, payload = value
        planning_only = planning_only or (payload is not None and payload.get("_lp_metadata_projection") == "planning")
        with self._lock:
            core = None if payload is None else {k: deepcopy(v) for k, v in payload.items() if k in CORE_FIELDS}
            self._core.put(key, (expiry, core))
            self._display.discard(key)
            if payload is not None and key in self._core.rows and not planning_only:
                detail = {k: deepcopy(v) for k, v in payload.items() if k in DISPLAY_FIELDS}
                self._display.put(key, (expiry, detail))

    def discard_display(self, key):
        with self._lock:
            self._display.discard(key)

    def has_display(self, key):
        with self._lock:
            return key in self._display.rows

    def __delitem__(self, key):
        with self._lock:
            if key not in self._core.rows:
                raise KeyError(key)
            self._core.discard(key)
            self._display.discard(key)

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._core.rows))

    def __len__(self):
        with self._lock:
            return len(self._core.rows)

    def clear(self):
        with self._lock:
            self._core.rows.clear()
            self._core.bytes = 0
            self._display.rows.clear()
            self._display.bytes = 0

    def status(self):
        with self._lock:
            return dict(core_entries=len(self._core.rows), core_retained_value_bytes=self._core.bytes,
                        display_entries=len(self._display.rows), display_retained_value_bytes=self._display.bytes)
