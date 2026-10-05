from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import threading
import time
from types import SimpleNamespace

import pytest

from polymarket.models.clob.account import ClobTrade, MakerOrder, OpenOrder

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_trading import (
    LpNewerAccountFacts,
    PolymarketTradingClient,
    TradingConfig,
)
from open_trader.prediction_arbitrage_store import (
    LP_RESERVED_MANUAL_SESSION_ID,
    PredictionArbitrageStore,
)
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from tests.test_polymarket_lp import _SDKAccountClient


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class _CountingAccountClient(_SDKAccountClient):
    def __init__(self, now: datetime) -> None:
        super().__init__(now)
        self.calls = {"balance": 0, "orders": 0, "trades": 0, "positions": 0}

    def get_balance_allowance(self, **kwargs: object) -> object:
        self.calls["balance"] += 1
        return super().get_balance_allowance(**kwargs)

    def list_open_orders(self, **kwargs: object) -> list[object]:
        self.calls["orders"] += 1
        return super().list_open_orders(**kwargs)

    def list_account_trades(self, **kwargs: object) -> list[object]:
        self.calls["trades"] += 1
        return super().list_account_trades(**kwargs)

    def list_positions(self, **kwargs: object) -> list[object]:
        self.calls["positions"] += 1
        return super().list_positions(**kwargs)


class _TwoSessionAccountClient(_CountingAccountClient):
    def __init__(self, now: datetime) -> None:
        super().__init__(now)
        owner = str(self.open_order.owner)
        self.open_orders = [self.open_order]
        for index in (1, 2):
            suffix = f"{index:02d}"
            self.open_orders.append(
                OpenOrder(
                    id=f"order-{suffix}",
                    market="0x" + suffix * 32,
                    asset_id="0x" + suffix * 64,
                    owner=owner,
                    maker_address=owner,
                    side="BUY",
                    price=Decimal("0.30"),
                    original_size=Decimal("10"),
                    size_matched=Decimal("0"),
                    outcome="YES",
                    order_type="GTC",
                    status="LIVE",
                    associate_trades=(),
                    created_at=now,
                    expiration=now + timedelta(minutes=10),
                )
            )

    def list_open_orders(self, **kwargs: object) -> list[object]:
        self.calls["orders"] += 1
        return self.open_orders


class _GatedAccountClient(_CountingAccountClient):
    def __init__(self, now: datetime) -> None:
        super().__init__(now)
        self.first_order_started = threading.Event()
        self.release_first_order = threading.Event()

    def list_open_orders(self, **kwargs: object) -> list[object]:
        self.calls["orders"] += 1
        if not self.first_order_started.is_set():
            self.first_order_started.set()
            assert self.release_first_order.wait(timeout=10)
        # CountingAccountClient also instruments this method; call the SDK
        # fixture directly so one blocked network read is counted once.
        return _SDKAccountClient.list_open_orders(self, **kwargs)


class _TriggerAccountClient(_CountingAccountClient):
    def __init__(self, now: datetime) -> None:
        super().__init__(now)
        self.open_order = self.open_order.model_copy(
            update={
                "side": "BUY",
                "price": Decimal("0.30"),
                "original_size": Decimal("10"),
                "size_matched": Decimal("0"),
                "status": "LIVE",
            }
        )
        self.open_orders_read = threading.Event()

    def list_positions(self, **kwargs: object) -> list[object]:
        rows = super().list_positions(**kwargs)
        self.open_orders_read.set()
        return rows


class _TriggerPublicClient:
    def __init__(
        self,
        now: datetime,
        *,
        book_entered: threading.Event | None = None,
        release_book: threading.Event | None = None,
    ) -> None:
        self.now = now
        self.book_entered = book_entered or threading.Event()
        self.release_book = release_book or threading.Event()
        if release_book is None:
            self.release_book.set()

    def get_order_books(self, *, token_ids: list[str]) -> list[dict[str, object]]:
        token_id = token_ids[0]
        assert token_id == "0x" + "1" * 64
        self.book_entered.set()
        assert self.release_book.wait(timeout=2)
        self.release_book.set()
        return [
            {
                "market": "0x" + "c" * 64,
                "condition_id": "0x" + "c" * 64,
                "asset_id": token_id,
                "token_id": token_id,
                "timestamp": self.now,
                "bids": [{"price": Decimal("0.30"), "size": Decimal("16020")}],
                "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
                "min_order_size": 1,
                "tick_size": 0.001,
                "neg_risk": False,
                "hash": "book-trigger",
            }
        ]

    def close(self) -> None:
        return None


class _FirstReadFailingAccountClient(_TriggerAccountClient):
    def __init__(self, now: datetime) -> None:
        super().__init__(now)
        self.fail_reads = True

    def get_balance_allowance(self, **kwargs: object) -> object:
        self.calls["balance"] += 1
        if self.fail_reads:
            raise OSError("sdk_account_unavailable")
        return _SDKAccountClient.get_balance_allowance(self, **kwargs)


class _ScoringGatedAccountClient(_CountingAccountClient):
    def __init__(self, now: datetime) -> None:
        super().__init__(now)
        self.scoring_entered = threading.Event()
        self.release_scoring = threading.Event()

    def get_order_scoring(self, *, order_id: str) -> bool:
        self.scoring_entered.set()
        assert self.release_scoring.wait(timeout=5)
        return super().get_order_scoring(order_id=order_id)


class _TwoMarketPublicClient:
    def __init__(self, now: datetime) -> None:
        # Book freshness compares receipt/source time with the service clock.
        self.now = now
        self.market_calls: list[str] = []
        self.book_calls: list[str] = []

    def get_market(self, *, id: str) -> dict[str, object]:
        index = int(id.rsplit("-", 1)[1])
        suffix = f"{index:02d}"
        self.market_calls.append(id)
        return {
            "id": id,
            "condition_id": "0x" + suffix * 32,
            "state": {"accepting_orders": True},
            "outcomes": {"yes": {"token_id": "0x" + suffix * 64, "label": "YES"}},
            "trading": {
                "fees_enabled": True,
                "fee_schedule": {"exponent": 1, "rate": 0, "taker_only": True},
                "minimum_order_size": 1,
                "minimum_tick_size": 0.001,
            },
            "rewards": {"rewards_min_size": 1, "rewards_max_spread": 10},
        }

    def get_order_book(self, *, token_id: str) -> dict[str, object]:
        self.book_calls.append(token_id)
        suffix = token_id.removeprefix("0x")[:2]
        return {
            "market": "0x" + suffix * 32,
            "condition_id": "0x" + suffix * 32,
            "asset_id": token_id,
            "token_id": token_id,
            "timestamp": self.now,
            "bids": [{"price": 0.29, "size": 100}],
            "asks": [{"price": 0.31, "size": 100}],
            "min_order_size": 1,
            "tick_size": 0.001,
            "neg_risk": False,
            "hash": f"book-{suffix}",
        }

    def get_order_books(self, *, token_ids: list[str]) -> list[dict[str, object]]:
        return [self.get_order_book(token_id=token_id) for token_id in token_ids]

    def close(self) -> None:
        return None


class _BlockingTwoMarketPublicClient(_TwoMarketPublicClient):
    def __init__(self, now: datetime) -> None:
        super().__init__(now)
        self.active_books = 0
        self.max_active_books = 0
        self.lock = threading.Lock()
        self.books_entered: list[str] = []
        self.both_books_started = threading.Barrier(2)
        self.books_started = threading.Event()
        self.release_books = threading.Event()

    def get_order_book(self, *, token_id: str) -> dict[str, object]:
        with self.lock:
            self.active_books += 1
            self.max_active_books = max(self.max_active_books, self.active_books)
            self.books_entered.append(token_id)
            if self.active_books == 2:
                self.books_started.set()
        try:
            self.both_books_started.wait(timeout=5)
            assert self.release_books.wait(timeout=5)
            return super().get_order_book(token_id=token_id)
        finally:
            with self.lock:
                self.active_books -= 1


def _request(now: datetime, *, index: int) -> dict[str, object]:
    suffix = f"{index:02d}"
    return {
        "market_id": f"market-{index}",
        "condition_id": "0x" + suffix * 32,
        "token_id": "0x" + suffix * 64,
        "outcome": "YES",
        "question": f"Will it happen {index}?",
        "price": "0.30",
        "quantity": "10",
        "review_at": now + timedelta(minutes=10),
        "entry_order_id": f"order-{suffix}",
        "owned_order_ids": [f"order-{suffix}"],
    }


def _service(
    tmp_path, now: datetime | None = None
) -> tuple[PolymarketLPService, _CountingAccountClient, _TwoMarketPublicClient]:
    now = now or datetime.now(UTC)
    account = _CountingAccountClient(now)
    public = _TwoMarketPublicClient(now)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    for index in (1, 2):
        store.lp_create_session(
            f"session-{index:02d}",
            f"session-{index:02d}-key",
            state="entry_open",
            payload=_request(now, index=index),
        )
    return (
        PolymarketLPService(store, adapter),
        account,
        public,
    )


def _round_adapter() -> tuple[
    PolymarketTradingClient, _GatedAccountClient, _TwoMarketPublicClient
]:
    account = _GatedAccountClient(NOW)
    public = _TwoMarketPublicClient(NOW)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    return adapter, account, public


def _run_lp_snapshot(
    adapter: PolymarketTradingClient, token: object
) -> tuple[object, BaseException | None]:
    try:
        request = _request(NOW, index=1)
        request["_lp_account_round"] = token
        return adapter.lp_snapshot(request), None
    except BaseException as exc:  # worker boundary under test
        return None, exc


def _join_snapshot_workers(
    threads: list[threading.Thread],
    captures: dict[str, tuple[object, BaseException | None]],
) -> None:
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    results = [captures[thread.name] for thread in threads]
    assert all(result[1] is not None for result in results)
    assert all(
        str(result[1]) == "lp_account_round_invalid" for result in results
    )


def test_old_owner_completion_cannot_replace_newer_bundle() -> None:
    adapter, account, _public = _round_adapter()
    token = adapter.lp_account_round_begin()
    old_capture: dict[str, tuple[object, BaseException | None]] = {}

    def old_reader() -> None:
        old_capture["old"] = _run_lp_snapshot(adapter, token)

    owner = threading.Thread(target=old_reader)
    owner.start()
    try:
        assert account.first_order_started.wait(timeout=1)
        adapter.lp_account_round_invalidate(token)

        # The next generation owns a new Future and completes immediately.
        new_snapshot, new_error = _run_lp_snapshot(adapter, token)
        assert new_error is None
        assert new_snapshot is not None

        account.release_first_order.set()
        owner.join(timeout=2)
        assert not owner.is_alive()
        assert old_capture["old"][1] is not None

        cached_snapshot, cached_error = _run_lp_snapshot(adapter, token)
        assert cached_error is None
        assert cached_snapshot is not None
        # Generation one read once; the late generation-zero failure never
        # evicted or refilled the newer cache slot.
        assert account.calls["orders"] == 2
    finally:
        account.release_first_order.set()
        owner.join(timeout=2)
        adapter.lp_account_round_end(token)


def _observe_round_waiters(token, expected: int, monkeypatch):
    """Signal only when consumers have selected the owner's shared Future."""
    with token.lock:
        future = token.future
    assert future is not None
    original_result = future.result
    lock = threading.Lock()
    arrived = threading.Event()
    count = 0

    def result(*args, **kwargs):
        nonlocal count
        with lock:
            count += 1
            if count == expected:
                arrived.set()
        return original_result(*args, **kwargs)

    monkeypatch.setattr(future, "result", result)
    return arrived


def test_round_lifecycle_without_consumer_makes_no_network_calls() -> None:
    adapter, account, _public = _round_adapter()
    token = adapter.lp_account_round_begin()
    adapter.lp_account_round_end(token)

    assert all(value == 0 for value in account.calls.values())


def test_service_preserves_wait_semantics_for_an_ended_real_round(tmp_path, caplog) -> None:
    import logging
    caplog.set_level(logging.INFO, logger="open_trader.polymarket_trading")
    service, account, _public = _service(tmp_path, NOW)
    service.clock = lambda: NOW
    token = service.exchange.lp_account_round_begin()
    service.exchange.lp_account_round_end(token)

    result = service.reconcile_facts("session-01", monitor=True, account_round=token)

    assert result[3] == "account_round_invalid"
    row = service.store.lp_session("session-01")
    assert row is not None
    assert row["state"] == "entry_open"
    assert row["facts_error"] == "account_round_invalid"
    assert row["reconcile_reason"] == "account_round_invalid"
    assert row["publication_pending"] is True
    assert account.calls == {"balance": 0, "orders": 0, "trades": 0, "positions": 0}
    assert service.store.lp_actions("session-01") == []
    records = [row for row in caplog.records if row.name == "open_trader.polymarket_trading"]
    assert records
    assert all(row.levelno == logging.INFO for row in records)
    assert any("lp_read_wait stage=account" in row.getMessage() for row in records)
    assert any("lp_read_wait stage=facts_read" in row.getMessage() for row in records)
    wait_records = [row for row in records if row.getMessage().startswith("lp_read_wait ")]
    assert all("reason=account_round_invalid" in row.getMessage() for row in wait_records)
    assert all(row.getMessage().startswith(("lp_read_wait ", "lp_read_task_end ")) for row in records)


def test_service_preserves_wait_semantics_for_round_invalidated_while_reading(
    tmp_path,
) -> None:
    adapter, account, _public = _round_adapter()
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "session-01",
        "session-01-key",
        state="entry_open",
        payload=_request(NOW, index=1),
    )
    service = PolymarketLPService(store, adapter)
    service.clock = lambda: NOW
    token = service.exchange.lp_account_round_begin()

    result_container: list[tuple[object, ...]] = []

    def reconcile() -> None:
        result_container.append(
            service.reconcile_facts("session-01", monitor=True, account_round=token)
        )

    worker = threading.Thread(target=reconcile)
    worker.start()
    try:
        assert account.first_order_started.wait(timeout=2)
        service.exchange.lp_account_round_invalidate(token)
        account.release_first_order.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
    finally:
        account.release_first_order.set()
        worker.join(timeout=2)
        service.exchange.lp_account_round_end(token)

    result = result_container[0]
    assert result[3] == "account_round_invalid"
    row = store.lp_session("session-01")
    assert row is not None
    assert row["state"] == "entry_open"
    assert row["facts_error"] == "account_round_invalid"
    assert account.calls["balance"] == 1


def test_service_keeps_non_round_value_errors_external(tmp_path, monkeypatch) -> None:
    service, _account, _public = _service(tmp_path, NOW)
    service.clock = lambda: NOW

    def malformed_snapshot(_request):
        raise ValueError("account_response_shape_invalid")

    monkeypatch.setattr(service.exchange, "lp_snapshot", malformed_snapshot)
    result = service.reconcile_facts("session-01", monitor=True)

    assert result[3] == "external_snapshot_unknown"
    row = service.store.lp_session("session-01")
    assert row is not None
    assert row["state"] == "needs_attention"
    assert row["facts_error"] == "external_snapshot_unknown"


@pytest.mark.parametrize("finish", ["end", "invalidate"])
def test_late_snapshot_rejects_end_or_invalidation(finish: str, monkeypatch) -> None:
    adapter, account, _public = _round_adapter()
    token = adapter.lp_account_round_begin()
    captures: dict[str, tuple[object, BaseException | None]] = {}

    def reader():
        captures[threading.current_thread().name] = _run_lp_snapshot(
            adapter, token
        )

    owner = threading.Thread(target=reader)
    owner.start()
    assert account.first_order_started.wait(timeout=1)
    waiters: list[threading.Thread] = []
    try:
        joined = _observe_round_waiters(token, 2, monkeypatch)
        waiter_a = threading.Thread(target=reader)
        waiter_b = threading.Thread(target=reader)
        waiter_a.start()
        waiter_b.start()
        waiters = [waiter_a, waiter_b]
        assert joined.wait(timeout=2), "both waiters must select the old generation"

        if finish == "end":
            adapter.lp_account_round_end(token)
        else:
            adapter.lp_account_round_invalidate(token)
        # Ending/invalidation returned while the account owner remains blocked.
        # This proves nonblocking lifecycle behavior without a scheduler deadline.
        assert owner.is_alive()
        assert not account.release_first_order.is_set()
        account.release_first_order.set()
        _join_snapshot_workers([owner, *waiters], captures)

        if finish == "end":
            _same_token, same_error = _run_lp_snapshot(adapter, token)
            assert str(same_error) == "lp_account_round_invalid"
            token = adapter.lp_account_round_begin()
        next_snapshot, error = _run_lp_snapshot(adapter, token)
        assert error is None
        assert next_snapshot is not None
        # Owner + parked waiters consumed one bundle; the next caller read
        # exactly one new bundle.
        assert account.calls["orders"] == 2
    finally:
        account.release_first_order.set()
        adapter.lp_account_round_end(token)
        for thread in [owner, *waiters]:
            thread.join(timeout=2)


def test_registered_service_trade_rejects_before_publication(tmp_path) -> None:
    now = datetime.now(UTC)
    account = _CountingAccountClient(now)
    public = _TwoMarketPublicClient(now)
    wallet = str(account.open_order.owner)
    adapter = PolymarketTradingClient(
        TradingConfig(wallet, wallet),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    for index in (1, 2):
        store.lp_create_session(
            f"session-{index:02d}",
            f"session-{index:02d}-key",
            state="entry_open",
            payload=_request(now, index=index),
        )
    service = PolymarketLPService(store, adapter)
    engine = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=adapter,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    engine._breaker_open = False
    request_b = _request(now, index=2)
    engine._lp_auto_pool()._update(
        lambda document: document.update(
            intents={
                "intent-02": {
                    "intent_id": "intent-02",
                    "session_id": "session-02",
                    "order_id": "order-02",
                    "state": "active",
                    "financial_status": "known",
                    "reserved_usd": "3.00",
                    "inventory_cost_usd": "0",
                    "realized_pnl_usd": "0",
                    **{
                        key: request_b[key]
                        for key in (
                            "condition_id", "market_id", "token_id", "outcome",
                            "price", "quantity",
                        )
                    },
                    "created_at": now.isoformat(),
                }
            }
        )
    )
    first_validated = threading.Event()
    validator_entered = threading.Event()
    validator_release = threading.Event()

    def validator(session, snapshot):
        if str(session.get("session_id")) == "session-01":
            first_validated.set()
        elif str(session.get("session_id")) == "session-02":
            assert first_validated.wait(timeout=2)
            validator_entered.set()
            assert validator_release.wait(timeout=2)

    service._facts_validator = validator
    tick_result: list[dict[str, object]] = []
    worker = threading.Thread(target=lambda: tick_result.append(service.tick()))
    worker.start()
    try:
        assert validator_entered.wait(timeout=2)
        store.lp_register_trade_change("session-01")
        validator_release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        session_b = store.lp_session("session-02")
        assert session_b is not None
        # The adapter wrapper externalizes the reason, but publication is
        # durably rejected and the financial permission falls back to UNKNOWN.
        assert session_b["facts_error"] in {
            "external_snapshot_unknown",
            "account_round_invalid",
        }
        assert session_b["fee_status"] == "unknown"
        assert account.calls["balance"] == 1
        auto = engine.lp_auto_state()
        assert auto["funds"]["status"] == "unknown"
        assert auto["funds"]["available_usd"] is None
        assert auto["intents"][0]["financial_status"] == "unknown"

        service.tick()
        assert account.calls["balance"] == 2
    finally:
        validator_release.set()
        worker.join(timeout=2)


def test_trade_after_scoring_blocks_old_bundle_apply(tmp_path, monkeypatch) -> None:
    from open_trader import polymarket_trading
    now = datetime.now(UTC)
    receipt_scope = threading.local()

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            if getattr(receipt_scope, "active", False):
                return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
            return datetime.now(tz)

    @contextmanager
    def receipt_clock():
        previous = getattr(receipt_scope, "active", False)
        receipt_scope.active = True
        try:
            yield
        finally:
            receipt_scope.active = previous

    monkeypatch.setattr(polymarket_trading, "datetime", Clock)
    account = _ScoringGatedAccountClient(now)
    public = _TwoMarketPublicClient(now)
    wallet = str(account.open_order.owner)
    adapter = PolymarketTradingClient(
        TradingConfig(wallet, wallet),
        account,
        public_client_factory=lambda: public,
    )
    account_facts = adapter._lp_account_facts
    public_client = adapter._lp_snapshot_public_client

    def read_account(**kwargs):
        with receipt_clock():
            return account_facts(**kwargs)

    @contextmanager
    def read_public():
        with receipt_clock(), public_client() as client:
            yield client

    monkeypatch.setattr(adapter, "_lp_account_facts", read_account)
    monkeypatch.setattr(adapter, "_lp_snapshot_public_client", read_public)
    store = PredictionArbitrageStore(tmp_path)
    for index in (1, 2):
        store.lp_create_session(
            f"session-{index:02d}",
            f"session-{index:02d}-key",
            state="entry_open",
            payload=_request(now, index=index),
        )
    service = PolymarketLPService(store, adapter, clock=lambda: now)
    engine = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=adapter,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    engine._breaker_open = False
    request_b = _request(now, index=2)
    engine._lp_auto_pool()._update(lambda document: document.update(intents={
        "intent-02": {
            "intent_id": "intent-02",
            "session_id": "session-02",
            "order_id": "order-02",
            "state": "active",
            "financial_status": "known",
            "reserved_usd": "3.00",
            "inventory_cost_usd": "0",
            "realized_pnl_usd": "0",
            **{
                key: request_b[key]
                for key in (
                    "condition_id", "market_id", "token_id", "outcome",
                    "price", "quantity",
                )
            },
            "created_at": now.isoformat(),
        }
    }))
    tick_results: list[dict[str, object]] = []
    worker = threading.Thread(target=lambda: tick_results.append(service.tick()))
    worker.start()
    try:
        assert account.scoring_entered.wait(timeout=2)
        store.lp_register_trade_change("session-01")
        account.release_scoring.set()
        worker.join(timeout=3)
        assert not worker.is_alive()

        session_b = store.lp_session("session-02")
        assert session_b is not None
        assert session_b["facts_error"] == "account_round_invalid"
        assert session_b["fee_status"] == "unknown"
        assert session_b["position_reconciled"] is False
        assert "financial_status" not in session_b
        # The post-scoring guard rejected the result before strategy apply;
        # no fresh scoring observation was attached to the old bundle.
        assert session_b.get("scoring_checked_at") is None
        auto = engine.lp_auto_state()
        assert auto["funds"]["status"] == "unknown"
        assert auto["funds"]["available_usd"] is None
        assert auto["intents"][0]["financial_status"] == "unknown"
    finally:
        account.release_scoring.set()
        worker.join(timeout=2)
        adapter.close()


def test_exact_order_newer_evidence_advances_shared_fence(tmp_path, caplog) -> None:
    import logging
    caplog.set_level(logging.INFO, logger="open_trader.polymarket_trading")
    now = datetime.now(UTC)
    account = _CountingAccountClient(now)
    public = _TwoMarketPublicClient(now)
    # Start with no successful fill evidence; the exact-order receipt is 0.
    account.trade = _history_trade(
        id="trade-initial-failed", status="FAILED", size=Decimal("0")
    )
    wallet = str(account.open_order.owner)
    adapter = PolymarketTradingClient(
        TradingConfig(wallet, wallet), account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    for index in (1, 2):
        store.lp_create_session(
            f"session-{index:02d}", f"session-{index:02d}-key",
            state="entry_open", payload=_request(now, index=index),
        )
    service = PolymarketLPService(store, adapter)
    engine = PredictionExecutionService(
        store=store, monitor=SimpleNamespace(), trading=adapter,
        notifier=SimpleNamespace(), lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    engine._breaker_open = False
    request_b = _request(now, index=2)
    engine._lp_auto_pool()._update(lambda document: document.update(intents={
        "intent-02": {
            "intent_id": "intent-02", "session_id": "session-02",
            "order_id": "order-02", "state": "active",
            "financial_status": "known", "reserved_usd": "3.00",
            "inventory_cost_usd": "0", "realized_pnl_usd": "0",
            **{key: request_b[key] for key in (
                "condition_id", "market_id", "token_id", "outcome",
                "price", "quantity",
            )},
            "created_at": now.isoformat(),
        }
    }))

    requests = {
        f"order-{index:02d}": _request(now, index=index)
        for index in (1, 2)
    }
    request_a = requests["order-01"]

    def receipt(order_id: str, matched: Decimal):
        return _exact_order(
            order_id=order_id,
            request=requests[order_id],
            matched=matched,
            trade_ids=(),
            status="LIVE" if matched == 0 else "FILLED",
        )

    def get_order(*, order_id: str):
        if order_id == "order-01":
            # Hold A's exact lookup until B has crossed the adapter/validator
            # boundary. A then returns a newer fill through the normal service
            # read; authoritative fencing happens at that caller boundary.
            assert exact_order_release.wait(timeout=3)
            return receipt(order_id, Decimal("100"))
        return receipt(order_id, Decimal("0"))

    account.get_order = get_order
    exact_order_release = threading.Event()
    validator_entered = threading.Event()
    validator_release = threading.Event()

    def validator(session, snapshot):
        if str(session.get("session_id")) == "session-02":
            validator_entered.set()
            assert validator_release.wait(timeout=3)

    service._facts_validator = validator
    tick_results: list[dict[str, object]] = []
    worker = threading.Thread(target=lambda: tick_results.append(service.tick()))
    worker.start()
    try:
        assert validator_entered.wait(timeout=2)
        assert store.lp_trade_generation() == 0
        exact_order_release.set()
        for _ in range(100):
            if store.lp_trade_generation() == 1:
                break
            time.sleep(0.01)
        assert store.lp_trade_generation() == 1
        validator_release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
        session_b = store.lp_session("session-02")
        assert session_b is not None
        assert session_b["facts_error"] == "account_round_invalid"
        auto = engine.lp_auto_state()
        assert auto["funds"]["status"] == "unknown"
        assert auto["funds"]["available_usd"] is None
        assert auto["intents"][0]["financial_status"] == "unknown"

        # A new bundle at generation one contains A's matching confirmed fill.
        maker = MakerOrder(
            order_id="order-01", asset_id=request_a["token_id"],
            maker_address=wallet, owner=wallet, side="BUY",
            price=Decimal("0.30"), matched_amount=Decimal("100"),
            outcome="YES", fee_rate_bps=Decimal("0"),
        )
        account.trade = _history_trade(
            id="trade-new", market=request_a["condition_id"],
            asset_id=request_a["token_id"], maker_orders=(maker,),
        )
        service.tick()
        session_b = store.lp_session("session-02")
        assert session_b is not None
        assert session_b["facts_error"] is None
        assert store.lp_trade_generation() == 1
        auto = engine.lp_auto_state()
        assert auto["intents"][0]["financial_status"] == "known"
    finally:
        exact_order_release.set()
        validator_release.set()
        worker.join(timeout=2)
    records = [row for row in caplog.records if row.name == "open_trader.polymarket_trading"]
    assert not any("LpNewerAccountFacts" in row.getMessage() for row in records)
    assert any(row.levelno == logging.INFO and "lp_read_wait stage=facts_read" in row.getMessage()
               and "reason=account_round_invalid" in row.getMessage() for row in records)


def test_real_tick_shares_one_account_bundle_across_sessions(tmp_path) -> None:
    service, account, public = _service(tmp_path)

    result = service.tick()

    states = [
        (
            service.store.lp_session(f"session-{index:02d}")["state"],
            service.store.lp_session(f"session-{index:02d}")["facts_error"],
        )
        for index in (1, 2)
    ]
    assert states == [("entry_open", None), ("entry_open", None)]
    assert result["state"] == "ok"
    assert account.calls == {
        "balance": 1,
        "orders": 1,
        "trades": 1,
        "positions": 1,
    }
    assert sorted(public.market_calls) == ["market-1", "market-2"]
    assert sorted(public.book_calls) == ["0x" + "01" * 64, "0x" + "02" * 64]


def test_first_seen_episodes_share_one_service_account_round(tmp_path) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    now = datetime.now(UTC)

    account = _TwoSessionAccountClient(now)
    public = _TwoMarketPublicClient(now)
    wallet = str(account.open_order.owner)
    adapter = PolymarketTradingClient(
        TradingConfig(wallet, wallet),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter)
    for index in (1, 2):
        suffix = f"{index:02d}"
        _first_seen_episode(
            store,
            f"episode-{suffix}",
            baseline_front="10",
            anchors=(f"order-{suffix}",),
            token_id="0x" + suffix * 64,
            condition_id="0x" + suffix * 32,
        )

    assert service.tick() == {"state": "none", "session_id": None}

    # Two real first-seen episodes consumed one authenticated bundle and
    # retained their independent fresh public-book reads.
    assert account.calls == {
        "balance": 1,
        "orders": 1,
        "trades": 1,
        "positions": 1,
    }
    assert sorted(public.book_calls) == ["0x" + "01" * 64, "0x" + "02" * 64]
    for index in (1, 2):
        episode = store.lp_first_seen_episode(f"episode-{index:02d}")
        assert episode is not None
        assert episode["state"] == "monitoring"


def test_first_seen_protection_waits_for_an_ended_real_round(tmp_path) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    adapter, account, _public = _round_adapter()
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter)
    episode = _first_seen_episode(store, "ep-ended", anchors=("order-open",))
    token = adapter.lp_account_round_begin()
    adapter.lp_account_round_end(token)

    service._apply_first_seen_protection(episode, token)

    unchanged = store.lp_first_seen_episode("ep-ended")
    assert unchanged is not None
    assert unchanged["state"] == "monitoring"
    assert Decimal(str(unchanged["data_failures"])) == 0
    assert unchanged["reason_codes"] == []
    assert unchanged["cancel_targets"] == []
    assert unchanged["cancel_requested_at"] is None
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []
    assert all(value == 0 for value in account.calls.values())


def test_ended_round_does_not_converge_first_seen_canceling_state(tmp_path) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    adapter, account, _public = _round_adapter()
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter)
    _first_seen_episode(store, "ep-canceling", anchors=("order-open",))
    store.lp_update_first_seen_episode(
        "ep-canceling",
        state="canceling",
        patch={"cancel_targets": ["order-open"], "cancel_failed": []},
    )
    token = adapter.lp_account_round_begin()
    adapter.lp_account_round_end(token)

    episode = store.lp_first_seen_episode("ep-canceling")
    assert episode is not None
    service._apply_first_seen_protection(episode, token)

    unchanged = store.lp_first_seen_episode("ep-canceling")
    assert unchanged is not None
    assert unchanged["state"] == "canceling"
    assert unchanged["cancel_targets"] == ["order-open"]
    assert unchanged["canceled_order_ids"] == []
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []
    assert all(value == 0 for value in account.calls.values())


def test_first_seen_protection_waits_for_real_round_invalidated_while_reading(
    tmp_path,
) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    adapter, account, _public = _round_adapter()
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter)
    _first_seen_episode(store, "ep-invalidated", anchors=("order-open",))
    cancels: list[str] = []
    adapter.cancel_order = lambda order_id: cancels.append(order_id) or True

    results: list[object] = []

    def tick() -> None:
        results.append(service.tick())

    worker = threading.Thread(target=tick)
    worker.start()
    try:
        assert account.first_order_started.wait(timeout=2)
        adapter.lp_account_round_invalidate(service._lp_account_rounds.copy().pop())
        account.release_first_order.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
    finally:
        account.release_first_order.set()
        worker.join(timeout=2)

    assert results == [{"state": "none", "session_id": None}]
    unchanged = store.lp_first_seen_episode("ep-invalidated")
    assert unchanged is not None
    assert unchanged["state"] == "monitoring"
    assert Decimal(str(unchanged["data_failures"])) == 0
    assert unchanged["reason_codes"] == []
    assert unchanged["cancel_targets"] == []
    assert unchanged["cancel_requested_at"] is None
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []
    assert cancels == []
    assert account.calls["balance"] == 1
    assert account.calls["orders"] == 1


def test_store_first_seen_update_fence_rejects_in_one_transaction(tmp_path) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    store = PredictionArbitrageStore(tmp_path)
    _first_seen_episode(store, "ep-store-fence")
    episode = store.lp_update_first_seen_episode(
        "ep-store-fence", patch={"data_failures": 3}
    )
    assert store.lp_advance_trade_generation(store.lp_trade_generation())

    with pytest.raises(ValueError, match="^account_round_invalid$"):
        store.lp_update_first_seen_episode(
            "ep-store-fence",
            patch={"data_failures": 99},
            expected_generation=0,
        )

    unchanged = store.lp_first_seen_episode("ep-store-fence")
    assert unchanged == episode
    updated = store.lp_update_first_seen_episode(
        "ep-store-fence",
        patch={"data_failures": 4},
        expected_generation=1,
    )
    assert updated["data_failures"] == 4


class _FailingPublicClient:
    def get_order_books(self, *, token_ids: list[str]) -> list[dict[str, object]]:
        raise OSError("sdk_book_unavailable")

    def close(self) -> None:
        return None


def test_successful_cancel_retry_receipt_survives_its_own_generation_fence(
    tmp_path,
) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    now = datetime.now(UTC)
    account = _TriggerAccountClient(now)
    public = _TriggerPublicClient(now)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter, clock=lambda: datetime.now(UTC))
    _first_seen_episode(store, "ep-retry", anchors=("order-open",))
    before = store.lp_update_first_seen_episode(
        "ep-retry",
        state="canceling",
        patch={
            "cancel_targets": ["order-open"],
            "cancel_failed": ["order-open"],
            "blocked_notified": False,
        },
    )
    notifications: list[object] = []
    cancels: list[str] = []

    def cancel_order(order_id: str) -> object:
        cancels.append(order_id)
        return {"canceled": [order_id], "not_canceled": {}}

    adapter.cancel_order = cancel_order
    service.set_protection_notifier(lambda *_args, **_kwargs: notifications.append(1))
    token = adapter.lp_account_round_begin(store.lp_trade_generation)

    service._apply_first_seen_protection(before, token)

    after = store.lp_first_seen_episode("ep-retry")
    assert after is not None
    assert after["state"] == "canceling"
    assert after["cancel_failed"] == []
    assert after["canceled_order_ids"] == ["order-open"]
    assert after["cancel_requested_at"] is not None
    assert after["notification_sent"] is True
    assert cancels == ["order-open"]
    assert notifications
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID)


def test_conservative_cancel_receipt_survives_its_own_generation_fence(
    tmp_path,
) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    now = datetime.now(UTC)
    account = _TriggerAccountClient(now)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=_FailingPublicClient,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter, clock=lambda: datetime.now(UTC))
    _first_seen_episode(store, "ep-conservative", anchors=("order-open",))
    before = store.lp_update_first_seen_episode(
        "ep-conservative",
        patch={"data_failures": 9, "blocked_notified": False},
    )
    notifications: list[object] = []
    cancels: list[str] = []

    def cancel_order(order_id: str) -> object:
        cancels.append(order_id)
        return {"canceled": [order_id], "not_canceled": {}}

    adapter.cancel_order = cancel_order
    service.set_protection_notifier(lambda *_args, **_kwargs: notifications.append(1))
    token = adapter.lp_account_round_begin(store.lp_trade_generation)

    service._apply_first_seen_protection(before, token)

    after = store.lp_first_seen_episode("ep-conservative")
    assert after is not None
    assert after["state"] == "canceling"
    assert after["cancel_reason"] == "book_unreliable"
    assert after["cancel_failed"] == []
    assert after["canceled_order_ids"] == ["order-open"]
    assert after["cancel_requested_at"] is not None
    assert after["notification_sent"] is True
    assert cancels == ["order-open"]
    assert notifications
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID)


def test_post_read_invalidation_cannot_relabel_returned_rows(
    tmp_path, monkeypatch,
) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    now = datetime.now(UTC)
    account = _TriggerAccountClient(now)
    public = _TriggerPublicClient(now)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter, clock=lambda: datetime.now(UTC))
    before = _first_seen_episode(
        store,
        "ep-post-read",
        anchors=("order-open",),
    )
    before = store.lp_update_first_seen_episode(
        "ep-post-read",
        patch={"data_failures": 3, "blocked_notified": True},
    )
    notifications: list[object] = []
    cancels: list[str] = []
    original_read = adapter.lp_open_orders_for_round

    def read_then_invalidate(token: object) -> list[dict[str, object]]:
        rows = original_read(token)
        adapter.lp_account_round_invalidate(token)
        return rows

    adapter.lp_open_orders_for_round = read_then_invalidate
    adapter.cancel_order = lambda order_id: cancels.append(order_id) or True
    service.set_protection_notifier(lambda *_args, **_kwargs: notifications.append(1))
    token = adapter.lp_account_round_begin(store.lp_trade_generation)

    service._apply_first_seen_protection(before, token)

    unchanged = store.lp_first_seen_episode("ep-post-read")
    assert unchanged == before
    assert cancels == []
    assert notifications == []
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []


def test_invalid_round_after_external_read_failure_waits_without_unscoped_retry(
    tmp_path, monkeypatch,
) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    now = datetime.now(UTC)
    account = _FirstReadFailingAccountClient(now)
    public = _TriggerPublicClient(now)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter, clock=lambda: datetime.now(UTC))
    before = _first_seen_episode(
        store,
        "ep-external-invalid",
        anchors=("order-open",),
        baseline_front="12000",
    )
    before = store.lp_update_first_seen_episode(
        "ep-external-invalid",
        patch={"data_failures": 9, "blocked_notified": False},
    )
    notifications: list[object] = []
    cancels: list[str] = []
    adapter.cancel_order = lambda order_id: cancels.append(order_id) or True
    service.set_protection_notifier(lambda *_args, **_kwargs: notifications.append(1))
    token = adapter.lp_account_round_begin(store.lp_trade_generation)
    original_failure = service._first_seen_data_failure

    def fail_then_invalidate(episode, reason, *, gate_open):
        result = original_failure(episode, reason, gate_open=gate_open)
        adapter.lp_account_round_invalidate(token)
        return result

    monkeypatch.setattr(
        service, "_first_seen_data_failure", fail_then_invalidate
    )

    service._apply_first_seen_protection(before, token)

    unchanged = store.lp_first_seen_episode("ep-external-invalid")
    assert unchanged == before
    assert account.calls["balance"] == 1
    assert cancels == []
    assert notifications == []
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []

    account.fail_reads = False
    fresh_token = adapter.lp_account_round_begin(store.lp_trade_generation)
    service._apply_first_seen_protection(unchanged, fresh_token)

    recovered = store.lp_first_seen_episode("ep-external-invalid")
    assert recovered is not None
    assert recovered["state"] == "monitoring"
    assert recovered["data_failures"] == 0
    assert account.calls["balance"] == 2
    assert account.calls["orders"] == 1
    assert cancels == []


@pytest.mark.parametrize("blocked_notified", [False, True])
def test_register_fence_rejection_waits_without_touching_first_seen_episode(
    tmp_path, monkeypatch, blocked_notified,
) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    now = datetime.now(UTC)
    account = _TriggerAccountClient(now)
    public = _TriggerPublicClient(now)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter, clock=lambda: datetime.now(UTC))
    episode = _first_seen_episode(
        store,
        "ep-register-fence",
        anchors=("order-open",),
    )
    episode = store.lp_update_first_seen_episode(
        "ep-register-fence",
        patch={
            "data_failures": 3,
            "blocked_notified": blocked_notified,
        },
    )
    notifications: list[object] = []
    cancels: list[str] = []
    adapter.cancel_order = lambda order_id: cancels.append(order_id) or True
    service.set_protection_notifier(lambda *_args, **_kwargs: notifications.append(1))
    original_register = store.lp_register_fenced_actions
    register_injections = 0

    def advance_then_register(*args: object, **kwargs: object):
        nonlocal register_injections
        register_injections += 1
        assert store.lp_advance_trade_generation(store.lp_trade_generation())
        return original_register(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "lp_register_fenced_actions", advance_then_register)

    results: list[object] = []

    def tick() -> None:
        results.append(service.tick())

    worker = threading.Thread(target=tick)
    worker.start()
    try:
        assert account.open_orders_read.wait(timeout=2)
        worker.join(timeout=2)
        assert not worker.is_alive()
    finally:
        worker.join(timeout=2)

    assert results == [{"state": "none", "session_id": None}]
    assert register_injections == 1
    unchanged = store.lp_first_seen_episode("ep-register-fence")
    assert unchanged == episode
    assert notifications == []
    assert cancels == []
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []


def test_book_read_invalidation_waits_then_next_real_round_can_cancel(
    tmp_path, monkeypatch,
) -> None:
    from tests.test_polymarket_lp import _first_seen_episode

    now = datetime.now(UTC)
    account = _TriggerAccountClient(now)
    book_entered = threading.Event()
    release_book = threading.Event()
    public = _TriggerPublicClient(
        now, book_entered=book_entered, release_book=release_book
    )
    assert not release_book.is_set(), "Explicit read barrier must stay closed until invalidation"
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter, clock=lambda: datetime.now(UTC))
    original = _first_seen_episode(
        store,
        "ep-book-fence",
        anchors=("order-open",),
    )
    original = store.lp_update_first_seen_episode(
        "ep-book-fence",
        patch={"data_failures": 3, "blocked_notified": True},
    )
    notifications: list[object] = []
    cancels: list[str] = []
    def cancel_order(order_id: str) -> object:
        cancels.append(order_id)
        return {"canceled": [order_id], "not_canceled": {}}

    adapter.cancel_order = cancel_order
    service.set_protection_notifier(lambda *_args, **_kwargs: notifications.append(1))
    token = adapter.lp_account_round_begin(store.lp_trade_generation)

    result_container: list[BaseException | None] = []

    def apply_stale() -> None:
        try:
            service._apply_first_seen_protection(original, token)
            result_container.append(None)
        except BaseException as exc:
            result_container.append(exc)

    worker = threading.Thread(target=apply_stale)
    worker.start()
    try:
        assert book_entered.wait(timeout=2)
        adapter.lp_account_round_invalidate(token)
    finally:
        release_book.set()
        worker.join(timeout=2)
        assert not worker.is_alive()

    assert result_container == [None]
    unchanged = store.lp_first_seen_episode("ep-book-fence")
    assert unchanged == original
    assert notifications == []
    assert cancels == []
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []

    fresh_token = adapter.lp_account_round_begin(store.lp_trade_generation)
    service._apply_first_seen_protection(unchanged, fresh_token)

    updated = store.lp_first_seen_episode("ep-book-fence")
    assert updated is not None
    assert updated["state"] == "canceling"
    assert updated["cancel_targets"] == ["order-open"]
    assert cancels == ["order-open"]
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID)


def test_incomplete_shared_orders_block_first_seen_until_complete(tmp_path) -> None:
    from tests.test_polymarket_lp import (
        _FIRST_SEEN_CONDITION,
        _FIRST_SEEN_TOKEN,
        _first_seen_episode,
    )

    now = datetime.now(UTC)

    class IncompleteAccount(_SDKAccountClient):
        def __init__(self, now: datetime) -> None:
            super().__init__(now)
            self.complete = False

        def anchor_order(self) -> OpenOrder:
            return OpenOrder(
                id="m-1",
                market=_FIRST_SEEN_CONDITION,
                asset_id=_FIRST_SEEN_TOKEN,
                owner=str(self.open_order.owner),
                maker_address=str(self.open_order.owner),
                side="BUY",
                price=Decimal("0.30"),
                original_size=Decimal("2000"),
                size_matched=Decimal("0"),
                outcome="YES",
                order_type="GTC",
                status="LIVE",
                associate_trades=(),
                created_at=self.now,
                expiration=self.now + timedelta(minutes=10),
            )

        def list_open_orders(self, **_kwargs: object) -> list[object]:
            if self.complete:
                return [self.anchor_order()]
            # The SDK accepted a partial row; the adapter must not turn the
            # normalization drop into proof that the anchor disappeared.
            return [
                self.open_order,
                {
                    "id": "m-1",
                    "asset_id": _FIRST_SEEN_TOKEN,
                    "side": "BUY",
                },
            ]

    class TradingAdapter(PolymarketTradingClient):
        def __init__(self, account: IncompleteAccount, now: datetime) -> None:
            super().__init__(
                TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
                account,
            )
            self.books = {
                _FIRST_SEEN_TOKEN: {
                    "condition_id": _FIRST_SEEN_CONDITION,
                    "token_id": _FIRST_SEEN_TOKEN,
                    "received_at": now,
                    "source_timestamp": now,
                    "hash": "book-hash-first-seen",
                    "bids": [
                        {"price": Decimal("0.30"), "size": Decimal("8000")}
                    ],
                    "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
                }
            }

        def lp_order_books(
            self, token_ids: object, *, stop_event: object = None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                str(token_id): self.books[str(token_id)]
                for token_id in tuple(token_ids)  # type: ignore[arg-type]
                if str(token_id) in self.books
            }

    account = IncompleteAccount(now)
    adapter = TradingAdapter(account, now)
    cancels: list[str] = []
    adapter.cancel_order = lambda order_id: cancels.append(order_id) or True
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, adapter, clock=lambda: now)
    _first_seen_episode(
        store,
        "ep-incomplete",
        baseline_front="8000",
        anchors=("m-1",),
    )

    token = adapter.lp_account_round_begin()
    try:
        with pytest.raises(ValueError, match="open_orders_unknown"):
            adapter.lp_open_orders_for_round(token)
    finally:
        adapter.lp_account_round_end(token)

    service.tick()
    episode = store.lp_first_seen_episode("ep-incomplete")
    assert episode is not None
    assert episode["state"] == "monitoring"
    assert Decimal(str(episode["data_failures"])) == 1
    assert episode["cancel_targets"] == []
    assert cancels == []
    assert store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID) == []

    account.complete = True
    service.tick()
    recovered = store.lp_first_seen_episode("ep-incomplete")
    assert recovered is not None
    assert recovered["state"] == "monitoring"
    assert Decimal(str(recovered["data_failures"])) == 0
    assert cancels == []


def test_next_real_tick_reads_a_new_account_bundle(tmp_path) -> None:
    service, account, public = _service(tmp_path)

    service.tick()
    # Market reads are independent: finish them before the next financial
    # round consumes their fresh result instead of launching duplicate I/O.
    for future in tuple(service.exchange._lp_public_reads.values()):
        future.result(timeout=5)
    service.tick()

    assert account.calls == {
        "balance": 2,
        "orders": 2,
        "trades": 2,
        "positions": 2,
    }
    assert sorted(public.market_calls) == [
        "market-1",
        "market-2",
    ]
    assert sorted(public.book_calls) == [
        "0x" + "01" * 64,
        "0x" + "02" * 64,
    ]


def _adapter_with_history(*trades: object):
    account = _CountingAccountClient(NOW)
    account.lp_round_history = list(trades)
    account.list_account_trades = (
        lambda **kwargs: list(account.lp_round_history)
    )
    sdk_trades = account.list_account_trades

    def counted_trades(**kwargs):
        account.calls["trades"] += 1
        return sdk_trades(**kwargs)

    account.list_account_trades = counted_trades
    public = _TwoMarketPublicClient(NOW)
    adapter = PolymarketTradingClient(
        TradingConfig("0x" + "1" * 40, "0x" + "2" * 40),
        account,
        public_client_factory=lambda: public,
    )
    return adapter, account, public


def _history_trade(**overrides: object) -> ClobTrade:
    address = "0x3333333333333333333333333333333333333333"
    condition = "0x" + "c" * 64
    token = "0x" + "1" * 64
    maker = MakerOrder(
        order_id="order-1",
        asset_id=token,
        maker_address=address,
        owner=address,
        side="BUY",
        price=Decimal("0.30"),
        matched_amount=Decimal("100"),
        outcome="YES",
        fee_rate_bps=Decimal("0"),
    )
    values = {
        "id": "trade-1",
        "market": condition,
        "asset_id": token,
        "owner": address,
        "maker_address": address,
        "taker_order_id": "",
        "side": "BUY",
        "trader_side": "MAKER",
        "price": Decimal("0.30"),
        "size": Decimal("100"),
        "outcome": "YES",
        "status": "CONFIRMED",
        "fee_rate_bps": Decimal("0"),
        "bucket_index": 0,
        "transaction_hash": "0xtrade",
        "maker_orders": (maker,),
        "match_time": NOW,
        "last_update": NOW,
    }
    values.update(overrides)
    return ClobTrade(**values)


def test_unrelated_failed_history_does_not_poison_session() -> None:
    unrelated = _history_trade(
        id="trade-unrelated",
        market="0x" + "9" * 64,
        asset_id="0x" + "9" * 64,
        taker_order_id="",
        maker_orders=(),
        status="FAILED",
        size=Decimal("0"),
    )
    adapter, account, _public = _adapter_with_history(unrelated)
    request = _request(NOW, index=1)

    snapshot = adapter.lp_snapshot(request)

    assert snapshot["trades"] == []
    assert account.calls["trades"] == 1


def test_exact_ids_attribute_mixed_trades_without_top_level_token() -> None:
    maker_one = MakerOrder(
        order_id="order-1",
        asset_id="0x" + "1" * 64,
        maker_address="0x" + "3" * 40,
        owner="0x" + "3" * 40,
        side="BUY",
        price=Decimal("0.30"),
        matched_amount=Decimal("40"),
        outcome="YES",
        fee_rate_bps=Decimal("0"),
    )
    maker_two = MakerOrder(
        order_id="order-maker-2",
        asset_id="0x" + "9" * 64,
        maker_address="0x" + "3" * 40,
        owner="0x" + "3" * 40,
        side="BUY",
        price=Decimal("0.30"),
        matched_amount=Decimal("60"),
        outcome="YES",
        fee_rate_bps=Decimal("0"),
    )
    mixed = _history_trade(
        id="trade-mixed",
        asset_id="0x" + "1" * 64,
        taker_order_id="order-taker",
        maker_orders=(maker_one, maker_two),
    )
    adapter, _account, _public = _adapter_with_history(mixed)
    request = {
        **_request(NOW, index=1),
        "owned_order_ids": ["order-taker", "order-1", "order-maker-2"],
    }

    snapshot = adapter.lp_snapshot(request)

    assert len(snapshot["trades"]) == 1
    trade = snapshot["trades"][0]
    assert trade["trade_id"] == "trade-mixed"
    assert {row["order_id"] for row in trade["maker_orders"]} == {
        "order-1",
        "order-maker-2",
    }


def test_exact_relevant_missing_top_level_token_stays_unknown() -> None:
    maker = MakerOrder(
        order_id="order-1",
        asset_id="0x" + "1" * 64,
        maker_address="0x" + "3" * 40,
        owner="0x" + "3" * 40,
        side="BUY",
        price=Decimal("0.30"),
        matched_amount=Decimal("100"),
        outcome="YES",
        fee_rate_bps=Decimal("0"),
    )
    trade = _history_trade(
        id="trade-missing-top-token",
        asset_id="",
        taker_order_id="",
        maker_orders=(maker,),
    )
    adapter, _account, _public = _adapter_with_history(trade)
    request = {**_request(NOW, index=1), "owned_order_ids": ["order-1"]}

    # Exact IDs make this row relevant, but normalization cannot invent the
    # missing top-level/taker token. It remains fail-closed, not filtered.
    with pytest.raises(ValueError, match="external_snapshot_unknown"):
        adapter.lp_snapshot(request)


def test_malformed_maker_token_is_relevant_and_stays_unknown() -> None:
    request = _request(NOW, index=1)
    maker = MakerOrder(
        order_id="",
        asset_id=request["token_id"],
        maker_address="0x" + "3" * 40,
        owner="0x" + "3" * 40,
        side="BUY",
        price=Decimal("0.30"),
        matched_amount=Decimal("100"),
        outcome="YES",
        fee_rate_bps=Decimal("0"),
    )
    trade = _history_trade(
        id="trade-malformed-maker",
        market=request["condition_id"],
        asset_id="0x" + "9" * 64,
        taker_order_id="foreign",
        maker_orders=(maker,),
    )
    adapter, _account, _public = _adapter_with_history(trade)

    # The matching maker token proves the row is relevant despite the
    # different top-level token; its missing order ID must then fail closed.
    with pytest.raises(ValueError, match="external_snapshot_unknown"):
        adapter.lp_snapshot(request)


def test_matching_maker_legs_attribute_valid_mixed_trades() -> None:
    request = _request(NOW, index=1)
    token_id = request["token_id"]
    makers = (
        MakerOrder(
            order_id="order-maker-a",
            asset_id=token_id,
            maker_address="0x" + "3" * 40,
            owner="0x" + "3" * 40,
            side="BUY",
            price=Decimal("0.30"),
            matched_amount=Decimal("40"),
            outcome="YES",
            fee_rate_bps=Decimal("0"),
        ),
        MakerOrder(
            order_id="order-maker-b",
            asset_id=token_id,
            maker_address="0x" + "3" * 40,
            owner="0x" + "3" * 40,
            side="BUY",
            price=Decimal("0.30"),
            matched_amount=Decimal("60"),
            outcome="YES",
            fee_rate_bps=Decimal("0"),
        ),
    )
    trade = _history_trade(
        id="trade-maker-mixed",
        market=request["condition_id"],
        asset_id=token_id,
        taker_order_id="foreign",
        maker_orders=makers,
    )
    adapter, _account, _public = _adapter_with_history(trade)
    request = {
        **_request(NOW, index=1),
        "owned_order_ids": ["order-maker-a", "order-maker-b"],
    }

    snapshot = adapter.lp_snapshot(request)

    assert len(snapshot["trades"]) == 1
    assert snapshot["trades"][0]["trade_id"] == "trade-maker-mixed"
    assert {row["order_id"] for row in snapshot["trades"][0]["maker_orders"]} == {
        "order-maker-a",
        "order-maker-b",
    }


def _exact_order(
    *,
    matched: Decimal,
    trade_ids: tuple[str, ...],
    order_id: str = "order-1",
    request: dict[str, object] | None = None,
    status: str = "FILLED",
) -> OpenOrder:
    address = "0x3333333333333333333333333333333333333333"
    condition = str((request or {}).get("condition_id", "0x" + "c" * 64))
    token = str((request or {}).get("token_id", "0x" + "1" * 64))
    return OpenOrder(
        id=order_id,
        market=condition,
        asset_id=token,
        owner=address,
        maker_address=address,
        side="BUY",
        price=Decimal("0.30"),
        original_size=Decimal("100"),
        size_matched=matched,
        outcome="YES",
        order_type="GTD",
        status=status,
        associate_trades=trade_ids,
        created_at=NOW,
    )


def test_old_complete_exact_receipt_reuses_round_then_recovers() -> None:
    adapter, account, _public = _adapter_with_history(_history_trade())
    account = adapter._client
    account.get_order = lambda **kwargs: _exact_order(
        matched=Decimal("100"), trade_ids=()
    )
    token = adapter.lp_account_round_begin()
    request = {
        **_request(NOW, index=1),
        "entry_order_id": "order-1",
        "owned_order_ids": ["order-1"],
        "_lp_account_round": token,
    }

    snapshot = adapter.lp_snapshot(request)

    exact = next(row for row in snapshot["orders"] if row["order_id"] == "order-1")
    assert exact["status"] == "FILLED"
    assert exact["size_matched"] == Decimal("100")
    repeated = adapter.lp_snapshot(request)
    assert next(
        row for row in repeated["orders"] if row["order_id"] == "order-1"
    )["size_matched"] == Decimal("100")
    assert account.calls["trades"] == 1

    # A contradictory FAILED-only bundle cannot make the FILLED receipt look
    # old. Reject it, then recover from a fresh consistent bundle.
    account.lp_round_history[:] = [
        _history_trade(id="trade-failed", status="FAILED", size=Decimal("100"))
    ]
    adapter.lp_account_round_end(token)
    failed_token = adapter.lp_account_round_begin()
    failed_request = {**request, "_lp_account_round": failed_token}
    with pytest.raises(ValueError, match="external_snapshot_unknown"):
        adapter.lp_snapshot(failed_request)

    account.lp_round_history[:] = [_history_trade(id="trade-recovered")]
    recovered_token = adapter.lp_account_round_begin()
    recovered = adapter.lp_snapshot({**request, "_lp_account_round": recovered_token})
    assert next(
        row for row in recovered["orders"] if row["order_id"] == "order-1"
    )["size_matched"] == Decimal("100")
    assert account.calls["trades"] == 3
    assert adapter.lp_snapshot({**request, "_lp_account_round": recovered_token})
    assert account.calls["trades"] == 3


def test_failed_history_contradicts_filled_receipt() -> None:
    adapter, account, _public = _adapter_with_history(
        _history_trade(id="trade-failed", status="FAILED")
    )
    account.get_order = lambda **kwargs: _exact_order(
        matched=Decimal("100"), trade_ids=()
    )
    token = adapter.lp_account_round_begin()
    request = {
        **_request(NOW, index=1),
        "entry_order_id": "order-1",
        "owned_order_ids": ["order-1"],
        "_lp_account_round": token,
    }

    with pytest.raises(ValueError, match="external_snapshot_unknown"):
        adapter.lp_snapshot(request)
    assert account.calls["trades"] == 1


def test_duplicate_trade_identity_is_not_additional_fill() -> None:
    duplicated = _history_trade(id="trade-duplicate")
    adapter, account, _public = _adapter_with_history(duplicated, duplicated)
    account.get_order = lambda **kwargs: _exact_order(
        matched=Decimal("100"), trade_ids=()
    )
    token = adapter.lp_account_round_begin()
    request = {
        **_request(NOW, index=1),
        "entry_order_id": "order-1",
        "owned_order_ids": ["order-1"],
        "_lp_account_round": token,
    }

    snapshot = adapter.lp_snapshot(request)

    exact = next(row for row in snapshot["orders"] if row["order_id"] == "order-1")
    assert exact["status"] == "FILLED"
    assert exact["size_matched"] == Decimal("100")


def test_newer_exact_receipt_invalidates_shared_bundle() -> None:
    adapter, _account, _public = _adapter_with_history(_history_trade())
    account = adapter._client
    account.get_order = lambda **kwargs: _exact_order(
        matched=Decimal("110"), trade_ids=("trade-1",)
    )
    token = adapter.lp_account_round_begin()
    request = {
        **_request(NOW, index=1),
        "entry_order_id": "order-1",
        "owned_order_ids": ["order-1"],
        "_lp_account_round": token,
    }

    with pytest.raises(ValueError, match="external_snapshot_unknown"):
        adapter.lp_snapshot(request)


def test_auto_bounded_round_waits_for_launched_jobs(tmp_path, monkeypatch, request) -> None:
    """The runtime LP tick joins financial jobs, not blocked public reads.

    #249 separated this owner from automatic refill. Preserve concurrency,
    account-round cleanup and shared reads through the production tick seam.
    """
    from open_trader import polymarket_trading
    now = NOW
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(polymarket_trading, 'datetime', Clock)
    account = _TwoSessionAccountClient(now)
    public = _BlockingTwoMarketPublicClient(now)
    wallet = str(account.open_order.owner)
    adapter = PolymarketTradingClient(
        TradingConfig(wallet, wallet),
        account,
        public_client_factory=lambda: public,
    )
    request.addfinalizer(adapter.close)
    store = PredictionArbitrageStore(tmp_path)
    for index in (1, 2):
        store.lp_create_session(
            f"session-{index:02d}",
            f"session-{index:02d}-key",
            state="entry_open",
            payload=_request(now, index=index),
        )
    lp = PolymarketLPService(store, adapter, clock=lambda: now)
    engine = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=adapter,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=lp,
    )
    engine._breaker_open = False
    pool = engine._lp_auto_pool()

    def seed(document: dict[str, object]) -> None:
        intents: dict[str, object] = {}
        for index in (1, 2):
            suffix = f"{index:02d}"
            request = _request(now, index=index)
            intents[f"intent-{suffix}"] = {
                "intent_id": f"intent-{suffix}",
                "session_id": f"session-{suffix}",
                "order_id": f"order-{suffix}",
                "state": "unknown",
                "financial_status": "unknown",
                "reconcile_reason": "test_pending",
                "reserved_usd": "3.00",
                "inventory_cost_usd": "0",
                "realized_pnl_usd": "0",
                **{
                    key: request[key]
                    for key in (
                        "condition_id", "market_id", "token_id", "outcome",
                        "price", "quantity",
                    )
                },
                "created_at": now.isoformat(),
            }
        document.update(
            desired_running=False,
            target_buy_count=2,
            budget_usd="100",
            intents=intents,
        )

    pool._update(seed)
    result_container: list[object] = []
    run_errors: list[BaseException] = []

    def run():
        try:
            result_container.append(engine.lp_tick())
        except BaseException as exc:
            run_errors.append(exc)
            raise

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert public.books_started.wait(timeout=5), "Both SDK jobs must start before checking their owner"
        worker.join(timeout=1.2)
        assert not worker.is_alive()
        assert public.max_active_books == 2
        assert len(public.books_entered) == 2

        # Public reads retain their own jobs; financial publication and its
        # shared-account round finish without waiting for either book.
        assert not lp._lp_account_rounds, 'Tick must join financial workers before closing their shared round'
        assert not public.release_books.is_set(), 'Tick must finish without releasing slow public reads'
        assert all(intent['financial_status'] == 'known' for intent in pool.state()['intents'])
        futures = tuple(adapter._lp_public_reads.values())
        assert len(futures) == 2
        assert all(not future.done() for future in futures)
        public.release_books.set()
        for future in futures:
            future.result(timeout=5)
    finally:
        public.release_books.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert not run_errors
    assert result_container
    assert account.calls == {
        "balance": 1,
        "orders": 1,
        "trades": 1,
        "positions": 1,
    }
    assert sorted(public.market_calls) == ["market-1", "market-2"]
    assert sorted(public.books_entered) == [
        "0x" + "01" * 64,
        "0x" + "02" * 64,
    ]
    assert sorted(public.book_calls) == [
        "0x" + "01" * 64,
        "0x" + "02" * 64,
    ]
    assert public.max_active_books == 2
    state = pool.state()
    assert public.active_books == 0
    assert all(future.done() for future in futures)
    assert not lp._lp_account_rounds
    intents = {intent["intent_id"]: intent for intent in state["intents"]}
    for intent_id in ("intent-01", "intent-02"):
        intent = intents[intent_id]
        assert intent["financial_status"] == "known"
        session = store.lp_session(intent["session_id"])
        assert session is not None
        assert session["facts_error"] is None

    # Automatic refill consumes current account facts; it must not launch
    # this independent history/public-read lane, even with two managed intents.
    before_books = list(public.book_calls)
    account.calls = dict.fromkeys(account.calls, 0)
    engine.lp_auto_run_once(round_id='tick-does-not-couple-refill')
    assert public.book_calls == before_books
    assert pool._reconcile_jobs == {}
    assert account.calls == dict.fromkeys(account.calls, 1)


def test_provider_failure_completes_round_waiters(monkeypatch) -> None:
    adapter, account, _public = _round_adapter()
    provider_entered = threading.Event()
    release_provider = threading.Event()
    provider_calls = 0

    def provider() -> int:
        nonlocal provider_calls
        provider_calls += 1
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        raise RuntimeError("provider_failed")

    token = adapter.lp_account_round_begin(provider)
    captures: dict[str, tuple[object, BaseException | None]] = {}

    def reader() -> None:
        captures[threading.current_thread().name] = _run_lp_snapshot(
            adapter, token
        )

    owner = threading.Thread(target=reader)
    owner.start()
    try:
        assert provider_entered.wait(timeout=2)
        joined = _observe_round_waiters(token, 1, monkeypatch)
        waiter = threading.Thread(target=reader)
        waiter.start()
        assert joined.wait(timeout=2), "waiter must share the in-flight provider Future"
        assert waiter.is_alive()

        release_provider.set()
        for thread in (owner, waiter):
            thread.join(timeout=2)
            assert not thread.is_alive()

        errors = [result[1] for result in captures.values()]
        assert len(errors) == 2
        # The adapter's worker boundary converts both the original owner error
        # and the identical Future exception delivered to the waiter.
        assert all(
            isinstance(exc, ValueError) and str(exc) == "external_snapshot_unknown"
            for exc in errors
        )
        with token.lock:
            assert token.future is None
        assert provider_calls == 1, "all consumers must share one provider failure"
        # The failed provider fence ran before any external account request.
        assert all(value == 0 for value in account.calls.values())
    finally:
        release_provider.set()
        for thread in (owner, waiter if "waiter" in locals() else None):
            if thread is not None:
                thread.join(timeout=2)


def test_registered_cancel_receipt_survives_a_newer_fence(tmp_path) -> None:
    service, _account, _public = _service(tmp_path)
    store = service.store
    action_key = store.lp_register_fenced_actions(
        "session-01",
        [{
            "action_key": "session-01:registered-cancel",
            "payload": {
                "role": "entry",
                "order_id": "order-01",
                "targets": ["order-01"],
            },
        }],
        expected_generation=0,
        expected_trade_revision=0,
    )[0]["action_key"]
    # A newer fence invalidates later authorization, but cannot discard an
    # already-registered action's real receipt.
    store.lp_register_trade_change("session-02")
    assert store.lp_trade_generation() == 2

    service.exchange.cancel_order = lambda order_id: {
        "order_id": order_id,
        "status": "CANCELED",
    }
    service.finish_order_cancel(
        [
            (
                "session-01",
                action_key,
                {"role": "entry", "order_id": "order-01"},
            )
        ],
        ["order-01"],
    )

    actions = store.lp_actions("session-01")
    assert len(actions) == 1
    assert actions[0]["action_key"] == action_key
    assert actions[0]["state"] == "accepted"
    assert actions[0]["order_id"] == "order-01"

def test_blocked_mutation_registers_no_cancel_intent(tmp_path) -> None:
    service, _account, _public = _service(tmp_path)
    store = service.store
    session = store.lp_session("session-01")
    assert session is not None
    store.lp_update_session("session-01", patch={
        "passive_exit_order_id": "order-02",
    })
    session = store.lp_session("session-01")
    assert session is not None
    service._mutation_guard = lambda action="submit": False
    cancel_calls: list[str] = []
    service.exchange.cancel_order = (
        lambda order_id: cancel_calls.append(order_id) or True
    )

    with pytest.raises(RuntimeError, match="mutation_blocked"):
        service._cancel_owned_orders(session)
    blocked = service._request_passive_cancel(session)

    assert cancel_calls == []
    assert blocked["state"] == "needs_attention"
    assert blocked["reconciliation"] == "passive_cancel__MutationBlocked"
    assert store.lp_actions("session-01") == []
    assert store.lp_actions("session-02") == []
    assert store.lp_trade_generation() == 0


def test_old_passive_replacement_observes_shared_cancel_fence(tmp_path) -> None:
    now = datetime.now(UTC)

    def prepare(target: str):
        service, _account, _public = _service(tmp_path / target)
        store = service.store
        engine = PredictionExecutionService(
            store=store,
            monitor=SimpleNamespace(),
            trading=service.exchange,
            notifier=SimpleNamespace(),
            lock_path=(tmp_path / target) / "execution.lock",
            lp=service,
        )
        engine._breaker_open = False
        request = _request(now, index=1)
        engine._lp_auto_pool()._update(lambda document: document.update(intents={
            "intent-01": {
                "intent_id": "intent-01",
                "session_id": "session-01",
                "order_id": "order-01",
                "state": "active",
                "financial_status": "known",
                "reserved_usd": "3.00",
                "inventory_cost_usd": "0",
                "realized_pnl_usd": "0",
                **{
                    key: request[key]
                    for key in (
                        "condition_id", "market_id", "token_id", "outcome",
                        "price", "quantity",
                    )
                },
                "created_at": now.isoformat(),
            }
        }))
        store.lp_update_session("session-01", patch={
            "submit_status": "accepted",
            "residual_quantity": "2",
            "position_reconciled": True,
            "passive_exit_order_id": "passive-order-01",
            "passive_exit_price": Decimal("0.29"),
        })
        session = store.lp_session("session-01")
        assert session is not None
        cancel_calls: list[str] = []
        submit_calls: list[object] = []
        service.exchange.cancel_order = (
            lambda order_id: cancel_calls.append(order_id) or {
                "order_id": order_id,
                "status": "CANCELED",
            }
        )
        service.exchange.lp_create_limit_order = lambda **kwargs: (
            submit_calls.append(("sign", kwargs)) or SimpleNamespace(post_only=True)
        )
        service.exchange.lp_post_order = lambda signed: (
            submit_calls.append(("post", signed)) or {"order_id": "sell-new"}
        )
        snapshot = {
            "_lp_trade_generation": 0,
            "trades": [],
            "orders": [
                {
                    "order_id": "order-01",
                    "token_id": request["token_id"],
                    "market_id": request["market_id"],
                    "side": "BUY",
                    "price": "0.30",
                    "status": "LIVE",
                    "original_size": "10",
                    "size_matched": "0",
                },
                {
                    "order_id": "passive-order-01",
                    "token_id": request["token_id"],
                    "market_id": request["market_id"],
                    "side": "SELL",
                    "price": "0.29",
                    "status": "LIVE",
                    "original_size": "2",
                    "size_matched": "0",
                },
            ],
            "book": {
                "received_at": now.isoformat(),
                "asks": [{"price": Decimal("0.31"), "size": Decimal("5")}],
            },
        }
        return service, store, engine, session, cancel_calls, submit_calls, snapshot

    service, store, engine, session, cancels, submits, snapshot = prepare("stale")
    store.lp_register_trade_change("session-02")
    service._ensure_passive_exit(
        session, snapshot, Decimal("2"), trade_revision=0
    )

    assert cancels == [] and submits == []
    assert store.lp_actions("session-01") == []
    rejected = store.lp_session("session-01")
    assert rejected is not None
    assert rejected["facts_error"] == "account_round_invalid"
    assert rejected["fee_status"] == "unknown"
    assert rejected["position_reconciled"] is False
    auto = engine.lp_auto_state()
    assert auto["funds"]["status"] == "unknown"
    assert auto["intents"][0]["financial_status"] == "unknown"

    service, store, _engine, _session, cancels, submits, snapshot = prepare(
        "fresh"
    )
    service._ensure_passive_exit(
        _session, snapshot, Decimal("2"), trade_revision=0
    )

    assert cancels == ["passive-order-01"] and submits == []
    updated = store.lp_session("session-01")
    assert updated is not None
    assert updated["passive_cancel_requested"] is True
    actions = store.lp_actions("session-01")
    assert len(actions) == 1
    assert actions[0]["action_key"] == "session-01:passive-cancel:passive-order-01"
    assert actions[0]["state"] == "accepted"


def test_owned_cancel_success_keeps_cancel_specific_receipt_key(tmp_path) -> None:
    service, _account, _public = _service(tmp_path)
    store = service.store
    session = store.lp_session("session-01")
    assert session is not None
    store.lp_update_session("session-01", patch={
        "passive_exit_order_id": "order-02",
    })
    session = store.lp_session("session-01")
    assert session is not None
    service.exchange.cancel_order = lambda order_id: {
        "order_id": order_id,
        "status": "CANCELED",
    }

    service._cancel_owned_orders(session)

    actions = {
        action["action_key"]: action
        for action in store.lp_actions("session-01")
    }
    assert actions["session-01:entry-cancel:order-01"]["state"] == "accepted"
    assert actions["session-01:entry-cancel:order-01"]["role"] == "entry"
    assert (
        actions["session-01:passive_exit-cancel:order-02"]["state"]
        == "accepted"
    )
    assert (
        actions["session-01:passive_exit-cancel:order-02"]["role"]
        == "passive_exit"
    )
    updated = store.lp_session("session-01")
    assert updated is not None
    assert updated["entry_cancel_requested"] is True
    assert updated["passive_cancel_requested"] is True


def test_late_newer_facts_after_market_timeout_do_not_write_db(tmp_path) -> None:
    service, _account, _public = _service(tmp_path)
    store = service.store
    request = _request(NOW, index=1)
    read_started = threading.Event()
    release_read = threading.Event()

    def late_newer_facts(_request):
        read_started.set()
        assert release_read.wait(timeout=5)
        raise LpNewerAccountFacts(observed_trade_generation=0)

    service.exchange.lp_snapshot = late_newer_facts
    service._market_read_timeout = 0.05
    service._facts_owner.session_id = "session-01"
    try:
        result = service._reconcile_facts_once(
            "session-01", apply_lock=None, report_only=False
        )
        assert result[3] == "market_read_timeout"
        session = store.lp_session("session-01")
        assert session is not None
        assert session["facts_error"] == "market_read_timeout"
        assert store.lp_trade_generation() == 0

        release_read.set()
        assert read_started.wait(timeout=2)
        key = str(request["condition_id"])
        future = service._market_reads.get(key)
        assert future is not None
        for _ in range(100):
            if future.done():
                break
            time.sleep(0.01)
        assert future.done()
        late_error = future.exception()
        assert isinstance(late_error, LpNewerAccountFacts)

        # The abandoned worker returned only to the discarded Future.
        assert store.lp_trade_generation() == 0
        session = store.lp_session("session-01")
        assert session is not None
        assert session["facts_error"] == "market_read_timeout"
    finally:
        service._facts_owner.session_id = None
        release_read.set()


def test_stale_shared_bundle_cannot_register_or_send_protection_cancel(tmp_path) -> None:
    service, _account, _public = _service(tmp_path)
    store = service.store
    request = _request(NOW, index=1)
    store.lp_update_session("session-01", patch={
        "queue_protection": {
            "version": 2,
            "data_failures": 0,
            "levels": {
                "0.30": {
                    "state": "triggered",
                    "order_id": "order-01",
                    "baseline_price": "0.30",
                    "ratio": "2.0",
                }
            },
        }
    })
    session = store.lp_session("session-01")
    assert session is not None
    row = store.lp_session_with_revision("session-01", trading=True)
    assert row is not None
    _session, revision = row
    snapshot = {
        "_lp_trade_generation": 0,
        "trades": [],
        "orders": [{
            "order_id": "order-01",
            "token_id": request["token_id"],
            "market_id": request["market_id"],
            "side": "BUY",
            "price": "0.30",
            "status": "LIVE",
            "original_size": "10",
            "size_matched": "0",
        }],
    }
    original_is_current = service._account_bundle_is_current
    check_count = 0
    check_lock = threading.Lock()

    def invalidate_after_precheck(snapshot_arg):
        nonlocal check_count
        result = original_is_current(snapshot_arg)
        with check_lock:
            check_count += 1
            is_precheck = check_count == 1
        if is_precheck:
            assert service._invalidate_lp_trade_generation(0)
        return result

    service._account_bundle_is_current = invalidate_after_precheck
    cancel_calls: list[str] = []
    service.exchange.cancel_order = lambda order_id: cancel_calls.append(order_id) or True

    result = service._apply_tick_snapshot(
        session,
        snapshot,
        revision,
        "",
        apply_lock=None,
    )

    assert cancel_calls == []
    assert store.lp_actions("session-01") == []
    assert store.lp_trade_generation() == 1
    rejected = store.lp_session("session-01")
    assert rejected is not None
    assert rejected["facts_error"] == "account_round_invalid"


def test_known_failed_trade_reference_is_not_newer_account_evidence(tmp_path) -> None:
    service, account, _public = _service(tmp_path)
    request = _request(NOW, index=1)
    account.trade = _history_trade(
        id="known-failed",
        market=request["condition_id"],
        asset_id=request["token_id"],
        taker_order_id="order-01",
        status="FAILED",
        size=Decimal("0"),
        maker_orders=(),
    )
    account.get_order = lambda **kwargs: _exact_order(
        order_id="order-01",
        request=request,
        matched=Decimal("0"),
        trade_ids=("known-failed",),
        status="LIVE",
    )
    token = service.exchange.lp_account_round_begin()

    snapshot = service._read_snapshot({**request, "_lp_account_round": token})

    assert snapshot["account"]["authenticated"] is True
    assert service.store.lp_trade_generation() == 0


def test_stale_owned_cancel_batch_cannot_authorize_venue_calls(tmp_path) -> None:
    service, _account, _public = _service(tmp_path)
    store = service.store
    engine = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=service.exchange,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    engine._breaker_open = False
    request = _request(NOW, index=1)
    engine._lp_auto_pool()._update(lambda document: document.update(intents={
        "intent-01": {
            "intent_id": "intent-01",
            "session_id": "session-01",
            "order_id": "order-01",
            "state": "active",
            "financial_status": "known",
            "reserved_usd": "3.00",
            "inventory_cost_usd": "0",
            "realized_pnl_usd": "0",
            **{
                key: request[key]
                for key in (
                    "condition_id", "market_id", "token_id", "outcome",
                    "price", "quantity",
                )
            },
            "created_at": NOW.isoformat(),
        }
    }))
    store.lp_register_trade_change("session-02")
    session = store.lp_session("session-01")
    assert session is not None
    cancel_calls: list[str] = []
    service.exchange.cancel_order = lambda order_id: cancel_calls.append(order_id) or True

    snapshot = {"_lp_trade_generation": 0, "trades": [], "orders": []}
    with pytest.raises(ValueError, match="account_round_invalid"):
        service._cancel_owned_orders(
            session,
            expected_generation=0,
            expected_trade_revision=0,
        )

    assert cancel_calls == []
    assert store.lp_actions("session-01") == []
    rejected = store.lp_session("session-01")
    assert rejected is not None
    assert rejected["facts_error"] == "account_round_invalid"
    assert rejected["fee_status"] == "unknown"
    assert rejected["position_reconciled"] is False
    auto = engine.lp_auto_state()
    assert auto["funds"]["status"] == "unknown"
    assert auto["intents"][0]["financial_status"] == "unknown"

def test_stale_passive_exit_intent_fails_closed_before_post(tmp_path) -> None:
    now = datetime.now(UTC)
    service, _account, _public = _service(tmp_path)
    store = service.store
    engine = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=service.exchange,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    engine._breaker_open = False
    request = _request(now, index=1)
    engine._lp_auto_pool()._update(lambda document: document.update(intents={
        "intent-01": {
            "intent_id": "intent-01",
            "session_id": "session-01",
            "order_id": "order-01",
            "state": "active",
            "financial_status": "known",
            "reserved_usd": "3.00",
            "inventory_cost_usd": "0",
            "realized_pnl_usd": "0",
            **{
                key: request[key]
                for key in (
                    "condition_id", "market_id", "token_id", "outcome",
                    "price", "quantity",
                )
            },
            "created_at": now.isoformat(),
        }
    }))

    # The entry is already durably resolved, so only the stale shared bundle
    # can block this new passive-exit authorization.
    store.lp_update_session("session-01", patch={"submit_status": "accepted"})
    # A concurrent trade makes the captured shared bundle stale before the
    # new passive-exit intent is authorized.
    store.lp_register_trade_change("session-02")
    session = store.lp_session("session-01")
    assert session is not None
    submit_calls: list[object] = []
    service.exchange.lp_create_limit_order = lambda **kwargs: (
        submit_calls.append(("sign", kwargs)) or SimpleNamespace(post_only=True)
    )
    service.exchange.lp_post_order = lambda signed: (
        submit_calls.append(("post", signed)) or {"order_id": "sell-late"}
    )

    snapshot = {
        "_lp_trade_generation": 0,
        "trades": [],
        "orders": [{
            "order_id": "order-01",
            "token_id": request["token_id"],
            "market_id": request["market_id"],
            "side": "BUY",
            "price": "0.30",
            "status": "LIVE",
            "original_size": "10",
            "size_matched": "0",
        }],
        "book": {
            "received_at": now.isoformat(),
            "asks": [{"price": Decimal("0.31"), "size": Decimal("5")}],
        },
    }

    # The revision was captured with the shared read; do not re-read the
    # latest session row to relabel the stale bundle.
    service._ensure_passive_exit(
        session, snapshot, Decimal("2"), trade_revision=0
    )

    assert submit_calls == []
    assert store.lp_actions("session-01") == []
    rejected = store.lp_session("session-01")
    assert rejected is not None
    assert rejected.get("passive_exit_attempt_state") is None
    assert rejected["facts_error"] == "account_round_invalid"
    assert rejected["fee_status"] == "unknown"
    assert rejected["position_reconciled"] is False
    auto = engine.lp_auto_state()
    assert auto["funds"]["status"] == "unknown"
    assert auto["funds"]["available_usd"] is None
    intent = auto["intents"][0]
    assert intent["session_id"] == "session-01"
    assert intent["financial_status"] == "unknown"


def test_stale_protected_exit_intent_fails_closed_before_submit(tmp_path) -> None:
    now = datetime.now(UTC)
    service, _account, _public = _service(tmp_path)
    store = service.store
    engine = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=service.exchange,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=service,
    )
    engine._breaker_open = False
    request = _request(now, index=1)
    engine._lp_auto_pool()._update(lambda document: document.update(intents={
        "intent-01": {
            "intent_id": "intent-01",
            "session_id": "session-01",
            "order_id": "order-01",
            "state": "active",
            "financial_status": "known",
            "reserved_usd": "3.00",
            "inventory_cost_usd": "0",
            "realized_pnl_usd": "0",
            **{
                key: request[key]
                for key in (
                    "condition_id", "market_id", "token_id", "outcome",
                    "price", "quantity",
                )
            },
            "created_at": now.isoformat(),
        }
    }))
    store.lp_update_session("session-01", patch={
        "submit_status": "accepted",
        "residual_quantity": "2",
        "position_reconciled": True,
    })
    store.lp_register_trade_change("session-02")
    session = store.lp_session("session-01")
    assert session is not None
    submit_calls: list[object] = []
    service.exchange.submit_protected_sell = lambda **kwargs: (
        submit_calls.append(kwargs) or {"order_id": "protected-late"}
    )
    snapshot = {
        "_lp_trade_generation": 0,
        "trades": [],
        "orders": [],
        "book": {
            "received_at": now.isoformat(),
            "bids": [{"price": Decimal("0.29"), "size": Decimal("5")}],
        },
    }
    service._submit_protected_exit(
        session,
        Decimal("2"),
        snapshot,
        trade_revision=0,
    )

    assert submit_calls == []
    assert store.lp_actions("session-01") == []
    rejected = store.lp_session("session-01")
    assert rejected is not None
    assert rejected.get("protected_exit_attempt_state") is None
    assert rejected["facts_error"] == "account_round_invalid"
    assert rejected["fee_status"] == "unknown"
    assert rejected["position_reconciled"] is False
    auto = engine.lp_auto_state()
    assert auto["funds"]["status"] == "unknown"
    intent = auto["intents"][0]
    assert intent["session_id"] == "session-01"
    assert intent["financial_status"] == "unknown"


@pytest.mark.parametrize("state", ["entry_open", "needs_attention"])
@pytest.mark.parametrize("boundary", ["read", "pre_use", "scoring", "apply_lock", "busy_publication", "mutex", "legacy_handler", "fenced_handler"])
def test_account_version_wait_preserves_business_state_and_fault_episode(
    tmp_path, monkeypatch, state, boundary,
) -> None:
    service, _account, _public = _service(tmp_path, NOW)
    service.clock = lambda: NOW
    store = service.store
    prior = {
        "facts_checked_at": NOW,
        "fee_status": "known",
        "position_reconciled": True,
        "reconciliation": "external_snapshot_unknown" if state == "needs_attention" else None,
        "resume_state": "entry_open" if state == "needs_attention" else None,
        "needs_attention_episode": "original-fault" if state == "needs_attention" else None,
        "needs_attention_verified_recovery_episode": None,
        "needs_attention_recovery_due": False,
        "queue_protection": {"data_failures": 3, "state": "monitoring"},
    }
    store.lp_update_session("session-01", state=state, patch=prior)
    session, revision = store.lp_session_with_revision("session-01", trading=True)
    snapshot = dict(service._read_snapshot(_request(NOW, index=1)))
    snapshot["_lp_trade_generation"] = store.lp_trade_generation()
    last_good = session["facts_checked_at"]
    original_check = service._account_bundle_is_current
    checks = 0

    def check_then_invalidate(bundle):
        nonlocal checks
        checks += 1
        # Force invalidation at the final check inside the session mutex.
        if boundary == "mutex" and checks == 4:
            assert service._invalidate_lp_trade_generation(store.lp_trade_generation())
        return original_check(bundle)

    def scoring(_session):
        if boundary == "scoring":
            assert service._invalidate_lp_trade_generation(store.lp_trade_generation())
        return {}

    def acquire():
        assert service._invalidate_lp_trade_generation(store.lp_trade_generation())
        return object()

    monkeypatch.setattr(service, "_account_bundle_is_current", check_then_invalidate)
    monkeypatch.setattr(service, "_read_scoring", scoring)
    # The guard must prevent all strategy application from this stale read.
    monkeypatch.setattr(service, "_reconcile_session", lambda *_args, **_kwargs: pytest.fail("stale strategy apply"))
    if boundary == "read":
        def invalid_read(*_args, **_kwargs):
            raise ValueError("account_round_invalid")
        monkeypatch.setattr(service, "_read_snapshot", invalid_read)
        service.reconcile_facts("session-01", monitor=True)
    elif boundary == "busy_publication":
        def busy_after_trade_change():
            assert service._invalidate_lp_trade_generation(store.lp_trade_generation())
            return None
        service.reconcile_facts(
            "session-01", monitor=True,
            apply_lock=(busy_after_trade_change, lambda _handle: pytest.fail("no lock acquired")),
        )
    elif boundary == "legacy_handler":
        service._handle_snapshot_failure(session, ValueError("account_round_invalid"))
    elif boundary == "fenced_handler":
        service._handle_snapshot_failure_fenced(session, ValueError("account_round_invalid"), revision, None)
    else:
        if boundary == "pre_use":
            assert service._invalidate_lp_trade_generation(store.lp_trade_generation())
        service._apply_tick_snapshot(
            session, snapshot, revision, None,
            apply_lock=(acquire, lambda _handle: None) if boundary == "apply_lock" else None,
        )
    row = store.lp_session("session-01")
    assert row["state"] == state
    assert row["facts_error"] == "account_round_invalid"
    assert row["publication_pending"] is True
    assert "session-01" not in service._pending_facts
    assert row["fee_status"] == "unknown" and row["position_reconciled"] is False
    assert row["facts_checked_at"] == last_good
    for key in ("reconciliation", "resume_state", "needs_attention_episode",
                "needs_attention_verified_recovery_episode", "needs_attention_recovery_due", "queue_protection"):
        assert row.get(key) == session.get(key)
    assert store.lp_actions("session-01") == []


def _historical_cancel_service(tmp_path):
    from tests.test_polymarket_lp import _SDKPublicClient, _request as sdk_request
    now = datetime.now(UTC)

    class Account(_SDKAccountClient):
        live = None
        in_open_list = False
        cancels = []

        def list_open_orders(self, **kwargs):
            return [self.live] if self.live is not None and self.in_open_list else []

        def get_order(self, *, order_id):
            if self.live is not None and self.live.id == order_id:
                return self.live
            return OpenOrder.parse_response(None)

        def list_positions(self, **kwargs):
            return [{'asset': '0x' + '1' * 64, 'conditionId': '0x' + 'c' * 64, 'size': Decimal('100')}]

        def cancel_orders(self, *, order_ids):
            self.cancels.extend(order_ids)
            # Preserve unknown acknowledgements; a still-live order can retry.
            return SimpleNamespace(canceled=(), not_canceled={oid: 'unknown' for oid in order_ids})

    class ClosedPublic(_SDKPublicClient):
        def get_market(self, *, id):
            row = super().get_market(id=id)
            return row.model_copy(update={'state': row.state.model_copy(update={'closed': True})})

    account = Account(now)
    wallet = str(account.open_order.maker_address)
    adapter = PolymarketTradingClient(TradingConfig(wallet, wallet), account,
                                     public_client_factory=lambda: ClosedPublic(now))
    store = PredictionArbitrageStore(tmp_path)
    request = sdk_request(now)
    history = {
        oid: {'order_id': oid, 'side': side, 'token_id': request['token_id'],
              'price': '0.30', 'quantity': '200', 'original_size': '200',
              'status': 'UNKNOWN', 'size_matched': '100' if oid == 'order-1' else '0'}
        for oid, side in [('order-1', 'BUY'), ('augment-old', 'BUY'), ('sell-old', 'SELL')]}
    store.lp_create_session('historical', 'historical', state='entry_open', payload={
        **request, 'quantity': '200', 'group_buy_quantity': '400',
        'entry_order_id': 'order-1', 'augment_order_ids': ['augment-old'],
        'owned_order_ids': list(history), 'order_history': history,
        'submit_status': 'accepted', 'reserved_usd': '120'})
    for oid in history:
        store.lp_upsert_action('historical', f'old-cancel:{oid}', state='unknown', payload={
            'role': 'reconciliation_cancel', 'reason': 'group_fill_collect', 'order_id': oid})
    service = PolymarketLPService(store, adapter)
    engine = PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=adapter,
                                       notifier=SimpleNamespace(), lock_path=tmp_path/'execution.lock', lp=service)
    engine._breaker_open = False
    # Complete the closed-market read before ticks; account data remains fresh each round.
    adapter.lp_snapshot({**request, 'owned_order_ids': list(history)})
    return engine, service, adapter, account, store


def test_historical_null_receipts_do_not_create_cancel_storm_or_release_funds(tmp_path):
    engine, service, adapter, account, store = _historical_cancel_service(tmp_path)
    before = store.lp_actions('historical')
    generation = store.lp_trade_generation()
    try:
        for _ in range(3):
            engine.lp_tick()
        assert account.cancels == []
        assert store.lp_actions('historical') == before
        assert store.lp_trade_generation() == generation
        row = store.lp_session('historical')
        assert row['state'] != 'complete'
        assert row['owned_order_ids'] == ['order-1', 'augment-old', 'sell-old']
        assert row['reserved_usd'] == '120'
        assert row['order_history']['order-1']['status'] == 'UNKNOWN'
        assert row['fee_status'] == 'unknown'
        assert row['orders_terminal'] is False
        assert all(action['state'] == 'unknown' for action in store.lp_actions('historical'))
    finally:
        adapter.close()


@pytest.mark.parametrize('order_id,side', [('order-1', 'BUY'), ('augment-old', 'BUY'), ('sell-old', 'SELL')])
@pytest.mark.parametrize('source', ['receipt', 'open_list'])
def test_later_live_exact_id_overrides_empty_list_and_unknown_cancel_can_retry(tmp_path, monkeypatch, order_id, side, source):
    from open_trader import polymarket_trading
    engine, service, adapter, account, store = _historical_cancel_service(tmp_path)
    advance = [0.0]
    real_monotonic = time.monotonic
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(
        monotonic=lambda: real_monotonic() + advance[0], time=time.time))
    try:
        engine.lp_tick()
        generation = store.lp_trade_generation()
        assert account.cancels == []
        advance[0] = 61  # Expire the real adapter's null-receipt retry fence.
        account.live = account.open_order.model_copy(update={
            'id': order_id, 'side': side, 'status': 'LIVE', 'associate_trades': (),
            'original_size': Decimal('200'),
            'size_matched': Decimal('100') if order_id == 'order-1' else Decimal('0')})
        account.in_open_list = source == 'open_list'
        engine.lp_tick()
        assert account.cancels == [order_id]
        assert store.lp_trade_generation() > generation
        new_actions = [a for a in store.lp_actions('historical') if not a['action_key'].startswith('old-cancel:')]
        assert new_actions and all(a['order_id'] == order_id and a['state'] == 'unknown' for a in new_actions)
        # Unknown acknowledgement cannot suppress a still-positively-live order.
        engine.lp_tick()
        assert account.cancels == [order_id, order_id]
        assert store.lp_session('historical')['reserved_usd'] == '120'
        assert all(a['state'] == 'unknown' for a in store.lp_actions('historical'))
    finally:
        adapter.close()


@pytest.mark.parametrize('invalid', ['missing_generation', 'bool_generation', 'generation_mismatch', 'stale_generation',
                                    'missing_validator', 'missing_wallet', 'wrong_wallet', 'unauthenticated',
                                    'missing_complete', 'incomplete', 'missing_list', 'invalid_list',
                                    'malformed_row', 'missing_id', 'nonstring_id', 'missing_token', 'nonstring_token', 'expired', 'future'])
def test_invalid_account_proof_cannot_skip_historical_cancel_targets(tmp_path, invalid):
    engine, service, adapter, account, store = _historical_cancel_service(tmp_path)
    session, revision = store.lp_session_with_revision('historical', trading=True)
    generation = store.lp_trade_generation()
    token = adapter.lp_account_round_begin(store.lp_trade_generation)
    try:
        snapshot = adapter.lp_snapshot({**session, '_lp_account_round': token})
        snapshot['account'] = dict(snapshot['account'])
        facts = snapshot['account']
        if invalid == 'missing_generation':
            snapshot.pop('_lp_trade_generation')
        elif invalid == 'bool_generation':
            snapshot['_lp_trade_generation'] = bool(generation)
        elif invalid in {'generation_mismatch', 'stale_generation'}:
            snapshot['_lp_trade_generation'] = generation + 1
        elif invalid == 'missing_validator':
            service._facts_validator = None
        elif invalid in {'missing_wallet', 'wrong_wallet'}:
            facts['wallet_address'] = None if invalid == 'missing_wallet' else '0x' + '9' * 40
        elif invalid == 'unauthenticated':
            facts['authenticated'] = False
        elif invalid in {'missing_complete', 'incomplete'}:
            facts['open_orders_complete'] = None if invalid == 'missing_complete' else False
        elif invalid == 'missing_list':
            facts.pop('open_orders')
        elif invalid == 'invalid_list':
            facts['open_orders'] = {}
        elif invalid in {'malformed_row', 'missing_id', 'nonstring_id', 'missing_token', 'nonstring_token'}:
            valid = {'order_id': 'unrelated', 'token_id': session['token_id']}
            facts['open_orders'] = [
                object() if invalid == 'malformed_row' else
                {'token_id': session['token_id']} if invalid == 'missing_id' else
                {**valid, 'order_id': 123} if invalid == 'nonstring_id' else
                {'order_id': 'unrelated'} if invalid == 'missing_token' else
                {**valid, 'token_id': 123}
            ]
        elif invalid in {'expired', 'future'}:
            facts['checked_at'] = datetime.now(UTC) + timedelta(seconds=-61 if invalid == 'expired' else 60)
        passed_generation = generation + 1 if invalid == 'stale_generation' else generation
        before = len(store.lp_actions('historical'))
        service._claim_and_send_owned_cancels_off_lock(session, snapshot, initial_revision=revision,
            trade_generation=passed_generation, reason='group_fill_collect', allow_history=True)
        if invalid == 'stale_generation':
            # Old bundle cannot grant absence and the original Store CAS also rejects it.
            assert account.cancels == []
            assert len(store.lp_actions('historical')) == before
        else:
            assert set(account.cancels) == {'order-1', 'augment-old', 'sell-old'}
            assert len(store.lp_actions('historical')) == before + 3
    finally:
        adapter.lp_account_round_end(token)
        adapter.close()


@pytest.mark.parametrize('lane', ['off_lock', 'owned_sweep'])
def test_old_live_history_is_not_current_positive_cancel_evidence(tmp_path, lane):
    engine, service, adapter, account, store = _historical_cancel_service(tmp_path)
    session = store.lp_session('historical')
    history = {oid: {**row, 'status': 'LIVE'} for oid, row in session['order_history'].items()}
    store.lp_update_session('historical', patch={'order_history': history})
    session, revision = store.lp_session_with_revision('historical', trading=True)
    generation = store.lp_trade_generation()
    before = store.lp_actions('historical')
    token = adapter.lp_account_round_begin(store.lp_trade_generation)
    try:
        snapshot = adapter.lp_snapshot({**session, '_lp_account_round': token})
        if lane == 'off_lock':
            service._claim_and_send_owned_cancels_off_lock(session, snapshot,
                initial_revision=revision, trade_generation=generation,
                reason='group_fill_collect', allow_history=True)
        else:
            service._cancel_owned_orders(session, snapshot=snapshot,
                expected_generation=generation, expected_trade_revision=revision)
        assert account.cancels == []
        assert store.lp_actions('historical') == before
        assert store.lp_trade_generation() == generation
        assert store.lp_session('historical')['order_history'] == history
    finally:
        adapter.lp_account_round_end(token)
        adapter.close()


@pytest.mark.parametrize('terminal_reference', [
    'owned_order_ids', 'entry_order_id', 'passive_exit_order_id',
    'protected_exit_order_id', 'augment_order_ids',
])
def test_reconciliation_reads_only_unfinished_orders_after_repeated_replacement(tmp_path, terminal_reference):
    engine, service, adapter, account, store = _historical_cancel_service(tmp_path)
    session = store.lp_session('historical')
    history = {
        f'old-{index:02d}': {
            'order_id': f'old-{index:02d}', 'side': 'SELL',
            'token_id': session['token_id'], 'status': 'CANCELED',
            'price': '0.30', 'quantity': '100', 'original_size': '100',
            'size_matched': '0',
        }
        for index in range(12)
    }
    for oid, side in [('unknown-buy', 'BUY'), ('unknown-sell', 'SELL')]:
        history[oid] = {'order_id': oid, 'side': side, 'token_id': session['token_id'],
                        'status': 'UNKNOWN', 'quantity': '100', 'size_matched': '0'}
    store.lp_update_session('historical', patch={
        'entry_order_id': 'unknown-buy', 'passive_exit_order_id': 'unknown-sell',
        'augment_order_ids': [], 'owned_order_ids': list(history), 'order_history': history,
    })
    if terminal_reference != 'owned_order_ids':
        if terminal_reference in {'entry_order_id', 'augment_order_ids'}:
            history['old-00']['side'] = 'BUY'
        store.lp_update_session('historical', patch={
            terminal_reference: ['old-00'] if terminal_reference == 'augment_order_ids' else 'old-00',
            'order_history': history,
        })
    reads = []

    def read_order(*, order_id):
        reads.append(order_id)
        return OpenOrder.parse_response(None)

    account.get_order = read_order
    try:
        service.reconcile_facts('historical')
        assert reads == ['unknown-buy', 'unknown-sell']
        saved = store.lp_session('historical')
        assert len(saved['owned_order_ids']) == 14
        assert all(saved['order_history'][f'old-{index:02d}']['status'] == 'CANCELED'
                   for index in range(12))
        assert saved['orders_terminal'] is False
        assert Decimal(saved['residual_quantity']) == Decimal('100')
        # Reopening both consumers proves this is durable fact reuse, not an
        # in-memory cache whose benefit disappears after a service restart.
        adapter.close()
        adapter = PolymarketTradingClient(adapter.config, account,
            public_client_factory=adapter._public_client_factory)
        service = PolymarketLPService(PredictionArbitrageStore(tmp_path), adapter)
        reads.clear()
        service.reconcile_facts('historical')
        assert reads == ['unknown-buy', 'unknown-sell']
        assert len(store.lp_session('historical')['owned_order_ids']) == 14
    finally:
        adapter.close()


def test_terminal_receipt_reuse_keeps_exact_order_trade_validation(tmp_path):
    engine, service, adapter, account, store = _historical_cancel_service(tmp_path)
    history = store.lp_session('historical')['order_history']
    history['order-1']['status'] = 'CANCELED'
    store.lp_update_session('historical', patch={'order_history': history})
    # The exact owned maker ID is the remaining relevance evidence when a
    # historical response has a conflicting market and no top-level token.
    trade = _history_trade(market='0x' + '9' * 64, asset_id='')
    account.list_account_trades = lambda **kwargs: [trade]
    try:
        service.reconcile_facts('historical')
        assert store.lp_session('historical')['facts_error'] == 'external_snapshot_unknown'
    finally:
        adapter.close()


def test_current_open_order_overrides_reused_terminal_receipt(tmp_path):
    engine, service, adapter, account, store = _historical_cancel_service(tmp_path)
    history = store.lp_session('historical')['order_history']
    history['order-1']['status'] = 'CANCELED'
    store.lp_update_session('historical', patch={'order_history': history})
    account.live = account.open_order.model_copy(update={
        'id': 'order-1', 'side': 'BUY', 'status': 'LIVE', 'original_size': Decimal('200'),
        'size_matched': Decimal('100'),
    })
    account.in_open_list = True
    try:
        service.reconcile_facts('historical')
        saved = store.lp_session('historical')
        assert saved['order_history']['order-1']['status'] == 'LIVE'
        assert saved['orders_terminal'] is False
    finally:
        adapter.close()
