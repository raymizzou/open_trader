"""Issue #60 phase A slice 3 (A6): N_LEG cutover orchestrator tests.

The orchestrator (``scripts/run_nleg_cutover.py``) drives the planned-downtime
window: read-only precheck, snapshot, maintenance route, owner-stop
verification, migration, post verification, evidence assembly, the pre-boundary
restore path, and a dry-run mode that proves the whole pipeline on a replica
while the production bytes stay untouched (the #71 md5 sidecar pattern).

Fixtures are throwaway copies built through the public store/catalog APIs (the
same seam the slice-1 store-level tests use); no test touches a real
production path.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_store import (
    PredictionArbitrageStore,
    read_minimum_reader_generation,
)
from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore
from test_prediction_arbitrage_store import cross_preview_payload
from test_relation_catalog import compiled_problem
from test_relation_catalog_v2 import _endpoint, _payload

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_nleg_cutover.py"
)
_SPEC = importlib.util.spec_from_file_location("run_nleg_cutover", _SCRIPT)
orchestrator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(orchestrator)


# ---------------------------------------------------------------------------
# Copy-based fixtures (the slice-1 store-level test seam, copied verbatim so
# this suite stays self-contained).
# ---------------------------------------------------------------------------


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(_data_dir(tmp_path))


def _db_path(data_dir: Path) -> Path:
    return data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"


def quiesce_sqlite(data_dir: Path) -> None:
    """Checkpoint and flush the WAL so the main file bytes are stable.

    The store keeps thread-local connections; their GC-triggered close would
    otherwise checkpoint the WAL at an arbitrary later moment and race the
    byte-for-byte zero-write assertions (the #71 orchestrator test pattern).
    """

    import sqlite3

    connection = sqlite3.connect(_db_path(data_dir))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _preview_expiry() -> str:
    moment = datetime.now(UTC) + timedelta(seconds=10)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _create_plain_execution(
    store: PredictionArbitrageStore,
    market_id: str,
    total_max_cost: Decimal | None,
) -> str:
    payload: dict[str, object] = {
        "event_id": f"event-{market_id}",
        "market_id": market_id,
        "quantity": "20",
        "yes_max_price": "0.45",
        "no_max_price": "0.48",
    }
    if total_max_cost is not None:
        payload["total_max_cost"] = total_max_cost
    preview_id = store.create_preview(payload, expires_at=_preview_expiry())
    execution = store.consume_preview_and_create_execution(
        preview_id, f"key-{market_id}"
    )
    return str(execution["execution_id"])


def _create_cross_execution(
    store: PredictionArbitrageStore,
    market_id: str,
    total_max_cost: Decimal,
) -> str:
    preview_id = store.create_preview(
        cross_preview_payload(market_id=market_id, total_max_cost=total_max_cost),
        expires_at=_preview_expiry(),
    )
    execution = store.consume_preview_and_create_execution(
        preview_id, f"key-{market_id}"
    )
    return str(execution["execution_id"])


def _catalog_relation(    round_tag: str, tag: str, *, complete: bool
) -> dict[str, object]:
    contracts = [f"t7-{round_tag}-{tag}-a", f"t7-{round_tag}-{tag}-b"]
    payload = _payload(
        relation_type="EXACTLY_ONE",
        endpoints=[
            dict(
                _endpoint(contract_id=contract, event_identity_basis="event-t7")
            )
            for contract in contracts
        ],
    )
    if complete:
        payload["problem"] = compiled_problem(
            contracts,
            {contract: "BUY_YES" for contract in contracts},
            as_of="2026-08-15T00:00:00Z",
            release_at="2026-12-31T17:00:00Z",
            rule={contract: f"rules-{contract}" for contract in contracts},
        )
    else:
        payload["terminal_states"] = []
        payload["payouts"] = {}
        payload["capital_release"] = None
        payload.pop("problem", None)
    return payload


def _force_approved(catalog: RelationCatalogV2, version_id: str) -> str:
    """Approve without publishing: status APPROVED, no generation membership."""

    catalog.store.begin_write()
    record = catalog.store["versions"][version_id]
    record["status"] = "APPROVED"
    catalog.store.commit_write()
    return str(record["identity"])


def _fixture_catalog(
    db_path: Path, round_tag: str
) -> tuple[RelationCatalogV2, list[str], list[str]]:
    catalog = RelationCatalogV2(SqliteCatalogStore(str(db_path)))
    complete_identities: list[str] = []
    incomplete_identities: list[str] = []
    for index in range(3):
        result = catalog.ingest(
            _catalog_relation(round_tag, f"ok{index}", complete=True)
        )
        complete_identities.append(
            _force_approved(catalog, str(result["version_id"]))
        )
    for index in range(2):
        result = catalog.ingest(
            _catalog_relation(round_tag, f"bad{index}", complete=False)
        )
        incomplete_identities.append(
            _force_approved(catalog, str(result["version_id"]))
        )
    return catalog, complete_identities, incomplete_identities


def _manifest_file(tmp_path: Path, manifest: dict[str, str]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _read_report(work_dir: Path, name: str) -> dict[str, object]:
    return json.loads((work_dir / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# precheck
# ---------------------------------------------------------------------------


def _seed_consistent_precheck_fixture(tmp_path: Path) -> dict[str, str]:
    """One priceable holding execution + one old-shape execution covered by
    the manifest + 3 complete / 2 incomplete APPROVED relations."""

    store = _store(tmp_path)
    priced_id = _create_plain_execution(
        store, "market-priced", Decimal("12.34")
    )
    store.transition_execution(
        priced_id, state="holding_to_resolution", evidence={}
    )
    # Old-shape execution: payload carries no total_max_cost, the manifest
    # supplies its explicit price.
    old_shape_id = _create_plain_execution(store, "market-old", None)
    store.transition_execution(
        old_shape_id, state="holding_to_resolution", evidence={}
    )
    # Cross execution with a reserved reservation: priced from the
    # reservation amount exactly as the slice-1 migration prices it.
    cross_id = _create_cross_execution(store, "market-cross", Decimal("1.00"))
    store.transition_execution(
        cross_id, state="holding_to_resolution", evidence={}
    )
    _fixture_catalog(_db_path(_data_dir(tmp_path)), "r1")
    store.set_validation_mode("manual")
    store.set_cross_auto_mode("manual_confirm", "legacy_owner")
    return {
        "old_shape_id": old_shape_id,
        "priced_id": priced_id,
        "cross_id": cross_id,
    }


def test_precheck_consistent_fixture_passes_with_full_report(
    tmp_path: Path,
) -> None:
    ids = _seed_consistent_precheck_fixture(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {ids["old_shape_id"]: "3.00"})

    exit_code = orchestrator.main(
        [
            "precheck",
            "--data-dir",
            str(_data_dir(tmp_path)),
            "--manifest",
            str(manifest),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    report = _read_report(work_dir, "precheck.json")
    assert report["schema_version"] == "open_trader.prediction_cutover.precheck.v1"
    assert report["fence"] == 1
    # Pre-migration the n_leg_controls singleton row does not exist yet (the
    # store synthesizes defaults) — precheck records its absence.
    assert report["n_leg_controls_present"] is False
    # Informational legacy state is recorded, never gated.
    legacy = report["legacy_state"]
    assert legacy["validation_mode"] == "manual"
    assert legacy["cross_auto_state"]["configured_mode"] == "manual_confirm"
    # Catalog preview uses slice-1's completeness predicate: 3 publishable,
    # 2 blocked with the INCOMPLETE_MODEL cause.
    catalog = report["catalog"]
    assert catalog["approved_considered"] == 5
    assert catalog["published_preview"] == 3
    assert catalog["blocked_preview"] == 2
    assert set(catalog["blocked_causes"].values()) == {"INCOMPLETE_MODEL"}
    # All nonterminal executions price-previewed; the old-shape one via the
    # manifest override, the cross one via its reserved reservation.
    priced = {item["execution_id"]: item for item in report["nonterminal_executions"]}
    assert priced[ids["old_shape_id"]]["price_source"] == "manifest"
    assert priced[ids["priced_id"]]["price_source"] == "payload"
    assert priced[ids["cross_id"]]["price_source"] == "reserved_reservation"
    assert priced[ids["old_shape_id"]]["units"] == 3_000_000
    assert priced[ids["priced_id"]]["units"] == 12_340_000
    assert priced[ids["cross_id"]]["units"] == 1_000_000
    assert report["reserved_reservation_count"] == 1
    assert len(priced) == 3
    assert report["problems"] == {
        "unpriceable": [],
        "manifest_mismatch": [],
        "unexplained": [],
    }


def test_precheck_unpriceable_nonterminal_execution_exits_2(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    unpriced_id = _create_plain_execution(store, "market-unpriced", None)
    store.transition_execution(
        unpriced_id, state="holding_to_resolution", evidence={}
    )
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})

    exit_code = orchestrator.main(
        [
            "precheck",
            "--data-dir",
            str(_data_dir(tmp_path)),
            "--manifest",
            str(manifest),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    report = _read_report(work_dir, "precheck.json")
    unpriceable = report["problems"]["unpriceable"]
    assert [item["execution_id"] for item in unpriceable] == [unpriced_id]
    assert unpriceable[0]["state"] == "holding_to_resolution"


def test_precheck_manifest_mismatch_exits_2_listing_every_offender(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settled_id = _create_plain_execution(store, "market-settled", Decimal("1.00"))
    store.transition_execution(settled_id, state="both_rejected", evidence={})
    _create_plain_execution(store, "market-live", Decimal("2.00"))
    work_dir = tmp_path / "work"
    manifest = _manifest_file(
        tmp_path,
        {"execution:missing": "5.00", settled_id: "5.00"},
    )

    exit_code = orchestrator.main(
        [
            "precheck",
            "--data-dir",
            str(_data_dir(tmp_path)),
            "--manifest",
            str(manifest),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    report = _read_report(work_dir, "precheck.json")
    mismatches = report["problems"]["manifest_mismatch"]
    assert {item["execution_id"] for item in mismatches} == {
        "execution:missing",
        settled_id,
    }
    reasons = {item["execution_id"]: item["reason"] for item in mismatches}
    assert "missing" in reasons["execution:missing"]
    assert "settled" in reasons[settled_id]


# ---------------------------------------------------------------------------
# snapshot + restore
# ---------------------------------------------------------------------------


def _seed_simple_fixture(tmp_path: Path) -> str:
    store = _store(tmp_path)
    execution_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(
        execution_id, state="holding_to_resolution", evidence={}
    )
    quiesce_sqlite(_data_dir(tmp_path))
    return execution_id


def test_snapshot_restore_round_trip_returns_identical_md5_and_fence(
    tmp_path: Path,
) -> None:
    _seed_simple_fixture(tmp_path)
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"

    snapshot_code = orchestrator.main(
        [
            "snapshot",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
        ]
    )
    assert snapshot_code == 0
    report = _read_report(work_dir, "snapshot-report.json")
    assert report["schema_version"] == "open_trader.prediction_cutover.snapshot.v1"
    assert report["fence_source"] == 1
    assert report["fence_snapshot"] == 1
    snapshot_db = Path(report["snapshot_db"])
    assert snapshot_db.is_file()
    snapshot_md5 = report["md5"]["db"]
    assert snapshot_md5
    assert report["sha256"]["set"]

    # The target drifts after the snapshot (a further execution is created
    # through the public store API while the fence stays pre-migration), then
    # the whole-file restore returns it — no force needed below the boundary.
    store = _store(tmp_path)
    drifted_id = _create_plain_execution(store, "market-drift", Decimal("5.00"))
    store.transition_execution(
        drifted_id, state="holding_to_resolution", evidence={}
    )
    assert read_minimum_reader_generation(data_dir) == 1
    assert orchestrator.md5sum(_db_path(data_dir)) != snapshot_md5

    restore_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
        ]
    )
    assert restore_code == 0
    restore_report = _read_report(work_dir, "restore-report.json")
    assert restore_report["restored_md5"] == snapshot_md5
    assert restore_report["fence_after_restore"] == 1
    assert orchestrator.md5sum(snapshot_db) == orchestrator.md5sum(
        _db_path(data_dir)
    )
    assert read_minimum_reader_generation(data_dir) == 1


def test_restore_refuses_at_fence_two_without_force_acknowledgment(
    tmp_path: Path,
) -> None:
    _seed_simple_fixture(tmp_path)
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"
    assert (
        orchestrator.main(
            ["snapshot", "--data-dir", str(data_dir), "--work-dir", str(work_dir)]
        )
        == 0
    )
    store = _store(tmp_path)
    store.advance_minimum_reader_generation(2)
    quiesce_sqlite(data_dir)
    target = _db_path(data_dir)
    drifted = orchestrator.md5sum(target)

    exit_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    report = _read_report(work_dir, "restore-report.json")
    assert "fence" in str(report["error"]).lower()
    # Refusal must be total: zero DB writes.
    assert orchestrator.md5sum(target) == drifted
    assert read_minimum_reader_generation(data_dir) == 2

    # --force without the exact acknowledgment string is still refused.
    exit_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
            "--force",
            "--force-acknowledgment",
            "trust me",
        ]
    )
    assert exit_code == 2
    assert orchestrator.md5sum(target) == drifted

    # The exact acknowledgment string unlocks the pre-boundary override.
    exit_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
            "--force",
            "--force-acknowledgment",
            orchestrator.RESTORE_FORCE_ACKNOWLEDGMENT,
        ]
    )
    assert exit_code == 0
    assert read_minimum_reader_generation(data_dir) == 1


def test_restore_production_with_held_runtime_lock_exits_2_with_zero_mutations(
    tmp_path: Path,
) -> None:
    """--production restore promises the same owner-stopped guard as migrate.

    With the runtime ownership lock HELD, the restore must refuse (exit 2)
    BEFORE touching the target: the main file and its WAL siblings stay
    byte-identical and the failure text names the guard.
    """

    import fcntl
    import sqlite3

    runtime_root = tmp_path / "runtime"
    data_dir = runtime_root / "data"
    store = PredictionArbitrageStore(data_dir)
    execution_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(
        execution_id, state="holding_to_resolution", evidence={}
    )
    quiesce_sqlite(data_dir)
    work_dir = tmp_path / "work"
    assert (
        orchestrator.main(
            ["snapshot", "--data-dir", str(data_dir), "--work-dir", str(work_dir)]
        )
        == 0
    )
    # Uncheckpointed WAL frames beside the target: exactly what a live owner
    # leaves behind and what the guard must protect.
    writer = sqlite3.connect(_db_path(data_dir))
    writer.execute("CREATE TABLE wal_sentinel (value TEXT NOT NULL)")
    writer.execute("INSERT INTO wal_sentinel VALUES ('uncheckpointed')")
    writer.commit()
    from open_trader.prediction_release import write_prediction_runtime_record

    write_prediction_runtime_record(
        runtime_root / "prediction-service-runtime.json",
        {"state": "maintenance"},
    )
    lock_path = runtime_root / "data" / "prediction_arbitrage" / "runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        target = _db_path(data_dir)
        target_md5_before = orchestrator.md5sum(target)
        wal = target.with_name(target.name + "-wal")
        assert wal.is_file()
        wal_md5_before = orchestrator.md5sum(wal)

        exit_code = orchestrator.main(
            [
                "restore",
                "--data-dir",
                str(data_dir),
                "--production",
                "--launchctl-bin",
                str(_fake_launchctl(tmp_path, present=False)),
                "--work-dir",
                str(work_dir),
            ]
        )

        assert exit_code == 2
        report = _read_report(work_dir, "restore-report.json")
        assert "guard" in str(report["error"]).lower()
        assert "lock" in json.dumps(report["guard"]["failures"]).lower()
        # Zero mutations: the target set is byte-identical.
        assert orchestrator.md5sum(target) == target_md5_before
        assert orchestrator.md5sum(wal) == wal_md5_before
        assert read_minimum_reader_generation(data_dir) == 1
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
        writer.close()


def test_restore_production_with_guard_satisfied_restores_pre_migration_fence(
    tmp_path: Path,
) -> None:
    """With the guard satisfied (labels absent, lock free, record not ready),
    the --production restore proceeds: the forced whole-file restore returns
    the boundary-crossed target to the snapshot's pre-migration fence."""

    runtime_root = tmp_path / "runtime"
    data_dir = runtime_root / "data"
    store = PredictionArbitrageStore(data_dir)
    execution_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(
        execution_id, state="holding_to_resolution", evidence={}
    )
    quiesce_sqlite(data_dir)
    work_dir = tmp_path / "work"
    assert (
        orchestrator.main(
            ["snapshot", "--data-dir", str(data_dir), "--work-dir", str(work_dir)]
        )
        == 0
    )
    snapshot_report = _read_report(work_dir, "snapshot-report.json")
    # The target crosses the rollback boundary (the forced-restore path the
    # guard protects), and the runtime owners are verifiably gone.
    store.advance_minimum_reader_generation(2)
    quiesce_sqlite(data_dir)
    from open_trader.prediction_release import write_prediction_runtime_record

    write_prediction_runtime_record(
        runtime_root / "prediction-service-runtime.json",
        {"state": "stopped"},
    )

    exit_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--production",
            "--force",
            "--force-acknowledgment",
            orchestrator.RESTORE_FORCE_ACKNOWLEDGMENT,
            "--launchctl-bin",
            str(_fake_launchctl(tmp_path, present=False)),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    report = _read_report(work_dir, "restore-report.json")
    assert report["guard"]["passed"] is True
    assert report["restored_md5"] == snapshot_report["md5"]["db"]
    assert snapshot_report["fence_source"] == 1
    assert report["fence_after_restore"] == 1
    assert read_minimum_reader_generation(data_dir) == 1


def test_restore_refuses_runtime_root_data_dir_without_production_flag(
    tmp_path: Path,
) -> None:
    """Issue #60 final-review P1-2, restore side: a runtime-root data dir
    (release-checkout topology) must be refused without ``--production``, and
    the refusal must leave the target byte-identical."""

    runtime_root, data_dir = _seed_release_runtime_root(tmp_path)
    work_dir = tmp_path / "work"
    assert (
        orchestrator.main(
            ["snapshot", "--data-dir", str(data_dir), "--work-dir", str(work_dir)]
        )
        == 0
    )
    # Diverge the target from the snapshot: if the (buggy) restore ran, the
    # marker row would be wiped by the snapshot bytes; the refusal must
    # preserve it.
    marker_store = PredictionArbitrageStore(data_dir)
    marker_store.set_validation_mode("manual")
    del marker_store
    quiesce_sqlite(data_dir)
    target = _db_path(data_dir)
    diverged = orchestrator.md5sum(target)
    snapshot_db_md5 = _read_report(work_dir, "snapshot-report.json")["md5"]["db"]
    assert diverged != snapshot_db_md5

    exit_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    report = _read_report(work_dir, "restore-report.json")
    error = str(report["error"])
    assert "production" in error.lower()
    assert "--production" in error
    assert "--runtime-root" in error
    # Zero mutations: the diverged target bytes are untouched.
    assert orchestrator.md5sum(target) == diverged
    assert read_minimum_reader_generation(data_dir) == 1


def test_restore_production_runtime_root_with_guard_satisfied_proceeds(
    tmp_path: Path,
) -> None:
    """The same runtime-root topology with ``--production`` and the
    owner-stopped guard satisfied proceeds: the pre-boundary whole-file
    restore puts the snapshot bytes back over the target."""

    runtime_root, data_dir = _seed_release_runtime_root(tmp_path)
    work_dir = tmp_path / "work"
    assert (
        orchestrator.main(
            ["snapshot", "--data-dir", str(data_dir), "--work-dir", str(work_dir)]
        )
        == 0
    )
    snapshot_report = _read_report(work_dir, "snapshot-report.json")

    exit_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--production",
            "--runtime-root",
            str(runtime_root),
            "--launchctl-bin",
            str(_fake_launchctl(tmp_path, present=False)),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    report = _read_report(work_dir, "restore-report.json")
    assert report["guard"]["passed"] is True
    assert report["restored_md5"] == snapshot_report["md5"]["db"]
    assert read_minimum_reader_generation(data_dir) == 1


def test_restore_copy_failure_preserves_target_wal_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never a partial restore: a mid-copy failure exits 2 but the target's
    uncheckpointed WAL sibling still exists, byte-unchanged (the siblings may
    only be removed once the snapshot copy has fully succeeded)."""

    import shutil
    import sqlite3

    _seed_simple_fixture(tmp_path)
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"
    assert (
        orchestrator.main(
            ["snapshot", "--data-dir", str(data_dir), "--work-dir", str(work_dir)]
        )
        == 0
    )
    # Uncheckpointed WAL frames beside the target: a mid-copy failure must
    # never destroy these committed-but-uncheckpointed frames.
    writer = sqlite3.connect(_db_path(data_dir))
    writer.execute("CREATE TABLE wal_sentinel (value TEXT NOT NULL)")
    writer.execute("INSERT INTO wal_sentinel VALUES ('uncheckpointed')")
    writer.commit()
    target = _db_path(data_dir)
    wal = target.with_name(target.name + "-wal")
    assert wal.is_file()
    target_md5_before = orchestrator.md5sum(target)
    wal_md5_before = orchestrator.md5sum(wal)

    def _fail_mid_copy(source, destination, *args, **kwargs):
        destination.write(source.read(16))
        raise OSError("injected mid-copy failure")

    monkeypatch.setattr(shutil, "copyfileobj", _fail_mid_copy)
    exit_code = orchestrator.main(
        [
            "restore",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    error = _read_report(work_dir, "error.json")
    assert "mid-copy" in str(error["error"])
    # The target set is byte-unchanged: the WAL sibling survives the failure.
    assert orchestrator.md5sum(target) == target_md5_before
    assert wal.is_file()
    assert orchestrator.md5sum(wal) == wal_md5_before
    assert read_minimum_reader_generation(data_dir) == 1
    writer.close()


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------

GIT_SHA = "a" * 40


def test_migrate_copy_mode_migrates_fixture_and_closes_accounting(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(
        plain_id, state="holding_to_resolution", evidence={}
    )
    _fixture_catalog(_db_path(_data_dir(tmp_path)), "r1")
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})

    exit_code = orchestrator.main(
        [
            "migrate",
            "--data-dir",
            str(data_dir),
            "--manifest",
            str(manifest),
            "--git-sha",
            GIT_SHA,
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    report = _read_report(work_dir, "migrate-result.json")
    assert report["schema_version"] == "open_trader.prediction_cutover.migrate.v1"
    assert report["fence_before"] == 1
    assert report["fence_after"] == 2
    assert report["migration"]["outcome"] == "migrated"
    assert report["migration"]["total_unsettled_capital_units"] == 12_340_000
    assert report["migration"]["priced_execution_count"] == 1
    assert report["migration"]["audit_event_id"]
    # Source states come from the migration audit row (slice 1 records them
    # there; the store-level result does not carry them).
    assert report["source_states"] == {"holding_to_resolution": 1}
    assert report["activation"]["published"] == 3
    assert report["activation"]["blocked"] == 2
    assert set(report["activation"]["blocked_causes"].values()) == {
        "INCOMPLETE_MODEL"
    }
    assert report["actor"] == "n_leg_cutover"
    assert report["git_sha"] == GIT_SHA
    # The migrated store state is consistent: fence 2, controls initialized.
    control = _store(tmp_path).n_leg_control()
    assert control["mode"] == "MANUAL"
    assert control["contract_generation"] == 2
    assert control["total_unsettled_capital_units"] == 12_340_000
    # Post-verify's accounting: ACTIVE relations == published count.  The
    # activate_many publish path records ACTIVE as generation membership, so
    # the ACTIVE reader replays the persisted generation (mode=ro), matching
    # the store's own decode.
    active = orchestrator.read_active_identities(_db_path(data_dir))
    assert len(active) == report["activation"]["published"] == 3


def test_migrate_refuses_production_path_without_production_flag(
    tmp_path: Path,
) -> None:
    production_data_dir = _SCRIPT.parents[1] / "data"
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})
    production_db = (
        production_data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    before = orchestrator.md5sum(production_db) if production_db.is_file() else None

    exit_code = orchestrator.main(
        [
            "migrate",
            "--data-dir",
            str(production_data_dir),
            "--manifest",
            str(manifest),
            "--git-sha",
            GIT_SHA,
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    report = _read_report(work_dir, "migrate-result.json")
    assert "production" in str(report["error"]).lower()
    assert before is None or orchestrator.md5sum(production_db) == before


def test_migrate_production_with_held_runtime_lock_exits_2_with_zero_writes(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    data_dir = runtime_root / "data"
    store = PredictionArbitrageStore(data_dir)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(
        plain_id, state="holding_to_resolution", evidence={}
    )
    quiesce_sqlite(data_dir)
    from open_trader.prediction_release import write_prediction_runtime_record

    write_prediction_runtime_record(
        runtime_root / "prediction-service-runtime.json",
        {"state": "maintenance"},
    )
    lock_path = runtime_root / "data" / "prediction_arbitrage" / "runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    lock_handle = lock_path.open("a+")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        work_dir = tmp_path / "work"
        manifest = _manifest_file(tmp_path, {})
        target = _db_path(data_dir)
        before = orchestrator.md5sum(target)

        exit_code = orchestrator.main(
            [
                "migrate",
                "--data-dir",
                str(data_dir),
                "--manifest",
                str(manifest),
                "--git-sha",
                GIT_SHA,
                "--production",
                "--work-dir",
                str(work_dir),
            ]
        )

        assert exit_code == 2
        report = _read_report(work_dir, "migrate-result.json")
        assert "lock" in json.dumps(report["guard"]["failures"]).lower()
        # Zero DB writes: the guard refused before the store was opened.
        assert orchestrator.md5sum(target) == before
        assert read_minimum_reader_generation(data_dir) == 1
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def _seed_release_runtime_root(tmp_path: Path) -> tuple[Path, Path]:
    """The documented production topology OUTSIDE this checkout.

    ``install_prediction_service_launchd.sh`` lays out the runtime root as
    ``DATA_DIR=$RUNTIME_ROOT/data`` plus the record
    ``$RUNTIME_ROOT/prediction-service-runtime.json``. An operator running
    this script from a release checkout therefore points ``--data-dir`` at a
    dir that is NOT the checkout's own ``data/`` — the topology the P1-2
    production detection must recognise (pytest tmp_path is never under the
    repository).
    """

    from open_trader.prediction_release import write_prediction_runtime_record

    runtime_root = tmp_path / "runtime"
    data_dir = runtime_root / "data"
    store = PredictionArbitrageStore(data_dir)
    execution_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(
        execution_id, state="holding_to_resolution", evidence={}
    )
    quiesce_sqlite(data_dir)
    write_prediction_runtime_record(
        runtime_root / "prediction-service-runtime.json",
        {"state": "stopped"},
    )
    return runtime_root, data_dir


def test_migrate_refuses_runtime_root_data_dir_without_production_flag(
    tmp_path: Path,
) -> None:
    """Issue #60 final-review P1-2: production is the RUNTIME ROOT, not the
    script checkout. A data dir beside ``prediction-service-runtime.json``
    must be refused without ``--production`` even when the tool runs from a
    release checkout, and a refusal must write nothing."""

    runtime_root, data_dir = _seed_release_runtime_root(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})
    target = _db_path(data_dir)
    before = orchestrator.md5sum(target)

    exit_code = orchestrator.main(
        [
            "migrate",
            "--data-dir",
            str(data_dir),
            "--manifest",
            str(manifest),
            "--git-sha",
            GIT_SHA,
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    report = _read_report(work_dir, "migrate-result.json")
    error = str(report["error"])
    assert "production" in error.lower()
    assert "--production" in error
    assert "--runtime-root" in error
    # Zero writes: the refusal happens before the store is opened.
    assert orchestrator.md5sum(target) == before
    assert read_minimum_reader_generation(data_dir) == 1


def test_migrate_production_runtime_root_with_guard_satisfied_proceeds(
    tmp_path: Path,
) -> None:
    """The same runtime-root topology with ``--production`` and the
    owner-stopped guard satisfied (record stopped, labels absent, lock free)
    proceeds: the release-checkout operator path stays open."""

    runtime_root, data_dir = _seed_release_runtime_root(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})

    exit_code = orchestrator.main(
        [
            "migrate",
            "--data-dir",
            str(data_dir),
            "--manifest",
            str(manifest),
            "--git-sha",
            GIT_SHA,
            "--production",
            "--runtime-root",
            str(runtime_root),
            "--launchctl-bin",
            str(_fake_launchctl(tmp_path, present=False)),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    report = _read_report(work_dir, "migrate-result.json")
    assert report["guard"]["passed"] is True
    assert read_minimum_reader_generation(data_dir) == 2


# ---------------------------------------------------------------------------
# maintenance route
# ---------------------------------------------------------------------------


def _route_file(tmp_path: Path, payload: dict[str, object] | None) -> Path:
    route_file = tmp_path / "config" / "prediction-route.json"
    route_file.parent.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        route_file.write_text(json.dumps(payload), encoding="utf-8")
    return route_file


def test_maintenance_writes_route_record_and_timestamped_backup(
    tmp_path: Path,
) -> None:
    previous = {
        "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
        "mode": "service",
        "operation_id": "op-old",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    route_file = _route_file(tmp_path, previous)
    work_dir = tmp_path / "work"

    exit_code = orchestrator.main(
        [
            "maintenance",
            "--route-file",
            str(route_file),
            "--mode",
            "maintenance",
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    record = json.loads(route_file.read_text(encoding="utf-8"))
    assert record["schema_version"] == (
        "open_trader.frontend_gateway.prediction_route.v1"
    )
    assert record["mode"] == "maintenance"
    assert record["operation_id"]
    assert record["updated_at"]
    backups = list(route_file.parent.glob(route_file.name + ".backup-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == previous
    report = _read_report(work_dir, "maintenance-report.json")
    assert report["mode"] == "maintenance"
    assert report["previous_mode"] == "service"
    assert Path(report["backup_path"]) == backups[0]

    # Flipping back to service keeps a second timestamped backup.
    exit_code = orchestrator.main(
        [
            "maintenance",
            "--route-file",
            str(route_file),
            "--mode",
            "service",
            "--work-dir",
            str(work_dir),
        ]
    )
    assert exit_code == 0
    record = json.loads(route_file.read_text(encoding="utf-8"))
    assert record["mode"] == "service"
    assert len(list(route_file.parent.glob(route_file.name + ".backup-*"))) == 2


def test_maintenance_refuses_invalid_mode(tmp_path: Path) -> None:
    route_file = _route_file(tmp_path, None)

    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main(
            [
                "maintenance",
                "--route-file",
                str(route_file),
                "--mode",
                "legacy",
                "--work-dir",
                str(tmp_path / "work"),
            ]
        )

    assert excinfo.value.code == 2
    # Nothing was written to the route file.
    assert not route_file.exists()


# ---------------------------------------------------------------------------
# stop-verify + post-verify
# ---------------------------------------------------------------------------


def _fake_launchctl(tmp_path: Path, *, present: bool) -> Path:
    bin_dir = tmp_path / "launchctl-fake"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "launchctl"
    if present:
        body = (
            "#!/bin/sh\n"
            "printf 'PID\\tStatus\\tLabel\\n'\n"
            "printf '4242\\t0\\tcom.open-trader.prediction-service\\n'\n"
            "exit 0\n"
        )
    else:
        body = "#!/bin/sh\necho 'Could not find service' >&2\nexit 3\n"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _stop_verify(
    tmp_path: Path, runtime_root: Path, launchctl: Path
) -> tuple[int, Path]:
    work_dir = tmp_path / f"stop-verify-{abs(hash(str(runtime_root)))}"
    code = orchestrator.main(
        [
            "stop-verify",
            "--runtime-root",
            str(runtime_root),
            "--launchctl-bin",
            str(launchctl),
            "--work-dir",
            str(work_dir),
        ]
    )
    return code, work_dir


def test_stop_verify_passes_on_stopped_fixture_and_records_everything(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    (runtime_root / "data" / "prediction_arbitrage").mkdir(parents=True)
    from open_trader.prediction_release import write_prediction_runtime_record

    write_prediction_runtime_record(
        runtime_root / "prediction-service-runtime.json",
        {"state": "stopped"},
    )

    exit_code, work_dir = _stop_verify(
        tmp_path, runtime_root, _fake_launchctl(tmp_path, present=False)
    )

    assert exit_code == 0
    report = _read_report(work_dir, "stop-verify-report.json")
    assert report["labels"][orchestrator.SERVICE_LABEL]["state"] == "absent"
    assert report["labels"][orchestrator.HEALTH_LABEL]["state"] == "absent"
    assert report["lock"]["free"] is True
    assert report["runtime_record"]["state"] == "stopped"
    assert report["operator_bootout_commands"]


def test_stop_verify_fails_on_loaded_label(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    (runtime_root / "data" / "prediction_arbitrage").mkdir(parents=True)

    exit_code, work_dir = _stop_verify(
        tmp_path, runtime_root, _fake_launchctl(tmp_path, present=True)
    )

    assert exit_code == 2
    report = _read_report(work_dir, "stop-verify-report.json")
    service = report["labels"][orchestrator.SERVICE_LABEL]
    assert service["state"] == "present"
    assert service["pid"] == "4242"


def test_stop_verify_fails_on_held_lock(tmp_path: Path) -> None:
    import fcntl

    runtime_root = tmp_path / "runtime"
    lock_path = runtime_root / "data" / "prediction_arbitrage" / "runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        exit_code, work_dir = _stop_verify(
            tmp_path, runtime_root, _fake_launchctl(tmp_path, present=False)
        )
        assert exit_code == 2
        report = _read_report(work_dir, "stop-verify-report.json")
        assert report["lock"]["free"] is False
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_post_verify_passes_on_migrated_fixture(tmp_path: Path) -> None:
    store = _store(tmp_path)
    execution_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(
        execution_id, state="holding_to_resolution", evidence={}
    )
    _fixture_catalog(_db_path(_data_dir(tmp_path)), "r1")
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})
    assert (
        orchestrator.main(
            [
                "migrate",
                "--data-dir",
                str(data_dir),
                "--manifest",
                str(manifest),
                "--git-sha",
                GIT_SHA,
                "--work-dir",
                str(work_dir),
            ]
        )
        == 0
    )

    exit_code = orchestrator.main(
        [
            "post-verify",
            "--data-dir",
            str(data_dir),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    report = _read_report(work_dir, "post-verify-report.json")
    assert report["fence"] == 2
    assert report["controls"]["mode"] == "MANUAL"
    assert report["controls"]["contract_generation"] == 2
    assert report["scope_capability"] == "OBSERVE_ONLY"
    assert report["active_count"] == report["published_from_migrate"] == 3


def test_post_verify_fails_before_migration(tmp_path: Path) -> None:
    _seed_simple_fixture(tmp_path)
    work_dir = tmp_path / "work"

    exit_code = orchestrator.main(
        [
            "post-verify",
            "--data-dir",
            str(_data_dir(tmp_path)),
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 2
    report = _read_report(work_dir, "post-verify-report.json")
    assert report["fence"] == 1
    assert report["failures"]


def test_post_verify_url_probes_owner_state_and_legacy_removal(
    tmp_path: Path,
) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    store = _store(tmp_path)
    execution_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(
        execution_id, state="holding_to_resolution", evidence={}
    )
    _fixture_catalog(_db_path(_data_dir(tmp_path)), "r1")
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})
    assert (
        orchestrator.main(
            [
                "migrate",
                "--data-dir",
                str(data_dir),
                "--manifest",
                str(manifest),
                "--git-sha",
                GIT_SHA,
                "--work-dir",
                str(work_dir),
            ]
        )
        == 0
    )

    class _FakeService(BaseHTTPRequestHandler):
        healthy = True

        def _send(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/healthz" and self.healthy:
                self._send(
                    200,
                    {
                        "mode": "production",
                        "production_owner": True,
                        "git_sha": GIT_SHA,
                    },
                )
            elif self.path == "/api/prediction-arbitrage/state" and self.healthy:
                self._send(200, {"n_leg": {"contract_generation": 2}})
            else:
                self._send(503, {"error": "unavailable"})

        def do_POST(self) -> None:
            self._send(
                410, {"error_code": "legacy_strategy_removed"}
            )

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _FakeService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        exit_code = orchestrator.main(
            [
                "post-verify",
                "--data-dir",
                str(data_dir),
                "--url",
                url,
                "--expected-git-sha",
                GIT_SHA,
                "--work-dir",
                str(work_dir),
            ]
        )
        assert exit_code == 0
        report = _read_report(work_dir, "post-verify-report.json")
        probes = report["url_probes"]
        assert probes["healthz"]["status"] == 200
        assert probes["healthz"]["mode"] == "production"
        assert probes["healthz"]["production_owner"] is True
        assert probes["state"]["n_leg_contract_generation"] == 2
        assert probes["legacy_post"]["status"] == 410
        assert probes["legacy_post"]["error_code"] == "legacy_strategy_removed"

        # An unhealthy owner probe is an unexplained post-cutover state.
        _FakeService.healthy = False
        exit_code = orchestrator.main(
            [
                "post-verify",
                "--data-dir",
                str(data_dir),
                "--url",
                url,
                "--expected-git-sha",
                GIT_SHA,
                "--work-dir",
                str(work_dir),
            ]
        )
        assert exit_code == 2
    finally:
        server.shutdown()
        server.server_close()


def test_post_verify_url_410_probe_passes_on_real_retired_prediction_service(
    tmp_path: Path,
) -> None:
    """Issue #60 final-review P1-1: ``post-verify --url`` must pass against a
    REAL ``create_prediction_server`` in production mode at
    ``legacy_retired=True``. The live probe carries no session cookie, so the
    service must answer the legacy POST with 410 ``legacy_strategy_removed``
    BEFORE production auth (the reviewer-reproduced 403 ordering bug made
    this probe exit 2 against a healthy retired service)."""

    import threading

    from open_trader.prediction_service import create_prediction_server
    from tests.test_prediction_legacy_retirement import _RetiredHttpRuntime

    # The migrated fence-2 fixture (same seam as the fake-service probe test).
    store = _store(tmp_path)
    execution_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(
        execution_id, state="holding_to_resolution", evidence={}
    )
    _fixture_catalog(_db_path(_data_dir(tmp_path)), "r1")
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})
    assert (
        orchestrator.main(
            [
                "migrate",
                "--data-dir",
                str(data_dir),
                "--manifest",
                str(manifest),
                "--git-sha",
                GIT_SHA,
                "--work-dir",
                str(work_dir),
            ]
        )
        == 0
    )

    # Real HTTP layer (create_prediction_server); the runtime is the standard
    # production-mode retired fixture of the legacy-retirement suite.
    server = create_prediction_server(
        runtime=_RetiredHttpRuntime(legacy_retired=True),  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": GIT_SHA},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}"
        exit_code = orchestrator.main(
            [
                "post-verify",
                "--data-dir",
                str(data_dir),
                "--url",
                url,
                "--expected-git-sha",
                GIT_SHA,
                "--work-dir",
                str(work_dir),
            ]
        )

        assert exit_code == 0
        report = _read_report(work_dir, "post-verify-report.json")
        probes = report["url_probes"]
        assert probes["healthz"]["status"] == 200
        assert probes["healthz"]["mode"] == "production"
        assert probes["state"]["n_leg_contract_generation"] == 2
        assert probes["legacy_post"]["status"] == 410
        assert probes["legacy_post"]["error_code"] == "legacy_strategy_removed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_dry_run_full_pipeline_on_replica_proves_zero_production_writes(
    tmp_path: Path,
) -> None:
    production_data_dir = tmp_path / "prod" / "data"
    store = PredictionArbitrageStore(production_data_dir)
    priced_id = _create_plain_execution(store, "market-priced", Decimal("12.34"))
    store.transition_execution(
        priced_id, state="holding_to_resolution", evidence={}
    )
    old_shape_id = _create_plain_execution(store, "market-old", None)
    store.transition_execution(
        old_shape_id, state="holding_to_resolution", evidence={}
    )
    _fixture_catalog(_db_path(production_data_dir), "r1")
    quiesce_sqlite(production_data_dir)
    manifest = _manifest_file(tmp_path, {old_shape_id: "3.00"})
    production_db = _db_path(production_data_dir)
    md5_before = orchestrator.md5sum(production_db)
    work_dir = tmp_path / "work"

    exit_code = orchestrator.main(
        [
            "dry-run",
            "--production-data-dir",
            str(production_data_dir),
            "--manifest",
            str(manifest),
            "--git-sha",
            GIT_SHA,
            "--work-dir",
            str(work_dir),
        ]
    )

    assert exit_code == 0
    report = _read_report(work_dir, "dry-run-report.json")
    assert report["schema_version"] == "open_trader.prediction_cutover.dry_run.v1"
    for step in ("precheck", "snapshot", "migrate", "post_verify", "restore"):
        assert report["steps"][step]["ok"] is True, report["steps"]
    round_trip = report["round_trip"]
    assert round_trip["md5_matches_snapshot"] is True
    assert round_trip["fence"] == 1
    # The #71 proof pattern: the production md5 sidecar is byte-identical
    # before/after and always produced.
    sidecar = _read_report(work_dir, "dry-run-report.production-checksum.json")
    assert sidecar["md5_before"] == md5_before
    assert sidecar["md5_after"] == md5_before
    assert sidecar["zero_production_write"] is True
    assert sidecar["failure"] is None
    # The replica went to fence 2 and back; production never moved.
    assert orchestrator.read_minimum_reader_generation(production_data_dir) == 1


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _prepare_evidence_fixture(tmp_path: Path) -> Path:
    """Run snapshot -> migrate -> stop-verify -> post-verify into one work dir."""

    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(
        plain_id, state="holding_to_resolution", evidence={}
    )
    _fixture_catalog(_db_path(_data_dir(tmp_path)), "r1")
    data_dir = _data_dir(tmp_path)
    work_dir = tmp_path / "work"
    manifest = _manifest_file(tmp_path, {})
    common = ["--work-dir", str(work_dir)]
    assert (
        orchestrator.main(
            ["snapshot", "--data-dir", str(data_dir), *common]
        )
        == 0
    )
    assert (
        orchestrator.main(
            [
                "migrate",
                "--data-dir",
                str(data_dir),
                "--manifest",
                str(manifest),
                "--git-sha",
                GIT_SHA,
                *common,
            ]
        )
        == 0
    )
    runtime_root = tmp_path / "runtime"
    (runtime_root / "data" / "prediction_arbitrage").mkdir(parents=True)
    from open_trader.prediction_release import write_prediction_runtime_record

    write_prediction_runtime_record(
        runtime_root / "prediction-service-runtime.json",
        {"state": "stopped"},
    )
    assert (
        orchestrator.main(
            [
                "stop-verify",
                "--runtime-root",
                str(runtime_root),
                "--launchctl-bin",
                str(_fake_launchctl(tmp_path, present=False)),
                *common,
            ]
        )
        == 0
    )
    assert (
        orchestrator.main(["post-verify", "--data-dir", str(data_dir), *common])
        == 0
    )
    return work_dir


def _evidence(work_dir: Path, *, sha: str = GIT_SHA, start: str = "2026-08-31T10:00:00Z", end: str = "2026-08-31T10:20:00Z") -> int:
    return orchestrator.main(
        [
            "evidence",
            "--work-dir",
            str(work_dir),
            "--git-sha",
            sha,
            "--downtime-start",
            start,
            "--downtime-end",
            end,
        ]
    )


def test_evidence_assembles_all_required_sections(tmp_path: Path) -> None:
    work_dir = _prepare_evidence_fixture(tmp_path)

    exit_code = _evidence(work_dir)

    assert exit_code == 0
    evidence = _read_report(work_dir, "cutover-evidence.json")
    assert evidence["schema_version"] == (
        "open_trader.prediction_cutover.evidence.n_leg_v1"
    )
    assert evidence["git_sha"] == GIT_SHA
    snapshot = _read_report(work_dir, "snapshot-report.json")
    assert evidence["snapshot"]["db_md5"] == snapshot["md5"]["db"]
    assert evidence["snapshot"]["set_sha256"] == snapshot["sha256"]["set"]
    assert evidence["snapshot"]["fence_source"] == 1
    migration = _read_report(work_dir, "migrate-result.json")
    fingerprint = evidence["migration_fingerprint"]
    assert fingerprint["total_unsettled_capital_units"] == (
        migration["migration"]["total_unsettled_capital_units"]
    )
    assert fingerprint["priced_execution_count"] == (
        migration["migration"]["priced_execution_count"]
    )
    assert fingerprint["audit_event_id"] == (
        migration["migration"]["audit_event_id"]
    )
    assert fingerprint["published"] == 3
    assert fingerprint["blocked"] == 2
    assert evidence["fence"] == {"before": 1, "after": 2}
    assert evidence["catalog_counts"]["published"] == 3
    assert evidence["catalog_counts"]["blocked"] == 2
    assert evidence["downtime"] == {
        "start": "2026-08-31T10:00:00Z",
        "end": "2026-08-31T10:20:00Z",
    }
    stop = evidence["stop_verify"]
    assert stop["labels"][orchestrator.SERVICE_LABEL]["state"] == "absent"
    assert stop["lock"]["free"] is True
    assert stop["runtime_record"]["state"] == "stopped"
    assert evidence["post_verify"]["fence"] == 2
    # The irreversible-boundary field stays null until the first post-cutover
    # business write; the note documents the semantics.
    assert evidence["irreversible_boundary"] is None
    assert evidence["irreversible_boundary_note"]


def test_evidence_requires_every_component_and_valid_inputs(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty-work"
    empty.mkdir()

    exit_code = _evidence(empty)

    assert exit_code == 2
    report = _read_report(empty, "cutover-evidence.json")
    assert set(report["missing"]) == {
        "snapshot-report.json",
        "migrate-result.json",
        "stop-verify-report.json",
        "post-verify-report.json",
    }

    work_dir = _prepare_evidence_fixture(tmp_path)
    assert _evidence(work_dir, sha="short-sha") == 2
    assert _evidence(work_dir, start="not-a-timestamp") == 2
    assert _evidence(work_dir, end="") == 2
    # The prepared work dir still assembles cleanly for the operator.
    assert _evidence(work_dir) == 0
