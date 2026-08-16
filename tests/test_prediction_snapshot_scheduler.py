"""Issue #83: latest-snapshot-wins scheduler over the selected monitor set."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from open_trader.prediction_arbitrage import BookLevel
from open_trader.prediction_monitor_selection import SelectedComponent
from open_trader.prediction_snapshot_scheduler import (
    ComponentSnapshot,
    LegBook,
    SnapshotLeg,
    SnapshotScheduler,
    economic_fingerprint,
    order_ready,
    raw_solve_result,
)
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerResponse


NOW = datetime(2026, 8, 16, 9, 0, 0, tzinfo=UTC)
FRESH = timedelta(seconds=30)


def component(component_id: str = "c1") -> SelectedComponent:
    return SelectedComponent(
        component_id=component_id,
        contract_ids=("leg-a", "leg-b"),
        constraint_ids=(),
        action_ids=(),
        admission_score=1,
        portfolio=(),
        relation_fingerprint="r",
        terminal_fingerprint="t",
        portfolio_fingerprint="p",
        status="ACTIVE",
    )


def snapshot(
    *,
    component_id: str = "c1",
    price: str = "0.51",
    received_at: datetime = NOW,
    exchange_time: datetime | None = NOW,
    sequence: int | None = 7,
    fee: str | None = "0",
    available: bool = True,
) -> ComponentSnapshot:
    level = (BookLevel(Decimal(price), Decimal("10")),)
    return ComponentSnapshot(
        component_id=component_id,
        legs=(
            SnapshotLeg(
                leg_id="leg-a",
                book=LegBook(
                    bids=level,
                    asks=level,
                    taker_fee_bps=None if fee is None else Decimal(fee),
                    available=available,
                ),
                received_at=received_at,
                exchange_time=exchange_time,
                sequence=sequence,
            ),
            SnapshotLeg(
                leg_id="leg-b",
                book=LegBook(
                    bids=level,
                    asks=level,
                    taker_fee_bps=None if fee is None else Decimal(fee),
                    available=available,
                ),
                received_at=received_at,
                exchange_time=exchange_time,
                sequence=sequence,
            ),
        ),
    )


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.futures: list[Future[WorkerOutcome]] = []

    def submit(self, request: object) -> Future[WorkerOutcome]:
        future: Future[WorkerOutcome] = Future()
        self.requests.append(request)
        self.futures.append(future)
        return future


def outcome(
    *,
    status: str = "OK",
    termination: str = "COMPLETED",
    cleanup_proven: bool = True,
    native_status: str | None = "FEASIBLE",
    cost_bound: int | None = 5,
) -> WorkerOutcome:
    evidence = (
        None
        if native_status is None
        else {"native_status": native_status, "cost_upper_bound_units": cost_bound}
    )
    response = (
        None
        if native_status is None or status != "OK"
        else WorkerResponse("p", "cp_sat", "r", "OK", evidence, {}, ())
    )
    return WorkerOutcome("r", status, termination, 1, 1, 0, False, cleanup_proven, response)


def scheduler(
    server: FakeServer,
    source: dict[str, ComponentSnapshot | None],
    *,
    now_fn=lambda: NOW,
) -> SnapshotScheduler:
    def snapshot_for(item: SelectedComponent) -> ComponentSnapshot | None:
        return source.get(item.component_id)

    def build_solve_request(item: SelectedComponent, snap: ComponentSnapshot) -> object:
        return ("solve", item.component_id)

    return SnapshotScheduler(
        server,
        snapshot_for=snapshot_for,
        build_solve_request=build_solve_request,
        now_fn=now_fn,
        freshness=FRESH,
    )


def test_economic_fingerprint_ignores_timing_fields() -> None:
    base = snapshot()
    later = replace(
        base,
        legs=tuple(
            replace(
                leg,
                received_at=NOW + timedelta(seconds=1),
                exchange_time=NOW + timedelta(seconds=1),
                sequence=8,
            )
            for leg in base.legs
        ),
    )
    assert economic_fingerprint(base) == economic_fingerprint(later)
    moved = replace(
        base,
        legs=tuple(
            replace(
                leg,
                book=replace(
                    leg.book,
                    asks=(BookLevel(Decimal("0.52"), Decimal("10")),),
                ),
            )
            for leg in base.legs
        ),
    )
    assert economic_fingerprint(base) != economic_fingerprint(moved)


def test_same_fingerprint_is_deduped_and_only_refreshes_freshness() -> None:
    server = FakeServer()
    source = {"c1": snapshot(sequence=1)}
    plan = scheduler(server, source)
    assert plan.refresh([component()]) == {"c1": "scheduled"}
    source["c1"] = snapshot(sequence=2)
    assert plan.refresh([component()]) == {"c1": "deduped"}
    assert len(server.requests) == 1


def test_pending_is_replaced_and_stale_result_is_dropped() -> None:
    server = FakeServer()
    source = {"c1": snapshot(price="0.51")}
    plan = scheduler(server, source)
    assert plan.refresh([component()]) == {"c1": "scheduled"}
    source["c1"] = snapshot(price="0.52")
    assert plan.refresh([component()]) == {"c1": "replaced"}
    source["c1"] = snapshot(price="0.53")
    assert plan.refresh([component()]) == {"c1": "replaced"}
    assert len(server.requests) == 1
    server.futures[0].set_result(outcome(native_status="FEASIBLE", cost_bound=4))
    assert plan.result("c1") is None
    assert len(server.requests) == 2
    server.futures[1].set_result(outcome(native_status="FEASIBLE", cost_bound=6))
    result = plan.result("c1")
    assert result is not None
    assert result.status == "feasible"
    assert result.cost_bound == 6
    assert result.order_ready is True


def test_duplicate_of_in_flight_input_supersedes_pending() -> None:
    server = FakeServer()
    source = {"c1": snapshot(price="0.51")}
    plan = scheduler(server, source)
    plan.refresh([component()])
    source["c1"] = snapshot(price="0.52")
    assert plan.refresh([component()]) == {"c1": "replaced"}
    source["c1"] = snapshot(price="0.51")
    assert plan.refresh([component()]) == {"c1": "deduped"}
    server.futures[0].set_result(outcome(cost_bound=4))
    assert len(server.requests) == 1
    result = plan.result("c1")
    assert result is not None and result.cost_bound == 4


def test_failure_is_unknown_without_immediate_retry() -> None:
    server = FakeServer()
    source = {"c1": snapshot()}
    plan = scheduler(server, source)
    plan.refresh([component()])
    server.futures[0].set_result(
        outcome(status="UNKNOWN", termination="HARD_TIMEOUT", native_status=None)
    )
    result = plan.result("c1")
    assert result is not None
    assert result.status == "unknown"
    assert result.cost_bound is None
    assert plan.refresh([component()]) == {"c1": "deduped"}
    assert len(server.requests) == 1
    source["c1"] = snapshot(price="0.52")
    assert plan.refresh([component()]) == {"c1": "scheduled"}


def test_submit_failure_is_unknown_and_not_retried() -> None:
    class BusyServer:
        def submit(self, request: object) -> Future[WorkerOutcome]:
            raise RuntimeError("solver server queue is full")

    source = {"c1": snapshot()}
    plan = scheduler(BusyServer(), source)
    assert plan.refresh([component()]) == {"c1": "failed"}
    result = plan.result("c1")
    assert result is not None and result.status == "unknown"
    assert plan.refresh([component()]) == {"c1": "deduped"}


def test_order_ready_requires_timing_fields_and_freshness() -> None:
    complete = snapshot()
    assert order_ready(complete, now=NOW, freshness=FRESH) is True
    missing_sequence = replace(
        complete,
        legs=tuple(replace(leg, sequence=None) for leg in complete.legs),
    )
    assert order_ready(missing_sequence, now=NOW, freshness=FRESH) is False
    missing_exchange = replace(
        complete,
        legs=tuple(replace(leg, exchange_time=None) for leg in complete.legs),
    )
    assert order_ready(missing_exchange, now=NOW, freshness=FRESH) is False
    stale = replace(
        complete,
        legs=tuple(
            replace(leg, exchange_time=NOW - timedelta(seconds=31))
            for leg in complete.legs
        ),
    )
    assert order_ready(stale, now=NOW, freshness=FRESH) is False


def test_refresh_rejects_snapshot_for_wrong_component() -> None:
    server = FakeServer()

    def snapshot_for(item: SelectedComponent) -> ComponentSnapshot:
        return snapshot(component_id="other")

    plan = SnapshotScheduler(
        server,
        snapshot_for=snapshot_for,
        build_solve_request=lambda item, snap: ("solve", item.component_id),
        now_fn=lambda: NOW,
        freshness=FRESH,
    )
    with pytest.raises(ValueError, match="component"):
        plan.refresh([component()])


def test_raw_solve_result_mapping() -> None:
    assert raw_solve_result(outcome(native_status="OPTIMAL", cost_bound=9)) == ("feasible", 9)
    assert raw_solve_result(outcome(native_status="INFEASIBLE")) == ("infeasible", None)
    assert raw_solve_result(outcome(native_status="UNKNOWN")) == ("unknown", None)
    assert raw_solve_result(outcome(status="UNKNOWN", native_status=None)) == ("unknown", None)
    assert raw_solve_result(outcome(cleanup_proven=False, native_status=None)) == ("unknown", None)


def test_close_cancels_in_flight() -> None:
    server = FakeServer()
    plan = scheduler(server, {"c1": snapshot()})
    plan.refresh([component()])
    plan.close()
    assert server.futures[0].cancelled()
    assert plan.refresh([component()]) == {"c1": "closed"}
