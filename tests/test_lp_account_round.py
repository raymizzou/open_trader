from __future__ import annotations

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
        self.release_books = threading.Event()

    def get_order_book(self, *, token_id: str) -> dict[str, object]:
        with self.lock:
            self.active_books += 1
            self.max_active_books = max(self.max_active_books, self.active_books)
            self.books_entered.append(token_id)
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


def test_round_lifecycle_without_consumer_makes_no_network_calls() -> None:
    adapter, account, _public = _round_adapter()
    token = adapter.lp_account_round_begin()
    adapter.lp_account_round_end(token)

    assert all(value == 0 for value in account.calls.values())


@pytest.mark.parametrize("finish", ["end", "invalidate"])
def test_late_snapshot_rejects_end_or_invalidation(finish: str) -> None:
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
        waiter_a = threading.Thread(target=reader)
        waiter_b = threading.Thread(target=reader)
        waiter_a.start()
        waiter_b.start()
        waiters = [waiter_a, waiter_b]
        time.sleep(0.05)

        started = time.monotonic()
        if finish == "end":
            adapter.lp_account_round_end(token)
        else:
            adapter.lp_account_round_invalidate(token)
        assert time.monotonic() - started < 0.1
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
    validator_entered = threading.Event()
    validator_release = threading.Event()

    def validator(session, snapshot):
        if str(session.get("session_id")) == "session-02":
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


def test_trade_after_scoring_blocks_old_bundle_apply(tmp_path) -> None:
    now = datetime.now(UTC)
    account = _ScoringGatedAccountClient(now)
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


def test_exact_order_newer_evidence_advances_shared_fence(tmp_path) -> None:
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
            assert validator_entered.wait(timeout=3)
            return receipt(order_id, Decimal("100"))
        return receipt(order_id, Decimal("0"))

    account.get_order = get_order
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
        validator_release.set()
        worker.join(timeout=2)


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


def test_auto_bounded_round_waits_for_launched_jobs(tmp_path) -> None:
    now = datetime.now(UTC)
    account = _TwoSessionAccountClient(now)
    public = _BlockingTwoMarketPublicClient(now)
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
    lp = PolymarketLPService(store, adapter)
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
    started = time.monotonic()

    def run():
        try:
            result_container.append(engine.lp_auto_run_once())
        except BaseException as exc:
            run_errors.append(exc)
            raise

    worker = threading.Thread(target=run)
    worker.start()
    try:
        worker.join(timeout=1.2)
        assert not worker.is_alive()
        assert time.monotonic() - started < 2.0
        assert public.max_active_books == 2
        assert len(public.books_entered) == 2

        # Public reads retain their own jobs; financial publication and its
        # shared-account round finish without waiting for either book.
        assert all(future.done() for future in pool._reconcile_jobs.values())
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
    assert all(future.done() for future in pool._reconcile_jobs.values())
    intents = {intent["intent_id"]: intent for intent in state["intents"]}
    for intent_id in ("intent-01", "intent-02"):
        intent = intents[intent_id]
        assert intent["financial_status"] == "known"
        session = store.lp_session(intent["session_id"])
        assert session is not None
        assert session["facts_error"] is None


def test_provider_failure_completes_round_waiters() -> None:
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
        waiter = threading.Thread(target=reader)
        waiter.start()
        time.sleep(0.05)
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
