"""Issue #71 finishing: isolated-catalog activation orchestrator tests.

The orchestrator (``scripts/run_nleg_no_submit_validation.py``) copies the
production catalog via the SQLite online-backup API into an isolated work
directory, derives one same-event same-venue N>=3 exhaustive-group relation
from real venue metadata through the existing mechanical codecs, activates it
only inside the replica, and never writes the production database.

These tests use throwaway SQLite replicas (the
``benchmark_relation_activation`` pattern) and synthetic venue events; the
real run against real NegRisk events is executed by the operator separately.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from open_trader.polymarket_relation_discovery import (
    NativeComplementMarket,
    NativeComplementRelation,
    NegriskGroupMarket,
    NegriskGroupRelation,
)
from open_trader.prediction_n_leg_validation import readonly_v2_relations
from open_trader.relation_catalog import RelationCatalog

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_nleg_no_submit_validation.py"
)
_SPEC = importlib.util.spec_from_file_location("run_nleg_no_submit_validation", _SCRIPT)
orchestrator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(orchestrator)


@pytest.fixture(autouse=True)
def clean_contract_registry():
    """The orchestrator injects the books registry in-process; reset it."""

    from open_trader.prediction_n_leg_validation_books import (
        set_contract_token_map,
    )

    set_contract_token_map({})
    yield
    set_contract_token_map({})


def neg_risk_event(*, event_id: str = "event-n3", market_count: int = 3) -> dict:
    """One official Polymarket negRisk snapshot event (flat SDK-style keys)."""

    markets = []
    for index in range(market_count):
        markets.append(
            {
                "id": f"market-{index}",
                "conditionId": f"condition-{index}",
                "question": f"Will outcome {index} happen?",
                "description": "official index reaches threshold",
                "resolutionSource": "Binance",
                "endDate": "2026-12-31T17:00:00Z",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": json.dumps(
                    [f"yes-{index}", f"no-{index}"]
                ),
            }
        )
    return {
        "id": event_id,
        "title": "Which outcome resolves?",
        "active": True,
        "closed": False,
        "ended": False,
        "negRisk": True,
        "markets": markets,
    }


def complement_relation() -> NativeComplementRelation:
    """A production-like two-leg relation for the production stand-in."""

    market = NativeComplementMarket(
        event_id="event-prod",
        market_id="market-prod",
        condition_id="condition-prod",
        question="Will it happen?",
        rules="official rules",
        resolution_source="Binance",
        end_date="2026-12-31T17:00:00Z",
        yes_token_id="yes-prod",
        no_token_id="no-prod",
        rules_hash="rules-prod",
    )
    return NativeComplementRelation(event_id="event-prod", market=market)


def seed_production_stand_in(data_dir: Path) -> Path:
    """Create a production-shaped catalog (two-leg relations, zero ACTIVE)."""

    catalog = RelationCatalog(data_dir)
    catalog.ingest_mechanical_relation(complement_relation())
    path = catalog.path
    quiesce_sqlite(path)
    return path


def quiesce_sqlite(db_path: Path) -> None:
    """Checkpoint and flush the WAL so the main file bytes are stable.

    The store keeps thread-local connections; their GC-triggered close would
    otherwise checkpoint the WAL at an arbitrary later moment and race the
    byte-for-byte zero-write assertions.
    """

    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def replica_path(work_dir: Path) -> Path:
    return (
        work_dir
        / "catalog"
        / "prediction_arbitrage"
        / "prediction_arbitrage.sqlite3"
    )


def test_derive_and_activate_replica_relation_is_active_in_readonly_export(
    tmp_path: Path,
) -> None:
    group = orchestrator.derive_n3_group([neg_risk_event()])
    assert group.relation_type == "EXACTLY_ONE"
    assert len(group.markets) >= 3

    replica = replica_path(tmp_path)
    result = orchestrator.activate_replica_catalog(
        replica,
        group,
        actor="test",
        git_sha="test",
    )
    assert result["activation"] == "ACTIVE"

    exported = readonly_v2_relations(replica)
    rows = [
        row
        for row in exported["rows"].values()
        if row["activation"] == "ACTIVE"
    ]
    assert len(rows) == 1
    endpoints = rows[0]["endpoints"]
    assert len(endpoints) >= 3
    assert {str(endpoint["venue"]).casefold() for endpoint in endpoints} == {
        "polymarket"
    }
    assert (
        len({endpoint["event_identity_basis"] for endpoint in endpoints}) == 1
    )


def test_orchestrator_refuses_production_live_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps([neg_risk_event()]), encoding="utf-8")

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(tmp_path / "work"),
            "--live-catalog",
            str(production),
            "--events-json",
            str(events_file),
        ]
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    # The guard fires before any write: the production file is untouched and
    # no replica was created.
    assert production.read_bytes() == before
    assert not (tmp_path / "work" / "catalog").exists()


def test_backup_and_activation_leave_production_bytes_unchanged(
    tmp_path: Path,
) -> None:
    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()

    replica = replica_path(tmp_path / "work")
    orchestrator.backup_catalog_ro(production, replica)
    group = orchestrator.derive_n3_group([neg_risk_event()])
    orchestrator.activate_replica_catalog(
        replica, group, actor="test", git_sha="test"
    )

    # The production stand-in is byte-identical; only the replica changed.
    assert production.read_bytes() == before
    exported = readonly_v2_relations(replica)
    assert any(
        row["activation"] == "ACTIVE" and len(row["endpoints"]) >= 3
        for row in exported["rows"].values()
    )


def seed_active_n3_relation(replica: Path) -> None:
    """Seed one synthetic ACTIVE N>=3 relation (established harness pattern).

    Uses the same nested-model payload shape the harness live tests seed
    (fixture-shaped contracts a/b/c): its two-kind terminal model fits the
    harness's fixed 16-joint-state validation budget, so the live path can
    prove a qualified opportunity.  The real mechanical codec always compiles
    five terminal kinds per contract (125 raw states at N=3), which the
    honest harness budget reports as UNKNOWN — that gap is the operator's
    real-run finding, not something this smoke may paper over.
    """

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "prediction_n_leg_validation_frozen_n3.json"
        ).read_text(encoding="utf-8")
    )
    payload = {
        "relation_type": "EXACTLY_ONE",
        "endpoints": [
            {"venue": "polymarket", "contract_id": contract}
            for contract in ("a", "b", "c")
        ],
        "model": {
            "terminal_states": [{}],
            "payouts": [{}],
            "capital_release": "2026-08-16T06:00:00Z",
            "problem": dict(fixture["problem"]),
        },
    }
    replica.parent.mkdir(parents=True, exist_ok=True)
    from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore

    store = SqliteCatalogStore(replica)
    catalog = RelationCatalogV2(store)
    entry = catalog.ingest(payload)
    catalog.approve(entry["version_id"], actor="test", git_sha="test")
    store.begin_write()
    store["versions"][entry["version_id"]]["activation_status"] = "ACTIVE"
    store.commit_write()
    quiesce_sqlite(replica)


def fake_live_books(token_ids: tuple[str, ...]):
    """Fixture-shaped books for the seeded synthetic relation (a/b/c)."""

    from datetime import UTC, datetime
    from decimal import Decimal

    from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook

    now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
    prices = {"a": "0.33", "b": "0.32", "c": "0.31"}
    return {
        token: ThresholdOrderBook(
            token,
            (BookLevel(Decimal(prices[token]), Decimal("10")),),
            (),
            now,
        )
        for token in token_ids
        if token in prices
    }


class FakeSolverServerOwner:
    """In-process solver owner returning real #50 evidence per request.

    Same seam the existing harness live tests use: the real owned
    ``SolverServerOwner`` spawns worker subprocesses whose per-request
    ``RLIMIT_AS`` application crashes on this macOS host (``ValueError:
    current limit exceeds maximum limit``), so the smoke exercises the
    orchestrator/harness chain with the established in-process solver.
    """

    def __init__(self, command: object) -> None:
        del command
        self.submit_calls = 0

    def submit(self, request: object):
        from concurrent.futures import Future

        from open_trader.prediction_n_leg import canonical_payload
        from open_trader.prediction_solver import (
            solve_with_constraint_generation,
        )
        from open_trader.prediction_solver_backends import CpSatBackend
        from open_trader.prediction_solver_worker import (
            WorkerOutcome,
            WorkerResponse,
        )

        self.submit_calls += 1
        evidence = solve_with_constraint_generation(
            request.request, CpSatBackend(), request.limits
        )
        future: Future = Future()
        future.set_result(
            WorkerOutcome(
                request.request_id,
                "OK",
                "COMPLETED",
                1,
                1,
                0,
                False,
                True,
                WorkerResponse(
                    "p",
                    "cp_sat",
                    request.request_id,
                    "OK",
                    canonical_payload(evidence),
                    {},
                    (),
                ),
                "9.15.6755",
            )
        )
        return future

    def close(self) -> None:
        return None


def test_cli_without_flags_assembles_original_live_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: with no budget flags the CLI live budget equals VALIDATION_BUDGET.

    Independent source of truth: the brief's fixed values 16/16 with 2
    support rechecks.  The replay budget default stays the original constant.
    """

    import inspect

    import open_trader.prediction_n_leg_validation as validation

    replica = replica_path(tmp_path / "work")
    seed_active_n3_relation(replica)
    captured: dict[str, object] = {}

    def fake_run_live(rows: object, **kwargs: object) -> dict[str, object]:
        del rows
        captured.update(kwargs)
        return {"status": "BLOCKED", "reason": "BUDGET_CAPTURE"}

    monkeypatch.setattr(validation, "run_live", fake_run_live)

    exit_code = validation.main(
        [
            "--live-catalog",
            str(replica),
            "--data-dir",
            str(tmp_path / "run"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert exit_code == 2  # BLOCKED report (stubbed live section)
    budget = captured["budget"]
    assert budget.max_quantity_vectors == 16
    assert budget.max_joint_states == 16
    assert budget.max_support_rechecks == 2
    assert budget == validation.VALIDATION_BUDGET
    # The replay path keeps its original constant default untouched.
    assert (
        inspect.signature(validation.run_replay).parameters["budget"].default
        is validation.VALIDATION_BUDGET
    )


def test_cli_rejects_non_positive_live_budget_flags() -> None:
    """B1: budget flag values must be >= 1; parser.error exits with code 2."""

    import open_trader.prediction_n_leg_validation as validation

    for flag in ("--live-max-joint-states", "--live-max-quantity-vectors"):
        with pytest.raises(SystemExit) as excinfo:
            validation.main([flag, "0"])
        assert excinfo.value.code == 2
        with pytest.raises(SystemExit) as excinfo:
            validation.main([flag, "-3"])
        assert excinfo.value.code == 2


def test_harness_argv_passes_live_budget_flags_through() -> None:
    """B3: the orchestrator forwards the live budget flags into harness argv."""

    args = orchestrator._parse_args(
        [
            "--live-max-joint-states",
            "256",
            "--live-max-quantity-vectors",
            "64",
        ]
    )
    argv = orchestrator._harness_argv(args, Path("replica.sqlite3"), Path("r.json"))

    assert argv[-4:] == [
        "--live-max-joint-states",
        "256",
        "--live-max-quantity-vectors",
        "64",
    ]


def test_harness_argv_without_live_budget_flags_omits_them() -> None:
    """B3: without the flags the harness argv stays exactly as before."""

    args = orchestrator._parse_args([])
    argv = orchestrator._harness_argv(args, Path("replica.sqlite3"), Path("r.json"))

    assert "--live-max-joint-states" not in argv
    assert "--live-max-quantity-vectors" not in argv


def no_arb_real_books(token_ids: tuple[str, ...]):
    """No-arbitrage books for the real codec relation (three 0.50 asks)."""

    from datetime import UTC, datetime
    from decimal import Decimal

    from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook

    now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
    return {
        token: ThresholdOrderBook(
            token,
            (BookLevel(Decimal("0.50"), Decimal("10")),),
            (),
            now,
        )
        for token in token_ids
    }


def add_min_profit_qualification(replica: Path) -> None:
    """Inject the established min-profit gate into the activated codec payload.

    Disclosed B2 deviation: the mechanical codec always stores
    ``qualification_constraints: []`` (probe-verified), and with empty
    constraints the harness's NO_QUALIFIED_OPPORTUNITY negative-proof path is
    unreachable at any price — the least-bad worker candidate then verifies as
    "qualified" and the live column honestly reports ``N_LESS_THAN_3``.  The
    established harness test pattern
    (``relation_payload(qualification=True)`` in
    ``tests/test_prediction_n_leg_validation.py``) reaches the negative proof
    through the same min-profit constraint used here, injected into BOTH
    payload copies (facade top-level ``problem`` and the nested ``model``
    mirror) of the already-activated real 5-terminal-kind codec relation.  The
    125-joint-state budget starvation of the red leg is untouched.
    """

    import sqlite3

    constraint = {
        "constraint_id": "min-profit",
        "rule_version": "v1",
        "metric": "GUARANTEED_PROFIT_UNITS",
        "comparison": "GREATER_THAN_OR_EQUAL",
        "threshold_numerator": 1,
        "threshold_denominator": 1,
    }
    connection = sqlite3.connect(replica)
    try:
        version_id, payload_raw = connection.execute(
            "SELECT version_id, payload FROM catalog_v2_versions"
        ).fetchone()
        payload = json.loads(payload_raw)
        payload["problem"]["qualification_constraints"] = [constraint]
        payload["model"]["problem"]["qualification_constraints"] = [constraint]
        connection.execute(
            "UPDATE catalog_v2_versions SET payload=? WHERE version_id=?",
            (json.dumps(payload), version_id),
        )
        connection.commit()
    finally:
        connection.close()


def test_real_codec_relation_budget_flags_turn_unknown_into_negative_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 red-green pair over the real mechanical codec relation.

    The relation is derived through ``derive_n3_group`` (5 terminal kinds per
    contract, 125 joint states at N=3) and activated inside the replica via
    ``activate_replica_catalog``.  Same scenario, both legs:

    - no budget flags: the real model starves the fixed 16-joint-state
      validation budget (probe: ``ORACLE_STATE_LIMIT_EXCEEDED``) -> UNKNOWN
      -> live FAIL ``NO_QUALIFIED_SOLUTION``;
    - ``--live-max-joint-states 256 --live-max-quantity-vectors 64``: the
      expanded live budget lets the exact oracle prove the negative
      (``NO_QUALIFIED_OPPORTUNITY``) -> live PASS, report PASS, no
      order-ready decision, zero side effects, zero production writes.
    """

    import open_trader.prediction_n_leg_validation_books as books_module

    group = orchestrator.derive_n3_group([neg_risk_event()])
    assert group.relation_type == "EXACTLY_ONE"
    assert len(group.markets) >= 3
    replica = replica_path(tmp_path / "catalog-work")
    orchestrator.activate_replica_catalog(replica, group, actor="test", git_sha="test")
    add_min_profit_qualification(replica)

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    monkeypatch.setattr(books_module, "live_books", no_arb_real_books)
    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        FakeSolverServerOwner,
    )

    base_args = [
        "--production-db",
        str(production),
        "--live-catalog",
        str(replica),
        "--replica-ready",
    ]

    work_unknown = tmp_path / "work-unknown"
    exit_code = orchestrator.main(base_args + ["--work-dir", str(work_unknown)])
    report = json.loads(
        (work_unknown / "report.json").read_text(encoding="utf-8")
    )
    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert report["live"]["status"] == "FAIL"
    assert report["live"]["reason"] == "NO_QUALIFIED_SOLUTION"
    assert report["live"]["zero_side_effects"]["submitted_orders"] == 0
    assert report["live"]["zero_side_effects"]["mutation_attempts"] == 0

    work_negative = tmp_path / "work-negative"
    exit_code = orchestrator.main(
        base_args
        + [
            "--work-dir",
            str(work_negative),
            "--live-max-joint-states",
            "256",
            "--live-max-quantity-vectors",
            "64",
        ]
    )
    report = json.loads(
        (work_negative / "report.json").read_text(encoding="utf-8")
    )
    assert exit_code == 0
    assert report["status"] == "PASS"
    assert report["live"]["status"] == "PASS"
    assert report["live"]["qualified_verified"] is False
    assert report["live"]["legs"] == 0
    # Negative-proof pass: no order-ready decision exists at all.
    assert report["live"]["execution_decision"] is None
    assert not (report["live"]["execution_decision"] or {}).get("order_ready")
    assert report["live"]["fingerprints"]["negative_proof"]
    assert report["live"]["zero_side_effects"]["submitted_orders"] == 0
    assert report["live"]["zero_side_effects"]["mutation_attempts"] == 0
    sidecar = json.loads(
        (work_negative / "report.production-checksum.json").read_text(
            encoding="utf-8"
        )
    )
    assert sidecar["zero_production_write"] is True
    assert production.read_bytes() == before


def test_end_to_end_synthetic_run_produces_pass_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_n_leg_validation_books as books_module

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_active_n3_relation(replica)
    # The harness resolves --book-source MODULE:ATTR by attribute at run
    # time, so patching the module attribute injects the fake books.
    monkeypatch.setattr(books_module, "live_books", fake_live_books)
    # The owned solver server's worker subprocesses crash on this macOS host
    # (per-request RLIMIT_AS application); use the established in-process
    # solver seam instead.
    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        FakeSolverServerOwner,
    )

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
        ]
    )

    assert exit_code == 0
    report = json.loads(
        (work_dir / "report.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "PASS"
    assert report["replay"]["status"] == "PASS"
    assert report["live"]["status"] == "PASS"
    assert report["live"]["execution_decision"]["order_ready"] is False
    assert report["zero_side_effects"]["submitted_orders"] == 0
    assert report["zero_side_effects"]["mutation_attempts"] == 0
    assert isinstance(report["pid"], int) and report["pid"] > 0
    assert report["cwd"]
    assert report["data_dir"]
    assert report["captured_at"]
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(encoding="utf-8")
    )
    assert sidecar["md5_before"] == sidecar["md5_after"]
    assert sidecar["zero_production_write"] is True
    assert sidecar["md5_before"] == orchestrator.md5sum(production)
    assert production.read_bytes() == before


# ---------------------------------------------------------------------------
# C2: --fresh-replica mode.  The default production-replica copy inherits the
# production APPROVED/PENDING timeline, so the one-shot approve->activate is
# refused by the generation-consistency gate (real run:
# ACTIVATION_BLOCKED_INCONSISTENT).  Fresh mode builds an EMPTY catalog
# instead (real run proved the same derived group activates ACTIVE there)
# while keeping the production md5 sidecar as zero-write evidence.
# ---------------------------------------------------------------------------


def test_fresh_replica_mode_end_to_end_pass_without_production_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import open_trader.prediction_n_leg_validation_books as books_module

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps([neg_risk_event()]), encoding="utf-8")

    monkeypatch.setattr(books_module, "live_books", no_arb_real_books)
    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        FakeSolverServerOwner,
    )
    # Disclosed B2 pattern: the mechanical codec always stores empty
    # qualification_constraints, so the negative-proof PASS path needs the
    # same min-profit injection the B2 red-green pair uses; applied right
    # after the orchestrator's real activation, test-side only.
    real_activate = orchestrator.activate_replica_catalog

    def activate_then_qualify(replica_db: Path, group: object, **kwargs: object):
        result = real_activate(replica_db, group, **kwargs)
        add_min_profit_qualification(replica_db)
        return result

    monkeypatch.setattr(orchestrator, "activate_replica_catalog", activate_then_qualify)

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--fresh-replica",
            "--events-json",
            str(events_file),
            "--live-max-joint-states",
            "256",
            "--live-max-quantity-vectors",
            "64",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    # Honest logging: fresh mode says exactly what it did.
    assert "fresh replica (no production data)" in captured.err

    report = json.loads(
        (work_dir / "report.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "PASS"
    assert report["live"]["status"] == "PASS"
    assert report["live"]["qualified_verified"] is False
    assert report["live"]["legs"] == 0
    assert report["live"]["execution_decision"] is None
    assert report["live"]["fingerprints"]["negative_proof"]
    assert report["live"]["zero_side_effects"]["submitted_orders"] == 0
    assert report["live"]["zero_side_effects"]["mutation_attempts"] == 0

    # The fresh replica holds exactly the derived ACTIVE N>=3 relation and no
    # production stand-in residue (the stand-in's event/condition ids never
    # appear anywhere in the exported relation set).
    exported = readonly_v2_relations(replica)
    active = [
        row for row in exported["rows"].values() if row["activation"] == "ACTIVE"
    ]
    assert len(active) == 1
    assert len(active[0]["endpoints"]) >= 3
    assert "prod" not in json.dumps(exported["rows"])

    # Production bytes untouched and the md5 sidecar is still produced in
    # fresh mode, proving zero production writes.
    assert production.read_bytes() == before
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(encoding="utf-8")
    )
    assert sidecar["md5_before"] == sidecar["md5_after"]
    assert sidecar["md5_before"] == orchestrator.md5sum(production)
    assert sidecar["zero_production_write"] is True


def test_fresh_replica_refuses_combination_with_replica_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    production = seed_production_stand_in(tmp_path / "prod")
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_active_n3_relation(replica)

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
            "--fresh-replica",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()


# ---------------------------------------------------------------------------
# C3: the orchestrator builds the conditionId -> YES clobTokenId mapping from
# the derived group's venue metadata, injects it into the books module before
# calling the harness (real-run gap B: actions are keyed by conditionId while
# get_order_books is keyed by clobTokenId -> MISSING_BOOKS), and resolves the
# default --book-source to the contract-keyed wrapper when a mapping exists.
# Explicit --book-source always passes through verbatim.
# ---------------------------------------------------------------------------


CONTRACT_KEYED_BOOK_SOURCE = (
    "open_trader.prediction_n_leg_validation_books:contract_keyed_live_books"
)


def _stub_harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    import open_trader.prediction_n_leg_validation as validation

    captured: dict[str, list[str]] = {}

    def fake_harness_main(argv: list[str]) -> int:
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(validation, "main", fake_harness_main)
    return captured


def _write_events(tmp_path: Path) -> Path:
    events_file = tmp_path / "events.json"
    events_file.write_text(
        json.dumps([neg_risk_event()]), encoding="utf-8"
    )
    return events_file


def test_orchestrator_injects_contract_map_and_resolves_default_book_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both derive modes inject the mapping; the default --book-source
    resolves to the contract-keyed wrapper when the mapping is non-empty."""

    import open_trader.prediction_n_leg_validation_books as books_module

    production = seed_production_stand_in(tmp_path / "prod")
    events_file = _write_events(tmp_path)
    captured = _stub_harness(monkeypatch)
    expected_map = {
        "condition-0": "yes-0",
        "condition-1": "yes-1",
        "condition-2": "yes-2",
    }

    # Fresh mode.
    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(tmp_path / "work-fresh"),
            "--fresh-replica",
            "--events-json",
            str(events_file),
        ]
    )
    assert exit_code == 0
    assert books_module.contract_token_map() == expected_map
    argv = captured["argv"]
    assert argv[argv.index("--book-source") + 1] == CONTRACT_KEYED_BOOK_SOURCE

    # Default production-replica mode injects the same way.
    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(tmp_path / "work-replica"),
            "--events-json",
            str(events_file),
        ]
    )
    assert exit_code == 0
    assert books_module.contract_token_map() == expected_map
    argv = captured["argv"]
    assert argv[argv.index("--book-source") + 1] == CONTRACT_KEYED_BOOK_SOURCE


def test_explicit_book_source_is_passed_through_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = seed_production_stand_in(tmp_path / "prod")
    events_file = _write_events(tmp_path)
    captured = _stub_harness(monkeypatch)

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(tmp_path / "work"),
            "--fresh-replica",
            "--events-json",
            str(events_file),
            "--book-source",
            "tests.example_module:example_books",
        ]
    )

    assert exit_code == 0
    argv = captured["argv"]
    assert (
        argv[argv.index("--book-source") + 1]
        == "tests.example_module:example_books"
    )


# ---------------------------------------------------------------------------
# Review fix round P2: the activation facade derives its store path from the
# replica's grandparent directory, so ``--live-catalog`` placed inside the
# production data dir under a nonstandard file name used to open the
# production database read-write.  The guard must refuse the derived write
# path, and the replica file itself must carry the conventional name.
# ---------------------------------------------------------------------------


def test_orchestrator_refuses_live_catalog_whose_derived_write_path_hits_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P2: refuse before any store is constructed when the derived write
    path resolves to the production catalog.

    Independent source of truth: the facade's write path is
    ``default_catalog_path(live_catalog.parent.parent)``, which here resolves
    to the production stand-in; the refusal must fire before
    ``SqliteCatalogStore`` ever receives that path (recorded via its
    constructor, per the approved brief).
    """

    from open_trader.relation_catalog_v2 import SqliteCatalogStore

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    events_file = _write_events(tmp_path)

    opened: list[str] = []
    real_init = SqliteCatalogStore.__init__

    def recording_init(self: object, db_path: object) -> None:
        opened.append(str(db_path))
        real_init(self, db_path)

    monkeypatch.setattr(SqliteCatalogStore, "__init__", recording_init)

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(tmp_path / "work"),
            "--live-catalog",
            str(production.parent / "validation.sqlite3"),
            "--events-json",
            str(events_file),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    # The production stand-in was never opened through the write-path store.
    assert str(production) not in opened
    assert production.read_bytes() == before


def test_activate_replica_catalog_rejects_nonstandard_replica_filename(
    tmp_path: Path,
) -> None:
    """P2: the replica file itself must be the conventional catalog name.

    Previously only the parent directory name was checked, so a sibling file
    inside the production data dir slipped through and the facade's derived
    store silently targeted the production database.
    """

    group = orchestrator.derive_n3_group([neg_risk_event()])
    replica = (
        tmp_path / "catalog" / "prediction_arbitrage" / "validation.sqlite3"
    )
    with pytest.raises(ValueError, match="prediction_arbitrage.sqlite3"):
        orchestrator.activate_replica_catalog(
            replica, group, actor="test", git_sha="test"
        )


# ---------------------------------------------------------------------------
# Review fix round P3: the docstring exit-code contract promises "guard/step
# refusal (reason on stderr)" with exit 2, but derive/backup/activate
# exceptions used to escape as a traceback with exit 1, and the md5 sidecar
# was only written after the harness — so the failed runs that most need
# zero-production-write evidence (real-run gap A: ACTIVATION_BLOCKED_INCONSISTENT)
# produced none.
# ---------------------------------------------------------------------------


def test_derive_refusal_exits_2_and_still_writes_checksum_sidecar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P3: a derive failure is a step refusal (stderr reason, exit 2) and the
    before/after md5 sidecar is still produced with zero_production_write."""

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    events_file = tmp_path / "events.json"
    # A valid JSON list with no N>=3 group: load succeeds, derive refuses.
    events_file.write_text(json.dumps([]), encoding="utf-8")

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--events-json",
            str(events_file),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "[nleg-no-submit] refused: derive:" in captured.err
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(
            encoding="utf-8"
        )
    )
    assert sidecar["md5_before"] == sidecar["md5_after"]
    assert sidecar["md5_before"] == orchestrator.md5sum(production)
    assert sidecar["zero_production_write"] is True
    assert "derive" in str(sidecar["failure"])
    assert production.read_bytes() == before


def test_activate_refusal_exits_2_and_still_writes_checksum_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P3: an activation failure (real-run gap A shape) is a step refusal and
    still records the zero-write checksum evidence before exiting 2."""

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    events_file = _write_events(tmp_path)

    def failing_activate(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("replica activation did not become ACTIVE: {...}")

    monkeypatch.setattr(orchestrator, "activate_replica_catalog", failing_activate)

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--events-json",
            str(events_file),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "[nleg-no-submit] refused: activate:" in captured.err
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(
            encoding="utf-8"
        )
    )
    assert sidecar["md5_before"] == sidecar["md5_after"]
    assert sidecar["zero_production_write"] is True
    assert "activate" in str(sidecar["failure"])
    assert production.read_bytes() == before


# ---------------------------------------------------------------------------
# Review fix round 2: three remaining gaps in the same defect class (the
# sidecar-must-write + exit-2 contract on specific refusal paths).
# ---------------------------------------------------------------------------


def test_fresh_replica_derive_refusal_exits_2_and_writes_sidecar_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T1: fresh-replica derive refusal still produces the sidecar.

    In ``--fresh-replica`` mode nothing creates the work directory before
    activation (no backup step; the first mkdir lives inside
    ``RelationCatalog.__init__``), so the sidecar write after a derive
    refusal used to crash with FileNotFoundError (traceback, exit 1, no
    zero-write evidence).  The real preflight is exactly this mode.
    """

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps([]), encoding="utf-8")

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--fresh-replica",
            "--events-json",
            str(events_file),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "[nleg-no-submit] refused: derive:" in captured.err
    assert "Traceback" not in captured.err
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(
            encoding="utf-8"
        )
    )
    assert sidecar["zero_production_write"] is True
    assert "derive" in str(sidecar["failure"])
    assert production.read_bytes() == before


def test_fresh_replica_existing_replica_refusal_writes_sidecar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T2: the fresh-replica "catalog already exists" refusal keeps its
    exit-2 stderr reason AND still records the zero-write sidecar (the
    before-checksum was already taken when the refusal fires)."""

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    replica.parent.mkdir(parents=True, exist_ok=True)
    replica.write_bytes(b"pre-existing replica catalog bytes\n")

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--fresh-replica",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    assert "Traceback" not in captured.err
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(
            encoding="utf-8"
        )
    )
    assert sidecar["md5_before"] == sidecar["md5_after"]
    assert sidecar["zero_production_write"] is True
    assert "refus" in str(sidecar["failure"]).lower()
    assert "already exists" in str(sidecar["failure"])
    # The refusal path must not clobber the pre-existing replica file.
    assert replica.read_bytes() == b"pre-existing replica catalog bytes\n"
    assert production.read_bytes() == before


def seed_pending_n3_relation(replica: Path) -> None:
    """Seed one ingested-but-never-activated N>=3 relation (no ACTIVE rows)."""

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "prediction_n_leg_validation_frozen_n3.json"
        ).read_text(encoding="utf-8")
    )
    payload = {
        "relation_type": "EXACTLY_ONE",
        "endpoints": [
            {"venue": "polymarket", "contract_id": contract}
            for contract in ("a", "b", "c")
        ],
        "model": {
            "terminal_states": [{}],
            "payouts": [{}],
            "capital_release": "2026-08-16T06:00:00Z",
            "problem": dict(fixture["problem"]),
        },
    }
    replica.parent.mkdir(parents=True, exist_ok=True)
    from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore

    store = SqliteCatalogStore(replica)
    catalog = RelationCatalogV2(store)
    catalog.ingest(payload)
    quiesce_sqlite(replica)


def test_replica_ready_verify_refusal_exits_2_with_stderr_and_sidecar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T3: a --replica-ready replica without an ACTIVE N>=3 relation is a
    step refusal (stderr reason, exit 2, sidecar) — never a traceback."""

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_pending_n3_relation(replica)

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "[nleg-no-submit] refused: verify:" in captured.err
    assert "no ACTIVE" in captured.err
    assert "Traceback" not in captured.err
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(
            encoding="utf-8"
        )
    )
    assert sidecar["zero_production_write"] is True
    assert "no ACTIVE" in str(sidecar["failure"])
    assert production.read_bytes() == before


# ---------------------------------------------------------------------------
# Review fix round 3: two remaining isolation/evidence-contract defects.
# T1: a < 1 budget flag forwarded to the harness lets the harness's own
# parser.error SystemExit escape the orchestrator's ``except Exception`` —
# no refusal line, no sidecar, and the work directory already created.
# T2: ``--report`` pointing at the production database lets the harness
# overwrite the production catalog with the report JSON.
# ---------------------------------------------------------------------------


def test_live_budget_flag_below_one_is_refused_before_any_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T1: ``--live-max-joint-states 0`` is refused with exit 2.

    Independent source of truth: the harness CLI's own contract for the same
    flags is ``value >= 1 else exit 2`` (its ``parser.error``), so the
    orchestrator must reject a forwarded < 1 value itself — before the
    checksum and before any work directory exists — with a stderr refusal.
    Previously the value sailed through to the harness whose
    ``parser.error`` SystemExit escaped ``except Exception``: no refusal
    line, no sidecar, work directory already created.
    """

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    events_file = _write_events(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main(
            [
                "--production-db",
                str(production),
                "--work-dir",
                str(work_dir),
                "--fresh-replica",
                "--events-json",
                str(events_file),
                "--live-max-joint-states",
                "0",
            ]
        )

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    # The validation fires before the checksum, so no sidecar exists — and
    # before any filesystem work: no work directory, no replica, production
    # bytes untouched.
    assert not work_dir.exists()
    assert production.read_bytes() == before


def test_report_path_pointing_at_production_is_refused_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T2: ``--report`` pointing at the production database is refused.

    Independent source of truth: the harness writes its report JSON verbatim
    to ``Path(args.report)``, so pointing ``--report`` at the production
    catalog used to overwrite the production database with JSON (exit 0; the
    sidecar honestly recorded the change — after the fact).  The orchestrator
    must refuse before entering any write, using the same production path set
    as the ``_refusal_reason`` guard.
    """

    import open_trader.prediction_n_leg_validation_books as books_module

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_active_n3_relation(replica)
    # Real harness path (no real external calls): the established in-process
    # books/solver stubs let the red run reach the report write that used to
    # clobber the production stand-in.
    monkeypatch.setattr(books_module, "live_books", fake_live_books)
    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        FakeSolverServerOwner,
    )

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
            "--report",
            str(production),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    # Refusal before any write: the production stand-in was not overwritten
    # with the report JSON and neither the report nor its checksum sidecar
    # appeared at the production path.
    assert production.read_bytes() == before
    assert not production.with_suffix(
        orchestrator.CHECKSUM_SIDECAR_SUFFIX
    ).exists()


# ---------------------------------------------------------------------------
# Review fix round 4: three further review findings on the same orchestrator.
# T1: WAL-mode siblings (<db>-wal/-shm/-journal) are online parts of the
# production database but were not in the guard set — a report (or sidecar)
# landing on <db>-wal is flushed into the main file at the next checkpoint
# (real probe: "file is not a database") while the main-file md5 sidecar
# still compared equal, so zero_production_write evidence was falsified.
# T2: the eager argparse default for --work-dir created (and leaked) a temp
# directory on every parse, including refused parses.
# T3: the round-3 `except SystemExit` second-layer defense had no coverage.
# ---------------------------------------------------------------------------


def test_report_path_pointing_at_production_wal_sibling_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T1: ``--report <production>-wal`` is refused before any write.

    Independent source of truth: in WAL mode ``<db>-wal`` is an online part
    of the production database; overwriting it with report JSON corrupts the
    main file at the next checkpoint, and the main-file md5 sidecar still
    compares equal — falsified zero-write evidence.  The guard set must cover
    every production sibling, uniformly for --live-catalog, --report and the
    checksum sidecar, before any write happens.
    """

    import gc
    import sqlite3

    import open_trader.prediction_n_leg_validation_books as books_module

    production = seed_production_stand_in(tmp_path / "prod")
    # Deterministic WAL cleanup first: the catalog keeps thread-local
    # connections whose GC-triggered close checkpoints and deletes <db>-wal
    # at an arbitrary later moment (the established quiesce_sqlite concern).
    # Force that close now, then lay the stand-in bytes so only the
    # orchestrator could touch them.
    gc.collect()
    production_before = production.read_bytes()
    wal = production.with_name(production.name + "-wal")
    wal.write_bytes(b"stand-in WAL frames\n")
    wal_before = wal.read_bytes()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_active_n3_relation(replica)
    # Real harness path (no real external calls): the established in-process
    # books/solver stubs, so even the pre-fix run never leaves the process.
    monkeypatch.setattr(books_module, "live_books", fake_live_books)
    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        FakeSolverServerOwner,
    )

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
            "--report",
            str(wal),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    # Refusal before any write: the WAL stand-in kept its bytes and the main
    # production database is still readable.
    assert wal.read_bytes() == wal_before
    assert production.read_bytes() == production_before
    connection = sqlite3.connect(
        f"{production.resolve().as_uri()}?mode=ro", uri=True
    )
    try:
        assert (
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        )
    finally:
        connection.close()


def test_refused_parse_leaks_no_default_work_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T2: a refused parse creates no default ``nleg-no-submit-*`` temp dir.

    Independent source of truth: the disclosed contract "validation happens
    before any filesystem work".  The argparse ``default`` for ``--work-dir``
    used to call ``tempfile.mkdtemp`` eagerly at parser-definition time, so
    even a refused parse (exit 2, no work) created and leaked an empty temp
    directory in the system temp area.
    """

    import tempfile

    production = seed_production_stand_in(tmp_path / "prod")
    temp_root = Path(tempfile.gettempdir())
    before = set(temp_root.glob("nleg-no-submit-*"))

    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main(
            [
                "--production-db",
                str(production),
                "--live-max-joint-states",
                "0",
            ]
        )

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    after = set(temp_root.glob("nleg-no-submit-*"))
    assert after - before == set()


def test_harness_system_exit_folds_into_refusal_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T3: a harness call exiting via SystemExit keeps the refusal contract.

    Independent source of truth: the round-3 exit-code contract — a harness
    call that raises SystemExit (e.g. its own parser.error) must produce the
    stderr refusal line, the sidecar ``failure`` record and exit 2, never a
    bare escape; only a harness that returns normally passes its 0/1/2
    through untouched.
    """

    import open_trader.prediction_n_leg_validation as validation

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_active_n3_relation(replica)

    def raising_harness_main(argv: list[str]) -> int:
        raise SystemExit(2)

    monkeypatch.setattr(validation, "main", raising_harness_main)

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "[nleg-no-submit] refused: harness:" in captured.err
    assert "Traceback" not in captured.err
    sidecar = json.loads(
        (work_dir / "report.production-checksum.json").read_text(
            encoding="utf-8"
        )
    )
    assert "SystemExit" in str(sidecar["failure"])
    assert sidecar["zero_production_write"] is True
    assert production.read_bytes() == before


# ---------------------------------------------------------------------------
# Review fix round 5: two further review findings on the orchestrator guard.
# T1/T2: every guard comparison used exact resolved-Path equality, which
# misses case variants on case-insensitive volumes (macOS realpath does not
# case-normalize): an upper-case spelling of the production file resolved to
# a different Path, the guard passed, and the report write landed on the
# production database itself.  T3: ``--report .`` made the guard's own
# ``report_path.with_suffix(...)`` candidate raise ValueError ("empty name")
# outside the refusal contract — traceback, exit 1, no refusal line.
# ---------------------------------------------------------------------------


def test_report_case_variant_of_existing_production_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T1: an existing upper-case spelling of the production file is
    refused for ``--report`` (exit 2, production bytes unchanged).

    Independent source of truth: on a case-insensitive volume the variant
    name denotes the SAME file (``os.path.samefile`` says so below), so a
    report written there overwrites the production database; exact
    resolved-Path equality cannot see that, and macOS realpath does not
    case-normalize either.
    """

    import os

    import open_trader.prediction_n_leg_validation_books as books_module

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    variant = production.with_name(production.name.upper())
    assert variant != production
    assert variant.exists()
    assert os.path.samefile(variant, production)
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_active_n3_relation(replica)
    # Real harness path (no real external calls): the established in-process
    # books/solver stubs, so even the pre-fix run never leaves the process.
    monkeypatch.setattr(books_module, "live_books", fake_live_books)
    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        FakeSolverServerOwner,
    )

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
            "--report",
            str(variant),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    assert "Traceback" not in captured.err
    # Refusal before any write: the production stand-in kept its bytes and
    # neither the report nor a checksum sidecar appeared under its name.
    assert production.read_bytes() == before
    assert not variant.with_suffix(
        orchestrator.CHECKSUM_SIDECAR_SUFFIX
    ).exists()


def test_report_casefold_variant_of_missing_production_sibling_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T2: a non-existing path whose casefolded spelling equals a guarded
    production path is refused conservatively.

    Independent source of truth: on a case-insensitive volume, creating
    that name later would land on the guarded sibling itself (here the
    production database's ``-journal`` online part, which does not exist on
    a quiesced catalog).  A target that does not exist yet cannot be
    compared with ``os.path.samefile``, so the guard refuses exact or
    casefolded equality with a guarded member instead.
    """

    import open_trader.prediction_n_leg_validation_books as books_module

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    variant = production.with_name(production.name.upper() + "-JOURNAL")
    assert variant != production
    assert not variant.exists()
    work_dir = tmp_path / "work"
    replica = replica_path(work_dir)
    seed_active_n3_relation(replica)
    # Real harness path (no real external calls): the established in-process
    # books/solver stubs, so even the pre-fix run never leaves the process.
    monkeypatch.setattr(books_module, "live_books", fake_live_books)
    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        FakeSolverServerOwner,
    )

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--live-catalog",
            str(replica),
            "--replica-ready",
            "--report",
            str(variant),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    assert "Traceback" not in captured.err
    assert production.read_bytes() == before
    # Refused before any write: the variant name was never created.
    assert not variant.exists()


def test_report_dot_path_is_refused_without_guard_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T3: ``--report .`` is a refusal (exit 2, stderr reason), never a
    guard traceback.

    Independent source of truth: the module docstring exit-code contract —
    exit 2 is "an orchestrator guard/step refusal ... with the reason on
    stderr".  ``Path('.').with_suffix(...)`` (the guard's own sidecar
    candidate) raises ``ValueError: ... empty name`` and the call sat
    outside the refusal contract: traceback, exit 1, no refusal line.
    """

    production = seed_production_stand_in(tmp_path / "prod")
    before = production.read_bytes()
    work_dir = tmp_path / "work"

    exit_code = orchestrator.main(
        [
            "--production-db",
            str(production),
            "--work-dir",
            str(work_dir),
            "--report",
            ".",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "refus" in (captured.err + captured.out).lower()
    assert "Traceback" not in captured.err
    assert production.read_bytes() == before
    # The refusal fires inside the guard, before any filesystem work.
    assert not work_dir.exists()
