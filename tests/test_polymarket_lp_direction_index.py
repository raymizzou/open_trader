"""One direction source through trial projection and both production callers."""
from collections.abc import ValuesView
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import gc
import hashlib
import json
from threading import Event
import weakref

import pytest

from open_trader import polymarket_lp_views as views
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_scratch import LPReadScratch
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_lp_candidate_exclusions import ExclusionExchange
from test_polymarket_lp_views import NOW, _trial, _trial_competition, _trial_direction


@pytest.mark.parametrize("failure", [None, RuntimeError, CancelledError])
def test_queue_build_never_copies_trial_details_and_releases_source(
    tmp_path, monkeypatch, failure
):
    exchange = ExclusionExchange(NOW, {f"M{i:02}": Decimal(100) for i in range(40)})
    exchange.two_sides = True
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True,
    )
    service.refresh_competition_cache()
    service.refresh_price_history()
    original_trial = views.lp_trial_candidates
    original_init = LPReadScratch.__init__
    sources, copies = [], []
    in_trial = False

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if in_trial:
            copies.append(weakref.ref(self))

    def trial(facts, **kwargs):
        nonlocal in_trial
        assert isinstance(facts, ValuesView)
        sources.append(weakref.ref(facts._mapping))
        in_trial = True
        try:
            return original_trial(facts, **kwargs)
        finally:
            in_trial = False

    def interrupted(*args, **kwargs):
        assert sources[-1]() is not None, "source died during projection"
        raise failure("controlled projection interruption")

    monkeypatch.setattr(LPReadScratch, "__init__", tracked_init)
    monkeypatch.setattr(views, "lp_trial_candidates", trial)
    if failure is not None:
        monkeypatch.setattr(views, "_lp_shortlist_rows", interrupted)
    was_enabled = gc.isenabled()
    gc.disable()  # No explicit collection may be needed to end this reader.
    try:
        for _ in range(3):
            service._candidate_queue_state = None
            if failure is None:
                state = service._candidate_queue_state_build()
                assert len(state["queue_normal"]) == 40
                assert len(state["directions_by_condition"]) == 40
            else:
                with pytest.raises(failure, match="controlled projection interruption"):
                    service._candidate_queue_state_build()
                assert service._candidate_queue_state is None
            if failure is None:
                assert sources[-1]() is state["directions_by_condition"]._source
                service._candidate_queue_state = None
                del state
            assert sources[-1]() is None, "released queue retained its source"
            assert copies == [], "trial duplicated the direction detail database"
    finally:
        if was_enabled:
            gc.enable()


def test_excluded_queue_reprojects_list_without_detail_database(tmp_path, monkeypatch):
    exchange = ExclusionExchange(NOW, {"m": Decimal(20)})
    exchange.two_sides = True
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(
        store, exchange, clock=lambda: exchange.now, exclusions_enabled=True,
    )
    service.refresh_competition_cache()
    service.refresh_price_history()
    state = service._candidate_queue_state_build()
    old_row = state["queue_normal"][0]
    assert old_row["token_id"] == "no-condition-m"
    original_trial = views.lp_trial_candidates
    forms = []

    def trial(facts, **kwargs):
        forms.append(type(facts))
        return original_trial(facts, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("reprojection duplicated direction details")

    monkeypatch.setattr(views, "lp_trial_candidates", trial)
    monkeypatch.setattr(LPReadScratch, "__init__", forbidden)
    assert store.lp_record_market_exclusion(
        "condition-m", "no-condition-m", "history_amplitude_exceeded",
        checked_at=NOW, cooldown_until=NOW + timedelta(hours=1), now=NOW,
    )
    service._evict_excluded_candidates()
    assert forms == [list]
    assert old_row["token_id"] == "token-condition-m"
    assert [d["market"]["outcome"] for d in state["directions_by_condition"]["condition-m"]] == ["YES"]
    assert service._candidate_queue_state is None


def test_inflight_projection_keeps_source_generation_and_discards_old_queue(tmp_path, monkeypatch):
    exchange = ExclusionExchange(NOW, {"m": Decimal(20)})
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now,
    )
    service.refresh_competition_cache()
    service.refresh_price_history()
    old = service._prepared_input_snapshot()
    entered, release = Event(), Event()
    original_shortlist = views._lp_shortlist_rows
    sources, pools = [], []

    def blocked(facts, **kwargs):
        sources.append(weakref.ref(facts._mapping))
        entered.set()
        assert release.wait(10), "independent projection watchdog"
        rows = original_shortlist(facts, **kwargs)
        pools.append([row["daily_pool_usd"] for row in rows])
        return rows

    monkeypatch.setattr(views, "_lp_shortlist_rows", blocked)
    with ThreadPoolExecutor(1) as worker:
        future = worker.submit(service._candidate_queue_state_build)
        try:
            assert entered.wait(10), "projection did not reach shortlist"
            catalog = dict(old["catalog"])
            catalog["markets"] = [{**row, "daily_pool_usd": Decimal(90)} for row in catalog["markets"]]
            assert service._publish_prepared_inputs(catalog, old["metadata"], state="known")
            assert sources[0]() is not None
        finally:
            release.set()
        assert future.result(timeout=10) is None
    assert pools == [[Decimal(20)]]
    assert sources[0]() is None
    assert service._candidate_queue_state is None
    monkeypatch.setattr(views, "_lp_shortlist_rows", original_shortlist)
    assert service._candidate_queue_state_build()["queue_normal"][0]["daily_pool_usd"] == Decimal(90)


def test_invalid_timestamp_returns_without_reading_or_retaining_source(monkeypatch):
    source = LPReadScratch({"direction": _trial_direction("m")})
    reference = weakref.ref(source)
    facts = source.values()
    del source

    def forbidden(*args):
        raise AssertionError("invalid timestamp read the source")

    monkeypatch.setattr(LPReadScratch, "__getitem__", forbidden)
    result = views.lp_trial_candidates(
        facts, competition={}, account_budget_facts={}, now=NOW.replace(tzinfo=None),
    )
    assert result["rows"] == [] and result["funnel"]["read"] == 0
    del facts
    assert reference() is None


@pytest.mark.parametrize("form", ["list", "tuple", "values", "native_values", "scratch"])
@pytest.mark.parametrize("available", ["480", None])
def test_full_projection_matches_before_index_for_all_inputs(form, available):
    # High eligibility, both sides, stale/missing prices and rejected/UNKNOWN
    # facts. The hash pins every field/reason/order from the pre-fix projection.
    directions = []
    for i in range(80):
        market_id = f"M{i:02}"
        for outcome, midpoint in (("YES", "0.40"), ("NO", "0.30")):
            row = _trial_direction(market_id, outcome=outcome, latest_midpoint=midpoint)
            if i % 5 == 0:
                row["history_summary"]["checked_at"] = NOW - timedelta(hours=2)
            elif i % 5 == 1:
                row["history_summary"].pop("latest_midpoint")
            directions.append(row)
    directions += [None, {}, {"market": None}, {"market": {"condition_id": " "}}]
    inactive = _trial_direction("inactive")
    inactive["reward_active"] = False
    unknown = _trial_direction("unknown")
    unknown["history_summary"] = {"state": "unknown"}
    directions += [inactive, unknown, _trial_direction("unaffordable", reward_min_size="2000")]
    competition = _trial_competition(*(f"M{i:02}" for i in range(80)), "inactive", "unknown", "unaffordable")
    competition["condition-M79"] = (None, NOW)
    if form == "tuple":
        facts = tuple(directions)
    elif form == "values":
        facts = ValuesView({str(i): row for i, row in enumerate(directions)})
    elif form == "native_values":
        facts = {str(i): row for i, row in enumerate(directions)}.values()
    elif form == "scratch":
        facts = LPReadScratch((f"key-{i}", row) for i, row in enumerate(directions)).values()
    else:
        facts = directions
    result = _trial(facts, competition=competition, available=available)
    digest = hashlib.sha256(json.dumps(result, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expected = {
        "480": "de0f3217b00151644e8624bcca73f8cd2f0cbf2feeb89043b5cb1b2fc4229f66",
        None: "af30b14731b5e2b9ff639d1a68b6134eb1141f11f5b62636f602b51291383bc5",
    }
    assert digest == expected[available]
    assert len(result["queue_normal"]) == (47 if available == "480" else 48)
    assert len(result["queue_backup"]) == 32
    assert result["funnel"]["excluded"]["competition_missing"] == 1
