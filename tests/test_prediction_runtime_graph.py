"""Issue #82: runtime relation graph grouping, lineage mapping, persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from open_trader.prediction_runtime_graph import RuntimeGraphStore, RuntimeRelationGraph


def row(
    version_id: str,
    endpoints: list[tuple[str, str]],
    *,
    activation: str = "ACTIVE",
    complete: bool = True,
) -> dict[str, object]:
    return {
        "version_id": version_id,
        "activation": activation,
        "endpoints": [{"venue": venue, "contract_id": contract} for venue, contract in endpoints],
        "model": {
            "terminal_states": ["YES", "NO"] if complete else [],
            "payouts": {"YES": "1"} if complete else {},
            "capital_release": "resolution" if complete else None,
        },
    }


def chain_generation(prefix: str = "v") -> dict[str, dict[str, object]]:
    """Two IMPLIES relations sharing one contract -> one connected group."""
    return {
        f"IMPLIES|polymarket:ca|polymarket:cb": row(f"{prefix}1", [("polymarket", "ca"), ("polymarket", "cb")]),
        f"IMPLIES|polymarket:cb|polymarket:cc": row(f"{prefix}2", [("polymarket", "cb"), ("polymarket", "cc")]),
    }


def make_graph(
    tmp_path: Path,
    generation: dict[str, dict[str, object]],
    *,
    code_version: str = "test",
) -> tuple[RuntimeRelationGraph, dict[str, dict[str, object]], dict[str, object]]:
    state = dict(generation)
    meta = {"generation": 1}

    def source() -> dict[str, dict[str, object]]:
        return dict(state)

    def meta_source() -> dict[str, object]:
        return dict(meta)

    graph = RuntimeRelationGraph(
        source,
        tmp_path,
        code_version=code_version,
        generation_meta_source=meta_source,
    )
    return graph, state, meta


def test_groups_partition_by_endpoint_connectivity(tmp_path: Path) -> None:
    generation = {
        **chain_generation(),
        "IMPLIES|polymarket:cd|polymarket:ce": row("v3", [("polymarket", "cd"), ("polymarket", "ce")]),
    }
    graph, _, _ = make_graph(tmp_path, generation)
    graph.refresh()
    components = graph.components()
    assert len(components) == 2
    identities = {id for component in components.values() for id in component.relation_identities}
    assert identities == set(generation)
    assert sorted(components.values(), key=lambda c: c.component_id)[0].contract_ids == (
        "polymarket:ca",
        "polymarket:cb",
        "polymarket:cc",
    )


def test_pure_update_reuses_lineage(tmp_path: Path) -> None:
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    first = graph.refresh()
    component = next(iter(graph.components().values()))
    state.clear()
    state.update(chain_generation("v2"))
    meta["generation"] += 1
    second = graph.refresh()
    assert second["generation"] == 2
    assert second["fingerprint"] != first["fingerprint"]
    updated = next(iter(graph.components().values()))
    assert updated.component_id == component.component_id
    assert updated.lineage_id == component.lineage_id
    assert updated.change_kind == "PURE_UPDATE"


def test_new_lineage_is_deterministic_and_empty_predecessor(tmp_path: Path) -> None:
    graph, _, _ = make_graph(tmp_path, chain_generation())
    graph.refresh()
    component = next(iter(graph.components().values()))
    assert component.change_kind == "NEW"
    assert component.predecessor_lineage_ids == ()
    other_graph, _, _ = make_graph(tmp_path / "other", chain_generation())
    other_graph.refresh()
    other = next(iter(other_graph.components().values()))
    assert other.lineage_id == component.lineage_id
    assert other.component_id == component.component_id


def test_extend_creates_successor_lineage(tmp_path: Path) -> None:
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    old = next(iter(graph.components().values()))
    state["IMPLIES|polymarket:cc|polymarket:cf"] = row(
        "v3", [("polymarket", "cc"), ("polymarket", "cf")]
    )
    meta["generation"] += 1
    graph.refresh()
    component = next(iter(graph.components().values()))
    assert component.change_kind == "EXTEND"
    assert component.lineage_id != old.lineage_id
    assert component.predecessor_lineage_ids == (old.lineage_id,)


def test_merge_creates_successor_lineage(tmp_path: Path) -> None:
    disjoint = {
        "IMPLIES|polymarket:ca|polymarket:cb": row("v1", [("polymarket", "ca"), ("polymarket", "cb")]),
        "IMPLIES|polymarket:cd|polymarket:ce": row("v2", [("polymarket", "cd"), ("polymarket", "ce")]),
    }
    graph, state, meta = make_graph(tmp_path, disjoint)
    graph.refresh()
    assert len(graph.components()) == 2
    old_lineages = tuple(sorted(c.lineage_id for c in graph.components().values()))
    state["IMPLIES|polymarket:cb|polymarket:cd"] = row(
        "v3", [("polymarket", "cb"), ("polymarket", "cd")]
    )
    meta["generation"] += 1
    graph.refresh()
    component = next(iter(graph.components().values()))
    assert component.change_kind == "MERGE"
    assert component.predecessor_lineage_ids == old_lineages
    assert len(graph.components()) == 1


def test_split_closes_old_lineage_and_keeps_predecessor(tmp_path: Path) -> None:
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    old = next(iter(graph.components().values()))
    state.clear()
    state.update(
        {
            "IMPLIES|polymarket:ca|polymarket:cb": row("v2", [("polymarket", "ca"), ("polymarket", "cb")]),
            "IMPLIES|polymarket:cc|polymarket:cd": row("v3", [("polymarket", "cc"), ("polymarket", "cd")]),
        }
    )
    meta["generation"] += 1
    graph.refresh()
    components = graph.components()
    assert len(components) == 2
    assert all(c.change_kind == "SPLIT" for c in components.values())
    assert all(c.predecessor_lineage_ids == (old.lineage_id,) for c in components.values())
    stored = RuntimeGraphStore(tmp_path).load()
    assert all(c.status == "CLOSED" for c in stored[2].values() if c.lineage_id == old.lineage_id)


def test_remove_closes_lineage_and_audits(tmp_path: Path) -> None:
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    old = next(iter(graph.components().values()))
    state.clear()
    meta["generation"] += 1
    graph.refresh()
    assert graph.components() == {}
    store = RuntimeGraphStore(tmp_path)
    _, _, stored = store.load()
    assert stored[old.component_id].status == "CLOSED"
    with pytest.raises(KeyError):
        graph.components()[old.component_id]


def test_restart_recovers_without_rebuilding_unchanged_catalog(tmp_path: Path) -> None:
    graph, _, _ = make_graph(tmp_path, chain_generation("v1"))
    first = graph.refresh()
    restarted, state, meta = make_graph(tmp_path, chain_generation("v1"))
    assert restarted.refresh() == first
    assert restarted.current_generation() == first
    state["IMPLIES|polymarket:cd|polymarket:ce"] = row(
        "v2", [("polymarket", "cd"), ("polymarket", "ce")]
    )
    meta["generation"] += 1
    advanced = restarted.refresh()
    assert advanced["generation"] == 2
    assert len(restarted.components()) == 2


def test_failed_save_keeps_previous_state(tmp_path: Path) -> None:
    store = RuntimeGraphStore(tmp_path)
    graph, _, _ = make_graph(tmp_path, chain_generation())
    graph.refresh()
    before = store.load()
    with pytest.raises(TypeError):
        store.save(
            [
                # contract_ids containing a set breaks JSON encoding inside the
                # transaction, forcing a rollback of the whole save.
                type(before[2][next(iter(before[2]))])(
                    component_id="bad",
                    lineage_id="bad",
                    generation=2,
                    change_kind="NEW",
                    relation_identities=("r",),
                    contract_ids=({"polymarket:zz"},),  # type: ignore[arg-type]
                    predecessor_lineage_ids=(),
                    status="ACTIVE",
                )
            ],
            [{"action": "NEW", "from_component": "", "to_component": "bad", "generation": 2, "code_version": "test"}],
            2,
            "fingerprint",
        )
    assert store.load() == before


def test_only_active_complete_relations_enter_groups(tmp_path: Path) -> None:
    generation = {
        "IMPLIES|polymarket:ca|polymarket:cb": row("v1", [("polymarket", "ca"), ("polymarket", "cb")]),
        "IMPLIES|polymarket:cb|polymarket:cc": row(
            "v2", [("polymarket", "cb"), ("polymarket", "cc")], activation="UNKNOWN"
        ),
        "IMPLIES|polymarket:cd|polymarket:ce": row(
            "v3", [("polymarket", "cd"), ("polymarket", "ce")], complete=False
        ),
    }
    graph, _, _ = make_graph(tmp_path, generation)
    graph.refresh()
    component = next(iter(graph.components().values()))
    assert component.relation_identities == ("IMPLIES|polymarket:ca|polymarket:cb",)
    assert component.contract_ids == ("polymarket:ca", "polymarket:cb")


def test_current_generation_is_monotonic(tmp_path: Path) -> None:
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    first = graph.refresh()
    assert graph.current_generation() == first
    assert first["generation"] == 1
    state.clear()
    state.update(chain_generation("v2"))
    meta["generation"] += 1
    second = graph.refresh()
    assert second["generation"] == 2
    assert graph.current_generation() == second


def test_generation_bump_without_structure_change_updates_generation_only(
    tmp_path: Path,
) -> None:
    graph, _, meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    component = next(iter(graph.components().values()))
    # The catalog publishes again, but the admitted rows are unchanged.
    meta["generation"] += 1
    result = graph.refresh()
    assert result["generation"] == 2
    updated = next(iter(graph.components().values()))
    assert updated.lineage_id == component.lineage_id
    assert updated.component_id == component.component_id
    assert updated.change_kind == component.change_kind
    assert updated.generation == 2


def test_real_catalog_generation_and_pure_update(tmp_path: Path) -> None:
    from open_trader.relation_catalog import RelationCatalog

    def discovery(contract_a: str, contract_b: str, market_date: str) -> dict[str, object]:
        return {
            "discovery_source": "exchange_metadata",
            "discovered_at": "2026-08-15T02:32:00Z",
            "relation_type": "IMPLIES",
            "semantics": {"statement": "A implies B", "direction": "A_TO_B"},
            "source_evidence": [{"source": "x"}],
            "model": {
                "completeness": "COMPLETE",
                "terminal_states": ["YES", "NO"],
                "payouts": {"YES": "1"},
                "capital_release": "resolution",
            },
            "markets": [
                {
                    "venue": "Polymarket", "contract_id": contract_a, "title": "t",
                    "market_date": market_date, "expires_at": "2026-12-31T17:00:00Z",
                    "event_identity_basis": "e", "settlement_observation_key": contract_a,
                    "settlement_rules": "r", "cancellation_rules": "c",
                },
                {
                    "venue": "Polymarket", "contract_id": contract_b, "title": "t",
                    "market_date": market_date, "expires_at": "2026-12-31T17:00:00Z",
                    "event_identity_basis": "e", "settlement_observation_key": contract_b,
                    "settlement_rules": "r", "cancellation_rules": "c",
                },
            ],
        }

    catalog = RelationCatalog(tmp_path)
    graph = RuntimeRelationGraph(
        catalog.current_generation,
        tmp_path,
        code_version="test",
        generation_meta_source=catalog.generation_meta,
    )
    first = catalog.ingest(discovery("ca", "cb", "2026-08-15T00:00:00Z"))
    catalog.approve(first["version_id"], {"version_id": first["version_id"]}, actor="t", git_sha="s")
    graph.refresh()
    component = next(iter(graph.components().values()))
    assert component.change_kind == "NEW"
    assert component.relation_identities == ("IMPLIES|polymarket:ca|polymarket:cb",)
    second = catalog.ingest(discovery("ca", "cb", "2026-08-16T00:00:00Z"))
    catalog.approve(second["version_id"], {"version_id": second["version_id"]}, actor="t", git_sha="s")
    catalog.replace(
        {"version_id": first["version_id"]},
        {"version_id": second["version_id"]},
        reason="rules_changed",
        actor="t",
        git_sha="s",
    )
    graph.refresh()
    updated = next(iter(graph.components().values()))
    assert updated.component_id == component.component_id
    assert updated.lineage_id == component.lineage_id
    assert updated.change_kind == "PURE_UPDATE"
