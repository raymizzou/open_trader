"""Issue #96 relation catalog lifecycle governance tests.

Cases 1-5 cover generation expiry rotation, the intake guard, the bounded
stale-pending exit, and blocked/error approval audit rows. Repair round 1
adds all-or-nothing coverage for the expiry rotation transaction itself.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from open_trader.polymarket_relation_discovery import (
    NativeComplementMarket,
    NativeComplementRelation,
)
from open_trader.prediction_relation_candidates import (
    prepare_mechanical_relation_candidates,
)
from open_trader.relation_catalog import RelationCatalog
from test_relation_catalog import compiled_relation_discovery

EXPIRED_NOW = "2026-09-15T00:00:00Z"
STALE_ACTOR = "lifecycle:expire"


def complement(tag: str) -> NativeComplementRelation:
    """One VENUE_METADATA YES/NO pair tagged so ``event_id`` orders candidates."""
    return NativeComplementRelation(
        event_id=f"{tag}-event",
        market=NativeComplementMarket(
            event_id=f"{tag}-event",
            market_id=f"market-{tag}",
            condition_id=f"condition-{tag}",
            question=f"Will {tag} win?",
            rules="official index",
            resolution_source="Binance",
            end_date="2026-12-31T17:00:00Z",
            yes_token_id=f"yes-{tag}",
            no_token_id=f"no-{tag}",
            rules_hash=f"rules-{tag}",
        ),
    )


def member(
    tag: str,
    *,
    as_of: str = "2026-08-15T00:00:00Z",
    release: str,
) -> dict[str, object]:
    return compiled_relation_discovery(
        [f"{tag}-a", f"{tag}-b"],
        {f"{tag}-a": "BUY_YES", f"{tag}-b": "BUY_YES"},
        as_of=as_of,
        release=release,
        rule=f"rules-{tag}",
    )


def audit_rows(catalog: RelationCatalog) -> list[dict[str, object]]:
    connection = sqlite3.connect(catalog.path)
    try:
        rows = connection.execute(
            "SELECT action, identity, version_id, actor, git_sha, note "
            "FROM catalog_v2_audit ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    keys = ("action", "identity", "version_id", "actor", "git_sha", "note")
    return [dict(zip(keys, row)) for row in rows]


def test_expiry_rotation_drops_past_members_and_publishes_new_generation(
    tmp_path: Path,
) -> None:
    """Case 1: members whose capital_release is past an injected ``now`` leave
    the ACTIVE generation automatically; a new generation is published, each
    dropped member carries an ``expired`` audit row, the future member stays
    ACTIVE, and a rerun is a no-op."""
    catalog = RelationCatalog(tmp_path)
    stale_one = catalog.ingest(member("stale1", release="2026-08-31T20:00:00Z"))["version_id"]
    stale_two = catalog.ingest(member("stale2", release="2026-08-31T20:00:00Z"))["version_id"]
    survivor = catalog.ingest(member("survivor", release="2026-12-31T17:00:00Z"))["version_id"]
    for version_id in (stale_one, stale_two, survivor):
        approved = catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
        assert approved["activation"] == "ACTIVE"

    identity_by_version = {
        version_id: catalog.detail(version_id)["identity"]
        for version_id in (stale_one, stale_two, survivor)
    }
    meta_before = catalog.generation_meta()["generation"]
    result = catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")

    dropped_ids = sorted(item["version_id"] for item in result["dropped"])
    assert dropped_ids == sorted([stale_one, stale_two])
    generation = catalog.current_generation()
    assert identity_by_version[stale_one] not in generation
    assert identity_by_version[stale_two] not in generation
    assert identity_by_version[survivor] in generation
    assert generation[identity_by_version[survivor]]["activation"] == "ACTIVE"
    assert catalog.generation_meta()["generation"] > meta_before

    expired_rows = [row for row in audit_rows(catalog) if row["action"] == "expired"]
    assert {row["version_id"] for row in expired_rows} == {stale_one, stale_two}
    for row in expired_rows:
        assert row["actor"] == STALE_ACTOR
        assert row["git_sha"] == "sha-exp"
        assert row["identity"]

    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[stale_one]["status"] == "EXPIRED"
    assert rows[stale_two]["activation"] == "EXPIRED"
    assert {row["version_id"] for row in catalog.list("history")} >= {stale_one, stale_two}

    noop = catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")
    assert noop["dropped"] == []
    assert [row for row in audit_rows(catalog) if row["action"] == "expired"] == expired_rows
    assert catalog.generation_meta()["generation"] > 0


def test_expiry_rotation_unblocks_the_timeline_deadlocked_candidate(tmp_path: Path) -> None:
    """Case 2 (root cause): with stale members pinning the shared timeline, a
    later-window Tier-1-style candidate is approved into
    ACTIVATION_BLOCKED_INCONSISTENT; after ``expire_stale_members`` the same
    candidate returns to PENDING and approving it publishes it ACTIVE."""
    catalog = RelationCatalog(tmp_path)
    stale_one = catalog.ingest(member("stale1", release="2026-08-31T20:00:00Z"))["version_id"]
    survivor = catalog.ingest(member("survivor", release="2026-12-31T17:00:00Z"))["version_id"]
    for version_id in (stale_one, survivor):
        approved = catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
        assert approved["activation"] == "ACTIVE"
    candidate_id = catalog.ingest(
        member(
            "december",
            as_of="2026-11-01T00:00:00Z",
            release="2026-12-24T17:00:00Z",
        )
    )["version_id"]

    blocked = catalog.approve_many([{"version_id": candidate_id}], actor="op", git_sha="sha")
    assert blocked["results"][0]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    assert blocked["counts"]["blocked"] == 1
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[candidate_id]["status"] == "APPROVED"
    generation_before = dict(catalog.current_generation())

    result = catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")
    assert [item["version_id"] for item in result["dropped"]] == [stale_one]
    assert result["reset_pending"] == [
        {"version_id": candidate_id, "identity": rows[candidate_id]["identity"]}
    ]
    assert dict(catalog.current_generation()) == {
        key: value
        for key, value in generation_before.items()
        if key != rows[stale_one]["identity"]
    }

    republished = catalog.approve(candidate_id, {"version_id": candidate_id}, actor="op", git_sha="sha")
    assert republished["activation"] == "ACTIVE"
    assert rows[candidate_id]["identity"] in catalog.current_generation()


def _drifted(payload: dict[str, object], statement: str) -> dict[str, object]:
    drifted = {**payload, "semantics": {**payload["semantics"], "statement": statement}}
    return drifted


def test_intake_guard_rejects_discoveries_whose_latest_version_is_terminal(
    tmp_path: Path,
) -> None:
    """Case 3: an identity whose ``latest`` version is REVOKED (and separately
    EXPIRED) produces no new PENDING version on ingest — one ``intake-rejected``
    audit row per dropped discovery instead — while a fresh identity still
    ingests normally."""
    catalog = RelationCatalog(tmp_path)
    revoked_payload = member("revoked", release="2026-12-31T17:00:00Z")
    revoked_id = catalog.ingest(revoked_payload)["version_id"]
    catalog.approve(revoked_id, {"version_id": revoked_id}, actor="op", git_sha="sha")
    catalog.revoke(revoked_id, {"version_id": revoked_id}, reason="rules_changed", actor="op", git_sha="sha")

    expired_active = catalog.ingest(member("expired", release="2026-08-31T20:00:00Z"))["version_id"]
    catalog.approve(expired_active, {"version_id": expired_active}, actor="op", git_sha="sha")
    rotation = catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")
    assert [item["version_id"] for item in rotation["dropped"]] == [expired_active]

    before_ids = {row["version_id"] for row in catalog.review_rows()}
    blocked_revoked = catalog.ingest(_drifted(revoked_payload, "re-listed after revoke"))
    blocked_expired = catalog.ingest(_drifted(member("expired", release="2026-08-31T20:00:00Z"), "after-expiry"))
    assert set(blocked_revoked) >= {"created", "version_id", "identity", "status"}
    assert blocked_revoked["created"] is False
    assert blocked_revoked["status"] == "REVOKED"
    assert blocked_expired["created"] is False
    assert blocked_expired["status"] == "EXPIRED"

    fresh = catalog.ingest(member("fresh", release="2026-12-31T17:00:00Z"))["version_id"]

    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert len(rows) == len(before_ids) + 1
    assert set(rows) - before_ids == {fresh}
    assert rows[fresh]["status"] == "PENDING"
    fresh_identity = rows[fresh]["identity"]
    assert fresh_identity != rows[revoked_id]["identity"]

    rejected = [
        row for row in audit_rows(catalog) if row["action"] == "intake-rejected"
    ]
    assert {row["version_id"] for row in rejected} <= {revoked_id, expired_active}
    assert any(row["identity"] == rows[revoked_id]["identity"] for row in rejected)
    assert any(row["identity"] == rows[expired_active]["identity"] for row in rejected)
    assert all(row["git_sha"] == "" for row in rejected)
    assert catalog.pending_count() == sum(
        1 for row in rows.values() if row["status"] == "PENDING"
    )


def test_reject_stale_pending_exits_non_latest_zombies_bounded(
    tmp_path: Path,
) -> None:
    """Case 4: PENDING versions that are no longer ``latest`` for their
    identity flip to REJECTED/``STALE_NON_LATEST`` with an audit row and leave
    the pending view; latest-and-pending rows are untouched; ``limit`` bounds
    each run."""
    catalog = RelationCatalog(tmp_path)
    zombies: list[str] = []
    superseding_latest: list[str] = []
    for tag in ("z1", "z2"):
        base = member(tag, release="2026-12-31T17:00:00Z")
        stale_id = catalog.ingest(_drifted(base, f"{tag} original"))["version_id"]
        latest_id = catalog.ingest(_drifted(base, f"{tag} superseding"))["version_id"]
        assert latest_id != stale_id
        zombies.append(stale_id)
        superseding_latest.append(latest_id)
    keeper = catalog.ingest(member("keeper", release="2026-12-31T17:00:00Z"))["version_id"]
    pending_before = catalog.pending_count()

    result = catalog.reject_stale_pending(actor="lifecycle:stale-exit", git_sha="sha-sr", limit=1)
    assert result["applied"] == 1
    first_version = result["rejected"][0]["version_id"]
    assert first_version in zombies
    assert catalog.pending_count() == pending_before - 1

    finished = catalog.reject_stale_pending(actor="lifecycle:stale-exit", git_sha="sha-sr")
    assert {item["version_id"] for item in finished["rejected"]} == set(zombies) - {first_version}
    assert finished["applied"] == 1

    rows = {row["version_id"]: row for row in catalog.review_rows()}
    for version_id in zombies:
        assert rows[version_id]["status"] == "REJECTED"
        assert rows[version_id]["activation"] == "REJECTED"
        record = catalog._versions()[version_id]
        assert record["activation_diagnostic"] == "STALE_NON_LATEST"
    assert all(
        item.get("activation_diagnostic") == "STALE_NON_LATEST"
        for item in finished["rejected"]
    )
    assert rows[keeper]["status"] == "PENDING"
    assert {row["version_id"] for row in catalog.list("pending_approval")} == (
        {keeper} | set(superseding_latest)
    )

    stale_audits = [row for row in audit_rows(catalog) if row["action"] == "stale-non-latest"]
    assert {row["version_id"] for row in stale_audits} == set(zombies)

    drained = catalog.reject_stale_pending(actor="lifecycle:stale-exit", git_sha="sha-sr")
    assert drained["applied"] == 0
    assert drained["rejected"] == []


def test_approve_many_writes_audit_rows_for_blocked_and_error_outcomes(
    tmp_path: Path,
) -> None:
    """Case 5: an ``approve_many`` batch with at least one blocked item and at
    least one erroring item records an ``approve-blocked`` row and an
    ``approve-error`` row, each carrying the actor and git_sha."""
    catalog = RelationCatalog(tmp_path)
    base = member("pinned", release="2026-12-31T17:00:00Z")
    active_id = catalog.ingest(base)["version_id"]
    catalog.approve(active_id, {"version_id": active_id}, actor="op", git_sha="sha")
    newer_id = catalog.ingest(_drifted(base, "superseding discovery"))["version_id"]
    missing_id = "v-" + "0" * 64

    batch = catalog.approve_many(
        [{"version_id": newer_id}, {"version_id": missing_id}],
        actor="local_operator",
        git_sha="sha-audit",
    )
    assert batch["results"][0]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    assert batch["results"][1]["error"] == "relation version not found"

    audits = {(row["action"], row["version_id"]): row for row in audit_rows(catalog)}
    blocked_row = audits[("approve-blocked", newer_id)]
    error_row = audits[("approve-error", missing_id)]
    for row in (blocked_row, error_row):
        assert row["actor"] == "local_operator"
        assert row["git_sha"] == "sha-audit"
    assert active_id in blocked_row["note"] or "ACTIVATION_BLOCKED" in blocked_row["note"]
    assert "not found" in error_row["note"]
    assert ("approve-blocked", missing_id) not in audits
    assert ("approve-error", newer_id) not in audits


def test_expiry_rotation_is_all_or_nothing_when_the_reset_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair round 1 finding 1: member drops, EXPIRED marks and the
    APPROVED→PENDING reset share ONE transaction, so a write failure after
    the drops are applied rolls all of it back — either the whole rotation
    aborts or nothing persists. The audit rows are intent-first (their own
    connection): a failure there aborts before any mutation and leaves zero
    ``expired`` rows; a mid-data failure leaves no partial state behind and
    a rerun fully recovers."""
    catalog = RelationCatalog(tmp_path)
    stale_one = catalog.ingest(member("stale1", release="2026-08-31T20:00:00Z"))["version_id"]
    survivor = catalog.ingest(member("survivor", release="2026-12-31T17:00:00Z"))["version_id"]
    for version_id in (stale_one, survivor):
        approved = catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
        assert approved["activation"] == "ACTIVE"
    candidate_id = catalog.ingest(
        member(
            "december",
            as_of="2026-11-01T00:00:00Z",
            release="2026-12-24T17:00:00Z",
        )
    )["version_id"]
    blocked = catalog.approve_many([{"version_id": candidate_id}], actor="op", git_sha="sha")
    assert blocked["results"][0]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"

    def identity_of(version_id: str) -> str:
        return str(catalog.detail(version_id)["identity"])

    stale_identity = identity_of(stale_one)
    survivor_identity = identity_of(survivor)
    candidate_identity = identity_of(candidate_id)
    meta_before = catalog.generation_meta()["generation"]

    # Branch A — intent-first audit: an audit-write failure aborts before any
    # data mutation, so no `expired` audit row can be orphaned ahead of data.
    monkeypatch.setattr(
        catalog._store, "write_audit", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("simulated audit outage")
        )
    )
    with pytest.raises(RuntimeError, match="simulated audit outage"):
        catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")
    monkeypatch.undo()
    assert catalog.generation_meta()["generation"] == meta_before
    assert [row for row in audit_rows(catalog) if row["action"] == "expired"] == []

    # Branch B — mid-flight store-write failure: SQLite busy contention at
    # COMMIT (WAL keeps one writer). The rotation holds one composite
    # transaction, so the commit failure rolls back the drops AND the reset
    # together instead of committing expiry while losing the reset.
    monkeypatch.setattr(
        catalog._store,
        "commit_write",
        lambda: (_ for _ in ()).throw(
            RuntimeError("simulated sqlite busy at commit")
        ),
    )
    with pytest.raises(RuntimeError, match="simulated sqlite busy"):
        catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")
    monkeypatch.undo()

    # Either way nothing partial survived: generation unchanged, no EXPIRED
    # marks, and the deadlocked bookkeeping was NOT half-reset. (Live members
    # carry review-state APPROVED with activation ACTIVE.)
    assert catalog.generation_meta()["generation"] == meta_before
    generation = catalog.current_generation()
    assert generation[stale_identity]["activation"] == "ACTIVE"
    assert generation[survivor_identity]["activation"] == "ACTIVE"
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[stale_one]["status"] == "APPROVED"
    assert rows[stale_one]["activation"] == "ACTIVE"
    assert rows[candidate_id]["status"] == "APPROVED"
    assert rows[candidate_id]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"

    # Recovery: once the fault clears, one rotation run does everything.
    result = catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")
    assert [item["version_id"] for item in result["dropped"]] == [stale_one]
    assert result["reset_pending"] == [
        {"version_id": candidate_id, "identity": candidate_identity}
    ]
    republished = catalog.approve(candidate_id, {"version_id": candidate_id}, actor="op", git_sha="sha")
    assert republished["activation"] == "ACTIVE"
    assert candidate_identity in catalog.current_generation()


def test_intake_guarded_rediscovery_does_not_consume_the_mechanical_slot(
    tmp_path: Path,
) -> None:
    """Repair round 2 finding 1: a rediscovered mechanical relation whose
    identity latest is REVOKED is intake-rejected by the catalog guard, so the
    prepare pass must count it skipped without a PREPARED entry — and it must
    not consume the sole ``max_components=1`` slot, so a fresh candidate
    sorting behind it still gets prepared in the same scan."""
    catalog = RelationCatalog(tmp_path)
    guarded = complement("aaa-guarded")
    guarded_id = catalog.ingest_mechanical_relation(guarded)["version_id"]
    approved = catalog.approve(
        guarded_id, {"version_id": guarded_id}, actor="op", git_sha="sha"
    )
    assert approved["activation"] == "ACTIVE"
    catalog.revoke(
        guarded_id,
        {"version_id": guarded_id},
        reason="rules_changed",
        actor="op",
        git_sha="sha",
    )
    fresh = complement("zzz-fresh")
    pending_before = catalog.pending_count()
    assert pending_before == 0

    report = prepare_mechanical_relation_candidates(
        catalog, [guarded, fresh], [], max_components=1
    )

    assert report["status"] == "PREPARED"  # the fresh one was still prepared
    assert report["skipped"] == 1  # the guarded one counted as skipped ...
    assert report["prepared"] == 1  # ... and did NOT consume the only slot
    fresh_identity = catalog.mechanical_relation_identity(fresh)
    assert [item["fingerprint"] for item in report["components"]] == [fresh_identity]
    assert report["components"][0]["event_id"] == "zzz-fresh-event"
    assert guarded_id not in set(report["version_ids"])
    assert len(report["version_ids"]) == 1

    rows = {
        row["version_id"]: row for row in catalog.review_rows()
        if row["status"] == "PENDING"
    }
    assert list(rows) == report["version_ids"]
    assert [row["identity"] for row in rows.values()] == [fresh_identity]
    rows_all = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows_all[guarded_id]["status"] == "REVOKED"
    assert catalog.pending_count() == pending_before + 1


def test_expiry_rotation_drops_only_active_members_not_already_revoked(
    tmp_path: Path,
) -> None:
    """Repair round 3 finding 1: single ``revoke`` marks its cause without
    popping the stored generation, so a REVOKED member whose capital_release
    is past ``now`` would be re-selected as stale and rewritten REVOKED→EXPIRED
    with an ``expired`` audit row attributed to ``lifecycle:expire``. The
    rotation builds its stale list from ACTIVE members only: the still-ACTIVE
    stale member expires normally, while the member the operator terminally
    revoked keeps its recorded history unchanged — same status, no ``expired``
    audit row, not listed in ``dropped``."""
    catalog = RelationCatalog(tmp_path)
    stale_active = catalog.ingest(member("stale1", release="2026-08-31T20:00:00Z"))["version_id"]
    revoked = catalog.ingest(member("revoked", release="2026-08-31T20:00:00Z"))["version_id"]
    for version_id in (stale_active, revoked):
        approved = catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
        assert approved["activation"] == "ACTIVE"
    assert catalog.revoke(
        revoked, {"version_id": revoked}, reason="rules_changed", actor="op", git_sha="sha"
    )["status"] == "REVOKED"
    revoked_identity = str(catalog.detail(revoked)["identity"])
    rows_before = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows_before[revoked]["status"] == "REVOKED"

    result = catalog.expire_stale_members(now=EXPIRED_NOW, actor=STALE_ACTOR, git_sha="sha-exp")

    assert [item["version_id"] for item in result["dropped"]] == [stale_active]
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[stale_active]["status"] == "EXPIRED"
    assert rows[stale_active]["activation"] == "EXPIRED"
    assert rows[revoked]["status"] == "REVOKED"
    assert rows[revoked]["activation"] == "REVOKED"
    expired_rows = [row for row in audit_rows(catalog) if row["action"] == "expired"]
    assert [row["version_id"] for row in expired_rows] == [stale_active]
    assert all(row["identity"] != revoked_identity for row in expired_rows)
