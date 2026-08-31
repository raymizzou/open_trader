#!/usr/bin/env python3
"""Issue #60 phase A slice 3 (A6): N_LEG cutover orchestrator.

Drives the planned-downtime cutover window for the prediction arbitrage
ledger, one fail-closed step at a time:

1. ``precheck``  — read-only (``mode=ro``) survey of the target data dir:
   every nonterminal execution must be priceable (payload ``total_max_cost``
   or manifest override), every manifest entry must reference an existing
   nonterminal execution, and the fence, ``n_leg_controls`` presence, legacy
   state (``validation_mode`` / ``cross_auto_state`` / derived legacy breaker,
   informational only) and the APPROVED catalog published/blocked preview
   (slice-1's own completeness predicate) are recorded.
2. ``snapshot``  — SQLite online-backup copy (source ``mode=ro``) of the
   catalog DB into ``<work-dir>/snapshot/`` with an md5 sidecar for the DB and
   its WAL siblings; the copy must open read-only at the same fence.
3. ``maintenance`` — atomically rewrite ``config/prediction-route.json``
   (``open_trader.frontend_gateway.prediction_route.v1``) with a timestamped
   backup of the previous record beside it.
4. ``stop-verify`` — read-only probes proving the runtime owners are gone:
   both launchd labels absent (the actual ``launchctl bootout`` is
   operator-executed; the exact commands are printed), the runtime record
   state recorded, and ``data/prediction_arbitrage/runtime.lock`` flock FREE.
5. ``migrate``    — runs the slice-1 store-level migration
   (``run_n_leg_cutover_migration`` + ``activate_approved_relations``).  The
   default refuses the production data dir (COPY-mode: aim it at a copy);
   ``--production`` additionally requires the owner-stopped guard.
6. ``post-verify`` — read-only proof of the migrated state (fence == 2,
   controls consistent, ACTIVE count == published) plus optional live
   ``--url`` probes (healthz owner metadata, ``n_leg.contract_generation``,
   one legacy POST -> 410 ``legacy_strategy_removed``).
7. ``evidence``   — assembles ``cutover-evidence.json``
   (``open_trader.prediction_cutover.evidence.n_leg_v1``) from the step
   reports in the work dir.
8. ``restore``    — pre-boundary whole-file rollback of the snapshot set
   (refuses at fence >= 2 without the explicit force acknowledgment; never a
   partial restore).
9. ``dry-run``    — the whole pipeline against a read-only backup replica of
   a fixture "production" dir, with the #71 before/after md5 sidecar proving
   zero production writes.

Exit codes: 0 = every check passed; 2 = at least one unexplained/blocked
state (the JSON report in the work dir lists every offending item).
Production writes happen only under ``--production`` with the owner-stopped
guard; network is touched only by the optional post-verify probes.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from open_trader.prediction_arbitrage_store import (  # noqa: E402
    N_LEG_READER_GENERATION,
    PredictionArbitrageStore,
    read_minimum_reader_generation,
)
from open_trader.prediction_n_leg_cutover import (  # noqa: E402
    INCOMPLETE_MODEL_CAUSE,
    N_LEG_SETTLED_EXECUTION_STATES,
    _price_units,
    activate_approved_relations,
    run_n_leg_cutover_migration,
)
from open_trader.prediction_n_leg_mode import (  # noqa: E402
    N_LEG_CONTRACT_GENERATION,
    SAME_EVENT_SAME_VENUE_SCOPE_ID,
)
from open_trader.prediction_release import load_prediction_runtime_record  # noqa: E402
from open_trader.prediction_service import (  # noqa: E402
    LEGACY_STRATEGY_REMOVED,
)
from open_trader.relation_catalog import _stored_payload_complete  # noqa: E402

PRODUCTION_DATA_DIR = _REPO / "data"
N_LEG_DB_REL = Path("prediction_arbitrage") / "prediction_arbitrage.sqlite3"
RUNTIME_LOCK_REL = Path("data") / "prediction_arbitrage" / "runtime.lock"
SERVICE_LABEL = "com.open-trader.prediction-service"
HEALTH_LABEL = "com.open-trader.prediction-arbitrage-health"
RUNTIME_RECORD_NAME = "prediction-service-runtime.json"
ROUTE_SCHEMA = "open_trader.frontend_gateway.prediction_route.v1"
ROUTE_MODES = ("maintenance", "service")
PRECHECK_REPORT = "precheck.json"
SNAPSHOT_REPORT = "snapshot-report.json"
RESTORE_REPORT = "restore-report.json"
MIGRATE_REPORT = "migrate-result.json"
MAINTENANCE_REPORT = "maintenance-report.json"
STOP_VERIFY_REPORT = "stop-verify-report.json"
POST_VERIFY_REPORT = "post-verify-report.json"
DRY_RUN_REPORT = "dry-run-report.json"
EVIDENCE_REPORT = "cutover-evidence.json"
EVIDENCE_SCHEMA = "open_trader.prediction_cutover.evidence.n_leg_v1"
CHECKSUM_SIDECAR_SUFFIX = ".production-checksum.json"
SNAPSHOT_DIR_NAME = "snapshot"
SNAPSHOT_CHECKSUM_NAME = "checksums.md5.json"
SNAPSHOT_CHECKSUM_SCHEMA = "open_trader.prediction_cutover.snapshot_checksum.v1"
# Exact string required to unlock a forced restore once the target fence has
# reached the N_LEG generation (the "no partial rollback" policy override).
RESTORE_FORCE_ACKNOWLEDGMENT = (
    "I acknowledge the N_LEG cutover irreversible boundary has already passed "
    "and I explicitly request this forced whole-file restore"
)
_SQLITE_SIBLING_SUFFIXES = ("-wal", "-shm", "-journal")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def md5sum(path: str | Path) -> str:
    """Hex md5 digest of one file, streamed."""

    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256sum(path: str | Path) -> str:
    """Hex sha256 digest of one file, streamed."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_catalog_ro(source: str | Path, destination: str | Path) -> Path:
    """Copy one SQLite catalog via the online-backup API, source mode=ro."""

    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f"catalog not found: {source}")
    if destination.exists():
        raise FileExistsError(f"destination catalog already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect_ro(source)
    try:
        replica = sqlite3.connect(destination)
        try:
            connection.backup(replica)
        finally:
            replica.close()
    finally:
        connection.close()
    return destination


def _paths_denote_same_file(candidate: Path, guarded: Path) -> bool:
    """Equality that is safe on case-insensitive volumes (the #71 pattern)."""

    try:
        if candidate.resolve() == guarded.resolve():
            return True
    except OSError:
        pass
    if candidate.exists() and guarded.exists():
        try:
            return os.path.samefile(candidate, guarded)
        except OSError:
            return False
    try:
        return str(candidate.resolve()).casefold() == str(guarded.resolve()).casefold()
    except OSError:
        return False


def targets_production_data_dir(data_dir: str | Path) -> bool:
    """Whether *data_dir* is bound to the production prediction runtime.

    Production is the runtime root, not this checkout (issue #60 final-review
    P1-2): ``install_prediction_service_launchd.sh`` lays out
    ``DATA_DIR=$RUNTIME_ROOT/data`` beside the record
    ``$RUNTIME_ROOT/prediction-service-runtime.json``, so any data dir whose
    parent carries that runtime record is production-bound — including when
    this script runs from a release checkout. The checkout's own ``data/``
    dir stays guarded for dev-checkout safety.
    """

    data_dir = Path(data_dir)
    if (data_dir.parent / RUNTIME_RECORD_NAME).is_file():
        return True
    return _paths_denote_same_file(data_dir, PRODUCTION_DATA_DIR)


def _sqlite_siblings(db: Path) -> dict[str, str]:
    """md5 digests of the WAL-mode sibling files that exist beside *db*."""

    siblings: dict[str, str] = {}
    for suffix in _SQLITE_SIBLING_SUFFIXES:
        sibling = db.with_name(db.name + suffix)
        if sibling.is_file():
            siblings[suffix] = md5sum(sibling)
    return siblings


def _set_sha256(root: Path) -> str:
    """One sha256 over the sorted (relative name, file digest) snapshot set."""

    combined = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        combined.update(path.relative_to(root).as_posix().encode("utf-8"))
        combined.update(b"\n")
        combined.update(sha256sum(path).encode("ascii"))
        combined.update(b"\n")
    return combined.hexdigest()


# ---------------------------------------------------------------------------
# snapshot + restore
# ---------------------------------------------------------------------------


def take_snapshot(
    data_dir: str | Path,
    work_dir: str | Path,
    *,
    production: bool = False,
) -> tuple[dict[str, object], bool]:
    """Online-backup one data dir into ``<work-dir>/snapshot/`` (read-only
    source), verify the copy opens at the source fence, and record the md5
    sidecar plus the snapshot-set sha256.  Returns (report, ok)."""

    data_dir = Path(data_dir)
    work_dir = Path(work_dir)
    db = db_path(data_dir)
    snapshot_root = work_dir / SNAPSHOT_DIR_NAME
    snapshot_db = snapshot_root / N_LEG_DB_REL
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.snapshot.v1",
        "data_dir": str(data_dir),
        "db": str(db),
        "snapshot_dir": str(snapshot_root),
        "snapshot_db": str(snapshot_db),
        "production": production,
        "captured_at": _utc_now(),
    }
    if not db.is_file():
        report["error"] = f"prediction catalog not found: {db}"
        return report, False
    fence_source = read_minimum_reader_generation(data_dir)
    report["fence_source"] = fence_source
    try:
        backup_catalog_ro(db, snapshot_db)
    except (OSError, sqlite3.Error) as exc:
        report["error"] = f"online-backup copy failed: {exc}"
        return report, False

    problems: list[str] = []
    try:
        fence_snapshot = read_minimum_reader_generation(snapshot_root)
    except (OSError, ValueError, sqlite3.Error) as exc:
        fence_snapshot = None
        problems.append(f"snapshot copy does not open read-only: {exc}")
    report["fence_snapshot"] = fence_snapshot
    if fence_snapshot is not None and fence_snapshot != fence_source:
        problems.append(
            f"snapshot fence {fence_snapshot} does not match source fence {fence_source}"
        )

    siblings = _sqlite_siblings(db)
    db_md5 = md5sum(snapshot_db)
    checksums = {
        "schema_version": SNAPSHOT_CHECKSUM_SCHEMA,
        "source_db": str(db),
        "snapshot_db": str(snapshot_db),
        "fence_source": fence_source,
        "md5": {"db": db_md5, "siblings": siblings},
        "sha256": {"db": sha256sum(snapshot_db)},
        "captured_at": _utc_now(),
    }
    sidecar_path = snapshot_root / SNAPSHOT_CHECKSUM_NAME
    sidecar_path.write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["md5"] = {"db": db_md5, "siblings": siblings}
    report["sha256"] = {"db": checksums["sha256"]["db"], "set": _set_sha256(snapshot_root)}
    report["checksum_sidecar"] = str(sidecar_path)
    if problems:
        report["error"] = "; ".join(problems)
        return report, False
    return report, True


def perform_restore(
    data_dir: str | Path,
    work_dir: str | Path,
    *,
    production: bool = False,
    force: bool = False,
    force_acknowledgment: str | None = None,
    runtime_root: str | Path | None = None,
    launchctl_bin: str = "launchctl",
) -> tuple[dict[str, object], bool]:
    """Whole-file restore of the work-dir snapshot set over the target.

    Boundary-pre rollback only: refuses once the target fence has reached the
    N_LEG generation unless ``--force`` carries the exact acknowledgment
    string.  A production restore runs the same owner-stopped guard as the
    migration (lock FREE, both launchd labels verifiably absent, runtime
    record not ``ready``) BEFORE anything on the target is touched — a guard
    failure means zero mutations.  Never a partial restore: the snapshot file
    is copied to a temp file beside the target and swapped in with
    ``os.replace`` after the stale WAL siblings are removed.
    """

    data_dir = Path(data_dir)
    work_dir = Path(work_dir)
    db = db_path(data_dir)
    snapshot_root = work_dir / SNAPSHOT_DIR_NAME
    snapshot_db = snapshot_root / N_LEG_DB_REL
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.restore.v1",
        "data_dir": str(data_dir),
        "db": str(db),
        "snapshot_dir": str(snapshot_root),
        "production": production,
        "forced": bool(force),
        "captured_at": _utc_now(),
    }

    def _refuse(reason: str) -> tuple[dict[str, object], bool]:
        report["error"] = reason
        return report, False

    checksums = _read_json_if_present(snapshot_root / SNAPSHOT_CHECKSUM_NAME)
    if not isinstance(checksums, dict) or not snapshot_db.is_file():
        return _refuse(f"no snapshot set found under {snapshot_root}")
    snapshot_report = _read_json_if_present(work_dir / SNAPSHOT_REPORT)
    fence_source = (
        checksums.get("fence_source")
        if isinstance(checksums.get("fence_source"), int)
        else (
            snapshot_report.get("fence_source")
            if isinstance(snapshot_report, dict)
            else None
        )
    )
    snapshot_md5 = checksums.get("md5", {}).get("db") if isinstance(checksums.get("md5"), dict) else None
    if not snapshot_md5 or not isinstance(fence_source, int):
        return _refuse("snapshot checksum sidecar is missing the db md5 or fence")

    if targets_production_data_dir(data_dir) and not production:
        return _refuse(
            f"refusing: {data_dir} is bound to the production runtime root "
            "(<data-dir>/../prediction-service-runtime.json or the checkout "
            "data dir); whole-file restore requires --production "
            "--runtime-root <runtime root> with the owner-stopped guard"
        )

    if not db.is_file():
        return _refuse(f"prediction catalog not found: {db}")
    fence_target = read_minimum_reader_generation(data_dir)
    report["fence_target"] = fence_target
    if fence_target >= N_LEG_READER_GENERATION:
        if not force:
            return _refuse(
                f"refusing: target fence is already {fence_target} (>= the "
                f"N_LEG generation {N_LEG_READER_GENERATION}); the rollback "
                "boundary has passed — a forced restore requires --force with "
                "the exact acknowledgment string"
            )
        if force_acknowledgment != RESTORE_FORCE_ACKNOWLEDGMENT:
            return _refuse(
                "refusing: --force requires --force-acknowledgment to match "
                "the exact RESTORE_FORCE_ACKNOWLEDGMENT string"
            )

    # The same owner-stopped precondition as the migration: production may
    # only be written when the owners are provably gone, and a guard failure
    # happens before any sibling removal or file replacement (zero mutations).
    if production:
        resolved_runtime_root = (
            Path(runtime_root) if runtime_root is not None else data_dir.parent
        )
        evidence, failures = production_owner_guard(resolved_runtime_root, launchctl_bin)
        report["guard"] = {
            "evidence": evidence,
            "failures": failures,
            "passed": not failures,
        }
        if failures:
            return _refuse(
                "refusing: production owner-stopped guard failed: "
                + "; ".join(failures)
            )

    # Never a partial restore: the snapshot is first fully copied and fsynced
    # into a temp file beside the target.  Only once that succeeds do the
    # stale WAL/SHM siblings go (immediately before the whole-file swap), so
    # a mid-copy failure leaves the target set byte-identical instead of
    # destroying committed-but-uncheckpointed WAL frames.
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=db.parent, prefix=f".{db.name}.restore.", delete=False
        ) as handle:
            temporary = handle.name
            with open(snapshot_db, "rb") as source:
                shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        removed_siblings: list[str] = []
        for suffix in _SQLITE_SIBLING_SUFFIXES:
            sibling = db.with_name(db.name + suffix)
            if sibling.exists():
                sibling.unlink()
                removed_siblings.append(sibling.name)
        report["removed_siblings"] = removed_siblings
        os.replace(temporary, db)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)

    restored_md5 = md5sum(db)
    report["restored_md5"] = restored_md5
    if restored_md5 != snapshot_md5:
        return _refuse("restored file md5 does not match the snapshot md5")
    fence_after = read_minimum_reader_generation(data_dir)
    report["fence_after_restore"] = fence_after
    if fence_after != fence_source:
        return _refuse(
            f"restored fence {fence_after} does not match the pre-migration "
            f"fence {fence_source}"
        )
    return report, True


def db_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / N_LEG_DB_REL


def _connect_ro(db: str | Path) -> sqlite3.Connection:
    path = Path(db)
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _write_report(work_dir: Path, name: str, report: Mapping[str, object]) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    path = work_dir / name
    path.write_text(
        json.dumps(dict(report), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def load_manifest(path: str | Path) -> dict[str, object]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError("manifest must be a JSON object of execution_id -> price")
    return raw


def _read_json_if_present(path: Path) -> object | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# precheck
# ---------------------------------------------------------------------------


def _catalog_preview(connection: sqlite3.Connection) -> dict[str, object]:
    """APPROVED publish preview through slice-1's completeness predicate.

    Reads the v2 catalog tables read-only and classifies each latest APPROVED
    version exactly as ``activate_approved_relations`` will: complete payloads
    are the publish preview, the rest are blocked with their cause.  The
    predicate itself is imported, never reimplemented here.
    """

    if not (
        _table_exists(connection, "catalog_v2_latest")
        and _table_exists(connection, "catalog_v2_versions")
    ):
        return {
            "catalog_tables_present": False,
            "approved_considered": 0,
            "published_preview": 0,
            "blocked_preview": 0,
            "blocked_causes": {},
        }
    latest = dict(
        connection.execute("SELECT identity, version_id FROM catalog_v2_latest")
    )
    versions = {
        str(row[0]): (str(row[1]), str(row[2] or ""))
        for row in connection.execute(
            "SELECT version_id, payload, status FROM catalog_v2_versions"
        )
    }
    considered = 0
    published = 0
    causes: dict[str, str] = {}
    for identity, version_id in latest.items():
        record = versions.get(str(version_id))
        if record is None or record[1] != "APPROVED":
            continue
        considered += 1
        try:
            payload = json.loads(record[0])
        except (TypeError, ValueError):
            causes[str(identity)] = "PAYLOAD_UNREADABLE"
            continue
        if _stored_payload_complete(payload):
            published += 1
        else:
            causes[str(identity)] = INCOMPLETE_MODEL_CAUSE
    return {
        "catalog_tables_present": True,
        "approved_considered": considered,
        "published_preview": published,
        "blocked_preview": len(causes),
        "blocked_causes": dict(sorted(causes.items())),
    }


def _legacy_state(connection: sqlite3.Connection) -> dict[str, object]:
    """Record the legacy automation/breaker state (informational only — the
    migration ignores it and initializes ``n_leg_controls`` from scratch)."""

    validation_mode: str | None = None
    cross_auto: dict[str, object] | None = None
    if _table_exists(connection, "validation_mode"):
        row = connection.execute(
            "SELECT mode FROM validation_mode WHERE singleton=1"
        ).fetchone()
        if row is not None:
            validation_mode = str(row[0])
    if _table_exists(connection, "cross_auto_state"):
        row = connection.execute(
            "SELECT configured_mode, armed, reason FROM cross_auto_state WHERE singleton=1"
        ).fetchone()
        if row is not None:
            cross_auto = {
                "configured_mode": str(row[0]),
                "armed": bool(row[1]),
                "reason": str(row[2]),
            }
    return {
        "validation_mode": validation_mode,
        "cross_auto_state": cross_auto,
        # The legacy breaker is the observe-only validation mode; recorded
        # with its derivation so the evidence trail is self-describing.
        "breaker": {
            "open": validation_mode == "observe_only",
            "basis": "validation_mode == 'observe_only'",
        },
    }


def gather_precheck(
    data_dir: str | Path,
    manifest: Mapping[str, object],
    *,
    production: bool = False,
) -> tuple[dict[str, object], bool]:
    """Read-only precheck of one target data dir -> (report, ok)."""

    data_dir = Path(data_dir)
    db = db_path(data_dir)
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.precheck.v1",
        "data_dir": str(data_dir),
        "db": str(db),
        "production": production,
        "captured_at": _utc_now(),
    }
    if not db.is_file():
        report["error"] = f"prediction catalog not found: {db}"
        return report, False
    report["fence"] = read_minimum_reader_generation(data_dir)

    unpriceable: list[dict[str, object]] = []
    mismatch: list[dict[str, object]] = []
    unexplained: list[dict[str, object]] = []
    previewed: list[dict[str, object]] = []
    reserved_count = 0
    controls_present = False
    controls_row: dict[str, object] | None = None
    states: dict[str, str] = {}

    connection = _connect_ro(db)
    try:
        if _table_exists(connection, "executions"):
            payloads: dict[str, dict[str, object]] = {}
            for execution_id, state, payload_raw in connection.execute(
                "SELECT execution_id, state, payload FROM executions"
            ):
                states[str(execution_id)] = str(state)
                try:
                    parsed = json.loads(str(payload_raw))
                except (TypeError, ValueError) as exc:
                    unexplained.append(
                        {
                            "execution_id": str(execution_id),
                            "reason": f"payload unreadable: {exc}",
                        }
                    )
                    continue
                if not isinstance(parsed, dict):
                    unexplained.append(
                        {
                            "execution_id": str(execution_id),
                            "reason": "payload is not a JSON object",
                        }
                    )
                    continue
                payloads[str(execution_id)] = parsed
            # Mirror the slice-1 migration's pricing exactly: reserved
            # cross reservations first (amount == the execution's unsettled
            # capital), then every other unsettled execution from its payload
            # total_max_cost or the manifest override.
            if _table_exists(connection, "cross_execution_reservations"):
                reserved: dict[str, object] = {
                    str(row[0]): row[1]
                    for row in connection.execute(
                        "SELECT execution_id, amount FROM cross_execution_reservations "
                        "WHERE state='reserved'"
                    )
                }
            else:
                reserved = {}
            reserved_count = len(reserved)
            for execution_id, amount in sorted(reserved.items()):
                units = _price_units(amount)
                if units is None:
                    unpriceable.append(
                        {
                            "execution_id": execution_id,
                            "state": states.get(execution_id),
                            "reason": (
                                "reserved cross reservation amount is unpriceable"
                            ),
                        }
                    )
                else:
                    previewed.append(
                        {
                            "execution_id": execution_id,
                            "state": states.get(execution_id),
                            "price_source": "reserved_reservation",
                            "price": str(amount),
                            "units": units,
                        }
                    )
            for execution_id, state in sorted(states.items()):
                if execution_id in reserved or state in N_LEG_SETTLED_EXECUTION_STATES:
                    continue
                price: object = payloads.get(execution_id, {}).get("total_max_cost")
                source = "payload"
                if execution_id in manifest:
                    price = manifest[execution_id]
                    source = "manifest"
                units = _price_units(price)
                if units is None:
                    unpriceable.append(
                        {
                            "execution_id": execution_id,
                            "state": state,
                            "reason": (
                                "no priceable total_max_cost and no manifest override"
                            ),
                        }
                    )
                else:
                    previewed.append(
                        {
                            "execution_id": execution_id,
                            "state": state,
                            "price_source": source,
                            "price": str(price),
                            "units": units,
                        }
                    )
        else:
            unexplained.append({"reason": "executions table is missing"})

        for execution_id in manifest:
            state = states.get(execution_id)
            if state is None:
                mismatch.append(
                    {
                        "execution_id": execution_id,
                        "reason": "manifest references missing execution",
                    }
                )
            elif state in N_LEG_SETTLED_EXECUTION_STATES:
                mismatch.append(
                    {
                        "execution_id": execution_id,
                        "reason": (
                            f"manifest references settled execution in state {state}"
                        ),
                    }
                )

        if _table_exists(connection, "n_leg_controls"):
            row = connection.execute(
                "SELECT mode, breaker_open, breaker_reason, "
                "total_unsettled_capital_units, contract_generation, updated_at "
                "FROM n_leg_controls WHERE singleton=1"
            ).fetchone()
            if row is not None:
                controls_present = True
                controls_row = {
                    "mode": str(row[0]),
                    "breaker_open": bool(row[1]),
                    "breaker_reason": row[2],
                    "total_unsettled_capital_units": int(row[3]),
                    "contract_generation": int(row[4]),
                    "updated_at": str(row[5]),
                }
        report["legacy_state"] = _legacy_state(connection)
        report["catalog"] = _catalog_preview(connection)
    finally:
        connection.close()

    report["reserved_reservation_count"] = reserved_count
    report["nonterminal_executions"] = previewed
    report["n_leg_controls_present"] = controls_present
    report["n_leg_controls"] = controls_row
    report["problems"] = {
        "unpriceable": unpriceable,
        "manifest_mismatch": mismatch,
        "unexplained": unexplained,
    }
    ok = not (unpriceable or mismatch or unexplained)
    return report, ok


# ---------------------------------------------------------------------------
# maintenance route
# ---------------------------------------------------------------------------


def perform_maintenance(
    route_file: str | Path,
    mode: str,
    *,
    operation_id: str | None = None,
) -> tuple[dict[str, object], bool]:
    """Atomically rewrite the prediction route record with a timestamped
    backup of the previous file beside it."""

    route_file = Path(route_file)
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.maintenance.v1",
        "route_file": str(route_file),
        "mode": mode,
        "captured_at": _utc_now(),
    }

    def _refuse(reason: str) -> tuple[dict[str, object], bool]:
        report["error"] = reason
        return report, False

    if mode not in ROUTE_MODES:
        return _refuse(
            f"refusing: route mode must be one of {list(ROUTE_MODES)}, got {mode!r}"
        )
    previous_mode: str | None = None
    if route_file.exists():
        try:
            previous = json.loads(route_file.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                previous_mode = (
                    str(previous["mode"])
                    if isinstance(previous.get("mode"), str)
                    else None
                )
        except (OSError, ValueError):
            previous_mode = None
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = route_file.with_name(f"{route_file.name}.backup-{timestamp}")
        shutil.copy2(route_file, backup_path)
        report["backup_path"] = str(backup_path)
    else:
        backup_path = None
        if not route_file.parent.exists():
            route_file.parent.mkdir(parents=True, exist_ok=True)
    report["previous_mode"] = previous_mode

    resolved_operation = operation_id or f"nleg-cutover-{_utc_now()}"
    if not resolved_operation.strip():
        return _refuse("refusing: --operation-id must be non-empty when given")
    record = {
        "schema_version": ROUTE_SCHEMA,
        "mode": mode,
        "operation_id": resolved_operation,
        "updated_at": _utc_now(),
    }
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=route_file.parent,
            prefix=f".{route_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, route_file)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    report["operation_id"] = resolved_operation
    return report, True


def _cmd_maintenance(args: argparse.Namespace) -> int:
    report, ok = perform_maintenance(
        args.route_file,
        args.mode,
        operation_id=args.operation_id,
    )
    _write_report(args.work_dir, MAINTENANCE_REPORT, report)
    return 0 if ok else 2


def _cmd_stop_verify(args: argparse.Namespace) -> int:
    report, ok = run_stop_verify(
        args.runtime_root, launchctl_bin=args.launchctl_bin
    )
    _write_report(args.work_dir, STOP_VERIFY_REPORT, report)
    return 0 if ok else 2


def _cmd_post_verify(args: argparse.Namespace) -> int:
    report, ok = run_post_verify(
        args.data_dir,
        work_dir=args.work_dir,
        url=args.url,
        expected_git_sha=args.expected_git_sha,
    )
    _write_report(args.work_dir, POST_VERIFY_REPORT, report)
    return 0 if ok else 2


# ---------------------------------------------------------------------------
# owner-stopped guard (lock + launchd labels + runtime record)
# ---------------------------------------------------------------------------


def probe_runtime_lock(runtime_root: str | Path) -> dict[str, object]:
    """Try a non-blocking exclusive flock on the runtime ownership lock.

    Read-only when possible: an absent lock file proves the owner cannot hold
    it (the runtime creates the file before locking), so nothing is created.
    """

    path = Path(runtime_root) / RUNTIME_LOCK_REL
    if not path.exists():
        return {"path": str(path), "free": True, "detail": "lock file absent"}
    detail = ""
    try:
        handle = os.open(path, os.O_RDWR)
    except OSError as exc:
        return {
            "path": str(path),
            "free": False,
            "detail": f"lock file cannot be opened for the probe: {exc}",
        }
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        detail = f"runtime ownership lock is HELD: {exc}"
        free = False
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        detail = "runtime ownership lock is FREE"
        free = True
    finally:
        os.close(handle)
    return {"path": str(path), "free": free, "detail": detail}


def launchctl_label_probe(label: str, launchctl_bin: str) -> dict[str, object]:
    """Read-only ``launchctl print``/``list`` probes for one label."""

    gui_target = f"gui/{os.getuid()}/{label}"
    result: dict[str, object] = {"label": label, "gui_target": gui_target}
    outputs: list[str] = []
    probes_ok = True
    for action in ("print", "list"):
        try:
            proc = subprocess.run(
                [launchctl_bin, action, gui_target if action == "print" else label],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            probes_ok = False
            result[f"{action}_exit"] = None
            result["error"] = f"launchctl {action} failed: {exc}"
            continue
        result[f"{action}_exit"] = proc.returncode
        outputs.append(proc.stdout + proc.stderr)
    if not probes_ok:
        result["state"] = "unknown"
        return result
    combined = "\n".join(outputs)
    if any("Could not find service" in line for line in combined.splitlines()):
        result["state"] = "absent"
        return result
    if result.get("print_exit") == 0 or result.get("list_exit") == 0:
        result["state"] = "present"
        for line in combined.splitlines():
            columns = line.split("\t")
            if len(columns) >= 3 and columns[0].strip().isdigit():
                result["pid"] = columns[0].strip()
                break
        return result
    result["state"] = "unknown"
    result["error"] = "launchctl probes returned an unexplained state"
    return result


def production_owner_guard(
    runtime_root: str | Path, launchctl_bin: str
) -> tuple[dict[str, object], list[str]]:
    """The stop-verify-equivalent precondition for production writes.

    Returns (evidence, failures): the production data dir may only be written
    when the ownership lock is FREE, both launchd labels are verifiably
    absent, and the runtime record does not claim a live (``ready``) owner.
    """

    runtime_root = Path(runtime_root)
    failures: list[str] = []
    lock = probe_runtime_lock(runtime_root)
    if not lock["free"]:
        failures.append(str(lock["detail"]))
    labels: dict[str, dict[str, object]] = {}
    for label in (SERVICE_LABEL, HEALTH_LABEL):
        probe = launchctl_label_probe(label, launchctl_bin)
        labels[label] = probe
        if probe["state"] == "present":
            failures.append(
                f"launchd label {label} is still loaded"
                + (f" (pid {probe['pid']})" if "pid" in probe else "")
            )
        elif probe["state"] == "unknown":
            failures.append(
                f"launchd label {label} state is unverifiable: "
                + str(probe.get("error", "probe failed"))
            )
    record_error: str | None = None
    record_state: str | None = None
    record_path = runtime_root / RUNTIME_RECORD_NAME
    try:
        record = load_prediction_runtime_record(record_path)
    except ValueError as exc:
        record_error = str(exc)
        failures.append(f"runtime record is unusable: {exc}")
    else:
        if record is not None:
            record_state = str(record.get("state"))
            if record_state == "ready":
                failures.append(
                    "runtime record state is 'ready' (live owner) — stop the "
                    "owner before writing the production data dir"
                )
    evidence = {
        "runtime_root": str(runtime_root),
        "lock": lock,
        "labels": labels,
        "runtime_record": {
            "path": str(record_path),
            "state": record_state,
            "present": record_state is not None or record_error is not None,
            "error": record_error,
        },
    }
    return evidence, failures


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


def read_active_identities(db: str | Path) -> dict[str, object]:
    """ACTIVE identities of the v2 catalog, read via SQLite ``mode=ro``.

    The ``activate_many`` publish path records ACTIVE as generation
    membership, not as the versions-table ``activation_status`` column (that
    column is set by the facade approve path), so the decode replays the
    persisted ``catalog_v2_generations`` rows exactly as
    ``SqliteCatalogStore._scan_generation`` does: newest anchor first, then
    the newer deltas on top.
    """

    path = Path(db)
    generation: dict[str, dict] = {}
    deltas: list[dict] = []
    connection = _connect_ro(path)
    try:
        if not _table_exists(connection, "catalog_v2_generations"):
            return {}
        rows = connection.execute(
            "SELECT members FROM catalog_v2_generations ORDER BY generation_id DESC"
        )
        for (members,) in rows:
            data = json.loads(str(members))
            if isinstance(data, dict) and data.get("kind") == "delta":
                deltas.append(data)
                continue
            if isinstance(data, dict):
                generation = (
                    data["members"] if data.get("kind") == "anchor" else data
                )
            break
    finally:
        connection.close()
    for delta in reversed(deltas):
        for identity, entry in (delta.get("added") or {}).items():
            generation[identity] = entry
        for identity in delta.get("removed") or []:
            generation.pop(identity, None)
    return {
        identity: entry
        for identity, entry in generation.items()
        if isinstance(entry, dict) and entry.get("status") == "ACTIVE"
    }


def _audit_payload(
    data_dir: Path, audit_event_id: object
) -> dict[str, object] | None:
    """Read one control_events audit row (mode=ro) for its payload."""

    if not audit_event_id:
        return None
    connection = _connect_ro(db_path(data_dir))
    try:
        row = connection.execute(
            "SELECT payload FROM control_events WHERE event_id=?",
            (str(audit_event_id),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def perform_migration(
    data_dir: str | Path,
    manifest: Mapping[str, object],
    git_sha: str,
    *,
    production: bool = False,
    runtime_root: str | Path | None = None,
    launchctl_bin: str = "launchctl",
) -> tuple[dict[str, object], bool]:
    """Run the slice-1 migration + APPROVED activation on one data dir.

    COPY-mode by default: the production data dir is refused unless
    ``--production`` is given, and production additionally requires the
    owner-stopped guard (lock FREE, labels absent, runtime record not
    ``ready``) BEFORE the store is opened — a guard failure means zero DB
    writes.
    """

    data_dir = Path(data_dir)
    db = db_path(data_dir)
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.migrate.v1",
        "data_dir": str(data_dir),
        "db": str(db),
        "production": production,
        "git_sha": git_sha,
        "actor": "n_leg_cutover",
        "captured_at": _utc_now(),
    }

    def _refuse(reason: str) -> tuple[dict[str, object], bool]:
        report["error"] = reason
        return report, False

    if not git_sha or not git_sha.strip():
        return _refuse("git_sha must be a non-empty provenance string")
    # The production-path refusal comes first: an operator pointing the tool
    # at production in COPY-mode must never even probe the target.
    if targets_production_data_dir(data_dir) and not production:
        return _refuse(
            f"refusing: {data_dir} is bound to the production runtime root "
            "(<data-dir>/../prediction-service-runtime.json or the checkout "
            "data dir); COPY-mode is the default — pass --production "
            "--runtime-root <runtime root> (with the owner-stopped guard) "
            "to migrate production in place"
        )
    if not db.is_file():
        return _refuse(f"prediction catalog not found: {db}")
    fence_before = read_minimum_reader_generation(data_dir)
    report["fence_before"] = fence_before
    if production:
        resolved_runtime_root = (
            Path(runtime_root) if runtime_root is not None else data_dir.parent
        )
        evidence, failures = production_owner_guard(resolved_runtime_root, launchctl_bin)
        report["guard"] = {
            "evidence": evidence,
            "failures": failures,
            "passed": not failures,
        }
        if failures:
            return _refuse(
                "refusing: production owner-stopped guard failed: "
                + "; ".join(failures)
            )

    store = PredictionArbitrageStore(data_dir)
    migration = run_n_leg_cutover_migration(store, manifest=dict(manifest))
    activation = activate_approved_relations(
        store, actor="n_leg_cutover", git_sha=git_sha
    )
    fence_after = read_minimum_reader_generation(data_dir)
    report["migration"] = dict(migration)
    report["activation"] = {
        key: (
            dict(value) if isinstance(value, Mapping) else value
        )
        for key, value in activation.items()
    }
    report["fence_after"] = fence_after
    audit = _audit_payload(data_dir, migration.get("audit_event_id"))
    if audit is not None and isinstance(audit.get("source_states"), dict):
        report["source_states"] = dict(audit["source_states"])
    elif migration.get("outcome") == "migrated":
        report["source_states"] = {}
    return report, True


def _cmd_migrate(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    report, ok = perform_migration(
        args.data_dir,
        manifest,
        args.git_sha,
        production=args.production,
        runtime_root=args.runtime_root,
        launchctl_bin=args.launchctl_bin,
    )
    _write_report(args.work_dir, MIGRATE_REPORT, report)
    return 0 if ok else 2


# ---------------------------------------------------------------------------
# stop-verify
# ---------------------------------------------------------------------------


def run_stop_verify(
    runtime_root: str | Path, *, launchctl_bin: str = "launchctl"
) -> tuple[dict[str, object], bool]:
    """Read-only probes proving the prediction runtime owners are gone.

    The actual ``launchctl bootout`` is operator-executed: the exact commands
    are printed and recorded.  Exit 2 if any owner/lock is still alive.
    """

    runtime_root = Path(runtime_root)
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.stop_verify.v1",
        "runtime_root": str(runtime_root),
        "captured_at": _utc_now(),
    }
    failures: list[str] = []
    bootout_commands: list[str] = []
    labels: dict[str, dict[str, object]] = {}
    for label in (SERVICE_LABEL, HEALTH_LABEL):
        probe = launchctl_label_probe(label, launchctl_bin)
        labels[label] = probe
        command = f"launchctl bootout gui/{os.getuid()}/{label}"
        bootout_commands.append(command)
        if probe["state"] == "present":
            failures.append(
                f"launchd label {label} is still loaded"
                + (f" (pid {probe['pid']})" if "pid" in probe else "")
            )
        elif probe["state"] == "unknown":
            failures.append(
                f"launchd label {label} state is unverifiable: "
                + str(probe.get("error", "probe failed"))
            )
    lock = probe_runtime_lock(runtime_root)
    if not lock["free"]:
        failures.append(str(lock["detail"]))
    record_state: str | None = None
    record_error: str | None = None
    record_path = runtime_root / RUNTIME_RECORD_NAME
    try:
        record = load_prediction_runtime_record(record_path)
    except ValueError as exc:
        record_error = str(exc)
        failures.append(f"runtime record is unusable: {exc}")
    else:
        record_state = str(record.get("state")) if record is not None else None

    report["labels"] = labels
    report["lock"] = lock
    report["runtime_record"] = {
        "path": str(record_path),
        "state": record_state,
        "present": record is not None or record_error is not None,
        "error": record_error,
    }
    report["operator_bootout_commands"] = bootout_commands
    report["failures"] = failures
    for label, probe in labels.items():
        if probe["state"] == "present":
            print(
                "operator action required: launchctl bootout "
                f"gui/{os.getuid()}/{label}",
                file=sys.stderr,
            )
    return report, not failures


# ---------------------------------------------------------------------------
# post-verify
# ---------------------------------------------------------------------------


def _http_probe(
    url: str, *, method: str = "GET", body: bytes | None = None, timeout: float = 5.0
) -> tuple[int | None, bytes]:
    """One HTTP probe; HTTP error statuses are results, transport errors None."""

    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except OSError:
            payload = b""
        return int(exc.code), payload
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return None, str(exc).encode("utf-8")


def _probe_live_service(
    base_url: str, *, expected_git_sha: str | None
) -> tuple[dict[str, object], list[str]]:
    """Probe /healthz, the state payload, and one legacy POST (410)."""

    failures: list[str] = []
    probes: dict[str, object] = {}

    status, raw = _http_probe(base_url.rstrip("/") + "/healthz")
    health: dict[str, object] = {"status": status}
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("healthz payload is not a JSON object")
    except (ValueError, UnicodeDecodeError):
        payload = {}
        if status is not None:
            failures.append(f"healthz probe returned an unreadable payload ({status})")
    if status is None:
        failures.append(f"healthz probe failed: {raw.decode('utf-8', 'replace')}")
        health["error"] = raw.decode("utf-8", "replace")
    else:
        health["mode"] = payload.get("mode")
        health["production_owner"] = payload.get("production_owner")
        health["git_sha"] = payload.get("git_sha")
        if status != 200:
            failures.append(f"healthz probe returned status {status}")
        if payload.get("mode") != "production":
            failures.append(
                f"healthz mode is {payload.get('mode')!r}, expected 'production'"
            )
        if payload.get("production_owner") is not True:
            failures.append("healthz production_owner is not true")
        git_sha = payload.get("git_sha")
        if not isinstance(git_sha, str) or not git_sha.strip():
            failures.append("healthz git_sha is missing")
        elif expected_git_sha and git_sha != expected_git_sha:
            failures.append(
                f"healthz git_sha {git_sha} does not match the expected {expected_git_sha}"
            )
    probes["healthz"] = health

    status, raw = _http_probe(base_url.rstrip("/") + "/api/prediction-arbitrage/state")
    state: dict[str, object] = {"status": status}
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("state payload is not a JSON object")
    except (ValueError, UnicodeDecodeError):
        payload = {}
        if status is not None:
            failures.append(f"state probe returned an unreadable payload ({status})")
    n_leg = payload.get("n_leg") if isinstance(payload.get("n_leg"), dict) else {}
    generation = n_leg.get("contract_generation")
    state["n_leg_contract_generation"] = generation
    if status != 200:
        failures.append(f"state probe returned status {status}")
    if generation != N_LEG_CONTRACT_GENERATION:
        failures.append(
            f"state n_leg.contract_generation is {generation!r}, expected "
            f"{N_LEG_CONTRACT_GENERATION}"
        )
    probes["state"] = state

    status, raw = _http_probe(
        base_url.rstrip("/") + "/api/prediction-arbitrage/mode",
        method="POST",
        body=b"{}",
    )
    legacy: dict[str, object] = {"status": status}
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("legacy probe payload is not a JSON object")
    except (ValueError, UnicodeDecodeError):
        payload = {}
        if status is not None:
            failures.append(f"legacy POST probe returned an unreadable payload ({status})")
    error_code = payload.get("error_code")
    legacy["error_code"] = error_code
    if status != 410:
        failures.append(f"legacy POST probe returned status {status}, expected 410")
    if error_code != LEGACY_STRATEGY_REMOVED:
        failures.append(
            f"legacy POST error_code is {error_code!r}, expected "
            f"{LEGACY_STRATEGY_REMOVED!r}"
        )
    probes["legacy_post"] = legacy
    return probes, failures


def run_post_verify(
    data_dir: str | Path,
    *,
    work_dir: str | Path,
    url: str | None = None,
    expected_git_sha: str | None = None,
) -> tuple[dict[str, object], bool]:
    """Read-only proof of the migrated state (plus optional live probes)."""

    data_dir = Path(data_dir)
    work_dir = Path(work_dir)
    db = db_path(data_dir)
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.post_verify.v1",
        "data_dir": str(data_dir),
        "db": str(db),
        "url": url,
        "captured_at": _utc_now(),
    }
    failures: list[str] = []
    if not db.is_file():
        report["error"] = f"prediction catalog not found: {db}"
        return report, False

    fence = read_minimum_reader_generation(data_dir)
    report["fence"] = fence
    if fence != N_LEG_READER_GENERATION:
        failures.append(
            f"fence is {fence}, expected the N_LEG generation {N_LEG_READER_GENERATION}"
        )

    controls: dict[str, object] | None = None
    scope_capability: str | None = None
    connection = _connect_ro(db)
    try:
        if _table_exists(connection, "n_leg_controls"):
            row = connection.execute(
                "SELECT mode, breaker_open, total_unsettled_capital_units, "
                "contract_generation FROM n_leg_controls WHERE singleton=1"
            ).fetchone()
            if row is not None:
                controls = {
                    "mode": str(row[0]),
                    "breaker_open": bool(row[1]),
                    "total_unsettled_capital_units": int(row[2]),
                    "contract_generation": int(row[3]),
                }
        if _table_exists(connection, "n_leg_execution_scopes"):
            row = connection.execute(
                "SELECT capability FROM n_leg_execution_scopes WHERE scope_id=?",
                (SAME_EVENT_SAME_VENUE_SCOPE_ID,),
            ).fetchone()
            if row is not None:
                scope_capability = str(row[0])
    finally:
        connection.close()
    if controls is None:
        failures.append("n_leg_controls singleton row is missing")
    else:
        report["controls"] = controls
        if controls["mode"] != "MANUAL":
            failures.append(
                f"n_leg_controls mode is {controls['mode']!r}, expected 'MANUAL'"
            )
        if controls["contract_generation"] != N_LEG_CONTRACT_GENERATION:
            failures.append(
                f"n_leg_controls contract_generation is "
                f"{controls['contract_generation']!r}, expected "
                f"{N_LEG_CONTRACT_GENERATION}"
            )
    report["scope_capability"] = scope_capability
    if scope_capability != "OBSERVE_ONLY":
        failures.append(
            f"{SAME_EVENT_SAME_VENUE_SCOPE_ID} capability is "
            f"{scope_capability!r}, expected 'OBSERVE_ONLY'"
        )

    active = read_active_identities(db)
    active_count = len(active)
    report["active_count"] = active_count
    report["active_identities"] = sorted(active)
    published: int | None = None
    migrate_result = _read_json_if_present(work_dir / MIGRATE_REPORT)
    if isinstance(migrate_result, dict) and isinstance(
        migrate_result.get("activation"), dict
    ):
        raw_published = migrate_result["activation"].get("published")
        if isinstance(raw_published, int):
            published = raw_published
    report["published_from_migrate"] = published
    if published is not None and active_count != published:
        failures.append(
            f"ACTIVE relation count {active_count} does not match the "
            f"published count {published} from the migration result"
        )

    if url:
        probes, probe_failures = _probe_live_service(
            url, expected_git_sha=expected_git_sha
        )
        report["url_probes"] = probes
        failures.extend(probe_failures)

    report["failures"] = failures
    return report, not failures


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def _cmd_dry_run(args: argparse.Namespace) -> int:
    """The whole pipeline against a read-only backup replica.

    The "production" dir is only ever READ (``mode=ro`` online backup); the
    before/after md5 sidecar is produced whenever the before-checksum was
    taken — failed runs included — with the failing step recorded (the #71
    zero-write proof pattern).
    """

    work_dir = Path(args.work_dir)
    sub = work_dir / "dry-run"
    production_data_dir = Path(args.production_data_dir)
    production_db = db_path(production_data_dir)
    report: dict[str, object] = {
        "schema_version": "open_trader.prediction_cutover.dry_run.v1",
        "production_data_dir": str(production_data_dir),
        "production_db": str(production_db),
        "git_sha": args.git_sha,
        "replica_data_dir": str(sub / "data"),
        "captured_at": _utc_now(),
        "steps": {},
    }
    ok = True
    failure: str | None = None
    md5_before: str | None = None

    def _step(name: str, report_name: str, runner) -> bool:
        try:
            step_report, step_ok = runner()
        except Exception as exc:  # noqa: BLE001 - fail-closed per step
            report["steps"][name] = {"ok": False, "error": str(exc)}
            return False
        _write_report(sub, report_name, step_report)
        report["steps"][name] = {
            "ok": step_ok,
            "report": str(sub / report_name),
        }
        return step_ok

    try:
        if not production_db.is_file():
            raise FileNotFoundError(
                f"production catalog not found: {production_db}"
            )
        md5_before = md5sum(production_db)
        replica_data = sub / "data"
        backup_catalog_ro(production_db, replica_data / N_LEG_DB_REL)
        manifest = load_manifest(args.manifest)
        ok = _step(
            "precheck",
            PRECHECK_REPORT,
            lambda: gather_precheck(replica_data, manifest, production=False),
        ) and ok
        ok = _step(
            "snapshot",
            SNAPSHOT_REPORT,
            lambda: take_snapshot(replica_data, sub, production=False),
        ) and ok
        ok = _step(
            "migrate",
            MIGRATE_REPORT,
            lambda: perform_migration(
                replica_data,
                manifest,
                args.git_sha,
                production=False,
            ),
        ) and ok
        ok = _step(
            "post_verify",
            POST_VERIFY_REPORT,
            lambda: run_post_verify(replica_data, work_dir=sub),
        ) and ok
        ok = _step(
            "restore",
            RESTORE_REPORT,
            # The rehearsal restores AFTER the replica migrated, i.e. at the
            # post-migration fence — exactly the path that requires the force
            # acknowledgment in a real window. The dry-run rehearses that
            # forced-restore mechanism on the replica, never on production.
            lambda: perform_restore(
                replica_data,
                sub,
                production=False,
                force=True,
                force_acknowledgment=RESTORE_FORCE_ACKNOWLEDGMENT,
            ),
        ) and ok

        snapshot_report = _read_json_if_present(sub / SNAPSHOT_REPORT)
        restore_report = _read_json_if_present(sub / RESTORE_REPORT)
        snapshot_md5 = None
        snapshot_fence = None
        if isinstance(snapshot_report, dict):
            md5s = snapshot_report.get("md5")
            if isinstance(md5s, dict):
                snapshot_md5 = md5s.get("db")
            if isinstance(snapshot_report.get("fence_source"), int):
                snapshot_fence = snapshot_report["fence_source"]
        restored_md5 = (
            restore_report.get("restored_md5")
            if isinstance(restore_report, dict)
            else None
        )
        restored_fence = (
            restore_report.get("fence_after_restore")
            if isinstance(restore_report, dict)
            else None
        )
        round_trip = {
            "md5_matches_snapshot": (
                restored_md5 is not None and restored_md5 == snapshot_md5
            ),
            "fence": restored_fence,
            "fence_matches_snapshot": (
                restored_fence is not None and restored_fence == snapshot_fence
            ),
        }
        report["round_trip"] = round_trip
        if not (round_trip["md5_matches_snapshot"] and round_trip["fence_matches_snapshot"]):
            ok = False
    except Exception as exc:  # noqa: BLE001 - fail-closed with sidecar
        failure = str(exc)
        report["error"] = failure
        ok = False

    if md5_before is not None:
        md5_after = md5sum(production_db)
        unchanged = md5_before == md5_after
        sidecar = {
            "schema_version": "open_trader.prediction_cutover.dry_run_checksum.v1",
            "production_db": str(production_db),
            "md5_before": md5_before,
            "md5_after": md5_after,
            "zero_production_write": unchanged,
            "failure": failure,
            "replica_data_dir": str(sub / "data"),
            "report": str(work_dir / DRY_RUN_REPORT),
            "captured_at": _utc_now(),
        }
        sidecar_path = work_dir / (Path(DRY_RUN_REPORT).stem + CHECKSUM_SIDECAR_SUFFIX)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        report["production_md5_unchanged"] = unchanged
        report["checksum_sidecar"] = str(sidecar_path)
        if not unchanged:
            ok = False

    _write_report(work_dir, DRY_RUN_REPORT, report)
    return 0 if ok else 2


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _validate_git_sha(value: str) -> str | None:
    """The exact 40-hex git SHA, or None."""

    value = value.strip()
    if len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value):
        return value
    return None


def _validate_iso_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return True


def _cmd_evidence(args: argparse.Namespace) -> int:
    """Assemble ``cutover-evidence.json`` from the step reports.

    Fail-closed: every required step report must exist and the provenance
    inputs (exact git SHA, downtime window) must be valid, or nothing is
    assembled.  The ``irreversible_boundary`` field stays ``null`` until the
    first post-cutover business write; the adjacent note documents it.
    """

    work_dir = Path(args.work_dir)

    def _require(name: str) -> dict[str, object] | None:
        payload = _read_json_if_present(work_dir / name)
        if isinstance(payload, dict):
            return payload
        return None

    snapshot_report = _require(SNAPSHOT_REPORT)
    migrate_report = _require(MIGRATE_REPORT)
    stop_report = _require(STOP_VERIFY_REPORT)
    post_report = _require(POST_VERIFY_REPORT)
    precheck_report = _require(PRECHECK_REPORT)

    missing = [
        name
        for name, payload in (
            (SNAPSHOT_REPORT, snapshot_report),
            (MIGRATE_REPORT, migrate_report),
            (STOP_VERIFY_REPORT, stop_report),
            (POST_VERIFY_REPORT, post_report),
        )
        if payload is None
    ]
    errors: list[str] = []
    git_sha = _validate_git_sha(args.git_sha)
    if git_sha is None:
        errors.append(
            f"--git-sha must be the exact 40-hex commit SHA, got {args.git_sha!r}"
        )
    downtime_start = args.downtime_start.strip()
    downtime_end = args.downtime_end.strip()
    if not _validate_iso_timestamp(downtime_start):
        errors.append(
            f"--downtime-start is not an ISO timestamp: {args.downtime_start!r}"
        )
    if not _validate_iso_timestamp(downtime_end):
        errors.append(
            f"--downtime-end is not an ISO timestamp: {args.downtime_end!r}"
        )

    report: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA,
        "work_dir": str(work_dir),
        "captured_at": _utc_now(),
        "missing": missing,
        "errors": errors,
    }
    if missing or errors:
        _write_report(work_dir, EVIDENCE_REPORT, report)
        return 2

    assert snapshot_report is not None
    assert migrate_report is not None
    assert stop_report is not None
    assert post_report is not None

    migration = (
        migrate_report.get("migration")
        if isinstance(migrate_report.get("migration"), dict)
        else {}
    )
    activation = (
        migrate_report.get("activation")
        if isinstance(migrate_report.get("activation"), dict)
        else {}
    )
    snapshot_md5 = (
        snapshot_report.get("md5") if isinstance(snapshot_report.get("md5"), dict) else {}
    )
    snapshot_sha = (
        snapshot_report.get("sha256")
        if isinstance(snapshot_report.get("sha256"), dict)
        else {}
    )
    fingerprint = {
        "outcome": migration.get("outcome"),
        "total_unsettled_capital_units": migration.get(
            "total_unsettled_capital_units"
        ),
        "priced_execution_count": migration.get("priced_execution_count"),
        "reserved_reservation_count": migration.get("reserved_reservation_count"),
        "audit_event_id": migration.get("audit_event_id"),
        "published": activation.get("published"),
        "blocked": activation.get("blocked"),
        "approved_considered": activation.get("approved_considered"),
        "blocked_causes": activation.get("blocked_causes", {}),
        "actor": migrate_report.get("actor"),
        "git_sha": migrate_report.get("git_sha"),
    }
    precheck_catalog = (
        precheck_report.get("catalog")
        if precheck_report is not None
        and isinstance(precheck_report.get("catalog"), dict)
        else None
    )
    catalog_counts: dict[str, object] = {
        "published": activation.get("published"),
        "blocked": activation.get("blocked"),
        "approved_considered": activation.get("approved_considered"),
    }
    if precheck_catalog is not None:
        catalog_counts["precheck"] = precheck_catalog
    stop_labels = (
        stop_report.get("labels") if isinstance(stop_report.get("labels"), dict) else {}
    )
    stop_lock = (
        stop_report.get("lock") if isinstance(stop_report.get("lock"), dict) else {}
    )
    stop_record = (
        stop_report.get("runtime_record")
        if isinstance(stop_report.get("runtime_record"), dict)
        else {}
    )
    post_controls = (
        post_report.get("controls") if isinstance(post_report.get("controls"), dict) else None
    )
    evidence: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA,
        "captured_at": _utc_now(),
        "git_sha": git_sha,
        "snapshot": {
            "db_md5": snapshot_md5.get("db"),
            "siblings_md5": snapshot_md5.get("siblings", {}),
            "db_sha256": snapshot_sha.get("db"),
            "set_sha256": snapshot_sha.get("set"),
            "fence_source": snapshot_report.get("fence_source"),
            "fence_snapshot": snapshot_report.get("fence_snapshot"),
        },
        "migration_fingerprint": fingerprint,
        "fence": {
            "before": migrate_report.get("fence_before"),
            "after": migrate_report.get("fence_after"),
        },
        "catalog_counts": catalog_counts,
        "downtime": {
            "start": downtime_start,
            "end": downtime_end,
        },
        "stop_verify": {
            "labels": stop_labels,
            "lock": stop_lock,
            "runtime_record": stop_record,
            "operator_bootout_commands": stop_report.get(
                "operator_bootout_commands", []
            ),
        },
        "post_verify": {
            "fence": post_report.get("fence"),
            "controls": post_controls,
            "scope_capability": post_report.get("scope_capability"),
            "active_count": post_report.get("active_count"),
            "published_from_migrate": post_report.get("published_from_migrate"),
            "url": post_report.get("url"),
            "url_probes": post_report.get("url_probes", {}),
            "failures": post_report.get("failures", []),
        },
        # Null until the first post-cutover business write crosses the
        # N_LEG capital boundary; after that point the snapshot restore path
        # is no longer a valid rollback.  The note keeps the evidence file
        # self-describing without inventing a timestamp.
        "irreversible_boundary": None,
        "irreversible_boundary_note": (
            "null until the first post-cutover business write; after that "
            "write the pre-migration snapshot is no longer a valid rollback "
            "target (no partial rollback policy)"
        ),
    }
    _write_report(work_dir, EVIDENCE_REPORT, evidence)
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _cmd_precheck(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    report, ok = gather_precheck(
        args.data_dir, manifest, production=args.production
    )
    _write_report(args.work_dir, PRECHECK_REPORT, report)
    return 0 if ok else 2


def _cmd_snapshot(args: argparse.Namespace) -> int:
    report, ok = take_snapshot(
        args.data_dir, args.work_dir, production=args.production
    )
    _write_report(args.work_dir, SNAPSHOT_REPORT, report)
    return 0 if ok else 2


def _cmd_restore(args: argparse.Namespace) -> int:
    report, ok = perform_restore(
        args.data_dir,
        args.work_dir,
        production=args.production,
        force=args.force,
        force_acknowledgment=args.force_acknowledgment,
        runtime_root=args.runtime_root,
        launchctl_bin=args.launchctl_bin,
    )
    _write_report(args.work_dir, RESTORE_REPORT, report)
    return 0 if ok else 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_nleg_cutover",
        description=(
            "Issue #60 N_LEG cutover orchestrator: fail-closed planned-downtime "
            "window driver (exit 0 only when everything checks; exit 2 with a "
            "machine-readable report otherwise)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    precheck = subparsers.add_parser(
        "precheck",
        help="Read-only cutover precheck of one target data dir.",
    )
    precheck.add_argument("--data-dir", type=Path, required=True)
    precheck.add_argument("--manifest", type=Path, required=True)
    precheck.add_argument(
        "--production",
        action="store_true",
        help="Acknowledge the target is the production data dir (read-only command; recorded in the report).",
    )
    precheck.add_argument("--work-dir", type=Path, required=True)
    precheck.set_defaults(func=_cmd_precheck)

    snapshot = subparsers.add_parser(
        "snapshot",
        help="Online-backup copy of the target data dir into <work-dir>/snapshot/.",
    )
    snapshot.add_argument("--data-dir", type=Path, required=True)
    snapshot.add_argument(
        "--production",
        action="store_true",
        help="Acknowledge the source is the production data dir (read-only command; recorded in the report).",
    )
    snapshot.add_argument("--work-dir", type=Path, required=True)
    snapshot.set_defaults(func=_cmd_snapshot)

    restore = subparsers.add_parser(
        "restore",
        help="Whole-file restore of the work-dir snapshot set over the target.",
    )
    restore.add_argument("--data-dir", type=Path, required=True)
    restore.add_argument(
        "--production",
        action="store_true",
        help="Acknowledge a production restore (owner-stopped guard applies).",
    )
    restore.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="Runtime root for the production guard (default: the data dir's parent).",
    )
    restore.add_argument(
        "--launchctl-bin",
        default=os.environ.get("OPEN_TRADER_LAUNCHCTL_BIN", "launchctl"),
        help="launchctl binary used by the production guard probes.",
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help="Allow a restore when the target fence has already reached the N_LEG generation.",
    )
    restore.add_argument(
        "--force-acknowledgment",
        default=None,
        help="Must match RESTORE_FORCE_ACKNOWLEDGMENT exactly when --force is given.",
    )
    restore.add_argument("--work-dir", type=Path, required=True)
    restore.set_defaults(func=_cmd_restore)

    migrate = subparsers.add_parser(
        "migrate",
        help="Run the slice-1 N_LEG migration + APPROVED activation on a data dir.",
    )
    migrate.add_argument("--data-dir", type=Path, required=True)
    migrate.add_argument("--manifest", type=Path, required=True)
    migrate.add_argument("--git-sha", required=True)
    migrate.add_argument(
        "--production",
        action="store_true",
        help="Allow the production data dir (requires the owner-stopped guard).",
    )
    migrate.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="Runtime root for the production guard (default: the data dir's parent).",
    )
    migrate.add_argument(
        "--launchctl-bin",
        default=os.environ.get("OPEN_TRADER_LAUNCHCTL_BIN", "launchctl"),
        help="launchctl binary used by the production guard probes.",
    )
    migrate.add_argument("--work-dir", type=Path, required=True)
    migrate.set_defaults(func=_cmd_migrate)

    maintenance = subparsers.add_parser(
        "maintenance",
        help="Atomically rewrite the prediction route record (maintenance|service).",
    )
    maintenance.add_argument("--route-file", type=Path, required=True)
    maintenance.add_argument("--mode", choices=list(ROUTE_MODES), required=True)
    maintenance.add_argument(
        "--operation-id",
        default=None,
        help="Operation id recorded in the route record (default: generated).",
    )
    maintenance.add_argument("--work-dir", type=Path, required=True)
    maintenance.set_defaults(func=_cmd_maintenance)

    stop_verify = subparsers.add_parser(
        "stop-verify",
        help="Read-only probes proving the prediction runtime owners are gone.",
    )
    stop_verify.add_argument("--runtime-root", type=Path, required=True)
    stop_verify.add_argument(
        "--launchctl-bin",
        default=os.environ.get("OPEN_TRADER_LAUNCHCTL_BIN", "launchctl"),
        help="launchctl binary used for the label probes.",
    )
    stop_verify.add_argument("--work-dir", type=Path, required=True)
    stop_verify.set_defaults(func=_cmd_stop_verify)

    post_verify = subparsers.add_parser(
        "post-verify",
        help="Read-only proof of the migrated state (plus optional live probes).",
    )
    post_verify.add_argument("--data-dir", type=Path, required=True)
    post_verify.add_argument(
        "--url",
        default=None,
        help="Base URL of the running prediction service to probe (optional).",
    )
    post_verify.add_argument(
        "--expected-git-sha",
        default=None,
        help="Git SHA the healthz endpoint must report (when --url is given).",
    )
    post_verify.add_argument("--work-dir", type=Path, required=True)
    post_verify.set_defaults(func=_cmd_post_verify)

    dry_run = subparsers.add_parser(
        "dry-run",
        help="Run the whole pipeline on a replica of a fixture production dir.",
    )
    dry_run.add_argument("--production-data-dir", type=Path, required=True)
    dry_run.add_argument("--manifest", type=Path, required=True)
    dry_run.add_argument("--git-sha", required=True)
    dry_run.add_argument("--work-dir", type=Path, required=True)
    dry_run.set_defaults(func=_cmd_dry_run)

    evidence = subparsers.add_parser(
        "evidence",
        help="Assemble cutover-evidence.json from the step reports in the work dir.",
    )
    evidence.add_argument("--work-dir", type=Path, required=True)
    evidence.add_argument(
        "--git-sha",
        required=True,
        help="The exact 40-hex git SHA the cutover ran at.",
    )
    evidence.add_argument(
        "--downtime-start", required=True, help="ISO timestamp of the window start."
    )
    evidence.add_argument(
        "--downtime-end", required=True, help="ISO timestamp of the window end."
    )
    evidence.set_defaults(func=_cmd_evidence)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - fail-closed, never a traceback
        work_dir = getattr(args, "work_dir", None)
        if work_dir is not None:
            try:
                _write_report(
                    Path(work_dir),
                    "error.json",
                    {
                        "schema_version": "open_trader.prediction_cutover.error.v1",
                        "command": getattr(args, "command", ""),
                        "error": str(exc),
                        "captured_at": _utc_now(),
                    },
                )
            except OSError:
                pass
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
