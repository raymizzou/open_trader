"""Read-only Predict.fun market and order-book source."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import websockets

from .polymarket_trading import KeychainError, PredictConfig, load_predict_api_key
from .prediction_arbitrage import BookLevel


PREDICT_REST_URL = "https://api.predict.fun"
PREDICT_WEBSOCKET_URL = "wss://ws.predict.fun/ws"
_REST_TIMEOUT_SECONDS = 10.0
_MAX_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class PredictMarket:
    market_id: str
    condition_id: str
    question: str
    rules: str
    category_slug: str = ""
    event_start_at: datetime | None = None
    event_end_at: datetime | None = None
    resolution_provider: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    settlement_asset: str = ""
    minimum_order_size: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0")
    fee_rate_bps: Decimal = Decimal("0")
    polymarket_condition_ids: tuple[str, ...] = ()
    rules_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class PredictBook:
    market_id: str
    yes_asks: tuple[BookLevel, ...]
    no_asks: tuple[BookLevel, ...]
    source_timestamp: datetime
    received_at: datetime


class PredictSource:
    """Fixed-mainnet, read-only Predict source with separate REST/WS health."""

    def __init__(
        self,
        config: PredictConfig,
        *,
        key_loader: Callable[[], str] = load_predict_api_key,
        urlopen_fn: Callable[..., Any] = urlopen,
        websocket_connect: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._key_loader = key_loader
        self._urlopen_fn = urlopen_fn
        self._websocket_connect = websocket_connect or websockets.connect
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._markets: dict[str, PredictMarket] = {}
        self._categories: dict[str, dict[str, object]] = {}
        self._books: dict[str, dict[str, PredictBook]] = {"rest": {}, "ws": {}}
        self._versions: dict[str, dict[str, int]] = {"rest": {}, "ws": {}}
        self._rest_status = "unknown"
        self._ws_status = "unknown"
        self._last_success: dict[str, datetime | None] = {"rest": None, "ws": None}
        self._failure_reason: dict[str, str | None] = {"rest": None, "ws": None}
        self._ws_generation = 0

    async def list_open_markets(self) -> tuple[PredictMarket, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        markets: list[PredictMarket] = []
        while True:
            query: dict[str, str] = {"first": "100", "status": "OPEN"}
            if cursor:
                query["cursor"] = cursor
            payload = await self._rest_json("/v1/markets", query)
            if payload is None:
                return ()
            rows = payload.get("data")
            if not isinstance(rows, list):
                self._mark_stale("rest")
                return ()
            for row in rows:
                category_slug = _text(row.get("categorySlug")) if isinstance(row, dict) else ""
                category = await self._category(category_slug) if category_slug else None
                market = _normalise_market(row, category)
                if market is not None:
                    self._markets[market.market_id] = market
                    markets.append(market)
            next_cursor = payload.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                return tuple(markets)
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def get_market(self, market_id: str) -> PredictMarket | None:
        payload = await self._rest_json(f"/v1/markets/{market_id}")
        if payload is None:
            return None
        row = payload.get("data")
        category_slug = _text(row.get("categorySlug")) if isinstance(row, dict) else ""
        category = await self._category(category_slug) if category_slug else None
        market = _normalise_market(row, category)
        if market is None or market.market_id != str(market_id):
            self._mark_stale("rest")
            return None
        self._markets[market.market_id] = market
        return market

    async def _category(self, slug: str) -> dict[str, object] | None:
        cached = self._categories.get(slug)
        if cached is not None:
            return cached
        payload = await self._rest_json(f"/v1/categories/{quote(slug, safe='')}")
        row = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(row, dict) and row.get("slug") == slug:
            self._categories[slug] = row
            return row
        return None

    async def get_order_book(self, market_id: str) -> PredictBook | None:
        market = self._markets.get(str(market_id)) or await self.get_market(str(market_id))
        if market is None:
            return None
        payload = await self._rest_json(f"/v1/markets/{market.market_id}/orderbook")
        if payload is None:
            return None
        return self._accept_book(market, payload.get("data"), source="rest")

    async def stream_books(self, market_ids: Sequence[str]) -> AsyncIterator[PredictBook]:
        try:
            api_key = self._key_loader()
        except KeychainError:
            self._set_status("ws", "pending", "api_key_pending")
            return
        if not api_key:
            self._set_status("ws", "pending", "api_key_pending")
            return

        markets: dict[str, PredictMarket] = {}
        for market_id in market_ids:
            market = self._markets.get(str(market_id)) or await self.get_market(str(market_id))
            if market is not None:
                markets[market.market_id] = market
        if not markets:
            self._mark_stale("ws")
            return

        attempt = 0
        while True:
            self._books["ws"].clear()
            self._versions["ws"].clear()
            self._set_status(
                "ws",
                "stale",
                "ws_connecting" if attempt == 0 else "ws_reconnecting",
            )
            try:
                connection = self._websocket_connect(
                    PREDICT_WEBSOCKET_URL, additional_headers={"x-api-key": api_key}
                )
                async with connection as websocket:
                    for request_id, market_id in enumerate(markets, start=1):
                        await websocket.send(
                            json.dumps(
                                {
                                    "method": "subscribe",
                                    "requestId": request_id,
                                    "params": [f"predictOrderbook/{market_id}"],
                                },
                                separators=(",", ":"),
                            )
                        )
                    attempt = 0
                    while True:
                        message = json.loads(await websocket.recv())
                        if not isinstance(message, dict):
                            self._mark_stale("ws")
                            continue
                        if message.get("topic") == "heartbeat":
                            await websocket.send(
                                json.dumps(
                                    {"method": "heartbeat", "data": message.get("data")},
                                    separators=(",", ":"),
                                )
                            )
                            continue
                        topic = message.get("topic")
                        if not isinstance(topic, str) or not topic.startswith("predictOrderbook/"):
                            continue
                        market = markets.get(topic.removeprefix("predictOrderbook/"))
                        book = (
                            self._accept_book(market, message.get("data"), source="ws")
                            if market
                            else None
                        )
                        if book is not None:
                            yield book
            except Exception as exc:
                if _http_status(exc) in {401, 403}:
                    self._set_status("ws", "auth_blocked", "auth_blocked")
                    return
                if _is_transport_error(exc):
                    self._mark_unavailable("ws")
                else:
                    self._mark_stale("ws")
                self._ws_generation += 1
                attempt += 1
                await _maybe_await(self._sleep_fn(min(2 ** (attempt - 1), _MAX_BACKOFF_SECONDS)))

    async def get_balance_snapshot(self) -> dict[str, object]:
        """Expose no account data until a later approved account-read scope."""

        return {
            "wallet": _masked_wallet(self._config.wallet_address),
            "status": "unavailable",
            "asset": "USDT",
            "value": None,
        }

    def snapshot(self) -> dict[str, object]:
        successes = [value for value in self._last_success.values() if value is not None]
        return {
            "venue": "predict.fun",
            "wallet": _masked_wallet(self._config.wallet_address),
            "rest": self._rest_status,
            "ws": self._ws_status,
            "ws_generation": self._ws_generation,
            "rest_last_success": _timestamp_text(self._last_success["rest"]),
            "ws_last_success": _timestamp_text(self._last_success["ws"]),
            "last_success": _timestamp_text(max(successes)) if successes else None,
            "balance": None,
            "balance_state": "unavailable",
            "settlement_asset": "USDT",
            "rest_reason": self._failure_reason["rest"],
            "ws_reason": self._failure_reason["ws"],
            "reason": self._failure_reason["rest"] or self._failure_reason["ws"],
        }

    async def _rest_json(
        self, path: str, query: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        try:
            api_key = self._key_loader()
        except KeychainError:
            self._set_status("rest", "pending", "api_key_pending")
            return None
        if not api_key:
            self._set_status("rest", "pending", "api_key_pending")
            return None
        url = PREDICT_REST_URL + path
        if query:
            url += "?" + urlencode(query)
        for attempt in range(6):
            request = Request(
                url, headers={"x-api-key": api_key, "User-Agent": "open-trader/0.1"}
            )
            try:
                payload = await asyncio.to_thread(self._read_json, request)
            except Exception as exc:
                status = _http_status(exc)
                if status in {401, 403}:
                    self._set_status("rest", "auth_blocked", "auth_blocked")
                    return None
                transport_error = _is_transport_error(exc)
                if transport_error:
                    self._mark_unavailable("rest")
                else:
                    self._mark_stale("rest")
                if status != 429 and not transport_error:
                    return None
                if attempt == 5:
                    return None
                await _maybe_await(
                    self._sleep_fn(min(2**attempt, _MAX_BACKOFF_SECONDS))
                )
                continue
            if not isinstance(payload, dict) or payload.get("success") is not True:
                self._mark_stale("rest")
                return None
            self._mark_ready("rest")
            return payload
        return None

    def _read_json(self, request: Request) -> object:
        with self._urlopen_fn(request, timeout=_REST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))

    def _accept_book(
        self, market: PredictMarket, payload: object, *, source: Literal["rest", "ws"]
    ) -> PredictBook | None:
        if not isinstance(payload, dict) or str(payload.get("marketId")) != market.market_id:
            self._mark_stale(source)
            return None
        version = payload.get("version")
        previous = self._versions[source].get(market.market_id)
        if not isinstance(version, int) or (
            previous is not None
            and (version < previous or (source == "ws" and version == previous))
        ):
            self._mark_stale(source)
            return None
        timestamp = _timestamp(payload.get("updateTimestampMs"))
        asks = _levels(payload.get("asks"), market.tick_size, ascending=True)
        bids = _levels(payload.get("bids"), market.tick_size, ascending=False)
        if timestamp is None or not asks or not bids:
            self._mark_stale(source)
            return None
        no_asks = tuple(
            BookLevel(
                price=(Decimal("1") - level.price).quantize(market.tick_size), size=level.size
            )
            for level in reversed(bids)
        )
        if any(level.price < 0 or level.price > 1 for level in no_asks):
            self._mark_stale(source)
            return None
        book = PredictBook(
            market_id=market.market_id,
            yes_asks=asks,
            no_asks=no_asks,
            source_timestamp=timestamp,
            received_at=self._now_fn(),
        )
        self._versions[source][market.market_id] = version
        self._books[source][market.market_id] = book
        self._mark_ready(source, at=book.received_at)
        return book

    def _mark_stale(self, source: Literal["rest", "ws"]) -> None:
        self._books[source].clear()
        if source == "rest":
            self._rest_status = "stale"
        else:
            self._ws_status = "stale"
        self._failure_reason[source] = f"{source}_stale"

    def _mark_unavailable(self, source: Literal["rest", "ws"]) -> None:
        # Keep the existing stale status during reconnects; the explicit reason
        # lets acceptance distinguish transport loss from malformed data.
        self._books[source].clear()
        if source == "rest":
            self._rest_status = "stale"
        else:
            self._ws_status = "stale"
        self._failure_reason[source] = "network_unavailable"

    def _mark_ready(self, source: Literal["rest", "ws"], *, at: datetime | None = None) -> None:
        if source == "rest":
            self._rest_status = "ready"
        else:
            self._ws_status = "ready"
        self._last_success[source] = at or self._now_fn()
        self._failure_reason[source] = None

    def _set_status(
        self, source: Literal["rest", "ws"], status: str, reason: str
    ) -> None:
        if source == "rest":
            self._rest_status = status
        else:
            self._ws_status = status
        self._failure_reason[source] = reason


def _normalise_market(
    payload: object, category: dict[str, object] | None
) -> PredictMarket | None:
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("tradingStatus") != "OPEN"
        or payload.get("isNegRisk") is not False
        or payload.get("isYieldBearing") is not False
        or payload.get("marketType") != "BINARY"
        or payload.get("marketVariant", "DEFAULT") not in {"DEFAULT", "STANDARD"}
    ):
        return None
    market_id = _text(payload.get("id"))
    condition_id = _text(payload.get("conditionId"))
    question = _text(payload.get("question"))
    rules = _text(payload.get("description"))
    category_slug = _text(payload.get("categorySlug"))
    event_start_at = _datetime(category.get("startsAt")) if category else None
    event_end_at = _datetime(category.get("endsAt")) if category else None
    resolution_provider = _text(category.get("resolutionProvider")) if category else ""
    outcomes = payload.get("outcomes")
    tokens: dict[str, str] = {}
    if isinstance(outcomes, list):
        for outcome in outcomes:
            if isinstance(outcome, dict):
                name = _text(outcome.get("name")).upper()
                token = _text(outcome.get("onChainId")) or _text(outcome.get("tokenId"))
                if name and token and name not in tokens:
                    tokens[name] = token
    collateral = payload.get("collateralToken")
    settlement_asset = _text(collateral.get("symbol")) if isinstance(collateral, dict) else ""
    minimum_order_size = _decimal(payload.get("minimumOrderSize"))
    precision = payload.get("decimalPrecision")
    fee_rate_bps = _decimal(payload.get("feeRateBps"))
    external_ids = payload.get("polymarketConditionIds")
    polymarket_condition_ids = (
        tuple(value for item in external_ids if (value := _text(item)))
        if isinstance(external_ids, list)
        else ()
    )
    if (
        not all(
            (
                market_id,
                condition_id,
                question,
                rules,
                category_slug,
                resolution_provider,
                settlement_asset,
            )
        )
        or event_start_at is None
        or event_end_at is None
        or event_end_at <= event_start_at
        or set(tokens) != {"YES", "NO"}
        or minimum_order_size is None
        or minimum_order_size <= 0
        or not isinstance(precision, int)
        or precision < 0
        or fee_rate_bps is None
        or fee_rate_bps < 0
    ):
        return None
    tick_size = Decimal(1).scaleb(-precision)
    fingerprint_input = {
        "category_slug": category_slug,
        "event_end_at": event_end_at.isoformat(),
        "event_start_at": event_start_at.isoformat(),
        "outcomes": sorted(tokens.items()),
        "polymarket_condition_ids": polymarket_condition_ids,
        "question": question,
        "resolution_provider": resolution_provider,
        "rules": rules,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PredictMarket(
        market_id=market_id,
        condition_id=condition_id,
        question=question,
        rules=rules,
        category_slug=category_slug,
        event_start_at=event_start_at,
        event_end_at=event_end_at,
        resolution_provider=resolution_provider,
        yes_token_id=tokens["YES"],
        no_token_id=tokens["NO"],
        settlement_asset=settlement_asset,
        minimum_order_size=minimum_order_size,
        tick_size=tick_size,
        fee_rate_bps=fee_rate_bps,
        polymarket_condition_ids=polymarket_condition_ids,
        rules_fingerprint=fingerprint,
    )


def _levels(value: object, tick_size: Decimal, *, ascending: bool) -> tuple[BookLevel, ...]:
    if not isinstance(value, list):
        return ()
    levels: list[BookLevel] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            return ()
        price = _decimal(item[0])
        size = _decimal(item[1])
        if price is None or size is None or price < 0 or price > 1 or size <= 0:
            return ()
        levels.append(BookLevel(price=price.quantize(tick_size), size=size))
    prices = [level.price for level in levels]
    if prices != sorted(prices, reverse=not ascending):
        return ()
    return tuple(levels)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, int) or value < 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC)


def _timestamp_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else str(value) if isinstance(value, int) else ""


def _masked_wallet(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_transport_error(exc: BaseException) -> bool:
    status = _http_status(exc)
    if status is not None:
        return status == 429 or status >= 500
    return isinstance(exc, (ConnectionError, OSError, TimeoutError, URLError))


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value


__all__ = ["PredictBook", "PredictMarket", "PredictSource"]
