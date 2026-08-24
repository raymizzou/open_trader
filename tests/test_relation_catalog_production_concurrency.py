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
from test_relation_catalog import compiled_relation_discovery
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
                solution=None,
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


def test_concurrent_over_budget_approvals_preserve_committed_generation(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    for index in range(6):
        left = f"condition-{index}"
        right = f"condition-{index + 1}"
        discovery = compiled_relation_discovery(
            [left, right],
            {left: "BUY_YES", right: "BUY_YES"},
            relation_type="IMPLIES",
        )
        version_id = catalog.ingest(discovery)["version_id"]
        result = catalog.approve(
            version_id,
            {"version_id": version_id},
            actor="concurrency-test",
            git_sha="a" * 40,
        )
        assert result["activation"] == "ACTIVE"

    before = catalog.current_generation()
    assert len(before) == 6

    candidate_ids = []
    for right in ("condition-7", "condition-8"):
        discovery = compiled_relation_discovery(
            ["condition-6", right],
            {"condition-6": "BUY_YES", right: "BUY_YES"},
            relation_type="IMPLIES",
        )
        candidate_ids.append(catalog.ingest(discovery)["version_id"])

    barrier = threading.Barrier(2)

    def approve(version_id: str) -> dict[str, object]:
        barrier.wait()
        return catalog.approve(
            version_id,
            {"version_id": version_id},
            actor="concurrency-test",
            git_sha="a" * 40,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, candidate_ids))

    assert [result["activation"] for result in results] == [
        "UNSUPPORTED_SIZE",
        "UNSUPPORTED_SIZE",
    ]
    reopened = RelationCatalog(tmp_path)
    assert reopened.current_generation() == before
    rows = {row["version_id"]: row for row in reopened.review_rows()}
    for version_id in candidate_ids:
        assert rows[version_id]["status"] == "APPROVED"
        assert rows[version_id]["activation"] == "UNSUPPORTED_SIZE"


def test_stale_snapshot_block_preserves_newer_same_identity_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = RelationCatalog(tmp_path)
    actor = "concurrency-test"
    git_sha = "a" * 40

    original_discovery = compiled_relation_discovery(
        ["condition-a", "condition-b"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
        relation_type="IMPLIES",
        rule="rules-original",
    )
    original = catalog.ingest(original_discovery)
    original_id = str(original["version_id"])
    assert catalog.approve(
        original_id,
        {"version_id": original_id},
        actor=actor,
        git_sha=git_sha,
    )["activation"] == "ACTIVE"

    replacement_discovery = compiled_relation_discovery(
        ["condition-a", "condition-b"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
        relation_type="IMPLIES",
        rule="rules-original",
    )
    replacement_discovery["discovery_source"] = "replacement-source"
    replacement = catalog.ingest(replacement_discovery)
    replacement_id = str(replacement["version_id"])
    replacement_approval = catalog.approve(
        replacement_id,
        {"version_id": replacement_id},
        actor=actor,
        git_sha=git_sha,
    )
    assert replacement_approval["status"] == "APPROVED"
    assert replacement_approval["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"

    stale_discovery = compiled_relation_discovery(
        ["condition-x", "condition-y"],
        {"condition-x": "BUY_YES", "condition-y": "BUY_YES"},
        relation_type="IMPLIES",
        rule="rules-stale",
    )
    stale = catalog.ingest(stale_discovery)
    stale_id = str(stale["version_id"])

    paused = threading.Event()
    release = threading.Event()
    pause_once = True
    pause_lock = threading.Lock()
    original_replace = catalog._catalog.replace

    def paused_replace(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal pause_once
        with pause_lock:
            should_pause = pause_once
            pause_once = False
        if should_pause:
            paused.set()
            if not release.wait(timeout=5):
                raise AssertionError("stale approval did not receive release")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(catalog._catalog, "replace", paused_replace)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        catalog.approve,
        stale_id,
        {"version_id": stale_id},
        actor=actor,
        git_sha=git_sha,
    )
    try:
        assert paused.wait(timeout=5)
        result = catalog.replace(
            {"version_id": original_id},
            {"version_id": replacement_id},
            reason="rules_changed",
            actor=actor,
            git_sha=git_sha,
        )
        assert result == {
            "revoked_version_id": original_id,
            "activated_version_id": replacement_id,
        }
        frozen = catalog.current_generation()
        assert len(frozen) == 1
        frozen_entry = frozen[str(original["identity"])]
        assert frozen_entry["version_id"] == replacement_id
    finally:
        release.set()
        try:
            stale_result = future.result(timeout=5)
        finally:
            pool.shutdown(wait=True)

    assert stale_result["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    reopened = RelationCatalog(tmp_path)
    assert reopened.current_generation() == frozen
    rows = {str(row["version_id"]): row for row in reopened.review_rows()}
    assert rows[replacement_id]["activation"] == "ACTIVE"
    assert rows[original_id]["activation"] == "SUPERSEDED"
    assert rows[stale_id]["status"] == "APPROVED"
    assert rows[stale_id]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
