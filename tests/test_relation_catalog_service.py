from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_trader.prediction_n_leg import problem_from_payload, validate_problem
from open_trader.prediction_n_leg_oracle import build_relation_components
from open_trader.prediction_service import create_prediction_server
from open_trader.relation_catalog import RelationCatalog
from test_prediction_arbitrage import threshold_relation
from test_relation_catalog import compiled_problem, compiled_relation_discovery


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
            # Issue #99: the activation gate compiles every prospective ACTIVE
            # set, so COMPLETE fixtures must carry a compiled problem.
            "problem": compiled_problem(
                ["condition-a", "condition-b"],
                {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
            )
            if complete
            else None,
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


def test_relations_list_is_bounded_and_excludes_compiled_problem(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    for index in range(3):
        payload = discovery()
        payload["markets"][0]["contract_id"] = f"condition-a-{index}"
        payload["markets"][1]["contract_id"] = f"condition-b-{index}"
        catalog.ingest(payload)
    with running(catalog) as base:
        status, page = response(
            base + "/api/prediction-arbitrage/relations?view=pending&limit=2&offset=0"
        )
        assert status == 200
        assert page["total"] == 3
        assert page["pending_count"] == 3
        assert len(page["items"]) == 2
        assert all("problem" not in item["model"] for item in page["items"])

        status, tail = response(
            base + "/api/prediction-arbitrage/relations?view=pending&limit=2&offset=2"
        )
        assert status == 200
        assert len(tail["items"]) == 1

        status, invalid = response(
            base + "/api/prediction-arbitrage/relations?view=pending&limit=0"
        )
        assert status == 400

        status, unknown = response(
            base + "/api/prediction-arbitrage/relations?view=pending&surprise=1"
        )
        assert status == 400


def test_relations_api_accepts_six_state_views_history_and_paging(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    pending_ids = []
    for index in range(4):
        payload = discovery()
        payload["markets"][0]["contract_id"] = f"condition-a-{index}"
        payload["markets"][1]["contract_id"] = f"condition-b-{index}"
        pending_ids.append(catalog.ingest(payload)["version_id"])
    active_id = catalog.ingest_threshold_relation(threshold_relation())["version_id"]
    catalog.approve(active_id, {"version_id": active_id}, actor="operator", git_sha="sha")
    catalog.reject(
        pending_ids[0], {"version_id": pending_ids[0]},
        reason="other", actor="operator", git_sha="sha",
    )
    with running(catalog) as base:
        status, page = response(
            base + "/api/prediction-arbitrage/relations?view=pending_approval&limit=2&offset=0"
        )
        assert status == 200
        assert page["total"] == 3
        assert page["pending_count"] == 3
        assert len(page["items"]) == 2
        status, tail = response(
            base + "/api/prediction-arbitrage/relations?view=pending_approval&limit=2&offset=2"
        )
        assert status == 200
        assert len(tail["items"]) == 1

        status, activated = response(
            base + "/api/prediction-arbitrage/relations?view=activated"
        )
        assert status == 200
        assert [item["version_id"] for item in activated["items"]] == [active_id]
        item = activated["items"][0]
        assert item["direction_code"] == "B_IMPLIES_A"
        assert item["statement"] == "B『BTC above $100000?』为 YES ⇒ A『BTC above $90000?』必须 YES"

        for view in ("approved_model_incomplete", "compiled_pending_activation", "activation_blocked", "source_changed_reapproval"):
            status, empty = response(base + f"/api/prediction-arbitrage/relations?view={view}")
            assert status == 200
            assert empty["items"] == []
            assert empty["total"] == 0

        status, history = response(
            base + "/api/prediction-arbitrage/relations?view=history"
        )
        assert status == 200
        assert {item["version_id"] for item in history["items"]} == {pending_ids[0]}

        status, invalid = response(
            base + "/api/prediction-arbitrage/relations?view=nonsense"
        )
        assert status == 400


def test_relations_detail_still_carries_compiled_problem(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest_threshold_relation(threshold_relation())["version_id"]
    with running(catalog) as base:
        status, detail = response(base + f"/api/prediction-arbitrage/relations/{version_id}")
        assert status == 200
        assert detail["model"]["problem"] is not None
        _, page = response(base + "/api/prediction-arbitrage/relations?view=pending")
        assert "problem" not in page["items"][0]["model"]


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


def test_threshold_relation_fingerprint_is_stable_across_discovery_times(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    with patch(
        "open_trader.relation_catalog._now",
        side_effect=["2026-08-15T02:32:00Z", "2026-08-16T09:00:00Z"],
    ):
        first = catalog.ingest_threshold_relation(threshold_relation())
        second = catalog.ingest_threshold_relation(threshold_relation())
    assert first["version_id"] == second["version_id"]
    assert second["occurrence_count"] == 2
    assert [row["version_id"] for row in catalog.review_rows()] == [first["version_id"]]


def _distinct_discovery(prefix: str, *, complete: bool) -> dict[str, object]:
    payload = discovery(complete=complete)
    payload["markets"][0]["contract_id"] = f"condition-a-{prefix}"
    payload["markets"][1]["contract_id"] = f"condition-b-{prefix}"
    return payload


def test_approve_batch_mixed_pending_matches_single_approve_shapes_and_counts(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    batch_active = catalog.ingest(_distinct_discovery("batch-active", complete=True))["version_id"]
    batch_incomplete = catalog.ingest(_distinct_discovery("batch-incomplete", complete=False))["version_id"]
    single_active = catalog.ingest(_distinct_discovery("single-active", complete=True))["version_id"]
    single_incomplete = catalog.ingest(_distinct_discovery("single-incomplete", complete=False))["version_id"]
    with running(catalog) as base:
        status, one = response(mutation(base, f"/api/prediction-arbitrage/relations/{single_active}/approve", {"version_id": single_active, "confirm": True}))
        assert status == 200
        status, other = response(mutation(base, f"/api/prediction-arbitrage/relations/{single_incomplete}/approve", {"version_id": single_incomplete, "confirm": True}))
        assert status == 200
        status, batch = response(mutation(base, "/api/prediction-arbitrage/relations/approve-batch", {"items": [{"version_id": batch_active}, {"version_id": batch_incomplete}], "confirm": True}))
        assert status == 200
        assert set(batch["results"][0]) == set(one)
        assert set(batch["results"][1]) == set(other)
        assert batch["results"][0] == {
            "version_id": batch_active,
            "identity": catalog.detail(batch_active)["identity"],
            "status": one["status"],
            "activation": one["activation"],
        }
        assert batch["results"][1] == {
            "version_id": batch_incomplete,
            "identity": catalog.detail(batch_incomplete)["identity"],
            "status": other["status"],
            "activation": other["activation"],
        }
        assert batch["counts"] == {"total": 2, "active": 1, "blocked": 1, "error": 0}
        status, again = response(mutation(base, f"/api/prediction-arbitrage/relations/{batch_active}/approve", {"version_id": batch_active, "confirm": True}))
    assert status == 409
    assert "no longer pending" in again["message"]


def test_approve_batch_rejects_invalid_payloads(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["version_id"]
    cases = [
        {"items": [{"version_id": version_id}]},
        {"items": [{"version_id": version_id}], "confirm": False},
        {"items": [], "confirm": True},
        {"items": [{}], "confirm": True},
        {"items": [{"version_id": version_id, "extra": 1}], "confirm": True},
        {"items": [{"version_id": 123}], "confirm": True},
        {"items": "not-a-list", "confirm": True},
    ]
    with running(catalog) as base:
        for payload in cases:
            status, denied = response(mutation(base, "/api/prediction-arbitrage/relations/approve-batch", payload))
            assert status == 400, payload
            assert "message" in denied


def test_approve_batch_is_read_only_in_shadow_mode(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["version_id"]
    runtime = _Runtime(catalog)
    runtime.mode = "shadow"
    server = create_prediction_server(
        runtime=runtime,  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        status, denied = response(mutation(base, "/api/prediction-arbitrage/relations/approve-batch", {"items": [{"version_id": version_id}], "confirm": True}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 403
    assert denied["code"] == "shadow_read_only"


def test_approve_batch_reports_entry_level_errors_and_continues(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    good = catalog.ingest(_distinct_discovery("batch-good", complete=True))["version_id"]
    consumed = catalog.ingest(_distinct_discovery("batch-consumed", complete=True))["version_id"]
    catalog.approve(consumed, {"version_id": consumed}, actor="operator", git_sha="sha")
    missing = "v-" + "0" * 64
    with running(catalog) as base:
        status, batch = response(mutation(base, "/api/prediction-arbitrage/relations/approve-batch", {"items": [{"version_id": good}, {"version_id": consumed}, {"version_id": missing}], "confirm": True}))
    assert status == 200
    assert batch["counts"] == {"total": 3, "active": 1, "blocked": 0, "error": 2}
    assert batch["results"][0]["version_id"] == good
    assert batch["results"][0]["activation"] == "ACTIVE"
    assert batch["results"][1] == {
        "version_id": consumed,
        "identity": catalog.detail(consumed)["identity"],
        "error": "relation version is no longer pending",
    }
    assert batch["results"][2] == {
        "version_id": missing,
        "error": "relation version not found",
    }
    rows = {str(row["version_id"]): row for row in catalog.review_rows()}
    assert rows[good]["status"] == "APPROVED"
    assert rows[good]["activation"] == "ACTIVE"


def test_relation_catalog_naive_as_of_approve_returns_json_blocked(
    tmp_path: Path,
) -> None:
    """R2.1: a candidate whose as_of is present but timezone-less (naive) must
    be blocked ACTIVATION_BLOCKED_INCONSISTENT through the HTTP layer with a
    normal JSON response — never a TypeError that breaks the HTTP error
    contract — and the catalog keeps serving later approvals."""
    catalog = RelationCatalog(tmp_path)

    def payload(tag: str, *, naive: bool = False) -> dict[str, object]:
        result = compiled_relation_discovery(
            [f"http-{tag}-a", f"http-{tag}-b"],
            {f"http-{tag}-a": "BUY_YES", f"http-{tag}-b": "BUY_YES"},
            rule=f"rules-http-{tag}",
        )
        for market in result["markets"]:
            market["event_identity_basis"] = "event-a"
        if naive:
            result["model"]["problem"]["as_of"] = "2029-01-01T00:00:00"
        return result

    good_id = catalog.ingest(payload("good"))["version_id"]
    naive_id = catalog.ingest(payload("naive", naive=True))["version_id"]
    with running(catalog) as base:
        status, approved = response(
            mutation(
                base,
                f"/api/prediction-arbitrage/relations/{good_id}/approve",
                {"version_id": good_id, "confirm": True},
            )
        )
        assert status == 200
        assert approved["activation"] == "ACTIVE"
        status, blocked = response(
            mutation(
                base,
                f"/api/prediction-arbitrage/relations/{naive_id}/approve",
                {"version_id": naive_id, "confirm": True},
            )
        )
    assert status == 200
    assert blocked["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
