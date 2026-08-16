from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_trader.prediction_n_leg import problem_from_payload, validate_problem
from open_trader.prediction_n_leg_oracle import build_relation_components
from open_trader.prediction_service import create_prediction_server
from open_trader.relation_catalog import RelationCatalog
from test_prediction_arbitrage import threshold_relation


def discovery(*, title: str = "Will Bitcoin trade above $100,000 before December 31, 2026?", complete: bool = True) -> dict[str, object]:
    return {
        "discovery_source": "exchange_metadata",
        "discovered_at": "2026-08-15T02:32:00Z",
        "relation_type": "IMPLIES",
        "semantics": {"statement": "A YES implies B YES", "direction": "A_TO_B"},
        "source_evidence": [{"source": "Polymarket rules", "quote": "resolves YES if..."}],
        "model": {
            "completeness": "COMPLETE" if complete else "INCOMPLETE",
            "terminal_states": ["YES", "NO", "VOID"],
            "payouts": "YES=1, NO=0, VOID=refund",
            "capital_release": "resolution",
        },
        "markets": [
            {
                "venue": "Polymarket", "contract_id": "condition-a", "title": title,
                "market_date": "2026-08-15T00:00:00Z", "expires_at": "2026-12-31T17:00:00Z",
                "event_identity_basis": "event-a", "settlement_observation_key": "btc-usd",
                "settlement_rules": "official index", "cancellation_rules": "void refunds",
            },
            {
                "venue": "Polymarket", "contract_id": "condition-b", "title": "Will Bitcoin trade above $90,000 before December 31, 2026?",
                "market_date": "2026-08-15T00:00:00Z", "expires_at": "2026-12-31T17:00:00Z",
                "event_identity_basis": "event-a", "settlement_observation_key": "btc-usd",
                "settlement_rules": "official index", "cancellation_rules": "void refunds",
            },
        ],
    }


class _Runtime:
    state = "RUNNING"
    mode = "production"
    production_owner = True
    store = None
    monitor = None
    execution = None
    cross_venue_monitor = None

    def __init__(self, catalog: RelationCatalog) -> None:
        self.relation_catalog = catalog


@contextmanager
def running(catalog: RelationCatalog):
    server = create_prediction_server(
        runtime=_Runtime(catalog),  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def response(request: str | Request) -> tuple[int, dict[str, object]]:
    try:
        with urlopen(request, timeout=5) as result:
            return result.status, json.loads(result.read().decode())
    except HTTPError as error:
        return error.code, json.loads(error.read().decode())


def mutation(base: str, path: str, payload: dict[str, object]) -> Request:
    return Request(base + path, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "Cookie": "ot_prediction_session=session-token",
        "Origin": base, "X-CSRF-Token": "csrf-token",
    })


def test_production_relation_catalog_http_reads_and_approves_the_opened_version(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["version_id"]
    with running(catalog) as base:
        status, queue = response(base + "/api/prediction-arbitrage/relations?view=pending")
        assert status == 200
        assert queue["pending_count"] == 1
        status, detail = response(base + f"/api/prediction-arbitrage/relations/{version_id}")
        assert status == 200
        expected = {"version_id": detail["version_id"]}
        status, approved = response(mutation(base, f"/api/prediction-arbitrage/relations/{version_id}/approve", {**expected, "confirm": True}))

    assert status == 200
    assert approved["activation"] == "ACTIVE"


def test_relation_catalog_mutation_rejects_extra_fields_before_approval(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["version_id"]
    detail = catalog.detail(version_id)
    expected = {"version_id": detail["version_id"]}
    with running(catalog) as base:
        status, denied = response(mutation(base, f"/api/prediction-arbitrage/relations/{version_id}/approve", {**expected, "confirm": True, "ignored": True}))
        assert status == 400
        assert "fields are invalid" in denied["message"]
        status, forbidden = response(mutation(base, f"/api/prediction-arbitrage/relations/{version_id}/approve", {**expected, "confirm": True}))

    assert status == 200
    assert forbidden["activation"] == "ACTIVE"


def test_relation_catalog_review_rows_expose_every_version_without_views(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    pending_id = catalog.ingest(discovery(complete=False))["version_id"]
    active_id = catalog.ingest(discovery(title="Active relation"))["version_id"]
    catalog.approve(active_id, {"version_id": active_id}, actor="operator", git_sha="sha")

    rows = catalog.review_rows()
    by_id = {row["version_id"]: row for row in rows}
    assert set(by_id) == {pending_id, active_id}
    assert by_id[pending_id]["status"] == "PENDING"
    assert by_id[active_id]["status"] == "APPROVED"
    assert by_id[active_id]["activation"] == "ACTIVE"
    assert by_id[pending_id]["model"]["terminal_states"] == []


def test_threshold_relation_enrichment_ingests_one_complete_pending_version(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    result = catalog.ingest_threshold_relation(threshold_relation())
    assert result["status"] == "PENDING"

    rows = catalog.review_rows()
    assert len(rows) == 1  # enrich success ingests COMPLETE only, no INCOMPLETE twin
    row = rows[0]
    assert row["model"]["terminal_states"] == ["NORMAL_YES", "NORMAL_NO", "VOID"]
    assert row["model"]["payouts"] == {
        "condition-a": {"NORMAL_YES": 1, "NORMAL_NO": 0, "VOID": 0},
        "condition-b": {"NORMAL_YES": 0, "NORMAL_NO": 1, "VOID": 0},
    }
    problem = problem_from_payload(row["model"]["problem"])
    assert validate_problem(problem) == ()
    assert len(build_relation_components(problem)) == 1

    assert catalog.current_generation() == {}  # no auto-approval/activation
    approved = catalog.approve(
        result["version_id"], {"version_id": result["version_id"]},
        actor="operator", git_sha="sha",
    )
    assert approved["activation"] == "ACTIVE"
    active = catalog.current_generation()
    assert list(active) == [row["identity"]]
    assert active[row["identity"]]["model"]["problem"] == row["model"]["problem"]


def test_threshold_relation_without_resolution_source_stays_incomplete(tmp_path: Path) -> None:
    base = threshold_relation()
    relation = replace(base, market_a=replace(base.market_a, resolution_source=""))
    catalog = RelationCatalog(tmp_path)
    result = catalog.ingest_threshold_relation(relation)

    row = catalog.review_rows()[0]
    assert row["model"]["terminal_states"] == []
    assert row["model"]["problem"] is None
    approved = catalog.approve(
        result["version_id"], {"version_id": result["version_id"]},
        actor="operator", git_sha="sha",
    )
    assert approved["activation"] == "INCOMPLETE"


def test_threshold_relation_capital_release_is_max_end_date(tmp_path: Path) -> None:
    base = threshold_relation()
    later = replace(base, market_b=replace(base.market_b, end_date="2027-03-01T00:00:00Z"))
    catalog = RelationCatalog(tmp_path)
    catalog.ingest_threshold_relation(later)

    assert catalog.review_rows()[0]["model"]["capital_release"] == "2027-03-01T00:00:00.000000Z"
