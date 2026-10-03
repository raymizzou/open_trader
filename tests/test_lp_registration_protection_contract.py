"""Queue-baseline contracts for unified exchange-order registration (#207).

The SDK boundary stays fake and offline; dashboard refresh, LP registration,
store persistence, and restart all use the real production objects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from polymarket.models.clob.order_book import OrderBook, OrderBookLevel
from polymarket.models.clob.account import OpenOrder

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

from tests.test_lp_order_registration_contract import (
    _Notifier,
    _SDKPublicClient,
    _runtime,
)

CONDITION_ID = "0x" + "c" * 64
TOKEN_ID = "0x" + "1" * 64
WALLET = "0x" + "a" * 40


class _BookClient(_SDKPublicClient):
    """A small stateful batch-book SDK boundary."""

    def __init__(self, now: datetime, *, results: tuple[object, ...]) -> None:
        super().__init__(now)
        self.results = list(results)
        self.last_book: object = None
        self.book_reads = 0

    def get_order_books(self, *, token_ids) -> tuple[object, ...]:
        assert tuple(token_ids) == (TOKEN_ID,)
        self.book_reads += 1
        if not self.results:
            return (self.last_book,)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.last_book = result
        return (result,)

    def close(self) -> None:
        return None


def _book(now: datetime, *, high_size: str, low_size: str, digest: str) -> OrderBook:
    return OrderBook(
        market=CONDITION_ID,
        asset_id=TOKEN_ID,
        timestamp=now,
        bids=(
            OrderBookLevel(price=Decimal("0.40"), size=Decimal(high_size)),
            OrderBookLevel(price=Decimal("0.30"), size=Decimal(low_size)),
        ),
        asks=(OrderBookLevel(price=Decimal("0.41"), size=Decimal("100")),),
        min_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        neg_risk=False,
        hash=digest,
    )


def _open_order(order_id: str, *, price: str, original: str, matched: str = "0") -> OpenOrder:
    return OpenOrder(
        id=order_id,
        market=CONDITION_ID,
        asset_id=TOKEN_ID,
        owner=WALLET,
        maker_address=WALLET,
        side="BUY",
        price=Decimal(price),
        original_size=Decimal(original),
        size_matched=Decimal(matched),
        outcome="YES",
        order_type="GTC",
        status="LIVE",
        associate_trades=(),
        created_at=datetime.now(UTC).replace(microsecond=0),
    )


def _one_group() -> tuple[object, ...]:
    return (
        _open_order("BUY-HIGH-1", price="0.40", original="10"),
        _open_order("BUY-HIGH-2", price="0.40", original="10"),
        _open_order("BUY-LOW", price="0.30", original="20"),
    )


def _stored_time(value: object) -> datetime:
    """Decode a timestamp after SQLite payload serialization."""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _known_bucket(baseline: dict[str, object], price: str) -> dict[str, object]:
    levels = baseline["levels"]
    assert isinstance(levels, dict)
    bucket = levels[price]
    assert isinstance(bucket, dict)
    return bucket


def _assert_ids_and_one_session(store: PredictionArbitrageStore) -> dict[str, object]:
    sessions = store.lp_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert set(session["owned_order_ids"]) == {
        "BUY-HIGH-1",
        "BUY-HIGH-2",
        "BUY-LOW",
    }
    assert set(session["order_history"]) == set(session["owned_order_ids"])
    return session


def test_multi_price_registration_uses_one_baseline_per_price(tmp_path) -> None:
    """Each BUY price gets a fresh-book baseline net of all own same-price IDs."""

    now = datetime.now(UTC).replace(microsecond=0)
    fresh_book = _book(
        now,
        high_size="100",
        low_size="80",
        digest="fresh-multi-price-book",
    )
    public = _BookClient(now, results=(fresh_book,))
    store, adapter, _, _, execution = _runtime(
        tmp_path, orders=_one_group()
    )
    try:
        adapter._public_client_factory = lambda: public
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"

        session = _assert_ids_and_one_session(store)
        baseline = session["queue_protection"]
        high = _known_bucket(baseline, "0.40")
        low = _known_bucket(baseline, "0.30")
        assert Decimal(str(high["baseline_front"])) == Decimal("80")
        assert Decimal(str(low["baseline_front"])) == Decimal("60")
        assert high["baseline_book_hash"] == "fresh-multi-price-book"
        assert low["baseline_book_hash"] == "fresh-multi-price-book"
        assert high["baseline_book_received_at"] is not None
        assert low["baseline_book_received_at"] is not None
        assert _stored_time(high["baseline_source_timestamp"]) == fresh_book.timestamp
        assert _stored_time(low["baseline_source_timestamp"]) == fresh_book.timestamp
    finally:
        adapter.close()


def test_failed_book_imports_then_registers_once_and_survives_restart(tmp_path) -> None:
    """A failed baseline stays an honest import; later depth cannot reset it."""

    now = datetime.now(UTC).replace(microsecond=0)
    fresh_book = _book(now, high_size="100", low_size="80", digest="fresh-book")
    deeper_book = _book(
        now + timedelta(seconds=1),
        high_size="180",
        low_size="150",
        digest="deeper-book",
    )
    public = _BookClient(
        now,
        results=(
            TimeoutError("book transport failed"),
            fresh_book,
            deeper_book,
        ),
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path, orders=_one_group()
    )
    try:
        adapter._public_client_factory = lambda: public
        provider = store.lp_trade_generation

        failed = execution.refresh_lp_dashboard_snapshot()
        assert failed["state"] == "ready"
        failed_session = _assert_ids_and_one_session(store)
        failed_baseline = failed_session["queue_protection"]
        for price in ("0.40", "0.30"):
            bucket = _known_bucket(failed_baseline, price)
            assert bucket.get("baseline_front") is None
        failed_counts = (
            account.balance_reads,
            account.order_reads,
            account.trade_reads,
            account.position_reads,
        )

        store.lp_advance_trade_generation(provider())
        public.now = datetime.now(UTC).replace(microsecond=0)
        registered = execution.refresh_lp_dashboard_snapshot()
        assert registered["state"] == "ready"
        registered_session = _assert_ids_and_one_session(store)
        registered_baseline = registered_session["queue_protection"]
        fresh: dict[str, dict[str, object]] = {}
        for price, front in (("0.40", "80"), ("0.30", "60")):
            bucket = _known_bucket(registered_baseline, price)
            assert bucket["state"] == "registered"
            assert Decimal(str(bucket["baseline_front"])) == Decimal(front)
            assert bucket["baseline_book_hash"] == "fresh-book"
            assert bucket["baseline_book_received_at"] is not None
            assert _stored_time(bucket["baseline_source_timestamp"]) == fresh_book.timestamp
            fresh[price] = bucket
        assert (
            account.balance_reads,
            account.order_reads,
            account.trade_reads,
            account.position_reads,
        ) == tuple(value + 1 for value in failed_counts)

        store.lp_advance_trade_generation(provider())
        public.now = datetime.now(UTC).replace(microsecond=0)
        deeper = execution.refresh_lp_dashboard_snapshot()
        assert deeper["state"] == "ready"
        deeper_session = _assert_ids_and_one_session(store)
        deeper_baseline = deeper_session["queue_protection"]
        for price in ("0.40", "0.30"):
            bucket = _known_bucket(deeper_baseline, price)
            assert bucket["baseline_front"] == fresh[price]["baseline_front"]
            assert bucket["baseline_book_hash"] == "fresh-book"
            assert (
                bucket["baseline_book_received_at"]
                == fresh[price]["baseline_book_received_at"]
            )
            assert (
                bucket["baseline_source_timestamp"]
                == fresh[price]["baseline_source_timestamp"]
            )

        restarted_store = PredictionArbitrageStore(tmp_path / "state.sqlite")
        restarted_lp = PolymarketLPService(
            restarted_store, adapter, clock=lambda: datetime.now(UTC)
        )
        restarted_execution = PredictionExecutionService(
            store=restarted_store,
            monitor=SimpleNamespace(),
            trading=adapter,
            notifier=_Notifier(),
            lock_path=tmp_path / "execution.lock",
            lp=restarted_lp,
        )
        restarted_execution._breaker_open = False
        assert restarted_execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        restarted_session = _assert_ids_and_one_session(restarted_store)
        restarted_baseline = restarted_session["queue_protection"]
        for price in ("0.40", "0.30"):
            bucket = _known_bucket(restarted_baseline, price)
            assert bucket["baseline_front"] == fresh[price]["baseline_front"]
            assert bucket["baseline_book_hash"] == "fresh-book"
            assert (
                bucket["baseline_book_received_at"]
                == fresh[price]["baseline_book_received_at"]
            )
            assert set(restarted_session["owned_order_ids"]) == set(
                deeper_session["owned_order_ids"]
            )
    finally:
        adapter.close()
