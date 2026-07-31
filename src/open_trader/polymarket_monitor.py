"""Concrete, read-only Polymarket event and order-book monitor."""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from polymarket import AsyncPublicClient
from polymarket.streams import MarketSpec

from .prediction_arbitrage import (
    BookLevel,
    ConfirmedBooks,
    MarketFacts,
    build_pair_intent,
    monitored_event_sort_key,
)
from .prediction_arbitrage_store import PredictionArbitrageStore
from .polymarket_relation_discovery import (
    RelationValidation,
    ThresholdHedgeIntent,
    ThresholdOrderBook,
    ThresholdRelation,
    ThresholdRelationDiscoveryResult,
    build_threshold_hedge_intent,
    simple_annualized_yield,
    threshold_relation_from_payload,
    threshold_relation_payload,
)


TOP_EVENT_LIMIT = 20
UNIVERSE_REFRESH_SECONDS = 5 * 60
BOOK_FRESHNESS_SECONDS = 10
READINESS_FRESHNESS_SECONDS = 60
READINESS_REFRESH_SECONDS = 30
HEARTBEAT_FRESHNESS_SECONDS = 30
STREAM_DISCONNECT_SECONDS = 15
UNIVERSE_STALE_SECONDS = 10 * 60
RUNTIME_WRITE_SECONDS = 1
PUBLIC_REFRESH_TIMEOUT_SECONDS = 30.0
PUBLIC_BOOK_CONCURRENCY = 8
STREAM_SUBSCRIPTION_CHUNK_SIZE = 250
THRESHOLD_BOOK_BATCH_SIZE = 100
RELATION_SCAN_LOG_LIMIT = 20
RELATION_CATALOG_FRESHNESS_SECONDS = 24 * 60 * 60


def _value(value: object, *names: str, default: object = None) -> object:
    """Read model, mapping, and official-client alias fields."""

    if value is None:
        return default
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        try:
            result = getattr(value, name)
        except Exception:
            continue
        return result
    # Pydantic models used by polymarket expose model_dump but keep snake_case
    # fields.  Calling it is only a fallback for test doubles and older SDKs.
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(by_alias=True)
            if isinstance(dumped, Mapping):
                for name in names:
                    if name in dumped:
                        return dumped[name]
        except Exception:
            pass
    return default


def _items(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, (str, bytes)):
        return (value,)
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return (value,)


async def _collect(value: object) -> tuple[object, ...]:
    if inspect.isawaitable(value):
        value = await value
    # The official AsyncPublicClient returns AsyncPaginator.  Its async
    # iterator yields Page objects, while iter_items() yields the actual
    # Event/Market models we need.
    iter_items = getattr(value, "iter_items", None)
    if callable(iter_items):
        return await _collect(iter_items())
    if hasattr(value, "__aiter__"):
        result: list[object] = []
        async for item in value:  # type: ignore[union-attr]
            page_items = _value(item, "items", default=None)
            if page_items is not None and _value(item, "has_more", default=None) is not None:
                result.extend(_items(page_items))
            else:
                result.append(item)
        return tuple(result)
    page_items = _value(value, "items", default=None)
    if page_items is not None and _value(value, "has_more", default=None) is not None:
        return _items(page_items)
    return _items(value)


async def _collect_first_page(value: object) -> tuple[object, ...]:
    if inspect.isawaitable(value):
        value = await value
    first_page = getattr(value, "first_page", None)
    if callable(first_page):
        return await _collect(await _call(first_page))
    return await _collect(value)


async def _call(value: object, *args: object, **kwargs: object) -> object:
    if not callable(value):
        return value
    result = value(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _timestamp(value: object, *, fallback: datetime) -> datetime:
    parsed = _timestamp_or_none(value)
    return fallback if parsed is None else parsed


def _timestamp_or_none(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = float(value)
        if not number == number or number in (float("inf"), float("-inf")):
            return None
        # CLOB timestamps are epoch milliseconds; small values are seconds.
        parsed = datetime.fromtimestamp(number / (1000 if number > 10_000_000_000 else 1), UTC)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age(now: datetime, then: datetime | None) -> float:
    if then is None:
        return float("inf")
    return max(0.0, (now - then).total_seconds())


def _display_age(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _state_flag(model: object, name: str, default: object = None) -> object:
    return _value(model, name, _camel(name), default=default)


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _outcome_pairs(outcomes: object) -> dict[str, str] | None:
    if outcomes is None:
        return None
    if not isinstance(outcomes, (Mapping, list, tuple)) and not isinstance(outcomes, str):
        values = []
        for name in ("yes", "no"):
            item = _value(outcomes, name, default=None)
            if item is not None:
                values.append(item)
    elif isinstance(outcomes, Mapping):
        values = list(outcomes.values())
    else:
        values = list(outcomes)  # type: ignore[arg-type]
    mapped: dict[str, str] = {}
    for outcome in values:
        label = _value(outcome, "label", "name", default=None)
        token = _value(outcome, "token_id", "tokenId", "asset_id", "assetId", default=None)
        if not isinstance(label, str) or not isinstance(token, str) or not token.strip():
            return None
        key = label.strip().casefold()
        if key not in {"yes", "no"} or key in mapped:
            return None
        mapped[key] = token.strip()
    if set(mapped) != {"yes", "no"} or len(mapped) != 2 or mapped["yes"] == mapped["no"]:
        return None
    return mapped


def _asks(value: object) -> tuple[BookLevel, ...] | None:
    levels: list[BookLevel] = []
    for level in _items(value):
        price = _decimal(_value(level, "price", default=None))
        size = _decimal(_value(level, "size", "quantity", default=None))
        if price is None or size is None or price <= 0 or size <= 0 or price > 1:
            return None
        levels.append(BookLevel(price=price, size=size))
    return tuple(levels) if levels else None


def _event_id(value: object) -> str | None:
    raw = _value(value, "id", "event_id", "eventId", default=None)
    return str(raw).strip() if isinstance(raw, (str, int)) and str(raw).strip() else None


class PolymarketMonitor:
    """One async public monitor with a serialized in-memory snapshot."""

    def __init__(
        self,
        *,
        store: PredictionArbitrageStore,
        trading: object,
        public_client_factory: Callable[[], object] = AsyncPublicClient,
        clock: Callable[[], datetime] | None = None,
        relation_discovery: Callable[[Sequence[object]], object] | object | None = None,
        relation_validator: object | None = None,
    ) -> None:
        self._store = store
        self._trading = trading
        self._public_client_factory = public_client_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._relation_discovery = relation_discovery
        self._relation_validator = relation_validator
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: object | None = None
        self._stream_handle: object | None = None
        self._events: dict[str, dict[str, object]] = {}
        self._markets: dict[str, dict[str, object]] = {}
        self._market_by_token: dict[str, str] = {}
        self._relations: dict[str, ThresholdRelation] = {}
        self._relation_by_token: dict[str, set[str]] = {}
        self._relation_books: dict[str, ThresholdOrderBook] = {}
        self._relation_volumes: dict[str, Decimal] = {}
        self._opportunities: dict[str, dict[str, object]] = {}
        self._books: dict[str, ConfirmedBooks] = {}
        self._readiness: dict[str, object] | None = None
        self._universe_at: datetime | None = None
        self._relations_at: datetime | None = None
        self._catalog_full_scanned_at: datetime | None = None
        self._catalog_last_attempt_at: datetime | None = None
        self._catalog_scan_started_at: datetime | None = None
        self._catalog_scan_duration_seconds: float | None = None
        self._catalog_status = "stale" if relation_discovery is not None else "unavailable"
        self._catalog_counts: dict[str, object] = {}
        self._catalog_last_full_run: dict[str, object] | None = None
        self._catalog_last_event_run: dict[str, object] | None = None
        self._activity_scan_due_at: datetime | None = None
        self._full_scan_task: asyncio.Task[None] | None = None
        self._catalog_loaded = False
        self._heartbeat_at: datetime | None = None
        self._stream_connected_at: datetime | None = None
        self._stream_disconnected_at: datetime | None = None
        self._last_runtime_write: datetime | None = None
        self._store_failed = False
        self._universe_failed = False
        self._relations_failed = False
        self._stream_message_at: datetime | None = None
        self._diagnostics: dict[str, object] = {
            "malformed_events": 0,
            "malformed_markets": 0,
            "last_error": None,
        }
        self._relation_scan_logs: deque[dict[str, object]] = deque(
            maxlen=RELATION_SCAN_LOG_LIMIT
        )

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=lambda: asyncio.run(self.run_forever()),
                name="polymarket-monitor",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = self._now()
            health = self._health(now)
            events = sorted(
                (copy.deepcopy(event) for event in self._events.values()),
                key=monitored_event_sort_key,
            )
            if health["status"] == "degraded":
                for event in events:
                    event["actionable"] = False
                    for market in event.get("markets", []):
                        if isinstance(market, Mapping) and market.get("actionable") is True:
                            market["actionable"] = False
                            market["eligibility_reason"] = "monitor_degraded"
            opportunities = [
                copy.deepcopy(value)
                for value in sorted(
                    self._opportunities.values(),
                    key=monitored_event_sort_key,
                )
            ]
            relation_health = self._relation_health(now)
            catalog = self._relation_catalog_snapshot(now)
            for opportunity in opportunities:
                confirmed_at = opportunity.get("confirmed_at")
                age = _age(now, confirmed_at if isinstance(confirmed_at, datetime) else None)
                opportunity["confirmed_age_seconds"] = age
                if age > BOOK_FRESHNESS_SECONDS:
                    opportunity["actionable"] = False
                    opportunity["eligibility_reason"] = "book_stale"
                if health["status"] == "degraded":
                    opportunity["actionable"] = False
                    opportunity["eligibility_reason"] = "monitor_degraded"
                if (
                    opportunity.get("market_type") == "threshold_hedge"
                    and catalog["status"] != "healthy"
                ):
                    opportunity["actionable"] = False
                    opportunity["eligibility_reason"] = (
                        "relation_discovery_" + str(catalog["status"])
                    )
            return {
                "status": health["status"],
                "health": health,
                "events": events,
                "opportunities": opportunities,
                "diagnostics": copy.deepcopy(self._diagnostics),
                "heartbeat_at": self._heartbeat_at,
                "universe_refreshed_at": self._universe_at,
                "readiness": copy.deepcopy(self._readiness),
                "relation_discovery": {
                    **relation_health,
                    "catalog": catalog,
                    "scan_logs": copy.deepcopy(list(self._relation_scan_logs)),
                    "codex_usage_24h": self._llm_usage(),
                    "annualized_distribution": self._annualized_distributions(),
                },
            }

    def opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        with self._lock:
            value = self._opportunities.get(str(opportunity_id))
            if value is None:
                return None
            result = copy.deepcopy(value)
            now = self._now()
            confirmed_at = result.get("confirmed_at")
            age = _age(now, confirmed_at if isinstance(confirmed_at, datetime) else None)
            result["confirmed_age_seconds"] = age
            if age > BOOK_FRESHNESS_SECONDS:
                result["actionable"] = False
                result["eligibility_reason"] = "book_stale"
            if self._health(now)["status"] == "degraded":
                result["actionable"] = False
                result["eligibility_reason"] = "monitor_degraded"
            relation_health = self._relation_health(now)
            catalog = self._relation_catalog_snapshot(now)
            if (
                result.get("market_type") == "threshold_hedge"
                and catalog["status"] != "healthy"
            ):
                result["actionable"] = False
                result["eligibility_reason"] = (
                    "relation_discovery_" + str(catalog["status"])
                )
            return result

    def refresh_once(self) -> dict[str, object]:
        """Perform one public refresh; useful to diagnostics and local checks."""

        return asyncio.run(self._refresh_once())

    async def _refresh_once(self) -> dict[str, object]:
        if not self._catalog_loaded:
            self._load_relation_catalog()
        client = self._client
        owned_client = client is None
        if client is None:
            client = self._public_client_factory()
            self._client = client
        try:
            await self._refresh_universe_bounded(client)
            return self.snapshot()
        except Exception as exc:
            self._record_error(exc, "universe")
            return self.snapshot()
        finally:
            if owned_client and self._stop_event.is_set():
                self._client = None

    async def run_forever(self) -> None:
        if not self._catalog_loaded:
            self._load_relation_catalog()
        client = self._public_client_factory()
        self._client = client
        self._maybe_schedule_full_scan(client)
        next_refresh = 0.0
        next_readiness_refresh = 0.0
        try:
            while not self._stop_event.is_set():
                self._maybe_schedule_full_scan(client)
                current = time.monotonic()
                if self._universe_at is None or current >= next_refresh:
                    try:
                        await self._refresh_universe_bounded(client)
                        next_readiness_refresh = current + READINESS_REFRESH_SECONDS
                    except Exception as exc:
                        self._record_error(exc, "universe")
                    next_refresh = current + UNIVERSE_REFRESH_SECONDS
                if current >= next_readiness_refresh:
                    await self._refresh_readiness()
                    next_readiness_refresh = current + READINESS_REFRESH_SECONDS
                if self._stream_handle is None:
                    try:
                        await self._subscribe(client)
                    except Exception as exc:
                        self._record_error(exc, "stream")
                        await asyncio.sleep(0.2)
                        continue
                try:
                    message = await asyncio.wait_for(
                        self._stream_next(self._stream_handle), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    self._write_runtime()
                    continue
                except (StopAsyncIteration, ConnectionError, OSError) as exc:
                    await self._close_stream()
                    self._disconnect_stream(exc)
                    await asyncio.sleep(0)
                    continue
                except Exception as exc:
                    await self._close_stream()
                    self._disconnect_stream(exc)
                    await asyncio.sleep(0)
                    continue
                self._stream_message_at = self._now()
                self._heartbeat_at = self._stream_message_at
                try:
                    await self._process_stream_event(client, message)
                except Exception as exc:
                    self._record_error(exc, "stream_event")
                self._write_runtime()
        finally:
            task = self._full_scan_task
            self._full_scan_task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self._close_stream()
            self._client = None

    async def _refresh_universe_bounded(self, client: object) -> None:
        await asyncio.wait_for(
            self._refresh_universe(client),
            timeout=PUBLIC_REFRESH_TIMEOUT_SECONDS,
        )

    async def _stream_next(self, handle: object) -> object:
        iterator = getattr(handle, "__anext__", None)
        if not callable(iterator):
            raise ConnectionError("public stream is not async iterable")
        return await iterator()

    async def _close_stream(self) -> None:
        handle = self._stream_handle
        self._stream_handle = None
        if handle is None:
            return
        close = getattr(handle, "close", None)
        if callable(close):
            try:
                await _call(close)
            except Exception:
                pass

    def _disconnect_stream(self, exc: BaseException) -> None:
        self._stream_disconnected_at = self._now()
        self._diagnostics["last_error"] = f"stream:{type(exc).__name__}"
        # The handle is closed by the next event-loop pass.  We deliberately do
        # not retain stream messages in the store.
        self._stream_handle = None

    async def _subscribe(self, client: object) -> None:
        with self._lock:
            token_ids = sorted(
                set(self._market_by_token) | set(self._relation_by_token)
            )
        if not token_ids:
            return
        await self._close_stream()
        subscribe = getattr(client, "subscribe", None)
        if not callable(subscribe):
            raise ConnectionError("public client has no market stream")
        specs = [
            MarketSpec(
                token_ids=token_ids[index : index + STREAM_SUBSCRIPTION_CHUNK_SIZE],
                custom_feature_enabled=True,
            )
            for index in range(0, len(token_ids), STREAM_SUBSCRIPTION_CHUNK_SIZE)
        ]
        handle = await _call(subscribe, specs[0] if len(specs) == 1 else specs)
        self._stream_handle = handle
        self._stream_connected_at = self._now()
        self._stream_disconnected_at = None

    async def _refresh_universe(self, client: object) -> None:
        list_events = getattr(client, "list_events", None)
        if not callable(list_events):
            raise RuntimeError("public client has no list_events")
        raw = await _call(
            list_events,
            closed=False,
            ended=False,
            order="volume24hr",
            ascending=False,
            page_size=TOP_EVENT_LIMIT,
        )
        rows = await _collect_first_page(raw)
        normalized: list[dict[str, object]] = []
        malformed_events = 0
        for row in rows:
            parsed = self._normalize_event(row)
            if parsed is None:
                malformed_events += 1
                continue
            normalized.append(parsed)
        normalized.sort(key=lambda item: (-item["volume_24h"], str(item["event_id"])))  # type: ignore[operator]
        normalized = normalized[:TOP_EVENT_LIMIT]
        markets: dict[str, dict[str, object]] = {}
        token_map: dict[str, str] = {}
        self._diagnostics["malformed_markets"] = 0
        for event_row in normalized:
            valid_markets: list[dict[str, object]] = []
            for raw_market in _items(event_row.pop("_raw_markets", ())):
                market_row = self._normalize_market(event_row, raw_market)
                if market_row is None:
                    self._diagnostics["malformed_markets"] = int(self._diagnostics["malformed_markets"]) + 1
                    continue
                valid_markets.append(market_row)
                market_id = str(market_row["market_id"])
                markets[market_id] = market_row
                token_map[str(market_row["yes_token_id"])] = market_id
                token_map[str(market_row["no_token_id"])] = market_id
            event_row["markets"] = valid_markets
            event_row["market_count"] = len(valid_markets)
            event_row.pop("_raw_markets", None)
        with self._lock:
            previous_opportunity_rows = copy.deepcopy(self._opportunities)
            previous_opportunities = set(previous_opportunity_rows)
            self._events = {str(item["event_id"]): item for item in normalized}
            self._markets = markets
            self._market_by_token = token_map
            self._diagnostics["malformed_events"] = malformed_events
            self._universe_at = self._now()
            self._universe_failed = False
        await self._subscribe(client)
        await self._refresh_readiness()
        semaphore = asyncio.Semaphore(PUBLIC_BOOK_CONCURRENCY)

        async def confirm(
            market_row: dict[str, object],
        ) -> dict[str, object] | None:
            async with semaphore:
                try:
                    return await self._confirm_market(client, market_row)
                except Exception as exc:
                    self._record_error(exc, "books")
                    return None

        confirmed = await asyncio.gather(
            *(confirm(market_row) for market_row in markets.values())
        )
        current_opportunities: dict[str, dict[str, object]] = {}
        for opportunity in confirmed:
            if opportunity is not None:
                current_opportunities[str(opportunity["opportunity_id"])] = opportunity
        if self._relation_discovery is not None:
            for opportunity in await self._refresh_relation_opportunities(client):
                current_opportunities[str(opportunity["opportunity_id"])] = opportunity
        with self._lock:
            self._opportunities = current_opportunities
            missing = previous_opportunities - set(current_opportunities)
        for opportunity_id in missing:
            previous = previous_opportunity_rows.get(opportunity_id, {})
            market_id = str(previous.get("market_id", opportunity_id.split(":", 1)[-1]))
            self._close_signal(market_id, "opportunity_closed")
        self._sync_event_rows()
        self._write_runtime(force=True)

    def _log_relation_scan(self, **fields: object) -> None:
        entry = {"at": self._now(), **fields}
        self._relation_scan_logs.append(entry)

    def _catalog_due(self, now: datetime) -> bool:
        if self._relation_discovery is None:
            return False
        anchor = self._catalog_last_attempt_at or self._catalog_full_scanned_at
        return anchor is None or _age(now, anchor) >= RELATION_CATALOG_FRESHNESS_SECONDS

    def _maybe_schedule_full_scan(self, client: object) -> None:
        task = self._full_scan_task
        if task is not None and not task.done():
            return
        if not self._catalog_due(self._now()):
            return
        self._full_scan_task = asyncio.create_task(
            self._run_full_relation_scan(client)
        )

    @staticmethod
    def _discovery_result(
        value: object,
        events: Sequence[object],
    ) -> ThresholdRelationDiscoveryResult:
        if isinstance(value, ThresholdRelationDiscoveryResult):
            return value
        relations = tuple(
            item for item in _items(value) if isinstance(item, ThresholdRelation)
        )
        return ThresholdRelationDiscoveryResult(
            relations=relations,
            events_seen=len(events),
            events_eligible=0,
            markets_seen=0,
            markets_normalized=0,
            threshold_markets=0,
            unique_tokens=len(
                {
                    token
                    for relation in relations
                    for token in (
                        relation.buy_leg_a.token_id,
                        relation.buy_leg_b.token_id,
                    )
                }
            ),
            rejection_counts={},
        )

    @staticmethod
    def _result_counts(
        result: ThresholdRelationDiscoveryResult,
    ) -> dict[str, object]:
        return {
            "events_seen": result.events_seen,
            "events_eligible": result.events_eligible,
            "markets_seen": result.markets_seen,
            "markets_normalized": result.markets_normalized,
            "threshold_markets": result.threshold_markets,
            "unique_tokens": result.unique_tokens,
            "relation_count": len(result.relations),
            "rejection_counts": dict(result.rejection_counts),
        }

    def _stored_full_scanned_at(
        self, state: Mapping[str, object] | None = None
    ) -> datetime | None:
        if isinstance(state, Mapping):
            for key in ("full_scanned_at", "scanned_at"):
                parsed = _timestamp_or_none(state.get(key))
                if parsed is not None:
                    return parsed
        reader = getattr(self._store, "_read_connection", None)
        if not callable(reader):
            return None
        try:
            with reader() as connection:
                row = connection.execute(
                    "SELECT full_scanned_at FROM relation_state WHERE singleton=1"
                ).fetchone()
            return _timestamp_or_none(row["full_scanned_at"]) if row is not None else None
        except Exception:
            return None

    def _load_relation_catalog(self) -> None:
        self._catalog_loaded = True
        if self._relation_discovery is None:
            self._catalog_status = "unavailable"
            return
        try:
            state = self._store.load_relation_state()
            if not isinstance(state, Mapping):
                self._catalog_status = "stale"
                return
            raw_relations = state.get("relations", ())
            if not isinstance(raw_relations, (list, tuple)):
                raise ValueError("invalid relation catalog")
            relations = tuple(
                threshold_relation_from_payload(item)
                for item in raw_relations
            )
            scanned_at = self._stored_full_scanned_at(state)
            self._set_relation_state(relations, (), scanned_at=scanned_at)
            self._catalog_full_scanned_at = scanned_at
            self._catalog_last_attempt_at = scanned_at
            self._catalog_counts = {
                "events_seen": 0,
                "events_eligible": 0,
                "markets_seen": 0,
                "markets_normalized": 0,
                "threshold_markets": 0,
                "relation_count": len(relations),
                "unique_tokens": len(
                    {
                        token
                        for relation in relations
                        for token in (
                            relation.buy_leg_a.token_id,
                            relation.buy_leg_b.token_id,
                        )
                    }
                ),
                "rejection_counts": {
                    "event_ineligible": 0,
                    "market_ineligible": 0,
                    "market_unparseable": 0,
                    "not_threshold": 0,
                    "duplicate_condition": 0,
                    "duplicate_token": 0,
                },
            }
            history = getattr(self._store, "relation_scan_history", None)
            if callable(history):
                try:
                    runs = history(limit=20)
                except Exception:
                    runs = ()
                for run in runs:
                    if not isinstance(run, Mapping):
                        continue
                    scope = str(run.get("scope", ""))
                    if scope == "full" and self._catalog_last_full_run is None:
                        self._catalog_last_full_run = dict(run)
                        for key in (
                            "events_seen",
                            "events_eligible",
                            "markets_seen",
                            "markets_normalized",
                            "threshold_markets",
                            "unique_tokens",
                            "relation_count",
                            "rejection_counts",
                        ):
                            if key in run and key != "rejection_counts":
                                self._catalog_counts[key] = copy.deepcopy(run[key])
                        saved_rejections = run.get("rejection_counts")
                        if isinstance(saved_rejections, Mapping):
                            self._catalog_counts["rejection_counts"].update(
                                saved_rejections
                            )
                        saved_items = run.get("rejection_counts_items")
                        if isinstance(saved_items, list):
                            self._catalog_counts["rejection_counts"].update(
                                {
                                    str(item[0]): int(item[1])
                                    for item in saved_items
                                    if isinstance(item, (list, tuple))
                                    and len(item) == 2
                                }
                            )
                        duration = _decimal(run.get("duration_seconds"))
                        self._catalog_scan_duration_seconds = (
                            float(duration) if duration is not None else None
                        )
                        self._catalog_last_attempt_at = (
                            _timestamp_or_none(run.get("completed_at"))
                            or _timestamp_or_none(run.get("started_at"))
                            or self._catalog_last_attempt_at
                        )
                    elif scope == "event" and self._catalog_last_event_run is None:
                        self._catalog_last_event_run = dict(run)
                        if run.get("status") == "completed":
                            self._activity_scan_due_at = _timestamp_or_none(
                                run.get("completed_at")
                            )
            self._catalog_status = (
                "degraded"
                if isinstance(self._catalog_last_full_run, Mapping)
                and self._catalog_last_full_run.get("status") == "failed"
                else "stale"
            )
            self._relations_failed = False
        except Exception as exc:
            self._catalog_status = "degraded"
            self._relations_failed = True
            self._record_error(exc, "relations")

    def _relation_catalog_snapshot(self, now: datetime) -> dict[str, object]:
        status = self._catalog_status
        if status not in {"scanning", "degraded", "unavailable"}:
            status = (
                "stale"
                if self._catalog_full_scanned_at is None
                or _age(now, self._catalog_full_scanned_at)
                > RELATION_CATALOG_FRESHNESS_SECONDS
                else "healthy"
            )
        return {
            "status": status,
            "scanned_at": self._catalog_full_scanned_at,
            "full_scanned_at": self._catalog_full_scanned_at,
            "age_seconds": _display_age(_age(now, self._catalog_full_scanned_at)),
            "scan_started_at": self._catalog_scan_started_at,
            "duration_seconds": self._catalog_scan_duration_seconds,
            "last_full_run": copy.deepcopy(self._catalog_last_full_run),
            "last_event_run": copy.deepcopy(self._catalog_last_event_run),
            "activity_scan_due_at": self._activity_scan_due_at,
            "activity_scan_due": self._activity_scan_due_at is not None,
            **copy.deepcopy(self._catalog_counts),
            "relation_count": len(self._relations),
        }

    async def _run_full_relation_scan(self, client: object) -> None:
        started = self._now()
        self._catalog_last_attempt_at = started
        self._catalog_scan_started_at = started
        self._catalog_status = "scanning"
        self._catalog_last_full_run = {
            "scope": "full",
            "status": "scanning",
            "started_at": started,
            "completed_at": None,
        }
        self._log_relation_scan(phase="started", status="scanning", scope="full")
        try:
            list_events = getattr(client, "list_events", None)
            if not callable(list_events):
                raise RuntimeError("public client has no list_events")
            raw = await _call(
                list_events,
                closed=False,
                ended=False,
                page_size=THRESHOLD_BOOK_BATCH_SIZE,
            )
            events = await _collect(raw)
            resolver = getattr(self._relation_discovery, "discover", self._relation_discovery)
            if not callable(resolver):
                raise RuntimeError("relation discovery is not callable")
            result = self._discovery_result(await _call(resolver, events), events)
            relations = tuple(result.relations)
            payload = {
                "relations": [threshold_relation_payload(relation) for relation in relations]
            }
            completed = self._now()
            self._store.save_relation_state(
                payload,
                full_scanned_at=completed.isoformat(),
            )
            self._set_relation_state(relations, events, scanned_at=completed)
            self._catalog_full_scanned_at = completed
            self._catalog_last_attempt_at = completed
            self._catalog_counts = self._result_counts(result)
            self._catalog_scan_duration_seconds = max(
                0.0, (completed - started).total_seconds()
            )
            self._catalog_status = "healthy"
            self._relations_failed = False
            self._catalog_last_full_run = {
                "scope": "full",
                "status": "completed",
                "started_at": started,
                "completed_at": completed,
                "duration_seconds": self._catalog_scan_duration_seconds,
                **self._catalog_counts,
            }
            try:
                self._store.record_relation_scan(
                    scope="full",
                    status="completed",
                    started_at=started.isoformat(),
                    completed_at=completed.isoformat(),
                    payload={
                        **self._catalog_counts,
                        "duration_seconds": self._catalog_scan_duration_seconds,
                        "rejection_counts_items": [
                            [str(key), int(value)]
                            for key, value in self._catalog_counts[
                                "rejection_counts"
                            ].items()
                        ],
                    },
                )
            except Exception as exc:
                self._store_failed = True
                self._record_error(exc, "store")
            self._log_relation_scan(
                phase="completed",
                status="healthy",
                scope="full",
                event_count=len(events),
                relation_count=len(relations),
            )
        except Exception as exc:
            completed = self._now()
            self._catalog_scan_duration_seconds = max(
                0.0, (completed - started).total_seconds()
            )
            self._catalog_status = "degraded"
            self._relations_failed = True
            self._record_error(exc, "relations")
            self._catalog_last_full_run = {
                "scope": "full",
                "status": "failed",
                "started_at": started,
                "completed_at": completed,
                "duration_seconds": self._catalog_scan_duration_seconds,
                "reason": type(exc).__name__,
            }
            try:
                self._store.record_relation_scan(
                    scope="full",
                    status="failed",
                    started_at=started.isoformat(),
                    completed_at=completed.isoformat(),
                    payload={"reason": type(exc).__name__},
                )
            except Exception as record_exc:
                self._store_failed = True
                self._record_error(record_exc, "store")
            self._log_relation_scan(
                phase="failed",
                status="degraded",
                scope="full",
                reason=type(exc).__name__,
            )

    def _set_relation_state(
        self,
        relations: Sequence[ThresholdRelation],
        events: Sequence[object],
        *,
        scanned_at: datetime | None = None,
    ) -> None:
        relation_map = {relation.relation_id: relation for relation in relations}
        token_map: dict[str, set[str]] = {}
        volumes: dict[str, Decimal] = {}
        for raw_event in events:
            event_id = _event_id(raw_event)
            if event_id is None:
                continue
            metrics = _value(raw_event, "metrics", default=None)
            volume = _decimal(
                _value(
                    metrics,
                    "volume_24hr",
                    "volume24hr",
                    default=_value(raw_event, "volume_24h", "volume24hr", default=0),
                )
            ) or Decimal("0")
            for relation in relations:
                if relation.event_id == event_id:
                    volumes[relation.relation_id] = volume
        for relation in relations:
            if relation.relation_id not in volumes:
                volumes[relation.relation_id] = relation.event_volume_24h or Decimal("0")
        for relation in relations:
            for token in (
                relation.market_a.yes_token_id,
                relation.market_a.no_token_id,
                relation.market_b.yes_token_id,
                relation.market_b.no_token_id,
            ):
                token_map.setdefault(token, set()).add(relation.relation_id)
        with self._lock:
            self._relations = relation_map
            self._relation_by_token = token_map
            self._relation_volumes = volumes
            self._relations_at = scanned_at or self._now()
            self._relations_failed = False

    async def _refresh_relation_event(self, client: object, event_id: str) -> bool:
        event_id = str(event_id).strip()
        started = self._now()
        self._catalog_last_event_run = {
            "scope": "event",
            "event_id": event_id,
            "status": "scanning",
            "started_at": started,
            "completed_at": None,
        }
        try:
            get_event = getattr(client, "get_event", None)
            if not callable(get_event):
                raise RuntimeError("public client has no get_event")
            raw_event = await _call(get_event, id=event_id)
            if _event_id(raw_event) != event_id:
                raise RuntimeError("event response ID mismatch")
            resolver = getattr(self._relation_discovery, "discover", self._relation_discovery)
            if not callable(resolver):
                raise RuntimeError("relation discovery is not callable")
            result = self._discovery_result(
                await _call(resolver, (raw_event,)),
                (raw_event,),
            )
            refreshed = tuple(
                relation for relation in result.relations if relation.event_id == event_id
            )
            remaining = tuple(
                relation
                for relation in self._relations.values()
                if relation.event_id != event_id
            )
            old_volumes = dict(self._relation_volumes)
            merged = remaining + refreshed
            full_scanned_at = self._catalog_full_scanned_at or self._stored_full_scanned_at()
            if full_scanned_at is None:
                raise RuntimeError("full relation catalog anchor unavailable")
            completed = self._now()
            payload = {
                "relations": [threshold_relation_payload(relation) for relation in merged]
            }
            self._store.save_relation_state(
                payload,
                full_scanned_at=full_scanned_at.isoformat(),
            )
            self._set_relation_state(
                merged,
                (raw_event,),
                scanned_at=full_scanned_at,
            )
            for relation_id, volume in old_volumes.items():
                if relation_id in self._relations and relation_id not in {
                    relation.relation_id for relation in refreshed
                }:
                    self._relation_volumes[relation_id] = volume
            self._relations_failed = False
            self._activity_scan_due_at = completed
            self._catalog_status = (
                "healthy"
                if _age(completed, full_scanned_at)
                <= RELATION_CATALOG_FRESHNESS_SECONDS
                else "stale"
            )
            self._catalog_last_event_run = {
                "scope": "event",
                "event_id": event_id,
                "status": "completed",
                "started_at": started,
                "completed_at": completed,
                "duration_seconds": max(0.0, (completed - started).total_seconds()),
                **self._result_counts(result),
            }
            try:
                self._store.record_relation_scan(
                    scope="event",
                    status="completed",
                    started_at=started.isoformat(),
                    completed_at=completed.isoformat(),
                    payload={
                        **self._result_counts(result),
                        "duration_seconds": max(
                            0.0, (completed - started).total_seconds()
                        ),
                        "rejection_counts_items": [
                            [str(key), int(value)]
                            for key, value in result.rejection_counts.items()
                        ],
                    },
                    event_id=event_id,
                )
            except Exception as exc:
                self._store_failed = True
                self._record_error(exc, "store")
            self._log_relation_scan(
                phase="event",
                status="healthy",
                scope="event",
                event_id=event_id,
                relation_count=len(refreshed),
            )
        except Exception as exc:
            completed = self._now()
            self._catalog_status = "degraded"
            self._relations_failed = True
            self._record_error(exc, "relations")
            self._catalog_last_event_run = {
                "scope": "event",
                "event_id": event_id,
                "status": "failed",
                "started_at": started,
                "completed_at": completed,
                "duration_seconds": max(0.0, (completed - started).total_seconds()),
                "reason": type(exc).__name__,
            }
            try:
                self._store.record_relation_scan(
                    scope="event",
                    status="failed",
                    started_at=started.isoformat(),
                    completed_at=completed.isoformat(),
                    payload={"reason": type(exc).__name__},
                    event_id=event_id,
                )
            except Exception as record_exc:
                self._store_failed = True
                self._record_error(record_exc, "store")
            self._log_relation_scan(
                phase="event",
                status="degraded",
                scope="event",
                event_id=event_id,
                reason=type(exc).__name__,
            )
            return False
        try:
            await self._subscribe(client)
        except Exception as exc:
            self._record_error(exc, "stream")
        return True

    async def _refresh_relation_books(self, client: object) -> None:
        get_books = getattr(client, "get_order_books", None)
        if not callable(get_books):
            raise RuntimeError("public client has no paired order-book read")
        tokens = sorted(self._relation_by_token)
        if not tokens:
            self._relation_books = {}
            return
        chunks = [
            tokens[index : index + THRESHOLD_BOOK_BATCH_SIZE]
            for index in range(0, len(tokens), THRESHOLD_BOOK_BATCH_SIZE)
        ]
        semaphore = asyncio.Semaphore(PUBLIC_BOOK_CONCURRENCY)

        async def fetch(chunk: list[str]) -> dict[str, ThresholdOrderBook]:
            async with semaphore:
                raw_books = await _call(get_books, token_ids=chunk)
                result: dict[str, ThresholdOrderBook] = {}
                received_at = self._now()
                for raw_book in _items(raw_books):
                    token = _value(
                        raw_book,
                        "token_id",
                        "asset_id",
                        "assetId",
                        default=None,
                    )
                    if not isinstance(token, str) or token not in chunk:
                        continue
                    asks = _asks(_value(raw_book, "asks", default=()))
                    bid_levels = _asks(_value(raw_book, "bids", default=()))
                    bids = bid_levels or ()
                    if asks is None:
                        continue
                    result[token] = ThresholdOrderBook(
                        token_id=token,
                        asks=asks,
                        bids=bids,
                        confirmed_at=received_at,
                    )
                return result

        fetched = await asyncio.gather(*(fetch(chunk) for chunk in chunks))
        books: dict[str, ThresholdOrderBook] = {}
        for batch in fetched:
            books.update(batch)
        self._relation_books = books

    async def _refresh_relation_opportunities(
        self, client: object
    ) -> tuple[dict[str, object], ...]:
        await self._refresh_relation_books(client)
        readiness = self._readiness or {}
        now = self._now()
        balance = _decimal(readiness.get("balance", readiness.get("p_usd_balance"))) or Decimal("0")
        allowance = _decimal(readiness.get("allowance", readiness.get("p_usd_allowance"))) or Decimal("0")
        rows: list[dict[str, object]] = []
        for relation in self._relations.values():
            candidate = build_threshold_hedge_intent(
                relation,
                self._relation_books,
                require_safe_unwind=False,
            )
            if candidate is None or candidate.minimum_profit <= 0:
                self._close_signal(relation.relation_id, "nonpositive_profit")
                continue
            validation = await self._validate_relation(relation)
            safe_intent = build_threshold_hedge_intent(
                relation,
                self._relation_books,
            )
            confirmed_at = min(
                self._relation_books[token].confirmed_at
                for token in (
                    relation.buy_leg_a.token_id,
                    relation.buy_leg_b.token_id,
                )
                if token in self._relation_books
            )
            confirmed_age = _age(now, confirmed_at)
            end_date = _timestamp_or_none(relation.market_b.end_date)
            remaining_days = (
                Decimal(str((end_date - now).total_seconds()))
                / Decimal("86400")
                if end_date is not None and end_date > now
                else None
            )
            annualized = (
                simple_annualized_yield(
                    candidate,
                    now=now,
                    resolution_at=end_date,
                )
                if end_date is not None
                else None
            )
            status = getattr(validation, "status", "llm_unavailable")
            reason_codes = tuple(getattr(validation, "reason_codes", ()))
            summary = str(getattr(validation, "summary", ""))
            if self._relation_validator is None:
                status = "llm_unavailable"
                reason_codes = ("RELATION_VALIDATOR_UNAVAILABLE",)
                summary = "关系校验器未配置，当前不可下单。"
            if status == "approved":
                eligibility_reason = "actionable"
            elif status == "llm_rejected":
                eligibility_reason = "llm_rejected"
            elif status == "deterministic_rejected":
                eligibility_reason = "deterministic_rejected"
            else:
                eligibility_reason = "llm_unavailable"
            if status == "approved" and safe_intent is None:
                eligibility_reason = "remediation_unsafe"
            elif status == "approved" and confirmed_age > BOOK_FRESHNESS_SECONDS:
                eligibility_reason = "book_stale"
            elif status == "approved" and not self._readiness_ready(readiness):
                eligibility_reason = "readiness_unavailable"
            elif status == "approved" and (
                _age(now, readiness.get("checked_at")) > READINESS_FRESHNESS_SECONDS
            ):
                eligibility_reason = "readiness_stale"
            elif status == "approved" and safe_intent is not None and (
                safe_intent.total_max_cost > balance
                or safe_intent.total_max_cost > allowance
            ):
                eligibility_reason = "insufficient_funds"
            actionable = status == "approved" and eligibility_reason == "actionable"
            intent = safe_intent
            row = self._relation_row(
                relation,
                candidate,
                intent,
                validation,
                volume=self._relation_volumes.get(relation.relation_id, Decimal("0")),
                confirmed_at=confirmed_at,
                confirmed_age=confirmed_age,
                resolution_at=relation.market_b.end_date,
                remaining_days=remaining_days,
                annualized=annualized,
                actionable=actionable,
                eligibility_reason=eligibility_reason,
                reason_codes=reason_codes,
                summary=summary,
            )
            rows.append(row)
            self._upsert_signal(row)
        self._log_relation_scan(
            phase="books",
            status="healthy",
            relation_count=len(self._relations),
            positive_count=len(rows),
        )
        return tuple(rows)

    async def _validate_relation(self, relation: ThresholdRelation) -> object:
        validator = self._relation_validator
        if validator is None:
            return RelationValidation(
                status="llm_unavailable",
                decision=None,
                relation=None,
                summary="关系校验器未配置，当前不可下单。",
                reason_codes=("RELATION_VALIDATOR_UNAVAILABLE",),
                evidence=(),
                uncertainties=(),
                model="",
                prompt_version="",
                cache_key="",
                cached=False,
                structured_result=None,
            )
        method = getattr(validator, "validate", validator)
        if not callable(method):
            raise RuntimeError("relation validator is not callable")
        return await asyncio.to_thread(method, relation)

    @staticmethod
    def _relation_row(
        relation: ThresholdRelation,
        candidate: ThresholdHedgeIntent,
        intent: ThresholdHedgeIntent | None,
        validation: object,
        *,
        volume: Decimal,
        confirmed_at: datetime,
        confirmed_age: float,
        resolution_at: str,
        remaining_days: Decimal | None,
        annualized: Decimal | None,
        actionable: bool,
        eligibility_reason: str,
        reason_codes: tuple[object, ...],
        summary: str,
    ) -> dict[str, object]:
        selected = intent or candidate
        legs = (selected.leg_a, selected.leg_b)
        structured = getattr(validation, "structured_result", None)
        proof = structured.get("proof") if isinstance(structured, Mapping) else None
        return {
            "opportunity_id": relation.relation_id,
            "relation_id": relation.relation_id,
            "event_id": relation.event_id,
            "market_id": relation.relation_id,
            "market_type": "threshold_hedge",
            "question": f"{relation.market_a.question} / {relation.market_b.question}",
            "question_a": relation.market_a.question,
            "question_b": relation.market_b.question,
            "condition_id_a": relation.market_a.condition_id,
            "condition_id_b": relation.market_b.condition_id,
            "token_id_a": relation.market_a.yes_token_id,
            "token_id_b": relation.market_b.yes_token_id,
            "relation": relation.relation,
            "rules_hash_a": relation.rules_hash_a,
            "rules_hash_b": relation.rules_hash_b,
            "buy_legs": [
                {
                    "label": leg.label,
                    "market_id": leg.market_id,
                    "condition_id": leg.condition_id,
                    "outcome": leg.outcome,
                    "token_id": leg.token_id,
                    "quantity": leg.quantity,
                    "max_price": leg.max_price,
                    "max_cost": leg.max_cost,
                }
                for leg in legs
            ],
            "volume_24h": volume,
            "confirmed_at": confirmed_at,
            "confirmed_age_seconds": confirmed_age,
            "resolution_at": resolution_at,
            "remaining_days": remaining_days,
            "profit": candidate.minimum_profit,
            "estimated_profit": candidate.minimum_profit,
            "net_edge": candidate.net_edge,
            "quantity": candidate.quantity,
            "maximum_fee": candidate.maximum_fee,
            "total_max_cost": candidate.total_max_cost,
            "minimum_payout": candidate.minimum_payout,
            "annualized_yield": annualized,
            "remediation_safe": intent is not None,
            "actionable": actionable,
            "eligibility": "actionable" if actionable else "visible_positive",
            "eligibility_reason": eligibility_reason,
            "llm_status": getattr(validation, "status", "llm_unavailable"),
            "llm_decision": getattr(validation, "decision", None),
            "llm_relation": getattr(validation, "relation", None),
            "llm_summary": summary,
            "llm_reason_codes": list(reason_codes),
            "llm_evidence": copy.deepcopy(getattr(validation, "evidence", ())),
            "llm_uncertainties": list(getattr(validation, "uncertainties", ())),
            "llm_proof": copy.deepcopy(proof),
            "codex_cached": bool(getattr(validation, "cached", False)),
            "codex_model": getattr(validation, "model", ""),
            "prompt_version": getattr(validation, "prompt_version", ""),
            "cache_key": getattr(validation, "cache_key", ""),
            "intent": intent,
        }

    def _normalize_event(self, value: object) -> dict[str, object] | None:
        event_id = _value(value, "id", "event_id", "eventId", default=None)
        state = _value(value, "state", default=None)
        active = _state_flag(state, "active", default=_value(value, "active", default=None))
        closed = _state_flag(state, "closed", default=_value(value, "closed", default=None))
        ended = _state_flag(state, "ended", default=_value(value, "ended", default=None))
        title = _value(value, "title", "name", default=None)
        slug = _value(value, "slug", default=None)
        metrics = _value(value, "metrics", default=None)
        volume = _decimal(_value(metrics, "volume_24hr", "volume24hr", default=_value(value, "volume_24hr", "volume24hr", default=None)))
        if (
            not isinstance(event_id, (str, int))
            or not str(event_id).strip()
            or not isinstance(title, str)
            or not title.strip()
            or active is not True
            or closed is True
            or ended is True
            or volume is None
            or volume < 0
        ):
            return None
        return {
            "event_id": str(event_id),
            "title": title,
            "slug": str(slug or ""),
            "volume_24h": volume,
            "profit": None,
            "actionable": False,
            "markets": [],
            "_raw_markets": _value(value, "markets", default=()),
        }

    def _normalize_market(
        self, event_row: Mapping[str, object], value: object
    ) -> dict[str, object] | None:
        market_id = _value(value, "id", "market_id", "marketId", default=None)
        condition_id = _value(value, "condition_id", "conditionId", "condition", default=None)
        question = _value(value, "question", default=None)
        slug = _value(value, "slug", default=None)
        state = _value(value, "state", default=None)
        active = _state_flag(state, "active", default=_value(value, "active", default=None))
        closed = _state_flag(state, "closed", default=_value(value, "closed", default=None))
        accepting = _state_flag(state, "accepting_orders", default=_value(value, "accepting_orders", "acceptingOrders", default=None))
        order_book_enabled = _state_flag(state, "enable_order_book", default=_value(value, "enable_order_book", "enableOrderBook", default=None))
        if active is not True or closed is True or accepting is not True or order_book_enabled is not True:
            return None
        outcomes = _outcome_pairs(_value(value, "outcomes", default=None))
        if (
            not isinstance(market_id, (str, int))
            or not str(market_id).strip()
            or not isinstance(condition_id, str)
            or not condition_id.strip()
            or not isinstance(question, str)
            or not question.strip()
            or not isinstance(slug, str)
            or not slug.strip()
            or outcomes is None
        ):
            return None
        trading = _value(value, "trading", default=None)
        fees_enabled = _value(trading, "fees_enabled", "feesEnabled", default=_value(value, "fees_enabled", "feesEnabled", default=None))
        neg_risk = _value(state, "neg_risk", "negRisk", default=_value(trading, "neg_risk", "negRisk", default=None))
        if neg_risk is True:
            reason = "neg_risk"
        elif fees_enabled is not False:
            reason = "fee_unverified_or_enabled"
        else:
            reason = "pending_confirmation"
        return {
            "event_id": event_row["event_id"],
            "market_id": str(market_id),
            "condition_id": condition_id,
            "slug": slug,
            "question": question,
            "volume_24h": event_row["volume_24h"],
            "yes_token_id": outcomes["yes"],
            "no_token_id": outcomes["no"],
            "fees_enabled": fees_enabled,
            "neg_risk": neg_risk,
            "minimum_order_size": _decimal(_value(trading, "minimum_order_size", "minimumOrderSize", default=None)),
            "tick_size": _decimal(_value(trading, "minimum_tick_size", "minimumTickSize", default=None)),
            "eligibility_reason": reason,
            "actionable": False,
            "gross_upper_bound": None,
        }

    async def _refresh_readiness(self) -> None:
        now = self._now()
        try:
            method = getattr(self._trading, "readiness_snapshot", None)
            if callable(method):
                value = await _call(method)
                readiness = dict(value) if isinstance(value, Mapping) else self._object_dict(value)
                if "geoblock" not in readiness:
                    geoblock_method = getattr(self._trading, "geoblock_allowed", None)
                    if callable(geoblock_method):
                        geoblock = await _call(geoblock_method)
                        readiness["geoblock"] = "allowed" if geoblock is True else "blocked"
            else:
                account = await _call(getattr(self._trading, "account_snapshot"))
                readiness = self._object_dict(account)
                geoblock = await _call(getattr(self._trading, "geoblock_allowed"))
                readiness["geoblock"] = "allowed" if geoblock is True else "blocked"
                if readiness.get("wallet_address"):
                    readiness["wallet"] = "ready"
                readiness["relayer"] = self._derive_relayer_readiness()
            checked_at = _timestamp_or_none(readiness.get("checked_at"))
            readiness["checked_at"] = checked_at
            self._readiness = readiness
        except Exception as exc:
            self._readiness = {"status": "unavailable", "checked_at": None, "error": type(exc).__name__}
            self._record_error(exc, "readiness")

    def _derive_relayer_readiness(self) -> str:
        """Use Task 2's safe merge-capability fact without signing."""

        client = getattr(self._trading, "_client", None)
        return "ready" if callable(getattr(client, "merge_positions", None)) else "unavailable"

    @staticmethod
    def _object_dict(value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            return dict(value)
        result: dict[str, object] = {}
        for name in (
            "wallet",
            "wallet_address",
            "geoblock",
            "relayer",
            "relayer_readiness",
            "balance",
            "allowance",
            "p_usd_balance",
            "p_usd_allowance",
            "checked_at",
        ):
            item = _value(value, name, default=None)
            if item is not None:
                result[name] = item
        return result

    async def _confirm_market(self, client: object, market_row: dict[str, object]) -> dict[str, object] | None:
        market_id = str(market_row["market_id"])
        if market_row.get("fees_enabled") is not False:
            market_row["eligibility_reason"] = "fee_unverified_or_enabled"
        if market_row.get("neg_risk") is True:
            market_row["eligibility_reason"] = "neg_risk"
        get_books = getattr(client, "get_order_books", None)
        if not callable(get_books):
            raise RuntimeError("public client has no paired order-book read")
        yes_token = str(market_row["yes_token_id"])
        no_token = str(market_row["no_token_id"])
        raw_books = await _call(get_books, token_ids=[yes_token, no_token])
        books_by_token: dict[str, object] = {}
        if isinstance(raw_books, Mapping):
            books_by_token = {str(key): item for key, item in raw_books.items()}
        else:
            for item in _items(raw_books):
                token = _value(item, "token_id", "asset_id", "assetId", default=None)
                if isinstance(token, str):
                    books_by_token[token] = item
        yes_book = books_by_token.get(yes_token)
        no_book = books_by_token.get(no_token)
        if yes_book is None or no_book is None:
            market_row["eligibility_reason"] = "book_token_mismatch"
            return None
        yes_asks = _asks(_value(yes_book, "asks", default=()))
        no_asks = _asks(_value(no_book, "asks", default=()))
        if yes_asks is None or no_asks is None:
            market_row["eligibility_reason"] = "book_unavailable"
            return None
        now = self._now()
        yes_timestamp = _timestamp_or_none(_value(yes_book, "timestamp", default=None))
        no_timestamp = _timestamp_or_none(_value(no_book, "timestamp", default=None))
        if yes_timestamp is None or no_timestamp is None:
            market_row["eligibility_reason"] = "book_timestamp_missing"
            return None
        confirmed_at = max(yes_timestamp, no_timestamp)
        books = ConfirmedBooks(
            yes_token_id=yes_token,
            no_token_id=no_token,
            yes_asks=yes_asks,
            no_asks=no_asks,
            confirmed_at=confirmed_at,
        )
        self._books[market_id] = books
        age = _age(now, confirmed_at)
        market_row["confirmed_at"] = confirmed_at
        market_row["confirmed_age_seconds"] = age
        market_row["gross_upper_bound"] = max(
            Decimal("0"),
            Decimal("1") - min(yes_asks, key=lambda level: level.price).price - min(no_asks, key=lambda level: level.price).price,
        )
        if age > BOOK_FRESHNESS_SECONDS:
            market_row["eligibility_reason"] = "books_stale"
            return None
        if market_row.get("fees_enabled") is not False:
            return None
        if market_row.get("neg_risk") is True:
            return None
        minimum = market_row.get("minimum_order_size")
        tick = market_row.get("tick_size")
        if not isinstance(minimum, Decimal):
            minimum = _decimal(_value(yes_book, "min_order_size", "minimum_order_size", default=None))
        if not isinstance(tick, Decimal):
            tick = _decimal(_value(yes_book, "tick_size", "minimum_tick_size", default=None))
        if minimum is None or tick is None:
            market_row["eligibility_reason"] = "market_facts_unavailable"
            return None
        readiness = self._readiness or {}
        readiness_age = _age(now, readiness.get("checked_at") if isinstance(readiness.get("checked_at"), datetime) else None)
        if readiness_age > READINESS_FRESHNESS_SECONDS:
            market_row["eligibility_reason"] = "readiness_stale"
            return None
        if not self._readiness_ready(readiness):
            market_row["eligibility_reason"] = "readiness_unavailable"
            return None
        balance = _decimal(readiness.get("balance", readiness.get("p_usd_balance"))) or Decimal("0")
        allowance = _decimal(readiness.get("allowance", readiness.get("p_usd_allowance"))) or Decimal("0")
        facts = MarketFacts(
            event_id=str(market_row["event_id"]),
            market_id=market_id,
            condition_id=str(market_row["condition_id"]),
            slug=str(market_row["slug"]),
            question=str(market_row["question"]),
            volume_24h=market_row["volume_24h"],  # type: ignore[arg-type]
            minimum_order_size=minimum,
            tick_size=tick,
            fee_verified_zero=True,
            neg_risk=False,
        )
        intent = build_pair_intent(facts, books, balance=balance, allowance=allowance)
        if intent is None:
            market_row["eligibility_reason"] = "no_threshold_candidate"
            return None
        opportunity_id = f"{market_row['event_id']}:{market_id}"
        opportunity = {
            "opportunity_id": opportunity_id,
            "event_id": market_row["event_id"],
            "market_id": market_id,
            "condition_id": market_row["condition_id"],
            "question": market_row["question"],
            "market_type": "standard_binary",
            "fee_status": "fee_free",
            "volume_24h": market_row["volume_24h"],
            "actionable": True,
            "eligibility": "actionable",
            "eligibility_reason": "actionable",
            "confirmed_at": confirmed_at,
            "confirmed_age_seconds": age,
            "profit": intent.minimum_profit,
            "estimated_profit": intent.minimum_profit,
            "net_edge": intent.net_edge,
            "quantity": intent.quantity,
            "yes_token_id": intent.yes_token_id,
            "no_token_id": intent.no_token_id,
            "yes_max_price": intent.yes_max_price,
            "no_max_price": intent.no_max_price,
            "yes_max_cost": intent.yes_max_cost,
            "no_max_cost": intent.no_max_cost,
            "total_max_cost": intent.total_max_cost,
            "intent": intent,
        }
        market_row.update({"actionable": True, "eligibility_reason": "actionable", "profit": intent.minimum_profit})
        self._upsert_signal(opportunity)
        return opportunity

    @staticmethod
    def _readiness_ready(readiness: Mapping[str, object]) -> bool:
        for key in ("wallet", "wallet_ready"):
            if key in readiness and readiness[key] not in (True, "ready", "allowed", "pass"):
                return False
        for key in ("geoblock", "relayer", "relayer_readiness"):
            if key in readiness and readiness[key] not in (True, "ready", "allowed", "pass"):
                return False
        if "geoblock" not in readiness:
            return False
        if "relayer" not in readiness and "relayer_readiness" not in readiness:
            return False
        if str(readiness.get("status", "ready")).casefold() in {"unavailable", "blocked", "fail", "failed"}:
            return False
        return "balance" in readiness or "p_usd_balance" in readiness

    async def _process_stream_event(self, client: object, message: object) -> None:
        self._stream_message_at = self._now()
        self._heartbeat_at = self._stream_message_at
        message_type = _value(message, "type", "event_type", default="")
        payload = _value(message, "payload", default=message)
        if message_type in {"new_market", "market_resolved"}:
            event_message = _value(payload, "event_message", "eventMessage", default=None)
            event_id = _event_id(event_message)
            if event_id is None:
                event_id = _event_id(payload)
            if event_id is not None and self._relation_discovery is not None:
                if await self._refresh_relation_event(client, event_id):
                    relation_rows = await self._refresh_relation_opportunities(client)
                    with self._lock:
                        self._opportunities = {
                            key: value
                            for key, value in self._opportunities.items()
                            if value.get("market_type") != "threshold_hedge"
                        }
                        self._opportunities.update(
                            {str(row["opportunity_id"]): row for row in relation_rows}
                        )
            return
        tokens: list[str] = []
        token = _value(payload, "token_id", "asset_id", "assetId", default=None)
        if isinstance(token, str):
            tokens.append(token)
        for change in _items(_value(payload, "price_changes", "priceChanges", default=())):
            changed_token = _value(change, "token_id", "asset_id", "assetId", default=None)
            if isinstance(changed_token, str):
                tokens.append(changed_token)
        standard_market_ids: set[str] = set()
        relation_ids: set[str] = set()
        for token_id in set(tokens):
            market_id = self._market_by_token.get(token_id)
            if market_id is not None:
                standard_market_ids.add(market_id)
            relation_ids.update(self._relation_by_token.get(token_id, set()))
        for market_id in standard_market_ids:
            market_row = self._markets.get(market_id)
            if market_row is None:
                continue
            if isinstance(token, str):
                self._update_stream_book(market_id, token, payload)
            opportunity = await self._confirm_market(client, market_row)
            with self._lock:
                opportunity_id = f"{market_row['event_id']}:{market_id}"
                previous = self._opportunities.pop(opportunity_id, None)
                if opportunity is not None:
                    self._opportunities[opportunity_id] = opportunity
            if opportunity is None and previous is not None:
                self._close_signal(market_id, "threshold_or_freshness")
        if relation_ids:
            relation_rows = await self._refresh_relation_opportunities(client)
            with self._lock:
                self._opportunities = {
                    key: value
                    for key, value in self._opportunities.items()
                    if value.get("market_type") != "threshold_hedge"
                }
                self._opportunities.update(
                    {str(row["opportunity_id"]): row for row in relation_rows}
                )
        self._sync_event_rows()

    def _update_stream_book(self, market_id: str, token: str, payload: object) -> None:
        asks = _asks(_value(payload, "asks", default=()))
        if asks is None:
            return
        current = self._books.get(market_id)
        if current is None:
            return
        if token == current.yes_token_id:
            self._books[market_id] = ConfirmedBooks(
                yes_token_id=current.yes_token_id,
                no_token_id=current.no_token_id,
                yes_asks=asks,
                no_asks=current.no_asks,
                confirmed_at=self._now(),
            )
        elif token == current.no_token_id:
            self._books[market_id] = ConfirmedBooks(
                yes_token_id=current.yes_token_id,
                no_token_id=current.no_token_id,
                yes_asks=current.yes_asks,
                no_asks=asks,
                confirmed_at=self._now(),
            )

    def _sync_event_rows(self) -> None:
        by_event: dict[str, list[dict[str, object]]] = {}
        for market_row in self._markets.values():
            by_event.setdefault(str(market_row["event_id"]), []).append(market_row)
        for event_id, event_row in self._events.items():
            markets = by_event.get(event_id, [])
            actionable = [row for row in markets if row.get("actionable") is True]
            event_row["markets"] = copy.deepcopy(markets)
            event_row["market_count"] = len(markets)
            event_row["actionable"] = bool(actionable)
            event_row["profit"] = max(
                (row.get("profit") for row in actionable if isinstance(row.get("profit"), Decimal)),
                default=None,
            )
            event_row["gross_upper_bound"] = max(
                (row.get("gross_upper_bound") for row in markets if isinstance(row.get("gross_upper_bound"), Decimal)),
                default=None,
            )

    def _upsert_signal(self, opportunity: Mapping[str, object]) -> None:
        market_id = str(opportunity["market_id"])
        peak_edge = opportunity.get("net_edge")
        peak_quantity = opportunity.get("quantity")
        peak_profit = opportunity.get("estimated_profit")
        try:
            for row in self._store.signal_history("all"):
                if row.get("market_id") == market_id and row.get("ended_at") is None:
                    peak_edge = self._max_decimal(peak_edge, row.get("peak_net_edge", row.get("net_edge")))
                    peak_quantity = self._max_decimal(peak_quantity, row.get("peak_quantity", row.get("quantity")))
                    peak_profit = self._max_decimal(peak_profit, row.get("peak_estimated_profit", row.get("estimated_profit")))
                    break
            self._store.upsert_signal(
                {
                    "event_id": opportunity["event_id"],
                    "market_id": market_id,
                    "question": opportunity["question"],
                    "started_at": self._now(),
                    "last_seen_at": self._now(),
                    "net_edge": opportunity.get("net_edge"),
                    "quantity": opportunity.get("quantity"),
                    "estimated_profit": opportunity.get("estimated_profit"),
                    "peak_net_edge": peak_edge,
                    "peak_quantity": peak_quantity,
                    "peak_estimated_profit": peak_profit,
                    "volume_24h": opportunity.get("volume_24h"),
                    "market_type": opportunity.get("market_type", "standard_binary"),
                    "annualized_yield": opportunity.get("annualized_yield"),
                    "llm_status": opportunity.get("llm_status"),
                    "llm_reason_codes": opportunity.get("llm_reason_codes"),
                }
            )
        except Exception as exc:
            self._store_failed = True
            self._record_error(exc, "store")

    def _close_signal(self, market_id: str, reason: str) -> None:
        try:
            self._store.close_signal(market_id, ended_at=self._now(), reason=reason)
        except Exception as exc:
            self._store_failed = True
            self._record_error(exc, "store")

    @staticmethod
    def _max_decimal(left: object, right: object) -> object:
        left_decimal = _decimal(left)
        right_decimal = _decimal(right)
        if left_decimal is not None and right_decimal is not None:
            return max(left_decimal, right_decimal)
        return left_decimal if left_decimal is not None else right_decimal

    def _record_error(self, exc: BaseException, component: str) -> None:
        self._diagnostics["last_error"] = f"{component}:{type(exc).__name__}"
        if component == "universe":
            self._universe_failed = True
        if component == "relations":
            self._relations_failed = True

    def _relation_health(self, now: datetime) -> dict[str, object]:
        if self._relation_discovery is None or self._relation_validator is None:
            status = "unavailable"
        elif self._catalog_status == "degraded" or self._relations_failed:
            status = "degraded"
        elif self._relations_at is None:
            status = "stale"
        elif _age(now, self._relations_at) > RELATION_CATALOG_FRESHNESS_SECONDS:
            status = "stale"
        else:
            status = "healthy"
        return {
            "status": status,
            "scanned_at": self._relations_at,
            "relation_count": len(self._relations),
            "positive_count": sum(
                1
                for row in self._opportunities.values()
                if row.get("market_type") == "threshold_hedge"
            ),
        }

    def _llm_usage(self) -> dict[str, int]:
        try:
            return self._store.llm_usage_24h()
        except Exception:
            return {
                "calls": 0,
                "successes": 0,
                "failures": 0,
                "cache_hits": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            }

    @staticmethod
    def _distribution(values: Sequence[object]) -> dict[str, object]:
        parsed = sorted(
            value
            for value in (_decimal(item) for item in values)
            if value is not None
        )
        if not parsed:
            return {
                "count": 0,
                "min": None,
                "median": None,
                "p75": None,
                "p90": None,
                "max": None,
            }

        def percentile(percent: int) -> Decimal:
            index = max(0, math.ceil(len(parsed) * percent / 100) - 1)
            return parsed[index]

        return {
            "count": len(parsed),
            "min": parsed[0],
            "median": percentile(50),
            "p75": percentile(75),
            "p90": percentile(90),
            "max": parsed[-1],
        }

    def _annualized_distributions(self) -> dict[str, dict[str, object]]:
        current = [
            row.get("annualized_yield")
            for row in self._opportunities.values()
            if row.get("market_type") == "threshold_hedge"
        ]
        history_7d = self._store.signal_history("7d")
        history_30d = self._store.signal_history("30d")
        return {
            "current": self._distribution(current),
            "7d": self._distribution(
                [row.get("annualized_yield") for row in history_7d]
            ),
            "30d": self._distribution(
                [row.get("annualized_yield") for row in history_30d]
            ),
        }

    def _write_runtime(self, *, force: bool = False) -> None:
        now = self._now()
        if not force and self._last_runtime_write is not None and _age(now, self._last_runtime_write) < RUNTIME_WRITE_SECONDS:
            return
        payload = self.snapshot()
        payload.pop("readiness", None)
        # PairIntent is an in-process execution input, not a JSON/store value.
        payload["opportunities"] = [
            {key: value for key, value in item.items() if key != "intent"}
            for item in payload.get("opportunities", [])
            if isinstance(item, Mapping)
        ]
        try:
            self._store.write_runtime(payload)
            self._last_runtime_write = now
        except Exception as exc:
            self._store_failed = True
            self._record_error(exc, "store")

    def _health(self, now: datetime) -> dict[str, object]:
        reasons: list[str] = []
        if self._store_failed:
            reasons.append("store_write_failed")
        if self._universe_at is None:
            reasons.append("universe_unavailable")
        elif _age(now, self._universe_at) > UNIVERSE_STALE_SECONDS:
            reasons.append("universe_stale")
        if self._universe_failed:
            reasons.append("universe_refresh_failed")
        if self._heartbeat_at is not None and _age(now, self._heartbeat_at) > HEARTBEAT_FRESHNESS_SECONDS:
            reasons.append("heartbeat_stale")
        elif (
            self._heartbeat_at is None
            and self._stream_connected_at is not None
            and _age(now, self._stream_connected_at) > STREAM_DISCONNECT_SECONDS
        ):
            reasons.append("heartbeat_missing")
        if self._stream_disconnected_at is not None and _age(now, self._stream_disconnected_at) > STREAM_DISCONNECT_SECONDS:
            reasons.append("stream_disconnected")
        for opportunity in self._opportunities.values():
            confirmed_at = opportunity.get("confirmed_at")
            if _age(now, confirmed_at if isinstance(confirmed_at, datetime) else None) > BOOK_FRESHNESS_SECONDS:
                reasons.append("books_stale")
                break
        readiness_at = self._readiness.get("checked_at") if self._readiness else None
        if not isinstance(readiness_at, datetime) or _age(now, readiness_at) > READINESS_FRESHNESS_SECONDS:
            reasons.append("readiness_stale")
        status = "loading" if self._universe_at is None else ("degraded" if reasons else "healthy")
        return {
            "status": status,
            "degraded": status == "degraded",
            "degraded_reasons": sorted(set(reasons)),
            "actionable": status == "healthy" and bool(self._opportunities),
            "opportunity_count": len(self._opportunities),
            "heartbeat_age_seconds": _display_age(_age(now, self._heartbeat_at)),
            "universe_age_seconds": _display_age(_age(now, self._universe_at)),
            "readiness_age_seconds": _display_age(_age(now, readiness_at if isinstance(readiness_at, datetime) else None)),
        }

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = ["PolymarketMonitor"]


async def _monitor_once_diagnostic_async(
    *,
    timeout: float,
    public_client_factory: Callable[[], object] = AsyncPublicClient,
) -> dict[str, object]:
    """Run the operator's public, non-mutating integration probe."""

    client = public_client_factory()
    raw = await _call(
        getattr(client, "list_events"),
        closed=False,
        ended=False,
        order="volume24hr",
        ascending=False,
        page_size=TOP_EVENT_LIMIT,
    )
    probe = object.__new__(PolymarketMonitor)
    rows: list[dict[str, object]] = []
    for item in await _collect_first_page(raw):
        normalized = probe._normalize_event(item)
        if normalized is not None:
            rows.append(normalized)
    rows.sort(key=lambda item: (-item["volume_24h"], str(item["event_id"])))  # type: ignore[operator]
    rows = rows[:TOP_EVENT_LIMIT]
    markets: list[dict[str, object]] = []
    for row in rows:
        for item in _items(row.get("_raw_markets", ())):
            normalized_market = probe._normalize_market(row, item)
            if normalized_market is not None:
                markets.append(normalized_market)
    markets.sort(key=lambda item: (-item["volume_24h"], str(item["market_id"])))  # type: ignore[operator]
    selected = next(
        (
            item
            for item in markets
            if item.get("fees_enabled") is False and item.get("neg_risk") is not True
        ),
        markets[0] if markets else None,
    )
    result: dict[str, object] = {
        "event_count": len(rows),
        "volumes": "present" if rows and all(isinstance(row.get("volume_24h"), Decimal) for row in rows) else "missing",
        "websocket_heartbeat": "fail",
        "paired_book_read": "fail",
        "mutations": 0,
        "result": "BLOCKED",
    }
    if selected is not None:
        get_books = getattr(client, "get_order_books", None)
        yes = str(selected["yes_token_id"])
        no = str(selected["no_token_id"])
        if callable(get_books):
            try:
                books = await _call(get_books, token_ids=[yes, no])
                tokens = {
                    str(_value(book, "token_id", "asset_id", "assetId", default=""))
                    for book in _items(books)
                }
                result["paired_book_read"] = "pass" if {yes, no} <= tokens else "fail"
            except Exception:
                result["paired_book_read"] = "fail"
        subscribe = getattr(client, "subscribe", None)
        if callable(subscribe):
            handle: object | None = None
            try:
                handle = await _call(subscribe, MarketSpec(token_ids=sorted({yes, no})))
                next_message = getattr(handle, "__anext__", None)
                if callable(next_message):
                    await asyncio.wait_for(next_message(), timeout=max(0.1, timeout))
                    result["websocket_heartbeat"] = "pass"
            except Exception:
                result["websocket_heartbeat"] = "fail"
            finally:
                close = getattr(handle, "close", None)
                if callable(close):
                    try:
                        await _call(close)
                    except Exception:
                        pass
    if (
        0 < result["event_count"] <= TOP_EVENT_LIMIT  # type: ignore[operator]
        and result["volumes"] == "present"
        and result["websocket_heartbeat"] == "pass"
        and result["paired_book_read"] == "pass"
    ):
        result["result"] = "PASS"
    return result


def monitor_once_diagnostic(
    *,
    timeout: float = 30.0,
    public_client_factory: Callable[[], object] = AsyncPublicClient,
) -> dict[str, object]:
    try:
        return asyncio.run(
            asyncio.wait_for(
                _monitor_once_diagnostic_async(
                    timeout=timeout,
                    public_client_factory=public_client_factory,
                ),
                timeout=timeout,
            )
        )
    except Exception:
        return {
            "event_count": "BLOCKED",
            "volumes": "BLOCKED",
            "websocket_heartbeat": "BLOCKED",
            "paired_book_read": "BLOCKED",
            "mutations": 0,
            "result": "BLOCKED",
        }
