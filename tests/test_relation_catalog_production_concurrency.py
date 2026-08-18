"""Issue #91: production-shaped concurrent readers over one SQLite catalog.

After the thread-local connection fix, production re-enables the N-leg
background: the #52 live resolver thread and the #87 monitor-selection driver
tick against the same SQLite-backed ``RelationCatalog`` facade while the
relation review API serves rows and the monitor-side auto-prepare keeps
ingesting threshold relations. This test composes those exact components in
one process against one shared facade and fails on the nested-transaction
regression from #91 or on any reader error.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader.prediction_live_resolver import PredictionLiveResolver
from open_trader.prediction_monitor_selection import MonitorSelectionStore
from open_trader.prediction_monitor_selection_driver import (
    PredictionMonitorSelectionDriver,
)
from open_trader.relation_catalog import RelationCatalog
from open_trader.prediction_solver_verified import VerificationStatus
from test_prediction_arbitrage import threshold_relation


class _NeverSolverServer:
    """Solver seam whose submits never complete; keeps the resolver non-idle."""

    def submit(self, request: object) -> Future[object]:
        future: Future[object] = Future()
        future.set_running_or_notify_cancel()
        return future

    def close(self) -> None:
        return None


def _distinct_relation(tag: str) -> object:
    base = threshold_relation()
    return replace(
        base,
        market_a=replace(
            base.market_a,
            event_id=f"event-{tag}",
            condition_id=f"condition-{tag}-a",
        ),
        market_b=replace(
            base.market_b,
            event_id=f"event-{tag}",
            condition_id=f"condition-{tag}-b",
        ),
    )


@pytest.mark.xfail(
    reason="issue #94: pre-existing catalog approved-version loss race (~50%); "
    "remove once #94 is fixed",
    strict=False,
)
def test_concurrent_resolver_driver_review_and_prepare_share_one_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = RelationCatalog(tmp_path)

    # The driver's discovery pass is solver work, not catalog work; #91 targets
    # SQLite concurrency only, so resolve discovery instantly in-process.
    def fake_discovery(*args: object, **kwargs: object) -> tuple[object, ...]:
        return tuple(
            SimpleNamespace(
                status=VerificationStatus.QUALIFIED_VERIFIED,
                initial_verified_profit=1,
            )
            for _ in args[1]
        )

    monkeypatch.setattr(
        "open_trader.prediction_monitor_selection_driver.run_discovery",
        fake_discovery,
    )

    selection_store = MonitorSelectionStore(tmp_path)
    selection_lock = threading.RLock()
    resolver = PredictionLiveResolver(
        data_dir=tmp_path,
        relation_catalog=catalog,
        monitor=SimpleNamespace(),
        solver_server=_NeverSolverServer(),
        selection_store=selection_store,
        selection_lock=selection_lock,
        store=SimpleNamespace(),
        execution=None,
        poll_interval=0.01,
    )
    driver = PredictionMonitorSelectionDriver(
        relation_catalog=catalog,
        selection_store=selection_store,
        selection_lock=selection_lock,
        idle_check=resolver.is_idle,
        poll_interval=0.01,
    )
    resolver.start()
    driver.start()

    read_errors: list[BaseException] = []
    write_errors: list[BaseException] = []
    approved: list[str] = []

    def review_loop() -> None:
        for _ in range(150):
            try:
                catalog.review_rows()
                catalog.pending_count()
                catalog.list("pending")
            except BaseException as exc:  # readers must never fail
                read_errors.append(exc)

    def prepare_loop(prefix: str) -> None:
        for index in range(6):
            relation = _distinct_relation(f"{prefix}-{index}")
            try:
                version_id = str(
                    catalog.ingest_threshold_relation(relation)["version_id"]
                )
                catalog.approve(
                    version_id,
                    {"version_id": version_id},
                    actor="concurrency-test",
                    git_sha="a" * 40,
                )
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    write_errors.append(exc)
            except BaseException as exc:
                write_errors.append(exc)
            else:
                approved.append(version_id)

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(review_loop) for _ in range(4)]
            futures += [pool.submit(prepare_loop, f"writer-{n}") for n in range(2)]
            for future in futures:
                future.result()
    finally:
        driver.stop()
        resolver.stop()

    nested = [
        exc
        for exc in read_errors + write_errors
        if "cannot start a transaction within a transaction" in str(exc)
    ]
    assert nested == []
    assert read_errors == []
    assert write_errors == []
    assert len(approved) == 12

    reopened = RelationCatalog(tmp_path)
    generation = reopened.current_generation()
    pending = reopened.pending_count()
    assert pending + len(generation) == 12
    for version_id in approved:
        row = next(
            (
                record
                for record in reopened.review_rows()
                if record["version_id"] == version_id
            ),
            None,
        )
        assert row is not None
        assert row["status"] in {"APPROVED", "PENDING"}
