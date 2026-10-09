"""Issue #309: strategy-level yield and price stay aligned across public paths."""

import io
import json
from decimal import Decimal

import pytest

from polymarket.models.clob.order_book import OrderBookLevel

from tests.test_lp_account_reservation_reconciliation import _advance, _refill_identity, runtime
from tests.test_lp_auto_refill_contract import RefillPublic, prepare


class StrategyPublic(RefillPublic):
    """Two-market books with distinct second levels for the worked example."""

    def __init__(self, clock):
        super().__init__(clock)
        self.second_prices = {1: Decimal(".37"), 2: Decimal(".39")}
        self.books_unavailable = False
        self.second_missing = False

    def list_current_rewards(self, *, sponsored):
        if sponsored:
            return ()
        return tuple(
            self.list_market_rewards(
                condition_id=_refill_identity(index)[1], sponsored=sponsored
            )[0]
            for index in self.second_prices
        )

    def list_markets(self, **kwargs):
        del kwargs
        return [self.get_market(id=f"market-{index}") for index in self.second_prices]

    def get_order_book(self, *, token_id):
        if self.books_unavailable:
            raise TimeoutError("offline fixture book unavailable")
        book = super().get_order_book(token_id=token_id)
        index = next(
            index
            for index in self.second_prices
            if token_id in {_refill_identity(index)[2], f"0x{index + 300:064x}"}
        )
        best = Decimal(".40")
        return book.model_copy(
            update={
                "bids": (
                    OrderBookLevel(price=best, size=Decimal("1000")),
                    OrderBookLevel(
                        price=self.second_prices[index],
                        size=Decimal("0" if self.second_missing else "1000"),
                    ),
                ),
                "asks": (OrderBookLevel(price=Decimal(".42"), size=Decimal("1000")),),
            }
        )


def _configure_and_refresh(runtime, level, *, public=None):
    if public is None:
        public = StrategyPublic(runtime.clock)
        public.rates = {1: "32", 2: "24"}
    count = len(public.second_prices)
    store, adapter, account, lp, execution, _ = prepare(
        runtime, budget="100", target=count, count=count, public=public
    )
    execution.lp_auto_set_desired_running(False)
    execution.lp_auto_configure(
        {"budget_usd": "100", "target_buy_count": count, "buy_price_level": level}
    )

    def public_response(request, **kwargs):
        del kwargs
        if request.data:
            body = json.loads(request.data)
            payload = {
                "history": {
                    token: [
                        {"t": stamp, "p": 0.4}
                        for stamp in range(body["start_ts"], body["end_ts"] + 1, 60)
                    ]
                    for token in body["markets"]
                }
            }
        else:
            payload = {
                "data": [
                    {
                        "condition_id": _refill_identity(index)[1],
                        "market_competitiveness": 10,
                    }
                    for index in public.second_prices
                ],
                "next_cursor": "LTE=",
            }
        return io.BytesIO(json.dumps(payload).encode())

    adapter._urlopen_fn = public_response
    assert lp.refresh_price_history()["state"] == "known"
    assert lp.refresh_competition_cache()["state"] == "known"
    return store, adapter, account, lp, execution


def _assert_strategy_rows(snapshot, *, level):
    # Main's independently worked literals; no production scoring helpers.
    expected = {
        1: [("market-1", ".40", "8", ".096216"),
            ("market-2", ".40", "8", ".065615")],
        2: [("market-2", ".39", "7.8", ".053232"),
            ("market-1", ".37", "7.4", ".046378")],
    }[level]
    rows = snapshot["candidates"]
    assert [row["market_id"] for row in rows] == [item[0] for item in expected]
    for row, (_, price, capital, yield_pct) in zip(rows, expected):
        assert Decimal(row["realtime_price"]) == Decimal(price)
        assert Decimal(row["realtime_capital"]) == Decimal(capital)
        assert Decimal(row["estimated_target_capital_usd"]) == Decimal(capital)
        assert Decimal(row["estimated_yield_pct_per_hour"]) == Decimal(yield_pct)


def test_strategy_level_changes_display_price_yield_and_rank(runtime):
    """Scan and maintenance each follow the paused strategy in both directions."""
    _, _, _, lp, execution = _configure_and_refresh(runtime, 1)
    _assert_strategy_rows(lp.refresh_candidates(force=True), level=1)
    _advance(runtime, seconds=30)
    _assert_strategy_rows(lp.refresh_candidate_recommendations(), level=1)
    _assert_strategy_rows(lp.candidate_snapshot(), level=1)

    execution.lp_auto_configure(
        {"budget_usd": "100", "target_buy_count": 2, "buy_price_level": 2}
    )
    _assert_strategy_rows(lp.refresh_candidate_recommendations(), level=2)
    _assert_strategy_rows(lp.refresh_candidates(force=True), level=2)
    _assert_strategy_rows(lp.candidate_snapshot(), level=2)

    execution.lp_auto_configure(
        {"budget_usd": "100", "target_buy_count": 2, "buy_price_level": 1}
    )
    _assert_strategy_rows(lp.refresh_candidates(force=True), level=1)
    _advance(runtime, seconds=30)
    _assert_strategy_rows(lp.refresh_candidate_recommendations(), level=1)
    _assert_strategy_rows(lp.candidate_snapshot(), level=1)


def test_auto_and_dashboard_share_selected_level_estimate(runtime):
    """A level-two dashboard estimate agrees with the actual automatic BUY."""
    public = StrategyPublic(runtime.clock)
    public.second_prices = {1: Decimal(".39")}
    public.rates = {1: "48"}
    _, _, account, lp, execution = _configure_and_refresh(runtime, 2, public=public)
    for snapshot in (lp.refresh_candidates(force=True),
                     lp.refresh_candidate_recommendations()):
        row = snapshot["candidates"][0]
        assert Decimal(row["realtime_price"]) == Decimal(".39")
        assert Decimal(row["estimated_target_quantity"]) == 20
        assert Decimal(row["estimated_target_capital_usd"]) == Decimal("7.8")
        assert Decimal(row["estimated_yield_pct_per_hour"]) == Decimal(".106463")
    dashboard = execution.refresh_lp_dashboard_snapshot()
    row = dashboard["candidates"][0]
    assert Decimal(row["realtime_price"]) == Decimal(".39")
    assert Decimal(row["estimated_target_capital_usd"]) == Decimal("7.8")
    assert Decimal(row["estimated_yield_pct_per_hour"]) == Decimal(".106463")
    assert execution.lp_dashboard()["candidates"] == dashboard["candidates"]
    execution.lp_auto_set_desired_running(True)
    state = execution.lp_auto_run_once(round_id="level-two-estimate")
    assert len(account.posts) == 1, state["last_round"]
    order = account.posts[0]
    assert Decimal(order.maker_amount) / order.taker_amount == Decimal(".39")
    assert Decimal(order.taker_amount) / 1000000 == 20
    estimate = state["last_round"]["candidates"][0]["minimum_order_estimate"]
    assert Decimal(estimate["yield_pct_per_hour"]).quantize(Decimal(".000001")) == Decimal(".106463")
    assert Decimal(estimate["capital_usd"]) == Decimal("7.8")
    # Strategy reward estimates never become paid/reusable account rewards.
    assert Decimal(state["funds"]["verified_rewards_usd"]) == 0


def _assert_no_old_level_estimate(snapshot):
    for field in ("candidates", "recommendations", "selected_results"):
        for row in snapshot.get(field, []):
            if row.get("estimate_state") == "known":
                assert Decimal(row["realtime_price"]) == Decimal(".39")
                assert Decimal(row["estimated_target_capital_usd"]) == Decimal("7.8")
                assert Decimal(row["estimated_yield_pct_per_hour"]) == Decimal(".106463")
            else:
                assert row.get("realtime_price") is None
                assert row.get("estimated_yield_pct_per_hour") is None
                assert row.get("estimated_target_capital_usd") is None
            selected = row.get("selected_direction")
            if selected and selected.get("guidance"):
                assert Decimal(selected["guidance"]["price"]) == Decimal(".39")
            for direction in (row.get("directions") or {}).values():
                if direction.get("guidance"):
                    assert Decimal(direction["guidance"]["price"]) == Decimal(".39")


@pytest.mark.parametrize("failure", ["unavailable", "missing-second"])
def test_level_change_never_relabels_old_or_missing_level_estimate(runtime, failure):
    """Unavailable quotes and restart cannot turn saved buy-one into buy-two."""
    public = StrategyPublic(runtime.clock)
    public.second_prices = {1: Decimal(".39")}
    public.rates = {1: "48"}
    store, adapter, account, lp, execution = _configure_and_refresh(runtime, 1, public=public)
    initial = lp.refresh_candidates(force=True)
    assert Decimal(initial["candidates"][0]["estimated_yield_pct_per_hour"]) == Decimal(".131229")
    dashboard_before = execution.refresh_lp_dashboard_snapshot()
    assert dashboard_before["candidates"]
    execution.lp_auto_configure(
        {"budget_usd": "100", "target_buy_count": 1, "buy_price_level": 2}
    )
    _assert_no_old_level_estimate(lp.candidate_snapshot())
    cached_dashboard = execution.lp_dashboard()
    _assert_no_old_level_estimate(cached_dashboard)
    for field in ("orders", "market_rewards", "reward_shares", "lp_orders_today"):
        assert cached_dashboard[field] == dashboard_before[field]
    public.books_unavailable = failure == "unavailable"
    public.second_missing = failure == "missing-second"
    _advance(runtime, seconds=61)
    _assert_no_old_level_estimate(lp.refresh_candidate_recommendations())
    _assert_no_old_level_estimate(lp.refresh_candidates(force=True))
    _assert_no_old_level_estimate(execution.refresh_lp_dashboard_snapshot())
    assert account.posts == account.cancels == []

    # Reproduce a prior release's durable minimum-order row without a level
    # marker. Such rows were always buy-one; never relabel them buy-two.
    saved = store.lp_screening_snapshot()
    assert saved["pool"]
    for row in saved["pool"].values():
        row.pop("estimate_buy_price_level", None)
    store.lp_save_screening_snapshot(saved)
    response = adapter._urlopen_fn
    adapter.close()
    _, restarted_adapter, restarted_account, restarted_lp, restarted = runtime(public_client=public)
    restarted_adapter._urlopen_fn = response
    assert restarted.lp_auto_state()["buy_price_level"] == 2
    _assert_no_old_level_estimate(restarted_lp.candidate_snapshot())
    _assert_no_old_level_estimate(restarted.refresh_lp_dashboard_snapshot())
    assert restarted_account.posts == restarted_account.cancels == []

    public.books_unavailable = public.second_missing = False
    _advance(runtime, seconds=61)
    # Renew the adapter receipt explicitly: business time advanced while its
    # shared-read TTL uses a separate real monotonic clock.
    restarted_adapter.lp_account_snapshot_shared(max_age_seconds=0)
    assert restarted_lp.refresh_price_history()["state"] == "known"
    assert restarted_lp.refresh_competition_cache()["state"] == "known"
    recovered = restarted_lp.refresh_candidates(force=True)
    assert len(recovered["candidates"]) == 1
    assert recovered["candidates"][0]["estimate_state"] == "known", recovered["funnel"]
    _assert_no_old_level_estimate(recovered)
    _assert_no_old_level_estimate(restarted.refresh_lp_dashboard_snapshot())
