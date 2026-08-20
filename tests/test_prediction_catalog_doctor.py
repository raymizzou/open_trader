"""Issue #99: relation-generation doctor report and confirmed cleanup apply."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.prediction_catalog_doctor import _diagnose_conflict, report
from open_trader.prediction_monitor_selection import relation_generation_problem
from open_trader.relation_catalog import RelationCatalog
from test_relation_catalog import compiled_relation_discovery


def _uncompilable_catalog(tmp_path: Path) -> tuple[RelationCatalog, list[dict[str, str]]]:
    """A four-member generation that does not compile: a 2v1 action conflict on
    ``condition-a`` plus one stale capital release row. Seeded through the v2
    core (``approve`` bypasses the activation gate) to mirror a live-but-broken
    production generation.
    """
    catalog = RelationCatalog(tmp_path)
    payloads = [
        compiled_relation_discovery(
            ["condition-a", "condition-b", "condition-z"],
            {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
        ),
        compiled_relation_discovery(
            ["condition-a", "condition-c", "condition-w"],
            {"condition-a": "BUY_NO", "condition-c": "BUY_YES", "condition-w": "BUY_YES"},
        ),
        compiled_relation_discovery(
            ["condition-a", "condition-d", "condition-v"],
            {"condition-a": "BUY_YES", "condition-d": "BUY_YES", "condition-v": "BUY_YES"},
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


def test_doctor_report_attributes_conflict_and_stale(tmp_path: Path) -> None:
    catalog, seeded = _uncompilable_catalog(tmp_path)
    payload = report(catalog)

    assert payload["compiles"] is True
    assert len(payload["conflicts"]) == 1
    conflict = payload["conflicts"][0]
    assert conflict["key"] == "polymarket:condition-a"
    assert conflict["label"] == "action"
    holders = {holder["identity"]: holder for holder in conflict["holders"]}
    assert set(holders) == {
        seeded[0]["identity"],
        seeded[1]["identity"],
        seeded[2]["identity"],
    }
    assert holders[seeded[0]["identity"]]["side"] == "BUY_YES"
    assert holders[seeded[1]["identity"]]["side"] == "BUY_NO"
    assert holders[seeded[2]["identity"]]["side"] == "BUY_YES"
    assert all(holder["payload"] for holder in conflict["holders"])
    assert conflict["removal"] == [seeded[1]["identity"]]

    assert len(payload["stale"]) == 1
    stale = payload["stale"][0]
    assert stale["merged_as_of"] == "2026-08-15T00:00:00+00:00"
    assert stale["identities"] == [
        {
            "identity": seeded[3]["identity"],
            "as_of": "2026-07-01T00:00:00+00:00",
            "stale_releases": ["2026-08-01T00:00:00+00:00"],
        }
    ]

    assert payload["proposed_removal"] == [seeded[1]["identity"], seeded[3]["identity"]]
    assert payload["remaining"] == 2
    assert payload["error"] is None

    # End to end: after the proposed removal the remainder is non-empty and
    # the compile seam accepts it.
    remaining_rows = {
        identity: row
        for identity, row in catalog.current_generation().items()
        if identity not in set(payload["proposed_removal"])
    }
    assert set(remaining_rows) == {seeded[0]["identity"], seeded[2]["identity"]}
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


def test_doctor_mixed_batch_removes_only_older_row(tmp_path: Path) -> None:
    """Mixed-batch generation removes only rows strictly older than the merged
    as_of; the freshest row survives and the remainder compiles."""
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
    assert len(payload["stale"]) == 1
    assert payload["stale"][0]["identities"] == [
        {
            "identity": seeded[1]["identity"],
            "as_of": "2026-07-01T00:00:00+00:00",
            "stale_releases": ["2026-08-01T00:00:00+00:00"],
        }
    ]
    assert payload["proposed_removal"] == [seeded[1]["identity"]]
    assert payload["remaining"] == 1
    assert payload["error"] is None


def test_doctor_early_as_of_with_later_release_is_fresh(tmp_path: Path) -> None:
    """Row-level staleness mirrors the oracle atom by atom: only a row with
    at least one atom releasing strictly before the merged max as_of enters
    proposed_removal.

    Review repro shape: X has an early ``problem.as_of`` (2026-07-01) but
    every atom releases after the merged as_of (2026-12-31) — the oracle
    judges it fresh and the old row-level ``as_of < merged_as_of`` predicate
    must not propose it. S releases at its own as_of (2026-07-01), both
    before newer row F's as_of (2026-08-15), so S is truly stale and must be
    the only removal; {F, X} then compiles.
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
    assert len(payload["stale"]) == 1
    assert payload["stale"][0]["merged_as_of"] == "2026-08-15T00:00:00+00:00"
    assert payload["stale"][0]["identities"] == [
        {
            "identity": seeded[2]["identity"],
            "as_of": "2026-07-01T00:00:00+00:00",
            "stale_releases": ["2026-07-01T00:00:00+00:00"],
        }
    ]
    assert payload["proposed_removal"] == [seeded[2]["identity"]]
    assert payload["remaining"] == 2
    assert payload["error"] is None

    remaining_rows = {
        identity: row
        for identity, row in catalog.current_generation().items()
        if identity not in set(payload["proposed_removal"])
    }
    assert set(remaining_rows) == {seeded[0]["identity"], seeded[1]["identity"]}
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
    """Review repro: a cause-marked UNKNOWN member U with a later as_of must
    not lift merged_as_of nor enter any proposal.

    U is revoked through the v2 cause ledger (facade ``current_generation()``
    then reports it as UNKNOWN), F is an oracle-fresh ACTIVE row (early as_of,
    every release after the admitted merged as_of) and S is a truly stale
    ACTIVE row. The doctor must propose only S: merged_as_of comes from the
    admitted rows alone (F's 2026-08-15, not U's 2027-01-01), F survives,
    remaining/compiles follow the admitted caliber, and U is counted as
    excluded.
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
    assert len(payload["stale"]) == 1
    assert payload["stale"][0]["merged_as_of"] == "2026-08-15T00:00:00+00:00"
    assert payload["stale"][0]["identities"] == [
        {
            "identity": s["identity"],
            "as_of": "2026-07-01T00:00:00+00:00",
            "stale_releases": ["2026-07-01T00:00:00+00:00"],
        }
    ]
    assert payload["proposed_removal"] == [s["identity"]]
    assert payload["remaining"] == 1
    assert payload["excluded"] == 1
    assert payload["error"] is None

    # Rebuild precheck: the UNKNOWN row must not lift the post-drop precheck.
    # Dropping the truly stale S leaves admitted {F}, which compiles, so the
    # precheck must NOT raise "post-drop generation does not compile"; the
    # activation gate (relation_catalog_v2.replace, out of issue-99 scope)
    # still compiles the remaining U as ACTIVE and refuses the publish.
    before = dict(catalog.current_generation())
    with pytest.raises(ValueError, match="rejected by the activation gate"):
        catalog.rebuild_generation(
            [s["identity"]], actor="op", git_sha="sha", note="doctor apply"
        )
    assert catalog.current_generation() == before

    # Dropping the healthy F (which the fixed doctor never proposes) leaves
    # admitted {S}: the precheck passes and the gate refuses; the precheck is
    # not fooled into claiming the post-drop set does not compile.
    with pytest.raises(ValueError, match="rejected by the activation gate"):
        catalog.rebuild_generation(
            [f["identity"]], actor="op", git_sha="sha", note="doctor apply"
        )
    assert catalog.current_generation() == before


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
    excluded the tie-break keeps the BUY_NO holder and proposes the BUY_YES
    row a1; counting U would instead propose a2 (the review's parity flip).
    """
    a1_payload = compiled_relation_discovery(
        ["condition-a", "condition-b", "condition-z"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
    )
    a2_payload = compiled_relation_discovery(
        ["condition-a", "condition-c", "condition-w"],
        {"condition-a": "BUY_NO", "condition-c": "BUY_YES", "condition-w": "BUY_YES"},
    )
    u_payload = compiled_relation_discovery(
        ["condition-a", "condition-u1", "condition-u2"],
        {"condition-a": "BUY_YES", "condition-u1": "BUY_YES", "condition-u2": "BUY_YES"},
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
        "action 'polymarket:condition-a' conflicts across compiled relations"
        in message
    )

    finding, removal = _diagnose_conflict(rows, message)
    holders = {holder["identity"]: holder for holder in finding["holders"]}
    assert set(holders) == {"a1", "a2"}
    assert removal == ["a1"]  # tie-break: BUY_NO serializes smaller, kept

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
    assert "conflict: action 'polymarket:condition-a'" in out
    assert "stale: " in out
    assert "proposed_removal: 2" in out
    assert "remaining: 2" in out

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
