"""Issue #99: relation-generation doctor report and confirmed cleanup apply."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.prediction_catalog_doctor import _diagnose_conflict, report
from open_trader.prediction_monitor_selection import relation_generation_problem
from open_trader.relation_catalog import RelationCatalog
from test_relation_catalog import compiled_relation_discovery


def _uncompilable_catalog(tmp_path: Path) -> tuple[RelationCatalog, list[dict[str, str]]]:
    """A four-member generation whose action identity conflicts: a 2v1
    settlement-rules conflict on ``condition-a`` (same side, different
    rules_hash — issue #111 removed direction-only conflicts, so the doctor
    attribution path is exercised with a non-direction conflict) plus one
    unrelated early-settling row. Since issue #110 the early row is fresh on
    its own component timeline (contract/observation-key disjoint from the
    trio), so the only compile failure left is the merge conflict. Seeded
    through the v2 core (``approve`` bypasses the activation gate) to mirror a
    live-but-broken production generation.
    """
    catalog = RelationCatalog(tmp_path)
    payloads = [
        compiled_relation_discovery(
            ["condition-a", "condition-b", "condition-z"],
            {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
            rule={"condition-a": "rules-doctor-a", "condition-b": "rules-issue-99", "condition-z": "rules-issue-99"},
        ),
        compiled_relation_discovery(
            ["condition-a", "condition-c", "condition-w"],
            {"condition-a": "BUY_YES", "condition-c": "BUY_YES", "condition-w": "BUY_YES"},
            rule={"condition-a": "rules-doctor-b", "condition-c": "rules-issue-99", "condition-w": "rules-issue-99"},
        ),
        compiled_relation_discovery(
            ["condition-a", "condition-d", "condition-v"],
            {"condition-a": "BUY_YES", "condition-d": "BUY_YES", "condition-v": "BUY_YES"},
            rule={"condition-a": "rules-doctor-a", "condition-d": "rules-issue-99", "condition-v": "rules-issue-99"},
        ),
        compiled_relation_discovery(
            ["condition-e", "condition-f", "condition-g"],
            {"condition-e": "BUY_YES", "condition-f": "BUY_YES", "condition-g": "BUY_YES"},
            as_of="2026-07-01T00:00:00Z",
            release="2026-08-01T00:00:00Z",
        ),
    ]
    seeded: list[dict[str, str]] = []
    for payload in payloads:
        converted = catalog._converted(payload)
        result = catalog._catalog.ingest(converted)
        catalog._catalog.approve(result["version_id"], actor="doctor", git_sha="")
        seeded.append(
            {"identity": str(result["identity"]), "version_id": str(result["version_id"])}
        )
    return catalog, seeded


def test_doctor_report_attributes_conflict_and_spares_disjoint_timeline(
    tmp_path: Path,
) -> None:
    catalog, seeded = _uncompilable_catalog(tmp_path)
    payload = report(catalog)

    assert payload["compiles"] is True
    assert len(payload["conflicts"]) == 1
    conflict = payload["conflicts"][0]
    assert conflict["key"] == "polymarket:condition-a:BUY_YES"
    assert conflict["label"] == "action"
    holders = {holder["identity"]: holder for holder in conflict["holders"]}
    assert set(holders) == {
        seeded[0]["identity"],
        seeded[1]["identity"],
        seeded[2]["identity"],
    }
    assert holders[seeded[0]["identity"]]["side"] == "BUY_YES"
    assert holders[seeded[1]["identity"]]["side"] == "BUY_YES"
    assert holders[seeded[2]["identity"]]["side"] == "BUY_YES"
    assert all(holder["payload"] for holder in conflict["holders"])
    assert conflict["removal"] == [seeded[1]["identity"]]

    # Issue #110: the early-settling row (condition-e/f/g) shares no contract
    # and no observation key with the trio, so it is judged on its own
    # component timeline (as_of 2026-07-01, release 2026-08-01) and is fresh —
    # no stale finding, no removal proposal for it.
    assert payload["stale"] == []

    assert payload["proposed_removal"] == [seeded[1]["identity"]]
    assert payload["remaining"] == 3
    assert payload["error"] is None

    # End to end: after the proposed removal the remainder is non-empty and
    # the compile seam accepts it.
    remaining_rows = {
        identity: row
        for identity, row in catalog.current_generation().items()
        if identity not in set(payload["proposed_removal"])
    }
    assert set(remaining_rows) == {
        seeded[0]["identity"],
        seeded[2]["identity"],
        seeded[3]["identity"],
    }
    problem, _ = relation_generation_problem(remaining_rows)
    assert problem is not None


def test_doctor_equality_as_of_is_not_stale(tmp_path: Path) -> None:
    """A row whose problem as_of equals the merged as_of is fresh.

    Both markets settling at the same instant naturally form
    ``problem.as_of == merged as_of``; the oracle only flags
    ``capital_release_at < problem.as_of``, so equality must not enter
    proposed_removal (the old top-level ``capital_release <= merged_as_of``
    rule removed exactly this row).
    """
    catalog = RelationCatalog(tmp_path)
    payload = compiled_relation_discovery(
        ["condition-a", "condition-b", "condition-z"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
        as_of="2026-08-15T00:00:00Z",
        release="2026-08-15T00:00:00Z",
    )
    version_id = catalog.ingest_controlled(payload)["version_id"]
    catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")

    result = report(catalog)
    assert result["compiles"] is True
    assert result["stale"] == []
    assert result["proposed_removal"] == []
    assert result["remaining"] == 1
    assert result["error"] is None


def test_doctor_mixed_batch_keeps_disjoint_timelines_fresh(tmp_path: Path) -> None:
    """Mixed-batch generation: since issue #110 each disjoint component is
    judged on its own timeline, so the early-settling row (as_of 2026-07-01,
    release 2026-08-01) is fresh in its own component and nothing is proposed;
    the whole generation compiles."""
    catalog = RelationCatalog(tmp_path)
    payloads = [
        compiled_relation_discovery(
            ["condition-a", "condition-b", "condition-z"],
            {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
        ),
        compiled_relation_discovery(
            ["condition-e", "condition-f", "condition-g"],
            {"condition-e": "BUY_YES", "condition-f": "BUY_YES", "condition-g": "BUY_YES"},
            as_of="2026-07-01T00:00:00Z",
            release="2026-08-01T00:00:00Z",
        ),
    ]
    seeded: list[dict[str, str]] = []
    for payload in payloads:
        converted = catalog._converted(payload)
        result = catalog._catalog.ingest(converted)
        catalog._catalog.approve(result["version_id"], actor="doctor", git_sha="")
        seeded.append(
            {"identity": str(result["identity"]), "version_id": str(result["version_id"])}
        )

    payload = report(catalog)
    assert payload["compiles"] is True
    assert payload["conflicts"] == []
    assert payload["stale"] == []
    assert payload["proposed_removal"] == []
    assert payload["remaining"] == 2
    assert payload["error"] is None


def test_doctor_early_as_of_with_later_release_is_fresh(tmp_path: Path) -> None:
    """Row-level staleness mirrors the oracle atom by atom, per component:
    only a row with an atom releasing strictly before its own component's
    as_of enters proposed_removal (issue #110).

    X has an early ``problem.as_of`` (2026-07-01) and every atom releases
    after it — fresh on its own timeline. S shares X's observation key (same
    as_of and rule), so X and S form one component whose timeline is
    2026-07-01; S releases exactly at that timeline, and equality is fresh.
    F is an unrelated disjoint component. Nothing is stale and the whole
    generation compiles.
    """
    catalog = RelationCatalog(tmp_path)
    payloads = [
        compiled_relation_discovery(
            ["condition-a", "condition-b", "condition-z"],
            {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
        ),  # F: fresh, as_of 2026-08-15, release 2026-12-31
        compiled_relation_discovery(
            ["condition-e", "condition-f", "condition-g"],
            {"condition-e": "BUY_YES", "condition-f": "BUY_YES", "condition-g": "BUY_YES"},
            as_of="2026-07-01T00:00:00Z",
            release="2026-12-31T17:00:00Z",
        ),  # X: early as_of, all releases after merged as_of -> fresh
        compiled_relation_discovery(
            ["condition-h", "condition-i", "condition-j"],
            {"condition-h": "BUY_YES", "condition-i": "BUY_YES", "condition-j": "BUY_YES"},
            as_of="2026-07-01T00:00:00Z",
            release="2026-07-01T00:00:00Z",
        ),  # S: as_of == release, both before F's as_of -> stale
    ]
    seeded: list[dict[str, str]] = []
    for payload in payloads:
        converted = catalog._converted(payload)
        result = catalog._catalog.ingest(converted)
        catalog._catalog.approve(result["version_id"], actor="doctor", git_sha="")
        seeded.append(
            {"identity": str(result["identity"]), "version_id": str(result["version_id"])}
        )

    payload = report(catalog)
    assert payload["compiles"] is True
    assert payload["conflicts"] == []
    assert payload["stale"] == []
    assert payload["proposed_removal"] == []
    assert payload["remaining"] == 3
    assert payload["error"] is None

    remaining_rows = {
        identity: row
        for identity, row in catalog.current_generation().items()
        if identity not in set(payload["proposed_removal"])
    }
    assert set(remaining_rows) == {
        seeded[0]["identity"],
        seeded[1]["identity"],
        seeded[2]["identity"],
    }
    problem, _ = relation_generation_problem(remaining_rows)
    assert problem is not None


def test_doctor_report_on_healthy_generation(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    payload = compiled_relation_discovery(
        ["condition-a", "condition-b", "condition-z"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
    )
    version_id = catalog.ingest_controlled(payload)["version_id"]
    catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")

    result = report(catalog)
    assert result["compiles"] is True
    assert result["conflicts"] == []
    assert result["stale"] == []
    assert result["proposed_removal"] == []
    assert result["remaining"] == 1
    assert result["error"] is None


def test_doctor_excludes_unknown_members_from_stale_attribution(
    tmp_path: Path,
) -> None:
    """Review repro, reshaped by issue #110: a cause-marked UNKNOWN member U
    must not lift any component timeline nor enter any proposal.

    U is revoked through the v2 cause ledger (facade ``current_generation()``
    then reports it as UNKNOWN), F and S are ACTIVE rows on disjoint
    components, each fresh on its own timeline — so the doctor proposes
    nothing (stale/proposal empty), counts U as excluded, and the remaining
    admitted set compiles. The former epilogue expected the activation gate
    to refuse the drop because U's late as_of poisoned the whole set; with
    staleness scoped to components the gate admits the disjoint set and the
    drop succeeds.
    """
    catalog = RelationCatalog(tmp_path)

    def seed(payload: dict[str, object], *, revoke: bool = False) -> dict[str, str]:
        converted = catalog._converted(payload)
        result = catalog._catalog.ingest(converted)
        catalog._catalog.approve(result["version_id"], actor="doctor", git_sha="")
        if revoke:
            catalog._catalog.revoke(result["version_id"], actor="doctor", git_sha="")
        return {
            "identity": str(result["identity"]),
            "version_id": str(result["version_id"]),
        }

    u = seed(
        compiled_relation_discovery(
            ["condition-u1", "condition-u2", "condition-u3"],
            {
                "condition-u1": "BUY_YES",
                "condition-u2": "BUY_YES",
                "condition-u3": "BUY_YES",
            },
            as_of="2027-01-01T00:00:00Z",
            release="2027-01-01T00:00:00Z",
        ),
        revoke=True,
    )
    f = seed(
        compiled_relation_discovery(
            ["condition-f1", "condition-f2", "condition-f3"],
            {
                "condition-f1": "BUY_YES",
                "condition-f2": "BUY_YES",
                "condition-f3": "BUY_YES",
            },
        ),  # fresh: as_of 2026-08-15, releases 2026-12-31
    )
    s = seed(
        compiled_relation_discovery(
            ["condition-s1", "condition-s2", "condition-s3"],
            {
                "condition-s1": "BUY_YES",
                "condition-s2": "BUY_YES",
                "condition-s3": "BUY_YES",
            },
            as_of="2026-07-01T00:00:00Z",
            release="2026-07-01T00:00:00Z",
        ),  # stale: releases before every admitted as_of
    )

    generation = catalog.current_generation()
    assert generation[u["identity"]]["activation"] == "UNKNOWN"
    assert generation[f["identity"]]["activation"] == "ACTIVE"
    assert generation[s["identity"]]["activation"] == "ACTIVE"

    payload = report(catalog)
    assert payload["compiles"] is True
    assert payload["conflicts"] == []
    assert payload["stale"] == []
    assert payload["proposed_removal"] == []
    assert payload["remaining"] == 2
    assert payload["excluded"] == 1
    assert payload["error"] is None

    # Rebuild precheck: the UNKNOWN row must not lift the post-drop precheck,
    # and with staleness scoped to components the activation gate no longer
    # refuses a disjoint post-drop set: dropping S succeeds (the gate used to
    # block this exact drop because U's as_of poisoned the whole ACTIVE set).
    result = catalog.rebuild_generation(
        [s["identity"]], actor="op", git_sha="sha", note="doctor apply"
    )
    assert result["status"] == "ACTIVE"
    assert set(result["remaining"]) == {u["identity"], f["identity"]}

    # Dropping the healthy F leaves admitted {U}, which also compiles per
    # component; the precheck is not fooled into claiming otherwise.
    result = catalog.rebuild_generation(
        [f["identity"]], actor="op", git_sha="sha", note="doctor apply"
    )
    assert result["status"] == "ACTIVE"
    assert set(result["remaining"]) == {u["identity"]}


def _doctor_row(payload: dict[str, object], *, activation: str) -> dict[str, object]:
    """Facade-style row shape consumed by the doctor's diagnostics."""
    return {
        "activation": activation,
        "model": {
            name: payload["model"].get(name)
            for name in ("terminal_states", "payouts", "capital_release", "problem")
        },
    }


def test_doctor_conflict_holders_exclude_unknown_rows() -> None:
    """UNKNOWN members never contribute conflict holders.

    A revoked member U holding the same conflicting action payload as ACTIVE
    row a1 must not turn the 1v1 ACTIVE tie into a 2v1 majority: with U
    excluded the tie-break keeps the smaller-serializing holder (a2, the
    ``rules-doctor-1`` settlement rules) and proposes row a1; counting U would
    instead propose a2 (the review's parity flip). Since issue #111 removed
    direction-only conflicts, the shared-contract conflict is a settlement
    rules difference on the same canonical BUY_YES action.
    """
    a1_payload = compiled_relation_discovery(
        ["condition-a", "condition-b", "condition-z"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
        rule={"condition-a": "rules-doctor-2", "condition-b": "rules-issue-99", "condition-z": "rules-issue-99"},
    )
    a2_payload = compiled_relation_discovery(
        ["condition-a", "condition-c", "condition-w"],
        {"condition-a": "BUY_YES", "condition-c": "BUY_YES", "condition-w": "BUY_YES"},
        rule={"condition-a": "rules-doctor-1", "condition-c": "rules-issue-99", "condition-w": "rules-issue-99"},
    )
    u_payload = compiled_relation_discovery(
        ["condition-a", "condition-u1", "condition-u2"],
        {"condition-a": "BUY_YES", "condition-u1": "BUY_YES", "condition-u2": "BUY_YES"},
        rule={"condition-a": "rules-doctor-2", "condition-u1": "rules-issue-99", "condition-u2": "rules-issue-99"},
    )
    rows = {
        "a1": _doctor_row(a1_payload, activation="ACTIVE"),
        "a2": _doctor_row(a2_payload, activation="ACTIVE"),
        "u": _doctor_row(u_payload, activation="UNKNOWN"),
    }
    try:
        relation_generation_problem({"a1": rows["a1"], "a2": rows["a2"]})
    except ValueError as exc:
        message = str(exc)
    else:
        pytest.fail("ACTIVE pair should conflict on condition-a")
    assert (
        "action 'polymarket:condition-a:BUY_YES' conflicts across compiled relations"
        in message
    )

    finding, removal = _diagnose_conflict(rows, message)
    holders = {holder["identity"]: holder for holder in finding["holders"]}
    assert set(holders) == {"a1", "a2"}
    assert removal == ["a1"]  # tie-break: rules-doctor-1 serializes smaller, kept

    # The UNKNOWN row changes nothing: with only the two ACTIVE rows the
    # verdict is identical.
    _, pair_removal = _diagnose_conflict(
        {"a1": rows["a1"], "a2": rows["a2"]}, message
    )
    assert pair_removal == removal == ["a1"]

    # Sanity: counting U as ACTIVE really would flip the verdict to a 2v1
    # majority against a2 — proving the exclusion is load-bearing.
    counted = dict(rows)
    counted["u"] = _doctor_row(u_payload, activation="ACTIVE")
    _, counted_removal = _diagnose_conflict(counted, message)
    assert counted_removal == ["a2"]


def test_doctor_apply_drops_proposed_and_keeps_remaining_active(tmp_path: Path) -> None:
    catalog, seeded = _uncompilable_catalog(tmp_path)
    result = catalog.rebuild_generation(
        [seeded[1]["identity"], seeded[3]["identity"]],
        actor="op",
        git_sha="sha",
        note="doctor apply",
    )

    assert result["status"] == "ACTIVE"
    assert set(result["remaining"]) == {seeded[0]["identity"], seeded[2]["identity"]}
    generation = catalog.current_generation()
    assert set(generation) == {seeded[0]["identity"], seeded[2]["identity"]}
    assert all(row["activation"] == "ACTIVE" for row in generation.values())
    assert report(catalog)["compiles"] is True

    versions = catalog._versions()
    for entry in (seeded[1], seeded[3]):
        assert versions[entry["version_id"]]["status"] == "REVOKED"
        assert versions[entry["version_id"]]["activation_status"] == "REVOKED"
    for entry in (seeded[0], seeded[2]):
        assert versions[entry["version_id"]]["status"] == "APPROVED"

    with sqlite3.connect(catalog.path) as conn:
        rows = conn.execute(
            "SELECT action, identity, version_id, actor, git_sha, note "
            "FROM catalog_v2_audit ORDER BY id"
        ).fetchall()
    assert [row[0] for row in rows] == ["rebuild_generation_drop"] * 2
    assert {row[1] for row in rows} == {seeded[1]["identity"], seeded[3]["identity"]}
    assert {row[2] for row in rows} == {seeded[1]["version_id"], seeded[3]["version_id"]}
    assert all(row[3] == "op" and row[4] == "sha" for row in rows)
    assert all(
        row[5] == "doctor apply intent; drop via rebuild_generation" for row in rows
    )


def test_doctor_apply_refuses_non_compiling_post_drop_set(tmp_path: Path) -> None:
    catalog, seeded = _uncompilable_catalog(tmp_path)
    before = dict(catalog.current_generation())

    with pytest.raises(ValueError, match="post-drop generation does not compile"):
        catalog.rebuild_generation([seeded[0]["identity"]], actor="op", git_sha="sha")

    assert catalog.current_generation() == before
    assert all(
        catalog._versions()[entry["version_id"]]["status"] != "REVOKED"
        for entry in seeded
    )
    with sqlite3.connect(catalog.path) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'catalog_v2_audit'"
        ).fetchone()
    assert table is None


def test_doctor_apply_override_still_fails_closed_on_gate_rejection(tmp_path: Path) -> None:
    catalog, seeded = _uncompilable_catalog(tmp_path)
    before = dict(catalog.current_generation())

    with pytest.raises(ValueError, match="rejected by the activation gate"):
        catalog.rebuild_generation(
            [seeded[0]["identity"]], actor="op", git_sha="sha", allow_uncompilable=True
        )

    assert catalog.current_generation() == before
    assert all(
        catalog._versions()[entry["version_id"]]["status"] != "REVOKED"
        for entry in seeded
    )
    # Intent-first auditing: the drop row is durable before the v2 replace,
    # so a gate rejection leaves an intent record but zero data changes.
    with sqlite3.connect(catalog.path) as conn:
        rows = conn.execute(
            "SELECT action, identity, version_id, note "
            "FROM catalog_v2_audit ORDER BY id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "rebuild_generation_drop"
    assert rows[0][1] == seeded[0]["identity"]
    assert rows[0][2] == seeded[0]["version_id"]
    assert rows[0][3] == "intent; drop via rebuild_generation"


def test_doctor_apply_audit_write_failure_aborts_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit-write failure must abort the whole apply before any data
    change: the generation stays bitwise unchanged, no version is REVOKED,
    and nothing is partially dropped."""
    catalog, seeded = _uncompilable_catalog(tmp_path)
    before_generation = dict(catalog.current_generation())
    before_versions = dict(catalog._versions())

    def failing_audit(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(catalog._store, "write_audit", failing_audit)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        catalog.rebuild_generation(
            [seeded[1]["identity"], seeded[3]["identity"]],
            actor="op",
            git_sha="sha",
            note="doctor apply",
        )

    assert catalog.current_generation() == before_generation
    assert catalog._versions() == before_versions
    assert all(
        catalog._versions()[entry["version_id"]]["status"] != "REVOKED"
        for entry in seeded
    )


def test_doctor_apply_audit_rows_visible_before_replace(tmp_path: Path) -> None:
    """The drop audit rows are written before the v2 replace: when replace
    runs, the intent rows are already durable in catalog_v2_audit."""
    catalog, seeded = _uncompilable_catalog(tmp_path)
    seen: list[list[tuple[str, str, str, str]]] = []
    original_replace = catalog._catalog.replace

    def recording_replace(change_set: list, *, actor: str, git_sha: str) -> dict:
        with sqlite3.connect(catalog.path) as conn:
            rows = conn.execute(
                "SELECT action, identity, version_id, note "
                "FROM catalog_v2_audit ORDER BY id"
            ).fetchall()
        seen.append(list(rows))
        return original_replace(change_set, actor=actor, git_sha=git_sha)

    catalog._catalog.replace = recording_replace
    result = catalog.rebuild_generation(
        [seeded[1]["identity"], seeded[3]["identity"]],
        actor="op",
        git_sha="sha",
        note="doctor apply",
    )
    assert result["status"] == "ACTIVE"
    assert len(seen) == 1
    assert [row[0] for row in seen[0]] == ["rebuild_generation_drop"] * 2
    assert {row[1] for row in seen[0]} == {seeded[1]["identity"], seeded[3]["identity"]}
    assert {row[2] for row in seen[0]} == {
        seeded[1]["version_id"],
        seeded[3]["version_id"],
    }
    assert all("intent; drop via rebuild_generation" in row[3] for row in seen[0])


def test_cli_catalog_doctor_report_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    catalog, seeded = _uncompilable_catalog(tmp_path)

    code = cli.main(["prediction-arb", "catalog-doctor", "--data-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "conflict: action 'polymarket:condition-a:BUY_YES'" in out
    # Issue #110: the early-settling row is fresh on its own component
    # timeline, so the report has no stale finding and proposes only the
    # minority conflicting holder.
    assert "stale: " not in out
    assert "proposed_removal: 1" in out
    assert "remaining: 3" in out

    code = cli.main(
        [
            "prediction-arb",
            "catalog-doctor",
            "--data-dir",
            str(tmp_path),
            "--drop",
            seeded[1]["identity"],
            seeded[3]["identity"],
            "--apply",
            "--yes",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "dropped:" in out
    assert "remaining: 2" in out
    assert "status: ACTIVE" in out
    assert len(catalog.current_generation()) == 2


def test_cli_catalog_doctor_apply_requires_yes_and_drop(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    catalog, seeded = _uncompilable_catalog(tmp_path)

    code = cli.main(
        [
            "prediction-arb",
            "catalog-doctor",
            "--data-dir",
            str(tmp_path),
            "--drop",
            seeded[0]["identity"],
            "--apply",
        ]
    )
    assert code == 2
    assert "requires --yes" in capsys.readouterr().err

    code = cli.main(
        [
            "prediction-arb",
            "catalog-doctor",
            "--data-dir",
            str(tmp_path),
            "--apply",
            "--yes",
        ]
    )
    assert code == 2
    assert "requires --drop" in capsys.readouterr().err


# -- Issue #110 review fix: cross-row attribution and its positive coverage ---


def _seed(catalog: RelationCatalog, payload: dict[str, object]) -> dict[str, str]:
    """Seed one payload through the v2 core (approve bypasses the gate)."""
    converted = catalog._converted(payload)
    result = catalog._catalog.ingest(converted)
    catalog._catalog.approve(result["version_id"], actor="doctor", git_sha="")
    return {"identity": str(result["identity"]), "version_id": str(result["version_id"])}


def _cross_key_payloads() -> tuple[dict[str, object], dict[str, object]]:
    """The review sandbox topology (production #102 ``_event_component``): row
    A = {xa, xb} with as_of 2026-07-01 / release 2026-08-01, row B = {ya, yb}
    with as_of 2027-01-01 / release 2027-02-01, no shared contract, but every
    contract carries the same settlement observation key K (A's key, injected
    verbatim into B). The oracle merges both rows and joins xa/ya by fingerprint
    into ONE component; a per-row grouping would keep two self-consistent
    components and attribute nothing."""
    early = compiled_relation_discovery(
        ["xa", "xb"],
        {"xa": "BUY_YES", "xb": "BUY_YES"},
        as_of="2026-07-01T00:00:00Z",
        release="2026-08-01T00:00:00Z",
        rule="rules-110-cross-key",
    )
    late = compiled_relation_discovery(
        ["ya", "yb"],
        {"ya": "BUY_YES", "yb": "BUY_YES"},
        as_of="2027-01-01T00:00:00Z",
        release="2027-02-01T00:00:00Z",
        rule="rules-110-cross-key",
    )
    shared_key = copy.deepcopy(
        early["model"]["problem"]["terminal_state_sets"][0]["settlement_observation_key"]
    )
    late_problem = late["model"]["problem"]
    for state in late_problem["terminal_state_sets"]:
        state["settlement_observation_key"] = copy.deepcopy(shared_key)
    for action in late_problem["actions"]:
        action["settlement_observation_key"] = copy.deepcopy(shared_key)
    return early, late


def test_doctor_cross_row_observation_key_joins_one_component(
    tmp_path: Path,
) -> None:
    """Same observation key on different contracts in different rows is ONE
    component (the oracle's merged-state join), so the early-settling row A is
    attributed stale against the component timeline 2027-01-01 and proposed
    for removal; the remainder compiles."""
    catalog = RelationCatalog(tmp_path)
    early, late = _cross_key_payloads()
    a = _seed(catalog, early)
    b = _seed(catalog, late)

    # The compile seam itself raises STALE for the merged cross-row component.
    with pytest.raises(ValueError, match="STALE_CAPITAL_RELEASE_AT"):
        relation_generation_problem(catalog.current_generation())

    payload = report(catalog)
    assert payload["compiles"] is True
    assert payload["conflicts"] == []
    assert len(payload["stale"]) == 1
    finding = payload["stale"][0]
    assert finding["component_as_of"] == "2027-01-01T00:00:00+00:00"
    assert finding["contracts"] == ["xa", "xb", "ya", "yb"]
    assert set(finding["identities"]) == {a["identity"], b["identity"]}
    assert [item["identity"] for item in finding["stale_identities"]] == [
        a["identity"]
    ]
    assert finding["stale_identities"][0]["as_of"] == "2026-07-01T00:00:00+00:00"
    assert finding["stale_identities"][0]["stale_releases"] == [
        "2026-08-01T00:00:00+00:00"
    ]
    assert payload["proposed_removal"] == [a["identity"]]
    assert payload["remaining"] == 1
    assert payload["error"] is None

    # After the proposed removal the remainder compiles.
    remaining_rows = {
        identity: row
        for identity, row in catalog.current_generation().items()
        if identity != a["identity"]
    }
    problem, _ = relation_generation_problem(remaining_rows)
    assert problem is not None


def test_cli_catalog_doctor_prints_component_as_of_for_cross_row_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The doctor CLI prints the component timeline and the stale identity for
    the cross-row observation-key topology (positive print assertions for the
    #110 keys, per the current cli.py stale-line format)."""
    catalog = RelationCatalog(tmp_path)
    early, late = _cross_key_payloads()
    a = _seed(catalog, early)
    _seed(catalog, late)

    code = cli.main(["prediction-arb", "catalog-doctor", "--data-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "compiles: True" in out
    assert f"stale: {a['identity']} " in out
    assert "as_of=2026-07-01T00:00:00+00:00" in out
    assert "component_as_of=2027-01-01T00:00:00+00:00" in out
    assert "proposed_removal: 1" in out
    assert "remaining: 1" in out


def test_doctor_shared_contract_cross_row_stale_attributes_early_row(
    tmp_path: Path,
) -> None:
    """Two rows sharing one contract under one consistent identity, timelines
    crossing: the shared contract welds both rows into one component whose
    timeline is 2027-01-01, so the early-settling row (x1/x2 releasing
    2026-08-01) is attributed stale, the proposal is exactly that row, and the
    confirmed drop applies and compiles."""
    catalog = RelationCatalog(tmp_path)
    donor = compiled_relation_discovery(
        ["shared-sink"],
        {"shared-sink": "BUY_YES"},
        as_of="2026-07-01T00:00:00Z",
        release="2027-02-01T00:00:00Z",
        rule="rules-110-shared",
    )
    donor_state = copy.deepcopy(donor["model"]["problem"]["terminal_state_sets"][0])
    donor_action = copy.deepcopy(donor["model"]["problem"]["actions"][0])
    early = compiled_relation_discovery(
        ["x1", "x2", "shared-sink"],
        {"x1": "BUY_YES", "x2": "BUY_YES", "shared-sink": "BUY_YES"},
        as_of="2026-07-01T00:00:00Z",
        release="2026-08-01T00:00:00Z",
        rule="rules-110-shared",
    )
    late = compiled_relation_discovery(
        ["y1", "y2", "shared-sink"],
        {"y1": "BUY_YES", "y2": "BUY_YES", "shared-sink": "BUY_YES"},
        as_of="2027-01-01T00:00:00Z",
        release="2027-02-01T00:00:00Z",
        rule="rules-110-shared",
    )
    for payload in (early, late):
        problem = payload["model"]["problem"]
        problem["terminal_state_sets"] = [
            copy.deepcopy(donor_state)
            if state["market_contract_id"] == "shared-sink"
            else state
            for state in problem["terminal_state_sets"]
        ]
        problem["actions"] = [
            copy.deepcopy(donor_action)
            if action["market_contract_id"] == "shared-sink"
            else action
            for action in problem["actions"]
        ]
    x = _seed(catalog, early)
    y = _seed(catalog, late)

    payload = report(catalog)
    assert payload["compiles"] is True
    assert payload["conflicts"] == []
    assert len(payload["stale"]) == 1
    finding = payload["stale"][0]
    assert finding["component_as_of"] == "2027-01-01T00:00:00+00:00"
    assert finding["contracts"] == ["shared-sink", "x1", "x2", "y1", "y2"]
    assert set(finding["identities"]) == {x["identity"], y["identity"]}
    assert [item["identity"] for item in finding["stale_identities"]] == [
        x["identity"]
    ]
    assert finding["stale_identities"][0]["stale_releases"] == [
        "2026-08-01T00:00:00+00:00"
    ]
    assert payload["proposed_removal"] == [x["identity"]]
    assert payload["remaining"] == 1
    assert payload["error"] is None

    result = catalog.rebuild_generation(
        [x["identity"]], actor="op", git_sha="sha", note="doctor apply"
    )
    assert result["status"] == "ACTIVE"
    assert set(result["remaining"]) == {y["identity"]}
    assert report(catalog)["compiles"] is True


# -- Issue #111 review fix: legacy stored payloads keep conflict attribution --


_FIXTURE_110 = Path(__file__).parent / "fixtures" / "issue_110_incident_payloads.json"


def _legacy_implies_catalog(tmp_path: Path) -> tuple[RelationCatalog, list[str]]:
    """Two verbatim issue-#110 incident IMPLIES payloads (old-format stored
    problems: one bare ``polymarket:{contract}`` action id per contract)
    sharing contract ``0x0630…3a71``, seeded through the v2 core because the
    fixture carries the verbatim v2 production shape (model fields at the top
    level). The shared action's ``account_id`` diverges on one side, so the
    seam fails on a NON-direction conflict under the canonical
    ``{venue}:{contract}:{side}`` identity while the stored ids stay bare."""
    data = json.loads(_FIXTURE_110.read_text())
    early, late = copy.deepcopy(data["blocked"][0]), copy.deepcopy(data["blocked"][1])
    shared = "0x0630fc3f77c2db5ecc473cf4f782e58143a16db6440e714805c8619aa8073a71"
    action = next(
        item
        for item in late["problem"]["actions"]
        if item["market_contract_id"] == shared
    )
    action["account_id"] = "catalog-v1"
    catalog = RelationCatalog(tmp_path)
    identities: list[str] = []
    for entry in (early, late):
        result = catalog._catalog.ingest(entry)
        catalog._catalog.approve(result["version_id"], actor="doctor", git_sha="")
        identities.append(str(result["identity"]))
    return catalog, identities


def test_doctor_attributes_legacy_payload_conflict_with_proposed_removal(
    tmp_path: Path,
) -> None:
    """Old-format stored payloads keep full conflict attribution (issue #111
    review P2).

    The merge seam canonicalizes every decoded member problem, so a
    non-direction conflict over old-format rows is raised under the canonical
    ``{venue}:{contract}:{side}`` action id while the stored payloads carry
    only bare ids. The doctor decodes through the same canonicalization, so
    ``report`` must still name the canonical conflict key, attribute holders
    to both identities, and propose the minority removal — not degrade to an
    unattributed error."""
    catalog, (early_id, late_id) = _legacy_implies_catalog(tmp_path)

    # The seam itself fails on the canonical BUY_NO clone of the shared action.
    with pytest.raises(
        ValueError,
        match=(
            "action 'polymarket:0x0630fc3f77c2db5ecc473cf4f782e58143a16db6440e"
            "714805c8619aa8073a71:BUY_NO' conflicts across compiled relations"
        ),
    ):
        relation_generation_problem(catalog.current_generation())

    payload = report(catalog)
    assert payload["compiles"] is True
    assert len(payload["conflicts"]) == 1
    conflict = payload["conflicts"][0]
    assert conflict["label"] == "action"
    assert (
        conflict["key"]
        == "polymarket:0x0630fc3f77c2db5ecc473cf4f782e58143a16db6440e714805c8619aa8073a71:BUY_NO"
    )
    holders = {holder["identity"]: holder for holder in conflict["holders"]}
    assert set(holders) == {early_id, late_id}
    assert all(holder["side"] == "BUY_NO" for holder in holders.values())

    # 1v1 tie: exactly one minority identity is proposed for removal, and the
    # remaining generation compiles end to end.
    assert len(payload["proposed_removal"]) == 1
    assert set(payload["proposed_removal"]) <= {early_id, late_id}
    assert payload["remaining"] == 1
    assert payload["error"] is None

    remaining_rows = {
        identity: row
        for identity, row in catalog.current_generation().items()
        if identity not in set(payload["proposed_removal"])
    }
    problem, _ = relation_generation_problem(remaining_rows)
    assert problem is not None
