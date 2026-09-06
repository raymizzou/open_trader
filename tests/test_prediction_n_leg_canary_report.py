"""Issue #65: the read-only manual-canary fact report (backend builder + CLI).

``build_canary_report`` is a pure function over the EXISTING tables (queue,
batches, audit stream, proofs, episodes, unsettled-capital ledger); it never
writes and never creates tables. Every accepted fact below comes from the
approved real-link scenario (two legs, 20 lots at 0.40, protected prices
400,001/leg): the ledger after the completed batch is 16,000,040 units, each
leg paid 8,000,000 units, the proven guaranteed profit is 3,999,960 units,
and the actual profit stays UNSETTLED until venue settlement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

REPORT_NOW = datetime(2026, 9, 4, 8, 30, 0, tzinfo=UTC)


def _completed_real_chain_store(tmp_path: Path):
    """The approved #64 real-resolver chain driven to one completed two-leg
    batch (confirm -> admit -> submit both FILLED -> reconcile), exactly the
    ``test_issue64_real_resolver_chain_confirm_admit_submit_complete`` flow.
    Returns the store; the resolver is stopped before returning."""
    from dataclasses import replace

    from open_trader.prediction_n_leg import canonical_payload, fingerprint
    from open_trader.prediction_n_leg_confirm import confirm_enqueue
    from open_trader.prediction_n_leg_driver import NLegOrderQueueDriver
    from open_trader.prediction_n_leg_execution import (
        ConfirmedHolding,
        ReconciliationContext,
        SettlementCashFlow,
    )
    from test_prediction_n_leg_confirm import _FakeTrading
    from test_prediction_n_leg_fail_closed_e2e import (
        _issue64_real_chain,
        _live_book,
    )

    store, resolver, monitor, component_id = _issue64_real_chain(tmp_path)
    try:
        entries = resolver.solutions()
        entry = entries[0]
        material = resolver.driver_execution_source(component_id)
        displayed = fingerprint(canonical_payload(entry["execution"]))
        result = confirm_enqueue(
            store,
            entries,
            component_id=component_id,
            displayed_fingerprint=displayed,
            idempotency_key="canary-report-1",
            now=datetime.now(UTC),
            partial_fill_proof=material["partial_fill_proof"],
            execution_source={
                "market": material["market"],
                "execution": material["execution"],
            },
        )
        assert result["state"] == "PENDING"

        def source_factory(frozen):
            fresh = resolver.driver_execution_source(str(frozen["component_id"]))
            if fresh is None:
                raise ValueError("N_LEG_SOURCE_UNAVAILABLE")
            return fresh["source"]

        def recon_factory(batch_id):
            batch = store.n_leg_batch(batch_id)
            now = datetime.now(UTC)
            account = replace(material["source"].account_snapshot, captured_at=now)
            flows, holdings = [], []
            for leg in batch["legs"]:
                receipt = leg["receipt"]
                flows.append(
                    SettlementCashFlow(
                        leg["client_order_id"],
                        receipt.get("venue_order_id"),
                        leg["venue_id"],
                        leg["account_id"],
                        leg["settlement_asset_id"],
                        int(receipt["cumulative_cost_units"]),
                        int(receipt["cumulative_fee_units"]),
                        now,
                        now,
                        (
                            receipt.get("rest_observation_version")
                            if receipt.get("rest_confirmed")
                            else receipt.get("sequence")
                        ),
                        bool(receipt["rest_confirmed"]),
                    )
                )
                if int(receipt["cumulative_filled_quantity"]) > 0:
                    holdings.append(
                        ConfirmedHolding(
                            leg["venue_id"],
                            leg["account_id"],
                            leg["asset_id"],
                            int(receipt["cumulative_filled_quantity"]),
                            now,
                            now,
                        )
                    )
            return ReconciliationContext(
                f"{batch_id}:v1",
                account,
                tuple(holdings),
                tuple(flows),
                now,
                now,
                now,
            )

        trading = _FakeTrading(
            [
                {"state": "FILLED", "error_code": None, "cost_units": 8_000_000},
                {"state": "FILLED", "error_code": None, "cost_units": 8_000_000},
            ]
        )
        driver = NLegOrderQueueDriver(
            store,
            books_provider=lambda cid: resolver.driver_books_snapshot(cid),
            source_factory=source_factory,
            trading=trading,
            reconciliation_context_factory=recon_factory,
        )
        monitor.books = {
            c: _live_book(c, "0.40") for c in ("real-a", "real-b")
        }
        summary = driver.tick(now=datetime.now(UTC))
        batch = store.n_leg_batch(str(summary["submitted"]))
        assert str(batch["state"]).startswith("RECONCILED")
        return store
    finally:
        resolver.stop()


def _bound_proof(store, entry, base_source):
    """The bound #74 proof record for *entry* against the store's CURRENT
    caps version — exactly the construction the approved ``_enqueued_e2e_store``
    fixture freezes into a confirm row."""
    from open_trader.prediction_executable_cost import (
        execution_solution_from_payload,
    )
    from open_trader.prediction_n_leg import fingerprint
    from open_trader.prediction_n_leg_execution import (
        ExecutionSolutionSource,
        PartialFillProofRecord,
        execution_solution_binding,
    )
    from test_prediction_n_leg_confirm import AS_OF, _decodable_execution

    source = ExecutionSolutionSource(
        _decodable_execution(entry),
        entry["market"],
        base_source.component,
        base_source.books,
        base_source.account_snapshot,
        AS_OF,
    )
    market = source.decode_market()
    execution = execution_solution_from_payload(
        _decodable_execution(entry),
        market_solution=market,
        account_snapshot=source.account_snapshot,
        now=AS_OF,
    )
    safety_version = int(store.n_leg_safety_config_latest()["version"])
    proof_values: dict[str, object] = {
        **execution_solution_binding(execution),
        "cap_config_version": f"caps-v{safety_version}",
        "max_partial_fill_loss": 100,
        "max_auto_repair_loss": 10,
        "solver_lower_bound": 0,
        "solver_upper_bound": 100,
        "solver_termination": "CLOSED",
        "solver_evidence_fingerprint": "solver-evidence-v1",
        "verifier_status": "QUALIFIED_VERIFIED",
        "verifier_fingerprint": "verifier-v1",
        "verifier_evidence_fingerprint": "verifier-evidence-v1",
        "status": "PARTIAL_FILL_SAFE",
        "schema_version": "open_trader.prediction_n_leg.partial_fill_proof.v1",
    }
    proof_values["fingerprint"] = fingerprint(proof_values)
    return PartialFillProofRecord(**proof_values).to_payload()


def _incident_e2_store(tmp_path: Path):
    """The approved e2 scenario: one leg FILLED, one REJECTED -> the incident
    batch opens, every other PENDING row is cleared. The filled leg books its
    conservative FOK bound (510 units); the rejected leg books zero."""
    from test_prediction_n_leg_confirm import (
        AS_OF,
        _FakeTrading,
        _confirm,
        _e2e_driver,
        _enqueued_e2e_store,
        _solution_entry,
    )

    store, row, base_source = _enqueued_e2e_store(tmp_path)
    second = _solution_entry(component_id="component:other")
    _confirm(store, [second], component_id="component:other", idempotency_key="e2e-second")
    trading = _FakeTrading([{"state": "FILLED", "error_code": None}, {"state": "REJECTED", "error_code": None}])
    driver = _e2e_driver(store, trading, base_source)
    summary = driver.tick(now=AS_OF)
    assert store.n_leg_batch(str(summary["submitted"]))["incident"] is not None
    return store


def test_r2_incident_batch_report_facts(tmp_path: Path) -> None:
    from open_trader.prediction_n_leg_canary_report import (
        build_canary_report,
        render_canary_report_markdown,
    )

    store = _incident_e2_store(tmp_path)
    report = build_canary_report(store, now=REPORT_NOW)

    incident_batches = [b for b in report["batches"] if b["incident"]]
    assert len(incident_batches) == 1
    batch = incident_batches[0]
    assert batch["state"] == "INCIDENT"
    # The incident record carries the reason, the batch id and the paid-cash
    # literal (filled leg books its 510-unit FOK bound, rejected leg zero).
    assert batch["incident"]["reason"] == "MIXED_TERMINAL_FILL"
    assert batch["incident"]["execution_batch_id"] == batch["execution_batch_id"]
    assert batch["incident"]["paid_cash_units"] == 510
    assert batch["paid_cash_units"] == 510

    # Repair authorization: the frozen ceilings plus the mandated estimate
    # label (complete-repair end-point estimate, NOT a worst-case bound).
    assert batch["repair_authorization"]["max_partial_fill_loss_units"] == 100
    assert batch["repair_authorization"]["max_auto_repair_loss_units"] == 10
    assert batch["repair_authorization"]["estimate_label"] == "完整修复终点估算、非最坏界"

    markdown = render_canary_report_markdown(report)
    assert "MIXED_TERMINAL_FILL" in markdown
    assert batch["execution_batch_id"] in markdown
    assert "完整修复终点估算、非最坏界" in markdown
    # Facts only: no advisory conclusion wording anywhere in the report.
    for advice in ("建议", "应当", "应该", "推荐", "考虑"):
        assert advice not in markdown


def test_r2_incident_paid_cash_excludes_fees_and_reports_fee_units(
    tmp_path: Path,
) -> None:
    import json
    import sqlite3

    from open_trader.prediction_n_leg_canary_report import (
        build_canary_report,
        render_canary_report_markdown,
    )
    from test_prediction_n_leg_confirm import _e2e_db

    store = _incident_e2_store(tmp_path)

    # The R3 ledger idiom: inject a non-zero venue fee directly into the
    # FILLED leg's receipt in the stored batch payload, then read back only
    # through the public report seam.
    with sqlite3.connect(_e2e_db(tmp_path)) as connection:
        batch_id, raw_payload = connection.execute(
            "SELECT execution_batch_id, payload FROM n_leg_batches"
            " WHERE state='INCIDENT'"
        ).fetchone()
        payload = json.loads(raw_payload)
        injected = 0
        for leg in payload["legs"]:
            receipt = leg.get("receipt")
            if (
                isinstance(receipt, dict)
                and int(receipt.get("cumulative_filled_quantity", 0)) > 0
            ):
                receipt["cumulative_fee_units"] = 100
                injected += 1
        assert injected == 1
        connection.execute(
            "UPDATE n_leg_batches SET payload=? WHERE execution_batch_id=?",
            (json.dumps(payload), batch_id),
        )

    report = build_canary_report(store, now=REPORT_NOW)
    incident_batches = [b for b in report["batches"] if b["incident"]]
    assert len(incident_batches) == 1
    batch = incident_batches[0]
    # The filled leg's cash literal stays 510 (R2 fixture); the injected
    # 100-unit fee is booked separately, aligned with the batch profit caliber.
    assert batch["profit"]["paid_cash_units"] == 510
    assert batch["profit"]["paid_fee_units"] == 100
    # Incident paid cash is cash-only (fees excluded), and the incident block
    # reports the fee on its own field.
    assert batch["incident"]["paid_cash_units"] == batch["profit"]["paid_cash_units"]
    assert batch["incident"]["paid_fee_units"] == 100

    markdown = render_canary_report_markdown(report)
    # The incident line separates cash from fees: 510 cash, 100 fees, never
    # the inflated 610 total rendered as cash.
    assert "事故已付现金：510 units" in markdown
    assert "事故已付现金：610 units" not in markdown
    assert "事故已付费用：100 units" in markdown


def test_r3_cumulative_cap_rejection_reported_as_monitoring_only(
    tmp_path: Path,
) -> None:
    import sqlite3
    from datetime import timedelta

    from open_trader.prediction_n_leg_canary_report import (
        build_canary_report,
        render_canary_report_markdown,
    )
    from open_trader.prediction_n_leg_mode import n_leg_update_safety_config
    from test_prediction_n_leg_confirm import (
        AS_OF,
        _FakeTrading,
        _confirm,
        _e2e_db,
        _e2e_driver,
        _enqueued_e2e_store,
        _solution_entry,
    )

    # Batch one: the fixture family runs to a completed two-leg batch; the
    # conservative unsettled bound (510/leg = 1020) stays on the ledger.
    store, row, base_source = _enqueued_e2e_store(tmp_path)
    trading = _FakeTrading(
        [
            {"state": "FILLED", "error_code": None, "cost_units": 400},
            {"state": "FILLED", "error_code": None, "cost_units": 400},
        ]
    )
    driver = _e2e_driver(store, trading, base_source, recon=True)
    summary = driver.tick(now=AS_OF)
    assert str(store.n_leg_batch(str(summary["submitted"]))["state"]).startswith(
        "RECONCILED"
    )
    assert store.n_leg_control()["total_unsettled_capital_units"] == 1020

    # Batch two (its own opportunity family): the freeze happens under a
    # 1,000,000-unit cumulative cap, so the confirm gate passes and the row
    # is enqueued (caps version bumped BEFORE the confirm re-verification).
    safety = store.n_leg_safety_config_latest()
    config = {
        key: value
        for key, value in safety["config"].items()
        if key != "caps_configured"
    }
    config["max_total_unsettled_capital_units"] = 1_000_000
    n_leg_update_safety_config(store, config=config, base_version=int(safety["version"]))
    second = _solution_entry(component_id="component:other")
    _confirm(
        store,
        [second],
        component_id="component:other",
        idempotency_key="r3-second",
        partial_fill_proof=_bound_proof(store, second, base_source),
    )

    # The cumulative unsettled capital rises to the cap from OUTSIDE this
    # batch stream (the approved :957 d2 ledger idiom): 999,500 held + the
    # 1020 reservation = 1,000,520 > 1,000,000.
    with sqlite3.connect(_e2e_db(tmp_path)) as connection:
        connection.execute(
            "UPDATE n_leg_controls SET total_unsettled_capital_units=999500"
        )

    untouched = _FakeTrading([])
    driver2 = _e2e_driver(store, untouched, base_source)
    result = driver2.tick(now=AS_OF + timedelta(seconds=1))

    # The admission-level rejection abandons the head back to monitoring
    # with zero side effects: no submit, no batch row, ledger untouched.
    assert result["abandoned"] == "UNSETTLED_CAP"
    assert untouched.calls == []
    assert store.n_leg_control()["total_unsettled_capital_units"] == 999_500

    report = build_canary_report(store, now=REPORT_NOW)
    # The cumulative total comes from the ledger table, never recomputed.
    assert report["ledger"]["total_unsettled_capital_units"] == 999_500
    # Exactly one batch row exists: the rejected second attempt left none.
    assert len(report["batches"]) == 1
    # The rejected request row is reported with its rejection reason and
    # rendered as monitoring-only.
    rejected = next(
        r
        for r in report["queue"]["requests"]
        if r["component_id"] == "component:other"
    )
    assert rejected["state"] == "ABANDONED"
    assert rejected["abandon_reason"] == "UNSETTLED_CAP"
    markdown = render_canary_report_markdown(report)
    assert "UNSETTLED_CAP" in markdown
    assert "仅监控" in markdown


def test_r4_same_store_same_now_is_byte_identical_json(tmp_path: Path) -> None:
    import json

    from open_trader.prediction_n_leg_canary_report import build_canary_report

    store = _incident_e2_store(tmp_path)
    dumps = lambda report: json.dumps(  # noqa: E731  (exact approved encoding)
        report, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    first = dumps(build_canary_report(store, now=REPORT_NOW))
    second = dumps(build_canary_report(store, now=REPORT_NOW))

    assert first == second


def test_r5_money_rendering_is_humanized_on_the_shared_scale() -> None:
    from open_trader.prediction_n_leg_canary_report import (
        render_canary_report_markdown,
    )

    # Canonical N-leg money rule (dashboard `predictionNLegUnitsMoney`):
    # 1,000,000 units per dollar, two decimals; profits signed.
    markdown = render_canary_report_markdown(
        {
            "generated_at": REPORT_NOW.isoformat(),
            "schema": "open_trader.prediction_n_leg.canary_report.v1",
            "ledger": {"total_unsettled_capital_units": 16_000_040},
            "queue": {},
            "batches": [
                {
                    "execution_batch_id": "nleg-b-canary",
                    "state": "RECONCILED_FULL",
                    "trigger_source": "MANUAL_CONFIRM",
                    "legs": [
                        {
                            "action_id": "action-a",
                            "submitted_quantity": 20,
                            "filled_quantity": 20,
                            "paid_cash_units": 8_000_000,
                            "paid_fee_units": 0,
                            "state": "FILLED",
                        }
                    ],
                    "profit": {
                        "guaranteed_profit_units": 3_999_960,
                        "paid_cash_units": 16_000_000,
                        "paid_fee_units": 0,
                        "actual_profit": "UNSETTLED",
                    },
                }
            ],
            "episodes": [],
            "proofs": [],
        }
    )
    assert "$16.00" in markdown
    assert "+$4.00" in markdown


def test_r5_cli_writes_json_and_markdown_read_only(tmp_path: Path) -> None:
    import hashlib
    import json as json_lib
    import subprocess
    import sys

    from open_trader.prediction_n_leg_canary_report import build_canary_report

    fixture_data = tmp_path / "fixture" / "data"
    _incident_e2_store(tmp_path / "fixture")
    db = fixture_data / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_nleg_canary_report.py"
    )
    out = tmp_path / "out"

    def snapshot() -> tuple[int, bytes]:
        return (
            db.stat().st_mtime_ns,
            hashlib.sha256(db.read_bytes()).digest(),
        )

    before = snapshot()
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--store",
            str(fixture_data),
            "--out",
            str(out),
            "--now",
            REPORT_NOW.isoformat(),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    json_files = sorted(out.glob("*.json"))
    markdown_files = sorted(out.glob("*.md"))
    assert len(json_files) == 1
    assert len(markdown_files) == 1
    # The exported JSON is exactly the builder's output for the same store.
    exported = json_lib.loads(json_files[0].read_text(encoding="utf-8"))
    assert exported == build_canary_report(
        fixture_store(fixture_data), now=REPORT_NOW
    )
    # Read-only proof: the source database is untouched (mtime + sha256).
    assert snapshot() == before


def fixture_store(fixture: Path):
    """A store-shaped handle for the builder over an existing fixture dir
    (the builder only reads ``store.path`` and opens it mode=ro)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        path=fixture / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )


def test_r1_complete_batch_report_facts(tmp_path: Path) -> None:
    from open_trader.prediction_n_leg_canary_report import build_canary_report

    store = _completed_real_chain_store(tmp_path)
    report = build_canary_report(store, now=REPORT_NOW)

    assert report["generated_at"] == REPORT_NOW.isoformat()
    # The cumulative total is read from the ledger table, never recomputed.
    assert report["ledger"]["total_unsettled_capital_units"] == 16_000_040
    assert len(report["batches"]) == 1
    batch = report["batches"][0]
    assert batch["state"] == "RECONCILED_FULL"

    # One paid row per leg: 20 shares x $0.40 = 8,000,000 units each.
    assert len(batch["legs"]) == 2
    for leg in batch["legs"]:
        assert leg["submitted_quantity"] == 20
        assert leg["filled_quantity"] == 20
        assert leg["paid_cash_units"] == 8_000_000
        assert leg["paid_fee_units"] == 0
        assert leg["state"] == "FILLED"

    # Per-leg direction facts (ticket acceptance item 4): the stored batch
    # payload legs carry ``side`` (execution.py writes leg.side verbatim),
    # and this real chain's EXACTLY_ONE codec compiles every action BUY_YES
    # (fixture ``_exactly_one_payload``), so both report legs carry the
    # same literal.
    assert report["batches"][0]["legs"][0]["side"] == "BUY_YES"
    assert report["batches"][0]["legs"][1]["side"] == "BUY_YES"

    # The markdown per-leg line renders the direction on the leg's own line.
    from open_trader.prediction_n_leg_canary_report import (
        render_canary_report_markdown,
    )

    markdown = render_canary_report_markdown(report)
    assert "方向 BUY_YES" in markdown

    # Conservation row: reserved == unsettled position (equal assertion).
    assert batch["conservation"]["reserved_units"] == 16_000_040
    assert batch["conservation"]["position_units"] == 16_000_040
    assert batch["conservation"]["equal"] is True

    # Profit three-part: proven lower bound, paid cash+fees, actual UNSETTLED.
    assert batch["profit"]["guaranteed_profit_units"] == 3_999_960
    assert batch["profit"]["paid_cash_units"] == 16_000_000
    assert batch["profit"]["paid_fee_units"] == 0
    assert batch["profit"]["actual_profit"] == "UNSETTLED"

    # Every batch carries its trigger source, read from the request payload.
    assert batch["trigger_source"] == "MANUAL_CONFIRM"


# ---------------------------------------------------------------------------
# Issue #65 S4 service surface: GET /api/prediction-arbitrage/n-leg/report
# rides the existing GET whitelist + availability gate and returns exactly
# the build_canary_report output (real clock — the endpoint injects no now).
# ---------------------------------------------------------------------------


class _ReportEndpointRuntime:
    """The #64 endpoint-test runtime idiom: a production-mode stub whose
    ``store`` is the only attribute the read-only report route touches."""

    state = "RUNNING"
    mode = "production"
    production_owner = True
    legacy_retired = True

    def __init__(self, store):
        self.store = store
        self.execution = None
        self.monitor = None
        self.cross_venue_monitor = None


def test_s4_report_endpoint_returns_builder_output(tmp_path: Path) -> None:
    from open_trader.prediction_n_leg_canary_report import build_canary_report
    from open_trader.prediction_service import create_prediction_server
    from test_prediction_api_contract import _json_response, _serve

    store = _incident_e2_store(tmp_path)
    server = create_prediction_server(
        runtime=_ReportEndpointRuntime(store),  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    )
    with _serve(server) as base:
        status, _headers, body = _json_response(
            base + "/api/prediction-arbitrage/n-leg/report"
        )
    assert status == 200
    # The report schema keys, verbatim from the builder's contract.
    assert set(body) == {
        "schema",
        "generated_at",
        "ledger",
        "queue",
        "batches",
        "audit",
        "proofs",
        "episodes",
    }
    assert body["schema"] == "open_trader.prediction_n_leg.canary_report.v1"
    # One known fixture fact: the approved e2 chain leaves exactly one
    # MIXED_TERMINAL_FILL incident batch.
    assert len(body["batches"]) == 1
    assert body["batches"][0]["incident"]["reason"] == "MIXED_TERMINAL_FILL"
    # No injected clock at the seam: everything except the generation stamp
    # equals the builder's output over the same store.
    expected = build_canary_report(store)
    body.pop("generated_at")
    expected.pop("generated_at")
    assert body == expected


# ---------------------------------------------------------------------------
# Issue #122 (approved case 10): the report's lineage facts are read-only
# passthroughs of the stored rows, so after a real rotation's re-armed
# successor admits, the claim key it shows is the successor's own graph
# lineage truth — never a synthetic display string.
# ---------------------------------------------------------------------------


def test_case10_report_claim_key_is_graph_lineage_truth(tmp_path: Path) -> None:
    import sqlite3
    from datetime import datetime, timedelta
    from decimal import Decimal

    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from open_trader.prediction_n_leg_canary_report import build_canary_report
    from open_trader.prediction_n_leg_episodes import EpisodeStore, EpisodeTracker
    from open_trader.prediction_runtime_graph import RuntimeGraphStore
    from test_prediction_runtime_graph import chain_generation, make_graph, row

    store = PredictionArbitrageStore(tmp_path)
    graph, state, meta = make_graph(tmp_path, chain_generation("v1"))
    graph.refresh()
    family = next(iter(graph.components().values()))
    store.n_leg_create_batch(
        {
            "execution_batch_id": "batch-family",
            "opportunity_episode_id": "episode-family",
            "episode_lineage_id": f"lineage:{family.component_id}",
            "mode": "MANUAL",
            "state": "ACTIVE",
            "entry_fingerprint": "entry-family",
            "execution_solution_fingerprint": "solution-family",
            "total_unsettled_capital_units": 1,
            "component_id": family.component_id,
        }
    )
    with sqlite3.connect(
        tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    ) as connection:
        connection.execute("UPDATE n_leg_controls SET active_batch_id=NULL")

    # Real rotation: one generation advance splits the family in two.
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
    successor = sorted(
        graph.components().values(), key=lambda component: component.component_id
    )[0]
    truth = {
        component_id: component.lineage_id
        for component_id, component in RuntimeGraphStore(tmp_path).load()[2].items()
        if component.status == "ACTIVE"
    }
    assert successor.lineage_id == truth[successor.component_id]
    assert family.lineage_id in successor.predecessor_lineage_ids

    # The successor re-arms through its own real negative-proof close.
    with sqlite3.connect(
        tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    ) as connection:
        created_at = connection.execute(
            "SELECT created_at FROM n_leg_lineage_claims"
        ).fetchone()[0]
    claim_at = datetime.fromisoformat(str(created_at))
    tracker = EpisodeTracker(store=EpisodeStore(tmp_path))
    fingerprints = {
        "component_generation": 1,
        "model_fingerprint": "model-1",
        "quote_fingerprint": "quote-1",
        "qualification_fingerprint": "qual-1",
        "qualification_policy_version": "1",
    }
    tracker.observe_qualified(
        successor.component_id,
        successor.lineage_id,
        Decimal("1"),
        False,
        None,
        fingerprints,
        claim_at + timedelta(hours=1),
    )
    for offset in (0, 300):
        tracker.observe_negative(
            successor.component_id,
            proof_fingerprint=f"proof-{offset}",
            generation=1,
            model_fingerprint="model-1",
            quote_fingerprint="quote-1",
            qualification_fingerprint="qual-1",
            binding_matches=True,
            quote_fresh=True,
            gap_seconds=300,
            now=claim_at + timedelta(hours=1, seconds=offset),
            qualification_policy_version="1",
        )

    store.n_leg_create_batch(
        {
            "execution_batch_id": "batch-rearmed",
            "opportunity_episode_id": "episode-rearmed",
            "episode_lineage_id": f"lineage:{successor.component_id}",
            "mode": "MANUAL",
            "state": "ACTIVE",
            "entry_fingerprint": "entry-rearmed",
            "execution_solution_fingerprint": "solution-rearmed",
            "total_unsettled_capital_units": 1,
            "component_id": successor.component_id,
        }
    )

    report = build_canary_report(store, now=REPORT_NOW)
    batch = next(
        b
        for b in report["batches"]
        if b["execution_batch_id"] == "batch-rearmed"
    )
    # The claim key the report shows is the successor's own graph lineage.
    assert batch["episode_lineage_id"] == truth[successor.component_id]
