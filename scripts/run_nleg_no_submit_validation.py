#!/usr/bin/env python3
"""Issue #71 finishing: isolated-catalog activation orchestrator.

Completes one real N>=3 no-submit validation run without ever writing the
production catalog:

1. Copy the production SQLite catalog (``data/prediction_arbitrage/
   prediction_arbitrage.sqlite3``) into an isolated work directory via the
   SQLite online-backup API with the source opened read-only (``mode=ro``).
2. Derive one same-event same-venue N>=3 exhaustive-group (EXACTLY_ONE)
   relation from real Polymarket NegRisk venue metadata through the existing
   mechanical codecs (#103) — never hand-written fields.  A specific event
   can be selected with ``--event`` (id) or ``--slug``; otherwise the top
   events by 24h volume are auto-scanned.  ``--events-json`` supplies
   synthetic venue metadata instead (tests only).
3. Activate the derived relation ONLY inside the replica (ingest -> approve
   -> activation through the existing v2-backed facade, which runs the v2
   batch activation core and records the activation bookkeeping) so
   ``readonly_v2_relations`` sees it as ACTIVE.
4. Invoke the existing harness CLI (``prediction nleg-validate``) with the
   replica catalog, the read-only live book source, and the isolated data
   dir.
5. Prove zero production writes by checksumming the production database
   before and after the run and recording both digests in a sidecar note
   next to the report (written whenever the before-checksum was taken —
   refused/failed runs included, carrying the step refusal reason).

Work-directory layout::

    <work-dir>/catalog/prediction_arbitrage/prediction_arbitrage.sqlite3
    <work-dir>/run/                                   (harness --data-dir)
    <work-dir>/report.json                            (harness --report)
    <work-dir>/report.production-checksum.json        (sidecar note)

``--fresh-replica`` selects the V2 test mode proven by real runs: instead of
copying the production catalog (whose inherited APPROVED/PENDING timeline
makes the one-shot approve->activate fail the generation-consistency gate as
``ACTIVATION_BLOCKED_INCONSISTENT``), it creates an EMPTY catalog directly at
the replica path, derives, and activates there.  The production database is
never opened for writing in either mode and the before/after md5 sidecar is
produced in both; the log states ``fresh replica (no production data)`` and
the default production-replica mode keeps failing honestly when the
activation gate refuses — it never silently switches modes.

Exit codes: 0 = report PASS, 1 = report FAIL, 2 = report BLOCKED or an
orchestrator guard/step refusal — including the replica-ready verify step,
a crashed harness call, and a harness call exiting via SystemExit (live
budget flags < 1 are already rejected at parse time, before any work and
before the default work directory's temp dir is created, which happens
only after flag validation passes) — with the reason on stderr (and,
whenever the before-checksum was taken, in the sidecar's ``failure``
field).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from open_trader.polymarket_monitor import (  # noqa: E402
    _collect_first_page,
)
from open_trader.polymarket_relation_discovery import (  # noqa: E402
    NegriskGroupRelation,
    _items,
    _json_model,
    _outcome_tokens,
    _text,
    _value,
    discover_mechanical_relation_catalog,
)
from open_trader.prediction_n_leg_validation import (  # noqa: E402
    readonly_v2_relations,
)
from open_trader.prediction_n_leg_validation_books import (  # noqa: E402
    set_contract_token_map,
)
from open_trader.relation_catalog import (  # noqa: E402
    RelationCatalog,
    _mechanical_complete_model,
    _mechanical_discovery_payload,
    default_catalog_path,
)
from open_trader.relation_catalog_v2 import (  # noqa: E402
    RelationCatalogV2,
    SqliteCatalogStore,
)

DEFAULT_PRODUCTION_DB = Path("data/prediction_arbitrage/prediction_arbitrage.sqlite3")
DEFAULT_BOOK_SOURCE = "open_trader.prediction_n_leg_validation_books:live_books"
CONTRACT_KEYED_BOOK_SOURCE = (
    "open_trader.prediction_n_leg_validation_books:contract_keyed_live_books"
)
DEFAULT_REPLAY_FIXTURE = _REPO / "tests" / "fixtures" / "prediction_n_leg_validation_frozen_n3.json"
DEFAULT_ACTOR = "nleg-orchestrator"
CATALOG_DIR_NAME = "catalog"
RUN_DIR_NAME = "run"
CHECKSUM_SIDECAR_SUFFIX = ".production-checksum.json"
MIN_GROUP_MARKETS = 3
AUTO_PICK_PAGE_SIZE = 100


def md5sum(path: str | Path) -> str:
    """Hex md5 digest of one file, streamed."""

    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_catalog_ro(source: str | Path, destination: str | Path) -> Path:
    """Copy one SQLite catalog via the online-backup API, source mode=ro."""

    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f"production catalog not found: {source}")
    if destination.exists():
        raise FileExistsError(f"replica catalog already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        replica = sqlite3.connect(destination)
        try:
            connection.backup(replica)
        finally:
            replica.close()
    finally:
        connection.close()
    return destination


def load_events_json(path: str | Path) -> list[object]:
    """Load synthetic venue metadata (tests only; never the production path)."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("--events-json must contain a JSON list of events")
    return raw


def fetch_neg_risk_events(
    *,
    event_id: str | None = None,
    slug: str | None = None,
) -> list[object]:
    """Fetch real venue metadata through the monitor's read-only public client.

    With ``event_id``/``slug`` exactly one event is fetched; otherwise the
    first page of open events ordered by 24h volume is returned for the
    auto-pick scan.  Read endpoints only.
    """

    from polymarket import AsyncPublicClient

    async def run() -> list[object]:
        client = AsyncPublicClient()
        try:
            if event_id or slug:
                event = await _maybe_await(
                    client.get_event(id=event_id, slug=slug)
                )
                return [event]
            return list(
                await _collect_first_page(
                    client.list_events(
                        closed=False,
                        ended=False,
                        order="volume24hr",
                        ascending=False,
                        page_size=AUTO_PICK_PAGE_SIZE,
                    )
                )
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await _maybe_await(close())

    return asyncio.run(run())


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def derive_n3_group(events: Sequence[object]) -> NegriskGroupRelation:
    """Derive one same-event same-venue N>=3 exhaustive group (#103 codecs)."""

    result = discover_mechanical_relation_catalog(list(events))
    for group in result.groups:
        if len(group.markets) >= MIN_GROUP_MARKETS:
            return group
    raise ValueError(
        "no negRisk event with >=3 eligible markets in venue metadata "
        f"(groups={len(result.groups)}, events_eligible={result.events_eligible}, "
        f"rejections={json.dumps(dict(result.rejection_counts), sort_keys=True)})"
    )


def build_contract_token_map(
    events: Sequence[object], group: NegriskGroupRelation
) -> dict[str, str]:
    """conditionId -> YES clobTokenId for the derived group's markets.

    Built from the same venue metadata the group was derived from and parsed
    by the discovery module's own outcome-token codec, so the mapping can
    never disagree with the mechanical relation (the group members carry the
    conditionId; the clobTokenIds live on the raw event markets).  Markets
    whose tokens did not parse are simply absent — the contract-keyed book
    wrapper skips unmapped ids without raising.
    """

    wanted = {market.condition_id for market in group.markets}
    mapping: dict[str, str] = {}
    for event in events:
        for raw_market in _items(_value(event, "markets", default=())):
            market = _json_model(raw_market)
            condition_id = _text(
                _value(market, "conditionId", "condition_id", default="")
            )
            if condition_id not in wanted or condition_id in mapping:
                continue
            tokens = _outcome_tokens(market)
            if tokens is not None:
                mapping[condition_id] = tokens["yes"]
    return mapping


def activate_replica_catalog(
    replica_db: str | Path,
    group: NegriskGroupRelation,
    *,
    actor: str,
    git_sha: str,
) -> dict[str, object]:
    """Ingest, approve and activate one derived relation inside the replica.

    The stored payload is the facade codec conversion of the mechanical
    relation plus one nested ``model`` mirror of exactly the same
    codec-produced compile artifacts: the facade/v2 activation chain reads
    the top-level compile fields while ``readonly_v2_relations`` (the
    harness's export) reads the nested ``model`` shape.  No field is
    invented and the production database is never opened for writing.
    """

    replica_db = Path(replica_db)
    if (
        replica_db.parent.name != "prediction_arbitrage"
        or replica_db.name != "prediction_arbitrage.sqlite3"
    ):
        raise ValueError(
            "replica catalog must live at "
            "<data-dir>/prediction_arbitrage/prediction_arbitrage.sqlite3"
        )
    # No write path may be derived: the facade's store must open exactly the
    # replica db itself, so the conventional path under the replica's own
    # data dir is verified against it (resolved) before any store exists.
    replica_data_dir = replica_db.parent.parent
    if default_catalog_path(replica_data_dir).resolve() != replica_db.resolve():
        raise ValueError(
            "replica catalog must live at the conventional path "
            f"({default_catalog_path(replica_data_dir)}); refusing to open a "
            "derived write path"
        )
    facade = RelationCatalog(replica_data_dir)
    discovery_payload = _mechanical_discovery_payload(
        group, _mechanical_complete_model(group)
    )
    converted = facade._converted(discovery_payload)
    payload = {
        **converted,
        "model": {
            key: converted[key]
            for key in ("terminal_states", "payouts", "capital_release", "problem")
            if converted.get(key) is not None
        },
    }
    v2 = RelationCatalogV2(SqliteCatalogStore(str(replica_db)))
    ingested = v2.ingest(payload)
    approved = facade.approve(
        ingested["version_id"],
        {"version_id": ingested["version_id"]},
        actor=actor,
        git_sha=git_sha,
    )
    if approved.get("activation") != "ACTIVE":
        raise RuntimeError(f"replica activation did not become ACTIVE: {approved}")
    exported = readonly_v2_relations(replica_db)
    row = exported["rows"].get(str(ingested["identity"]))
    if row is None or row.get("activation") != "ACTIVE":
        raise RuntimeError(
            f"replica activation not visible as ACTIVE: {ingested['identity']}"
        )
    return {
        "identity": str(ingested["identity"]),
        "version_id": str(ingested["version_id"]),
        "activation": "ACTIVE",
        "endpoints": len(row.get("endpoints") or ()),
    }


def _write_hits_production(target: Path, production_paths: set[Path]) -> bool:
    """Whether one write target lands on any guarded production path.

    Exact resolved-Path equality misses case variants on case-insensitive
    volumes (macOS realpath does not case-normalize, so an upper-case
    spelling of the production file resolves to a different Path while
    denoting the very same file).  An existing target is therefore compared
    with ``os.path.samefile`` against every guarded member — correct on
    every platform (review fix round 5).
    """

    resolved: Path | None = None
    with contextlib.suppress(OSError):
        resolved = target.resolve()
    if resolved is not None and resolved in production_paths:
        return True
    if target.exists():
        for member in production_paths:
            with contextlib.suppress(OSError):
                if os.path.samefile(target, member):
                    return True
        return False
    # A target that does not exist cannot be samefiled; refuse exact or
    # casefolded equality with a guarded member instead — conservative on
    # case-insensitive volumes, where creating that name later would land
    # on the guarded production file itself (review fix round 5).
    if resolved is None:
        return False
    folded = str(resolved).casefold()
    return any(
        str(member) == str(resolved) or str(member).casefold() == folded
        for member in production_paths
    )


def _refusal_reason(
    production_db: Path,
    live_catalog: Path,
    report_path: Path | None = None,
) -> str | None:
    """The orchestrator's own isolation guard (before any write happens)."""

    production_paths = {production_db.resolve()}
    with contextlib.suppress(OSError):
        production_paths.add(DEFAULT_PRODUCTION_DB.resolve())
    # WAL-mode siblings are online parts of the production database: a
    # report or sidecar landing on <db>-wal/-shm/-journal is flushed into
    # the main file at the next checkpoint (corrupting it) while the
    # main-file md5 sidecar still compares equal — falsified zero-write
    # evidence (review fix round 4).  The whole sibling set joins the same
    # guard set, checked uniformly for --live-catalog, --report and the
    # checksum sidecar below.
    for base in tuple(production_paths):
        for suffix in ("-wal", "-shm", "-journal"):
            production_paths.add(base.with_name(base.name + suffix))
    if _write_hits_production(live_catalog, production_paths):
        return (
            f"refusing: --live-catalog points at the production catalog "
            f"({live_catalog}); activation is only allowed inside the "
            f"isolated replica"
        )
    # The activation facade/store open the conventional catalog path derived
    # from the replica's own location; if that derived write path (resolved)
    # lands on the production database — e.g. --live-catalog placed inside
    # the production data dir under a different file name — refuse here,
    # before anything is opened.
    with contextlib.suppress(OSError):
        derived = default_catalog_path(live_catalog.parent.parent)
        if _write_hits_production(derived, production_paths):
            return (
                f"refusing: --live-catalog's derived catalog write path "
                f"({derived}) points at the production catalog "
                f"({production_db}); activation is only allowed inside the "
                f"isolated replica"
            )
    # The harness writes the report JSON verbatim to --report and the
    # orchestrator writes the checksum sidecar next to it; either landing on
    # the production database would overwrite it, so both resolved paths are
    # checked against the same production path set before any write (fix
    # round 3, case-safe membership per fix round 5).
    if report_path is not None:
        # A degenerate target ("." or "/") carries an empty name: forming
        # its sidecar candidate with ``with_suffix`` raises ValueError, and
        # a guard exception must never escape the refusal contract (review
        # fix round 5) — it is refused here instead.
        if not report_path.name:
            return (
                f"refusing: report output path ({report_path}) has an "
                f"empty file name; the report and its checksum sidecar "
                f"must be regular files"
            )
        for target in (
            report_path,
            report_path.with_suffix(CHECKSUM_SIDECAR_SUFFIX),
        ):
            if _write_hits_production(target, production_paths):
                return (
                    f"refusing: report output path ({target}) points at "
                    f"the production catalog ({production_db}); the "
                    f"report and its checksum sidecar must never "
                    f"overwrite it"
                )
    return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_nleg_no_submit_validation",
        description=(
            "Isolated-catalog N>=3 no-submit validation: replica activation "
            "plus harness run with zero production writes."
        ),
    )
    parser.add_argument(
        "--production-db",
        type=Path,
        default=DEFAULT_PRODUCTION_DB,
        help="Production catalog SQLite path (read-only for this script)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Isolated work directory (default: fresh temp dir, created only after flag validation passes)",
    )
    parser.add_argument(
        "--live-catalog",
        type=Path,
        default=None,
        help="Replica catalog path (default: <work-dir>/catalog/prediction_arbitrage/prediction_arbitrage.sqlite3)",
    )
    parser.add_argument(
        "--replay",
        type=Path,
        default=DEFAULT_REPLAY_FIXTURE,
        help="Frozen N>=3 replay snapshot for the harness CLI",
    )
    parser.add_argument(
        "--book-source",
        default=None,
        help=(
            "MODULE:ATTR read-only book source for the harness CLI "
            "(default: contract-keyed live books when the derived mapping "
            "is available, else the token-keyed live_books)"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Report JSON path (default: <work-dir>/report.json)",
    )
    parser.add_argument(
        "--event",
        default=None,
        help="NegRisk event id for venue-metadata derivation (default: auto-pick)",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="NegRisk event slug for venue-metadata derivation (default: auto-pick)",
    )
    parser.add_argument(
        "--events-json",
        type=Path,
        default=None,
        help="Synthetic venue metadata JSON list (tests only; skips the fetch)",
    )
    parser.add_argument(
        "--replica-ready",
        action="store_true",
        help=(
            "Skip backup/derive/activate and run the harness against the "
            "pre-activated replica given by --live-catalog (tests only)"
        ),
    )
    parser.add_argument(
        "--fresh-replica",
        action="store_true",
        help=(
            "Create an EMPTY catalog at the replica path instead of copying "
            "the production database (V2 test mode; derive + activate still "
            "run; production md5 sidecar still proves zero writes)"
        ),
    )
    parser.add_argument(
        "--live-max-joint-states",
        type=int,
        default=None,
        help=(
            "Pass --live-max-joint-states to the harness CLI (live-path "
            "budget; default keeps the fixed validation budget value)"
        ),
    )
    parser.add_argument(
        "--live-max-quantity-vectors",
        type=int,
        default=None,
        help=(
            "Pass --live-max-quantity-vectors to the harness CLI (live-path "
            "budget; default keeps the fixed validation budget value)"
        ),
    )
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument("--git-sha", default=DEFAULT_ACTOR)
    args = parser.parse_args(argv)
    # Same >=1 contract the harness CLI enforces for these flags, enforced
    # here first: rejecting before the checksum means the rejection is a plain
    # parse error (exit 2, reason on stderr) with no work directory and no
    # sidecar expected — instead of a forwarded harness parser.error whose
    # SystemExit used to escape the refusal contract below (fix round 3).
    for flag_name, value in (
        ("--live-max-joint-states", args.live_max_joint_states),
        ("--live-max-quantity-vectors", args.live_max_quantity_vectors),
    ):
        if value is not None and value < 1:
            parser.error(f"refusing: {flag_name} must be >= 1 (got {value})")
    if args.work_dir is None:
        # Lazily created only after the flag validation above passed: an
        # eager argparse default called mkdtemp at parse time, creating (and
        # leaking) an empty temp directory even on refused parses — against
        # the "validation happens before any filesystem work" disclosure
        # (review fix round 4).
        args.work_dir = Path(tempfile.mkdtemp(prefix="nleg-no-submit-"))
    return args


def _log(message: str) -> None:
    print(f"[nleg-no-submit] {message}", file=sys.stderr)


def _verify_replica_ready(replica_db: Path) -> dict[str, object]:
    """Fail fast unless the pre-activated replica holds an ACTIVE N>=3 relation."""

    if not replica_db.is_file():
        raise RuntimeError(f"replica catalog not found: {replica_db}")
    exported = readonly_v2_relations(replica_db)
    for identity, row in exported["rows"].items():
        if row.get("activation") == "ACTIVE" and len(row.get("endpoints") or ()) >= MIN_GROUP_MARKETS:
            return {"identity": identity, "endpoints": len(row["endpoints"])}
    raise RuntimeError(
        f"replica catalog has no ACTIVE N>={MIN_GROUP_MARKETS} relation: {replica_db}"
    )


def _harness_argv(
    args: argparse.Namespace, replica_db: Path, report_path: Path
) -> list[str]:
    argv = [
        "--replay",
        str(args.replay),
        "--live-catalog",
        str(replica_db),
        "--data-dir",
        str(args.work_dir / RUN_DIR_NAME),
        "--report",
        str(report_path),
    ]
    if args.book_source:
        argv += ["--book-source", args.book_source]
    if args.live_max_joint_states is not None:
        argv += ["--live-max-joint-states", str(args.live_max_joint_states)]
    if args.live_max_quantity_vectors is not None:
        argv += ["--live-max-quantity-vectors", str(args.live_max_quantity_vectors)]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    work_dir = args.work_dir
    production_db = Path(args.production_db)
    replica_db = Path(
        args.live_catalog
        or (
            work_dir
            / CATALOG_DIR_NAME
            / "prediction_arbitrage"
            / "prediction_arbitrage.sqlite3"
        )
    )
    report_path = Path(args.report or (work_dir / "report.json"))
    try:
        refusal = _refusal_reason(production_db, replica_db, report_path)
    except Exception as exc:  # noqa: BLE001 - a failing guard refuses
        # The guard's own failures (e.g. a production path whose resolve()
        # fails with ELOOP) are refusals too — never a traceback escaping
        # the exit-code contract (review fix round 5).  This sits before
        # the checksum, so no sidecar is due here.
        print(f"refusing: isolation guard failed: {exc}", file=sys.stderr)
        return 2
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2
    if args.replica_ready and args.live_catalog is None:
        print(
            "refusing: --replica-ready requires --live-catalog",
            file=sys.stderr,
        )
        return 2
    if args.fresh_replica and args.replica_ready:
        print(
            "refusing: --fresh-replica and --replica-ready are mutually "
            "exclusive (fresh mode derives and activates its own empty replica)",
            file=sys.stderr,
        )
        return 2
    if not production_db.is_file():
        print(
            f"refusing: production catalog not found: {production_db}",
            file=sys.stderr,
        )
        return 2

    checksum_before = md5sum(production_db)
    activated: dict[str, object] = {}
    contract_map: dict[str, str] = {}
    step = "backup"
    code = 0
    failure: str | None = None
    try:
        if args.replica_ready:
            step = "verify"
            activated = {
                "identity": _verify_replica_ready(replica_db)["identity"],
                "replica_ready": True,
            }
            _log(f"using pre-activated replica: {replica_db}")
        else:
            if args.fresh_replica:
                if replica_db.exists():
                    # Raised, not an early return: the before-checksum was
                    # already taken, so this refusal must fall through to the
                    # unified sidecar write below (review fix round 2).
                    raise RuntimeError(
                        f"refusing: --fresh-replica builds an empty replica, but "
                        f"a catalog already exists: {replica_db}"
                    )
                _log(
                    f"fresh replica (no production data): creating empty catalog "
                    f"at {replica_db}"
                )
            else:
                _log(f"backing up production catalog (mode=ro): {production_db}")
                backup_catalog_ro(production_db, replica_db)
            step = "derive"
            if args.events_json is not None:
                events = load_events_json(args.events_json)
            else:
                events = fetch_neg_risk_events(
                    event_id=args.event, slug=args.slug
                )
            group = derive_n3_group(events)
            _log(
                f"derived {group.relation_type} group over {len(group.markets)} "
                f"markets of event {group.event_id}"
            )
            contract_map = build_contract_token_map(events, group)
            set_contract_token_map(contract_map)
            step = "activate"
            activated = activate_replica_catalog(
                replica_db,
                group,
                actor=args.actor,
                git_sha=args.git_sha,
            )
            _log(f"activated in replica: {activated['identity']}")

        # Explicit --book-source always passes through verbatim; the default
        # resolves to the contract-keyed wrapper when the derived group produced
        # a conditionId -> YES token mapping (real-run gap B: harness actions are
        # keyed by conditionId while get_order_books is keyed by clobTokenId),
        # else to the token-keyed default.
        if args.book_source is None:
            args.book_source = (
                CONTRACT_KEYED_BOOK_SOURCE
                if contract_map
                else DEFAULT_BOOK_SOURCE
            )
            if contract_map:
                _log(
                    "no --book-source given; using contract-keyed live books "
                    "(conditionId -> YES clobTokenId from derived venue metadata)"
                )

        from open_trader.prediction_n_leg_validation import main as harness_main

        step = "harness"
        _log("running harness CLI: prediction nleg-validate")
        code = harness_main(_harness_argv(args, replica_db, report_path))
    except SystemExit as exc:
        # A harness-side parser.error (or any other SystemExit on the harness
        # path) does not derive from Exception: without this branch it used to
        # escape with no stderr refusal line and no sidecar (fix round 3).
        # Folded into the same refusal contract; a harness that returns
        # normally still passes its 0/1/2 exit code through untouched.
        failure = f"{step}: harness call exited via SystemExit: {exc}"
        _log(f"refused: {failure}")
        code = 2
    except Exception as exc:
        # Docstring contract: a step refusal reports one stderr reason line
        # and exits 2 — never a traceback (exit 1).  The verify step and the
        # harness call sit under the same contract (review fix round 2); a
        # harness that returns normally still passes its 0/1/2 exit code
        # through untouched.
        failure = f"{step}: {exc}"
        _log(f"refused: {failure}")
        code = 2

    # The sidecar is produced whenever the before-checksum was taken — on a
    # failed/refused run too, since that is where zero-write evidence matters
    # most (real-run gap A surfaced exactly here).
    checksum_after = md5sum(production_db)
    sidecar = {
        "schema_version": "open_trader.run_nleg_no_submit_validation.checksum.v1",
        "production_db": str(production_db),
        "md5_before": checksum_before,
        "md5_after": checksum_after,
        "zero_production_write": checksum_before == checksum_after,
        "failure": failure,
        "replica_catalog": str(replica_db),
        "activated_identity": str(activated.get("identity", "")),
        "report": str(report_path),
        "captured_at": _utc_now(),
    }
    sidecar_path = report_path.with_suffix(CHECKSUM_SIDECAR_SUFFIX)
    # In --fresh-replica mode nothing creates the work directory before
    # activation (no backup step; the first mkdir lives inside
    # RelationCatalog.__init__), so the sidecar write itself must ensure the
    # report directory exists (review fix round 2).
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _log(
        f"production checksum {'UNCHANGED' if sidecar['zero_production_write'] else 'CHANGED'}: "
        f"{sidecar_path}"
    )
    return code


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
