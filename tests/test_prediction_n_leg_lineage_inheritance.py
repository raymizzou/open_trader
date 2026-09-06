"""Issue #122: the N_LEG executed lock is inherited across graph successors.

The ``n_leg_lineage_claims`` lock must survive SPLIT/MERGE/EXTEND rotations:
every successor with a predecessor inherits the family lock (R1) through the
ancestor closure read live inside the admission transaction (R2), a successor
only re-arms through its OWN ``NO_QUALIFIED_OPPORTUNITY`` episode closed
after the ancestor claim (R3), the claim key becomes the resolved graph
lineage — never the frozen confirm string (R4) — and the same component's
own claim always rejects (no regression). Every expected lineage value below
comes from ``RuntimeGraphStore.load()``/``graph.components()`` truth and the
episodes come from the real ``EpisodeTracker``/``EpisodeStore`` rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_n_leg_episodes import EpisodeStore, EpisodeTracker
from open_trader.prediction_runtime_graph import RuntimeGraphStore
from test_prediction_runtime_graph import chain_generation, make_graph, row


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3")


def _store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path)


def _payload(component_id: str, name: str) -> dict[str, object]:
    """One admission payload whose identity is the payload ``component_id``;
    the frozen ``episode_lineage_id`` keeps the legacy display shape because
    the authoritative check resolves the lineage from the graph itself."""
    return {
        "execution_batch_id": f"batch-{name}",
        "opportunity_episode_id": f"episode-{name}",
        "episode_lineage_id": f"lineage:{component_id}",
        "mode": "MANUAL",
        "state": "ACTIVE",
        "entry_fingerprint": f"entry-{name}",
        "execution_solution_fingerprint": f"solution-{name}",
        "total_unsettled_capital_units": 1,
        "component_id": component_id,
    }


def _release_active_batch(tmp_path: Path) -> None:
    """Free the single-active-batch gate (not under test) after a family
    execution, exactly the approved B6 controls-seam idiom."""
    with sqlite3.connect(_db(tmp_path)) as connection:
        connection.execute("UPDATE n_leg_controls SET active_batch_id=NULL")


def _sole_claim(tmp_path: Path) -> tuple[str, str]:
    with sqlite3.connect(_db(tmp_path)) as connection:
        rows = connection.execute(
            "SELECT episode_lineage_id, created_at FROM n_leg_lineage_claims"
        ).fetchall()
    assert len(rows) == 1
    return str(rows[0][0]), str(rows[0][1])


def _close_negative_episode(
    tracker: EpisodeTracker,
    component_id: str,
    lineage_id: str,
    *,
    start,
) -> None:
    """Drive the real EpisodeTracker from a qualified observation through a
    full 300-second accepted-negative window to a NO_QUALIFIED_OPPORTUNITY
    close stamped at ``start + 300s``."""
    fingerprints = {
        "component_generation": 1,
        "model_fingerprint": "model-1",
        "quote_fingerprint": "quote-1",
        "qualification_fingerprint": "qual-1",
        "qualification_policy_version": "1",
    }
    tracker.observe_qualified(
        component_id,
        lineage_id,
        Decimal("1"),
        False,
        None,
        fingerprints,
        start,
    )
    for offset in (0, 300):
        tracker.observe_negative(
            component_id,
            proof_fingerprint=f"proof-{offset}",
            generation=1,
            model_fingerprint="model-1",
            quote_fingerprint="quote-1",
            qualification_fingerprint="qual-1",
            binding_matches=True,
            quote_fresh=True,
            gap_seconds=300,
            now=start + timedelta(seconds=offset),
            qualification_policy_version="1",
        )


# ---------------------------------------------------------------------------
# Approved case 3 (locked first): the same component's own claim always
# rejects — even with its own later NO_QUALIFIED_OPPORTUNITY episode close,
# which must never be read as a re-arm for the component itself.
# ---------------------------------------------------------------------------


def test_case3_same_component_reclaim_rejected_even_after_own_negative_close(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    store = _store(tmp_path)
    graph, _state, _meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    family = next(iter(graph.components().values()))
    assert family.change_kind == "NEW"

    store.n_leg_create_batch(_payload(family.component_id, "family"))
    _release_active_batch(tmp_path)
    _claim_key, claim_created_at = _sole_claim(tmp_path)
    claim_at = datetime.fromisoformat(claim_created_at)

    tracker = EpisodeTracker(store=EpisodeStore(tmp_path))
    _close_negative_episode(
        tracker,
        family.component_id,
        family.lineage_id,
        start=claim_at + timedelta(hours=1),
    )
    with sqlite3.connect(_db(tmp_path)) as connection:
        closed_at, close_reason = connection.execute(
            "SELECT closed_at, close_reason FROM opportunity_episodes"
            " WHERE component_id=?",
            (family.component_id,),
        ).fetchone()
    assert close_reason == "NO_QUALIFIED_OPPORTUNITY"
    assert datetime.fromisoformat(str(closed_at)) > claim_at

    with pytest.raises(ValueError, match="N_LEG_LINEAGE_ALREADY_CLAIMED"):
        store.n_leg_create_batch(_payload(family.component_id, "again"))


# ---------------------------------------------------------------------------
# Approved case 8: a payload component_id the graph cannot resolve fails
# closed with N_LEG_LINEAGE_UNKNOWN — admission never guesses an identity.
# ---------------------------------------------------------------------------


def test_case8_unknown_component_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    graph, _state, _meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()

    with pytest.raises(ValueError, match="N_LEG_LINEAGE_UNKNOWN"):
        store.n_leg_create_batch(_payload("component:not-in-graph", "unknown"))
    with sqlite3.connect(_db(tmp_path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM n_leg_lineage_claims").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM n_leg_batches").fetchone()[0] == 0
        )


# ---------------------------------------------------------------------------
# Approved case 1: a real SPLIT rotation cannot escape the executed lock —
# both successors inherit the family claim through the ancestor closure.
# ---------------------------------------------------------------------------


def test_case1_split_successor_inherits_family_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    family = next(iter(graph.components().values()))

    store.n_leg_create_batch(_payload(family.component_id, "family"))
    _release_active_batch(tmp_path)
    claim_key, _created_at = _sole_claim(tmp_path)
    assert claim_key == family.lineage_id

    # Real rotation: one generation advance splits the family into two groups
    # (the approved tests/test_prediction_runtime_graph.py split fixture).
    state.clear()
    state.update(
        {
            "IMPLIES|polymarket:ca|polymarket:cb": row(
                "v2", [("polymarket", "ca"), ("polymarket", "cb")]
            ),
            "IMPLIES|polymarket:cc|polymarket:cd": row(
                "v3", [("polymarket", "cc"), ("polymarket", "cd")]
            ),
        }
    )
    meta["generation"] += 1
    graph.refresh()
    successors = graph.components()
    assert len(successors) == 2
    assert all(
        component.change_kind == "SPLIT"
        and component.predecessor_lineage_ids == (family.lineage_id,)
        for component in successors.values()
    )

    for index, component in enumerate(sorted(successors.values(), key=lambda c: c.component_id)):
        with pytest.raises(ValueError, match="N_LEG_LINEAGE_INHERITED_CLAIMED"):
            store.n_leg_create_batch(_payload(component.component_id, f"split-{index}"))
    with sqlite3.connect(_db(tmp_path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM n_leg_lineage_claims").fetchone()[0]
            == 1
        )


# ---------------------------------------------------------------------------
# Approved case 5: the re-arm clock guard — an own NO_QUALIFIED_OPPORTUNITY
# close stamped BEFORE the ancestor claim never re-arms the successor.
# ---------------------------------------------------------------------------


def _split_family(
    tmp_path: Path,
) -> tuple[PredictionArbitrageStore, object, dict, object, object]:
    """One executed family (claim on its graph lineage) rotated into two real
    SPLIT successors; the active-batch gate is freed after the execution."""
    store = _store(tmp_path)
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    family = next(iter(graph.components().values()))
    store.n_leg_create_batch(_payload(family.component_id, "family"))
    _release_active_batch(tmp_path)
    state.clear()
    state.update(
        {
            "IMPLIES|polymarket:ca|polymarket:cb": row(
                "v2", [("polymarket", "ca"), ("polymarket", "cb")]
            ),
            "IMPLIES|polymarket:cc|polymarket:cd": row(
                "v3", [("polymarket", "cc"), ("polymarket", "cd")]
            ),
        }
    )
    meta["generation"] += 1
    graph.refresh()
    successors = sorted(
        graph.components().values(), key=lambda component: component.component_id
    )
    assert len(successors) == 2
    return store, graph, meta, family, successors


def test_case5_negative_close_older_than_claim_does_not_rearm(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    store, _graph, _meta, family, successors = _split_family(tmp_path)
    _claim_key, claim_created_at = _sole_claim(tmp_path)
    claim_at = datetime.fromisoformat(claim_created_at)

    # The successor's own episode closes NO_QUALIFIED_OPPORTUNITY, but two
    # hours BEFORE the family claim was recorded.
    tracker = EpisodeTracker(store=EpisodeStore(tmp_path))
    first = successors[0]
    _close_negative_episode(
        tracker,
        first.component_id,
        first.lineage_id,
        start=claim_at - timedelta(hours=2),
    )
    with sqlite3.connect(_db(tmp_path)) as connection:
        closed_at, close_reason = connection.execute(
            "SELECT closed_at, close_reason FROM opportunity_episodes"
            " WHERE component_id=?",
            (first.component_id,),
        ).fetchone()
    assert close_reason == "NO_QUALIFIED_OPPORTUNITY"
    assert datetime.fromisoformat(str(closed_at)) < claim_at

    with pytest.raises(ValueError, match="N_LEG_LINEAGE_INHERITED_CLAIMED"):
        store.n_leg_create_batch(_payload(first.component_id, "stale-evidence"))


# ---------------------------------------------------------------------------
# Approved case 6: a COMPONENT_RETIRED close is a retirement record, never a
# no-opportunity re-arm — even when it is stamped after the ancestor claim.
# ---------------------------------------------------------------------------


def test_case6_retired_close_is_not_rearm_evidence(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    store, _graph, _meta, _family, successors = _split_family(tmp_path)
    _claim_key, claim_created_at = _sole_claim(tmp_path)
    claim_at = datetime.fromisoformat(claim_created_at)

    tracker = EpisodeTracker(store=EpisodeStore(tmp_path))
    first = successors[0]
    fingerprints = {
        "component_generation": 1,
        "model_fingerprint": "model-1",
        "quote_fingerprint": "quote-1",
        "qualification_fingerprint": "qual-1",
        "qualification_policy_version": "1",
    }
    tracker.observe_qualified(
        first.component_id,
        first.lineage_id,
        Decimal("1"),
        False,
        None,
        fingerprints,
        claim_at + timedelta(hours=1),
    )
    tracker.component_retired(
        first.component_id, now=claim_at + timedelta(hours=2)
    )
    with sqlite3.connect(_db(tmp_path)) as connection:
        closed_at, close_reason = connection.execute(
            "SELECT closed_at, close_reason FROM opportunity_episodes"
            " WHERE component_id=?",
            (first.component_id,),
        ).fetchone()
    assert close_reason == "COMPONENT_RETIRED"
    assert datetime.fromisoformat(str(closed_at)) > claim_at

    with pytest.raises(ValueError, match="N_LEG_LINEAGE_INHERITED_CLAIMED"):
        store.n_leg_create_batch(_payload(first.component_id, "retired-evidence"))


# ---------------------------------------------------------------------------
# Approved case 2: the successor's own complete negative-proof close, stamped
# after the ancestor claim, re-arms the family — admission passes and the new
# claim lands on the successor's own graph lineage key.
# ---------------------------------------------------------------------------


def test_case2_rearmed_successor_admits_and_claims_own_lineage(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    store, _graph, _meta, family, successors = _split_family(tmp_path)
    _claim_key, claim_created_at = _sole_claim(tmp_path)
    claim_at = datetime.fromisoformat(claim_created_at)

    first, second = successors
    # The blocked sibling proves the lock still lives before the re-arm.
    with pytest.raises(ValueError, match="N_LEG_LINEAGE_INHERITED_CLAIMED"):
        store.n_leg_create_batch(_payload(first.component_id, "blocked-sibling"))

    tracker = EpisodeTracker(store=EpisodeStore(tmp_path))
    _close_negative_episode(
        tracker,
        first.component_id,
        first.lineage_id,
        start=claim_at + timedelta(hours=1),
    )
    with sqlite3.connect(_db(tmp_path)) as connection:
        episode_lineage_id, closed_at, close_reason = connection.execute(
            "SELECT episode_lineage_id, closed_at, close_reason"
            " FROM opportunity_episodes WHERE component_id=?",
            (first.component_id,),
        ).fetchone()
    assert episode_lineage_id == first.lineage_id
    assert close_reason == "NO_QUALIFIED_OPPORTUNITY"
    assert datetime.fromisoformat(str(closed_at)) > claim_at

    store.n_leg_create_batch(_payload(first.component_id, "rearmed"))
    with sqlite3.connect(_db(tmp_path)) as connection:
        rows = connection.execute(
            "SELECT episode_lineage_id, execution_batch_id"
            " FROM n_leg_lineage_claims ORDER BY created_at"
        ).fetchall()
    assert [str(row[0]) for row in rows] == [
        family.lineage_id,
        first.lineage_id,
    ]
    assert str(rows[1][1]) == "batch-rearmed"
    # The stored batch payload records the resolved lineage, not the frozen
    # display string.
    batch = store.n_leg_batch("batch-rearmed")
    assert batch is not None
    assert batch["episode_lineage_id"] == first.lineage_id
    # The untouched sibling stays locked by the inherited family claim.
    _release_active_batch(tmp_path)
    with pytest.raises(ValueError, match="N_LEG_LINEAGE_INHERITED_CLAIMED"):
        store.n_leg_create_batch(_payload(second.component_id, "sibling-after"))


# ---------------------------------------------------------------------------
# Approved case 4: two independent families, one executed — the real MERGE
# rotation's successor inherits the executed family's lock, and only its own
# later negative-proof close lets it through.
# ---------------------------------------------------------------------------


def test_case4_merge_successor_inherits_then_rearms(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    store = _store(tmp_path)
    graph, state, meta = make_graph(
        tmp_path,
        {
            "IMPLIES|polymarket:ca|polymarket:cb": row(
                "v1", [("polymarket", "ca"), ("polymarket", "cb")]
            ),
            "IMPLIES|polymarket:cd|polymarket:ce": row(
                "v2", [("polymarket", "cd"), ("polymarket", "ce")]
            ),
        },
    )
    graph.refresh()
    components = graph.components()
    assert len(components) == 2
    executed, spared = sorted(
        components.values(), key=lambda component: component.component_id
    )
    store.n_leg_create_batch(_payload(executed.component_id, "family"))
    _release_active_batch(tmp_path)
    claim_key, claim_created_at = _sole_claim(tmp_path)
    assert claim_key == executed.lineage_id
    claim_at = datetime.fromisoformat(claim_created_at)

    # Real rotation: the bridge relation merges both families into one group.
    state["IMPLIES|polymarket:cb|polymarket:cd"] = row(
        "v3", [("polymarket", "cb"), ("polymarket", "cd")]
    )
    meta["generation"] += 1
    graph.refresh()
    merged = list(graph.components().values())
    assert len(merged) == 1
    successor = merged[0]
    assert successor.change_kind == "MERGE"
    assert successor.predecessor_lineage_ids == tuple(
        sorted((executed.lineage_id, spared.lineage_id))
    )

    with pytest.raises(ValueError, match="N_LEG_LINEAGE_INHERITED_CLAIMED"):
        store.n_leg_create_batch(_payload(successor.component_id, "merged"))

    tracker = EpisodeTracker(store=EpisodeStore(tmp_path))
    _close_negative_episode(
        tracker,
        successor.component_id,
        successor.lineage_id,
        start=claim_at + timedelta(hours=1),
    )
    store.n_leg_create_batch(_payload(successor.component_id, "merged-rearmed"))
    with sqlite3.connect(_db(tmp_path)) as connection:
        keys = [
            str(key[0])
            for key in connection.execute(
                "SELECT episode_lineage_id FROM n_leg_lineage_claims"
                " ORDER BY created_at"
            ).fetchall()
        ]
    assert keys == [executed.lineage_id, successor.lineage_id]


# ---------------------------------------------------------------------------
# Approved case 7: a zero-predecessor NEW component sharing no identities or
# contracts with the executed family is never caught by the inherited lock.
# ---------------------------------------------------------------------------


def test_case7_new_component_without_predecessors_admits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    graph, _state, _meta = make_graph(
        tmp_path,
        {
            **chain_generation("v1"),
            "IMPLIES|polymarket:cx|polymarket:cy": row(
                "v2", [("polymarket", "cx"), ("polymarket", "cy")]
            ),
        },
    )
    graph.refresh()
    components = graph.components()
    assert len(components) == 2
    assert all(component.change_kind == "NEW" for component in components.values())
    family = next(
        component
        for component in components.values()
        if "polymarket:ca" in component.contract_ids
    )
    fresh = next(
        component
        for component in components.values()
        if "polymarket:cx" in component.contract_ids
    )
    assert fresh.predecessor_lineage_ids == ()

    store.n_leg_create_batch(_payload(family.component_id, "family"))
    _release_active_batch(tmp_path)

    store.n_leg_create_batch(_payload(fresh.component_id, "fresh"))
    with sqlite3.connect(_db(tmp_path)) as connection:
        keys = [
            str(key[0])
            for key in connection.execute(
                "SELECT episode_lineage_id FROM n_leg_lineage_claims"
                " ORDER BY created_at"
            ).fetchall()
        ]
    assert keys == [family.lineage_id, fresh.lineage_id]
