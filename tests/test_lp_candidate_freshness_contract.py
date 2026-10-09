"""Issue #310 candidate freshness and per-market recovery contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from open_trader import polymarket_lp, polymarket_trading
from tests.test_lp_read_diagnostics import wait_read_logs

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_account_reservation_reconciliation import runtime
from tests.test_lp_auto_refill_contract import RefillPublic, _refill_identity, prepare
from tests.test_polymarket_lp import _LPYieldBooksExchange


class _RequalificationPublic(RefillPublic):
    def __init__(self, clock):
        super().__init__(clock)
        self.metadata_requests = []
        self.failed_metadata = set()
        self.metadata_timeout = False
        self.failed_reward_conditions = set()

    def list_market_rewards(self, *, condition_id, sponsored):
        if condition_id in self.failed_reward_conditions:
            raise TimeoutError("Authorization=secret raw reward message")
        return super().list_market_rewards(condition_id=condition_id, sponsored=sponsored)

    def list_markets(self, *, condition_ids=(), **kwargs):
        if self.metadata_timeout:
            raise TimeoutError("Authorization=secret raw metadata message")
        return [self.get_market(id=_refill_identity(i)[0]) for i in (1, 2)
                if _refill_identity(i)[1] in condition_ids
                and _refill_identity(i)[0] not in self.failed_metadata]

    def get_market(self, *, id):
        self.metadata_requests.append(id)
        if id in self.failed_metadata:
            raise TimeoutError("secret raw metadata message")
        return super().get_market(id=id)


class _RewardFailureExchange(_LPYieldBooksExchange):
    """Two-market source fake with a controllable per-market reward gap."""

    def __init__(self, now: datetime) -> None:
        super().__init__(now, {"A": Decimal("100"), "B": Decimal("90")})
        self.failed_rewards: set[str] = set()
        self.source_reads = dict.fromkeys(("account", "metadata", "reward", "books"), 0)

    def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
        self.source_reads["reward"] += 1
        catalog = super().lp_reward_catalog(
            condition_ids=condition_ids, stop_event=stop_event
        )
        if self.failed_rewards:
            catalog["markets"] = tuple(
                row
                for row in catalog["markets"]
                if row.get("condition_id") not in self.failed_rewards
            )
        return catalog

    def lp_account_snapshot(self):
        self.source_reads["account"] += 1
        return super().lp_account_snapshot()

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        self.source_reads["metadata"] += 1
        return super().lp_market_metadata(condition_ids, stop_event=stop_event)

    def lp_order_books(self, token_ids, *, stop_event=None):
        self.source_reads["books"] += 1
        return super().lp_order_books(token_ids, stop_event=stop_event)


def test_one_market_reward_failure_does_not_age_healthy_candidate(tmp_path) -> None:
    now = datetime(2026, 10, 9, 8, tzinfo=UTC)
    current = {"now": now}
    exchange = _RewardFailureExchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"
    assert lp.refresh_competition_cache()["state"] == "known"
    initial = lp.refresh_candidates(force=True)
    assert {row["condition_id"] for row in initial["candidates"]} == {
        "condition-A",
        "condition-B",
    }
    initial_updated = {
        row["condition_id"]: row["updated_at"] for row in initial["candidates"]
    }

    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    exchange.failed_rewards = {"condition-B"}
    failed = lp.refresh_candidate_recommendations()
    by_condition = {row["condition_id"]: row for row in failed["candidates"]}

    assert by_condition["condition-A"]["updated_at"] != initial_updated["condition-A"]
    assert by_condition["condition-A"]["refresh_failed"] is False
    assert by_condition["condition-B"]["updated_at"] == initial_updated["condition-B"]
    assert by_condition["condition-B"]["refresh_failed"] is True
    assert failed["maintenance_consecutive_failures"] == 0

    assert by_condition["condition-A"]["selected_direction"]["eligible"] is True
    # A renews on its own 30-second lead while B's 60-second retry is pending.
    current["now"] = now + timedelta(seconds=95)
    exchange.now = current["now"]
    healthy = lp.refresh_candidate_recommendations()
    healthy_rows = {row["condition_id"]: row for row in healthy["candidates"]}
    assert healthy_rows["condition-A"]["updated_at"] != by_condition["condition-A"]["updated_at"]
    assert healthy_rows["condition-B"]["updated_at"] == initial_updated["condition-B"]

    current["now"] = now + timedelta(seconds=125)
    exchange.now = current["now"]
    exchange.failed_rewards = set()
    recovered = lp.refresh_candidate_recommendations()
    recovered_rows = {row["condition_id"]: row for row in recovered["candidates"]}

    assert recovered_rows["condition-B"]["updated_at"] != initial_updated["condition-B"]
    assert recovered_rows["condition-B"]["refresh_failed"] is False


def test_stale_best_candidate_refreshes_before_exclusion(runtime) -> None:
    public = _RequalificationPublic(runtime.clock)
    public.rates = {1: "240", 2: "24"}
    _store, _adapter, account, lp, execution, _public = prepare(
        runtime, count=2, target=1, public=public
    )
    execution.lp_auto_set_desired_running(False)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1, "buy_price_level": 2})
    execution.lp_auto_set_desired_running(True)
    market_id, condition_id, token_id = _refill_identity(1)
    stale_at = lp._now() - timedelta(seconds=65)
    with lp._candidate_state_lock:
        for direction in lp._candidate_qualification_facts[condition_id]["directions"]:
            direction["market"]["metadata_checked_at"] = stale_at

    public.metadata_requests.clear()
    state = execution.lp_auto_run_once(round_id="stale-best-refresh")

    assert [post.token_id for post in account.posts] == [token_id], state["last_round"]
    assert market_id in public.metadata_requests
    assert len(public.metadata_requests) <= 8  # two-market bounded refresh + presend
    assert Decimal(account.posts[0].maker_amount) / account.posts[0].taker_amount == Decimal(".39")
    assert lp._candidate_qualification_facts[condition_id]["directions"][0]["market"]["metadata_checked_at"] > stale_at


def test_failed_best_candidate_allows_fallback_and_later_recovery(runtime) -> None:
    public = _RequalificationPublic(runtime.clock)
    public.rates = {1: "240", 2: "24"}
    _store, _adapter, account, lp, execution, _public = prepare(
        runtime, count=2, target=1, public=public
    )
    execution.lp_auto_set_desired_running(False)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1, "buy_price_level": 2})
    execution.lp_auto_set_desired_running(True)
    market_a, condition_a, token_a = _refill_identity(1)
    _market_b, _condition_b, token_b = _refill_identity(2)
    old_updated = lp._candidate_pool[condition_a]["updated_at"]
    with lp._candidate_state_lock:
        for direction in lp._candidate_qualification_facts[condition_a]["directions"]:
            direction["market"]["metadata_checked_at"] = lp._now() - timedelta(seconds=65)
    public.failed_metadata = {market_a}

    failed = execution.lp_auto_run_once(round_id="best-failed-fallback")
    assert [post.token_id for post in account.posts] == [token_b], failed["last_round"]
    assert lp._candidate_pool[condition_a]["updated_at"] == old_updated
    assert lp._candidate_pool[condition_a]["refresh_failed"] is True
    assert account.cancels == []

    # A failed newcomer cannot remove the healthy at-level B incumbent.
    runtime.clock[0] += timedelta(seconds=30)
    held = execution.lp_auto_run_once(round_id="best-waiting-incumbent")
    assert account.cancels == []
    assert [post.token_id for post in account.posts] == [token_b], held["last_round"]
    assert lp._candidate_pool[condition_a]["updated_at"] == old_updated

    public.failed_metadata.clear()
    runtime.clock[0] += timedelta(seconds=35)
    recovered = execution.lp_auto_run_once(round_id="best-natural-recovery")
    assert lp._candidate_pool[condition_a]["updated_at"] != old_updated
    assert lp._candidate_pool[condition_a]["refresh_failed"] is False
    assert recovered["last_round"]["candidates"][0]["token_id"] == token_a, recovered["last_round"]


@pytest.mark.parametrize("source", ["reward", "metadata", "reward_timeout"])
def test_source_failures_and_recovery_have_correlated_safe_diagnostics(
    tmp_path, runtime, monkeypatch, caplog, source
) -> None:
    wait_read_logs()
    caplog.set_level(logging.INFO)
    diagnostic_source = "reward" if source.startswith("reward") else "metadata"
    if source == "reward":
        initial_time = datetime(2026, 10, 9, 8, tzinfo=UTC)
        current = [initial_time]
        exchange = _RewardFailureExchange(initial_time)
        lp = PolymarketLPService(PredictionArbitrageStore(tmp_path / "reward"), exchange,
                                 clock=lambda: current[0])
        assert lp.refresh_price_history()["state"] == "known"
        assert lp.refresh_competition_cache()["state"] == "known"
        lp.refresh_candidates(force=True)
        failed_condition = "condition-B"
        exchange.failed_rewards = {failed_condition}
    else:
        public = _RequalificationPublic(runtime.clock)
        _store, _adapter, _account, lp, _execution, _public = prepare(
            runtime, count=2, target=1, public=public
        )
        current = runtime.clock
        initial_time = current[0]
        failed_condition = _refill_identity(2)[1]
        public.metadata_timeout = source == "metadata"
        if source == "reward_timeout":
            public.failed_reward_conditions = {failed_condition}

    wait_read_logs()
    caplog.clear()
    current[0] = initial_time + timedelta(seconds=65)
    if source == "reward":
        exchange.now = current[0]
    failed = lp.refresh_candidate_recommendations()
    attempt_finished = current[0]
    # The consumer must receive facts from the attempt, even if time changes.
    current[0] += timedelta(seconds=1)
    wait_read_logs()
    records = [r for r in caplog.records if r.msg == "lp_candidate_maintenance event=%s facts=%s"]
    assert records, "missing correlated maintenance source evidence"
    events = [(r.args[0], r.args[1]) for r in records]
    begin = next(f for event, f in events if event == "begin")
    end = next(f for event, f in events if event == "end")
    sources = [f for event, f in events if event == "source"]
    assert {f["source"] for f in sources} == {"account", "metadata", "fees", "reward", "books"}
    assert all(f["batch_id"] == begin["batch_id"] for _, f in events)
    source_event = next(f for f in sources if f["source"] == diagnostic_source)
    failures = [r for r in source_event["markets"] if r["outcome"] == "failed"]
    assert len(failures) == (2 if source == "metadata" else 1)
    failed_market = failures[-1]["market"]
    assert failed_market and failed_market != failed_condition
    assert all(r["age_seconds"] >= 65 for r in failures)
    expected_cause = {"reward": "unknown", "metadata": "market_read_TimeoutError", "reward_timeout": "TimeoutError"}[source]
    assert all(r["cause"] == expected_cause for r in failures)
    retry = next(r for r in end["markets"] if r["market"] == failed_market)
    assert retry["outcome"] == "failed"
    assert retry["backoff_seconds"] == 60
    assert retry["next_attempt_at"] == (attempt_finished + timedelta(seconds=60)).isoformat()
    assert end["finished_at"] == attempt_finished.isoformat()
    assert all(failed_condition not in str(f) for _, f in events)
    assert "Authorization=secret" not in caplog.text
    assert all(r.exc_info is None for r in records)

    if source == "reward":
        exchange.failed_rewards.clear()
    else:
        public.metadata_timeout = False
        public.failed_reward_conditions.clear()
    current[0] = attempt_finished + timedelta(seconds=60)
    if source == "reward":
        exchange.now = current[0]
    recovered = lp.refresh_candidate_recommendations()
    wait_read_logs()
    ends = [r.args[1] for r in caplog.records if r.msg == "lp_candidate_maintenance event=%s facts=%s" and r.args[0] == "end"]
    recovery = ends[-1]
    assert recovery["batch_id"] != begin["batch_id"]
    recovery_market = next(r for r in recovery["markets"] if r["market"] == failed_market)
    assert recovery_market["outcome"] == "refreshed"
    assert recovery_market["backoff_seconds"] == 0
    assert recovery_market["next_attempt_at"] is None
    assert any(r["condition_id"] == failed_condition and r["refresh_failed"] is False for r in recovered["candidates"])

    if source == "reward":
        # Block only the real diagnostic output. The bounded consumer must
        # not hold maintenance or add exchange reads when its queue overflows.
        entered, release = Event(), Event()
        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5), "independent diagnostic watchdog"
            raise RuntimeError("secret sink failure")
        with monkeypatch.context() as patch:
            patch.setattr(polymarket_lp.logger, "info", blocked)
            try:
                for index in range(8):
                    current[0] += timedelta(seconds=30)
                    exchange.now = current[0]
                    before = dict(exchange.source_reads)
                    with ThreadPoolExecutor(1) as caller:
                        result = caller.submit(lp.refresh_candidate_recommendations).result(timeout=2)
                    assert all(not row["refresh_failed"] for row in result["candidates"])
                    assert exchange.source_reads == {k: v + 1 for k, v in before.items()}
                    if index == 0:
                        assert entered.wait(2)
                assert polymarket_trading._lp_read_log_queue.qsize() <= 32
                assert polymarket_trading._lp_read_log_dropped > 0
            finally:
                release.set()
                wait_read_logs()
        assert "secret sink failure" not in caplog.text
        assert "output_errors=" in caplog.text


class _ElevenMarketPublic(RefillPublic):
    """External venue with a different stored-level and selected-level winner."""

    def __init__(self, clock):
        super().__init__(clock)
        self.metadata_batches = []
        self.book_requests = []

    def list_markets(self, *, condition_ids=(), **kwargs):
        self.metadata_batches.append(tuple(condition_ids))
        return [self.get_market(id=_refill_identity(i)[0]) for i in range(1, 12)
                if _refill_identity(i)[1] in condition_ids]

    def list_market_rewards(self, *, condition_id, sponsored):
        index = next(i for i in range(1, 12) if _refill_identity(i)[1] == condition_id)
        template = super().list_market_rewards(
            condition_id=_refill_identity(1)[1], sponsored=sponsored
        )[0]
        config = template.rewards_config[0].model_copy(update={
            "id": index, "rate_per_day": Decimal("24" if index == 11 else "32")})
        return (template.model_copy(update={"condition_id": condition_id,
                                            "rewards_config": (config,)}),)

    def list_current_rewards(self, *, sponsored):
        return tuple(self.list_market_rewards(condition_id=_refill_identity(i)[1], sponsored=sponsored)[0]
                     for i in range(1, 12)) if not sponsored else ()

    def get_order_book(self, *, token_id):
        from polymarket.models.clob.order_book import OrderBookLevel
        index = next(i for i in range(1, 12)
                     if token_id in {_refill_identity(i)[2], f"0x{i + 300:064x}"})
        template = super().get_order_book(token_id=_refill_identity(1)[2])
        self.book_requests.append(token_id)
        return template.model_copy(update={
            "token_id": token_id, "market": _refill_identity(index)[1],
            "condition_id": _refill_identity(index)[1], "timestamp": self.clock[0],
            "bids": (OrderBookLevel(price=Decimal(".40"), size=Decimal("1000")),
                     OrderBookLevel(price=Decimal(".39" if index == 11 else ".37"), size=Decimal("1000"))),
            "asks": (OrderBookLevel(price=Decimal(".42"), size=Decimal("1000")),),
        })


def test_stale_best_at_selected_level_outside_display_head_gets_bounded_refresh(runtime):
    import io
    import json
    from tests.test_lp_order_registration_contract import _open_order

    public = _ElevenMarketPublic(runtime.clock)
    _store, adapter, account, lp, execution, _ = prepare(
        runtime, count=0, target=1, public=public
    )
    execution.lp_auto_set_desired_running(False)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1, "buy_price_level": 2})

    def public_response(request, **kwargs):
        if request.data:
            body = json.loads(request.data)
            payload = {"history": {token: [{"t": t, "p": .4}
                for t in range(body["start_ts"], body["end_ts"] + 1, 60)] for token in body["markets"]}}
        else:
            payload = {"data": [{"condition_id": _refill_identity(i)[1], "market_competitiveness": 10}
                                 for i in range(1, 12)], "next_cursor": "LTE="}
        return io.BytesIO(json.dumps(payload).encode())

    adapter._urlopen_fn = public_response
    assert lp.refresh_price_history()["state"] == "known"
    assert lp.refresh_competition_cache()["state"] == "known"
    # The public exploration seam publishes ten markets, then the eleventh.
    lp.refresh_candidates(force=True)
    scanned = lp.refresh_candidates(force=True)
    assert scanned["candidate_valid_count"] == 11
    _market_b, condition_b, token_b = _refill_identity(11)
    displayed = lp.candidate_snapshot()["candidates"]
    assert len(displayed) == 10
    assert condition_b not in {row["condition_id"] for row in displayed}
    # Independent hand-worked buy1 values supplied by Main, rounded only here.
    assert Decimal(displayed[0]["estimated_yield_raw"]).quantize(Decimal(".000001")) == Decimal(".096216")
    assert Decimal(lp._candidate_pool[condition_b]["estimated_yield_raw"]).quantize(Decimal(".000001")) == Decimal(".065615")

    stale_at = lp._now() - timedelta(seconds=65)
    with lp._candidate_state_lock:
        for direction in lp._candidate_qualification_facts[condition_b]["directions"]:
            direction["market"]["metadata_checked_at"] = stale_at
    public.metadata_batches.clear()
    public.book_requests.clear()

    # The account HTTP boundary accepts an eleventh-market order. Production
    # signing, admission, source validation and durable order handling remain real.
    def post(signed):
        runtime.clock[0] += timedelta(seconds=1)
        account.posts.append(signed)
        index = next(i for i in range(1, 12)
                     if signed.token_id in {_refill_identity(i)[2], f"0x{i + 300:064x}"})
        condition = _refill_identity(index)[1]
        price = Decimal(signed.maker_amount) / signed.taker_amount
        oid = f"eleven-refill-{index}"
        account.orders += (_open_order(oid, "BUY", price=str(price), original="20",
            token_id=signed.token_id, outcome="YES").model_copy(update={"market": condition, "condition_id": condition}),)
        return {"order_id": oid, "status": "LIVE", "accepted": True, "size_matched": "0"}

    account.post_order = post
    execution.lp_auto_set_desired_running(True)
    state = execution.lp_auto_run_once(round_id="eleventh-stale-selected-level")

    assert [post.token_id for post in account.posts] == [token_b], state["last_round"]
    assert public.metadata_batches and condition_b in public.metadata_batches[0]
    assert all(len(batch) <= 10 for batch in public.metadata_batches)
    assert sum(map(len, public.metadata_batches)) <= 10
    assert set().union(*map(set, public.metadata_batches)) == {condition_b}
    assert lp._candidate_qualification_facts[condition_b]["directions"][0]["market"]["metadata_checked_at"] > stale_at
    assert Decimal(account.posts[0].maker_amount) / account.posts[0].taker_amount == Decimal(".39")
    # Main's buy2 worked value, recomputed from the freshly read source facts.
    assert Decimal(state["last_round"]["candidates"][0]["minimum_order_estimate"]["yield_pct_per_hour"]).quantize(Decimal(".000001")) == Decimal(".053232")
