from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.relation_catalog import RelationCatalog, _threshold_complete_model
from open_trader.relation_catalog_v2 import SqliteCatalogStore
from test_prediction_arbitrage import threshold_relation


PROBLEM = _threshold_complete_model(threshold_relation())["problem"]


def discovery(
    *,
    relation_type: str = "EXACTLY_ONE",
    completeness: str = "COMPLETE",
    n: int = 3,
    venues: tuple[str, ...] | None = None,
    event_bases: tuple[str, ...] | None = None,
    problem: dict[str, object] | None = PROBLEM,
) -> dict[str, object]:
    venues = venues or tuple("Polymarket" for _ in range(n))
    event_bases = event_bases or tuple("event-a" for _ in range(n))
    markets = []
    for index in range(n):
        markets.append({
            "venue": venues[index],
            "contract_id": f"condition-{index}",
            "title": f"Market {index}",
            "market_date": "2026-08-15T00:00:00Z",
            "expires_at": "2026-12-31T17:00:00Z",
            "event_identity_basis": event_bases[index],
            "settlement_observation_key": "btc-usd",
            "settlement_rules": "official index",
            "cancellation_rules": "void refunds",
        })
    model: dict[str, object] = {"completeness": completeness}
    if completeness == "COMPLETE":
        model.update({
            "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
            "payouts": {
                f"condition-{index}": {"NORMAL_YES": 1, "NORMAL_NO": 0, "VOID": 0}
                for index in range(n)
            },
            "capital_release": "2026-12-31T17:00:00Z",
        })
        if problem is not None:
            model["problem"] = problem
    return {
        "discovery_source": "exchange_metadata",
        "discovered_at": "2026-08-15T02:32:00Z",
        "relation_type": relation_type,
        "semantics": {"statement": "exactly one resolves YES", "direction": "A_TO_B"},
        "source_evidence": [{"source": "Polymarket rules", "quote": "resolves YES if..."}],
        "model": model,
        "markets": markets,
    }


def test_ingest_controlled_accepts_complete_n3_payload_and_stays_pending(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    result = catalog.ingest_controlled(discovery())
    assert result["status"] == "PENDING"
    assert catalog.current_generation() == {}
    rows = catalog.review_rows()
    assert len(rows) == 1
    assert rows[0]["model"]["problem"] == PROBLEM


def test_list_pending_does_not_reload_catalog_per_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = RelationCatalog(tmp_path)
    for index in range(50):
        payload = discovery(completeness="INCOMPLETE", n=2)
        payload["markets"][0]["contract_id"] = f"condition-a-{index}"
        payload["markets"][1]["contract_id"] = f"condition-b-{index}"
        catalog.ingest(payload)

    store = catalog._store
    original_load_state = SqliteCatalogStore._load_state
    calls: list[object] = []

    def counting_load_state(self: SqliteCatalogStore, conn: object) -> dict:
        calls.append(conn)
        return original_load_state(self, conn)

    monkeypatch.setattr(SqliteCatalogStore, "_load_state", counting_load_state)

    rows = catalog.list("pending")

    assert len(rows) == 50
    assert len(calls) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"completeness": "INCOMPLETE"},
        {"n": 2},
        {"venues": ("Polymarket", "Polymarket", "Predict.fun")},
        {"event_bases": ("event-a", "event-a", "event-b")},
        {"problem": None},
        {"problem": {"schema_version": "open_trader.prediction_n_leg.problem.v1", "problem_id": "placeholder", "kind": "compiled"}},
    ],
)
def test_ingest_controlled_rejects_invalid_payloads(
    tmp_path: Path, kwargs: dict[str, object]
) -> None:
    catalog = RelationCatalog(tmp_path)
    with pytest.raises(ValueError):
        catalog.ingest_controlled(discovery(**kwargs))
    assert catalog.review_rows() == []
def test_cleanup_dry_run_lists_only_model_less_pending_rows(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    incomplete_id = catalog.ingest(discovery(completeness="INCOMPLETE"))["version_id"]
    complete_id = catalog.ingest(discovery(problem=PROBLEM))["version_id"]

    matches = catalog.cleanup_incomplete_pending(actor="op", git_sha="sha", dry_run=True)
    assert [match["version_id"] for match in matches] == [incomplete_id]
    assert matches[0]["identity"]
    assert matches[0]["fingerprint"]

    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[incomplete_id]["status"] == "PENDING"
    assert rows[complete_id]["status"] == "PENDING"


def test_cleanup_apply_rejects_incomplete_and_preserves_complete_active_rows(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    incomplete_id = catalog.ingest(discovery(completeness="INCOMPLETE"))["version_id"]
    active_id = catalog.ingest(
        discovery(relation_type="IMPLIES", n=2, problem=PROBLEM)
    )["version_id"]
    catalog.approve(active_id, {"version_id": active_id}, actor="op", git_sha="sha")

    result = catalog.cleanup_incomplete_pending(actor="op", git_sha="sha", dry_run=False)
    assert result["applied"] == 1
    assert result["rejected"][0]["version_id"] == incomplete_id
    assert result["rejected"][0]["status"] == "REJECTED"

    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[incomplete_id]["status"] == "REJECTED"
    assert rows[active_id]["status"] == "APPROVED"
    assert rows[active_id]["activation"] == "ACTIVE"


def test_cleanup_apply_rejects_latest_and_non_latest_pending_incomplete(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    first_payload = discovery(completeness="INCOMPLETE")
    second_payload = discovery(completeness="INCOMPLETE")
    second_payload["semantics"]["statement"] = "a different incomplete statement"
    first_id = catalog.ingest(first_payload)["version_id"]
    second_id = catalog.ingest(second_payload)["version_id"]
    assert first_id != second_id

    result = catalog.cleanup_incomplete_pending(actor="op", git_sha="sha", dry_run=False)
    assert result["applied"] == 2
    assert {row["version_id"] for row in result["rejected"]} == {first_id, second_id}

    rows = {row["version_id"]: row for row in catalog.review_rows()}
    for version_id in (first_id, second_id):
        assert rows[version_id]["status"] == "REJECTED"
        assert rows[version_id]["activation"] == "REJECTED"


def test_cli_relation_ingest_success(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    payload_file = tmp_path / "discovery.json"
    payload_file.write_text(json.dumps(discovery()), encoding="utf-8")

    code = cli.main([
        "prediction-arb",
        "relation-ingest",
        "--file",
        str(payload_file),
        "--data-dir",
        str(tmp_path / "data"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "version_id:" in out
    assert "identity:" in out
    assert "status: PENDING" in out

    catalog = RelationCatalog(tmp_path / "data")
    assert len(catalog.review_rows()) == 1
    assert catalog.current_generation() == {}


def test_cli_relation_ingest_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    payload_file = tmp_path / "discovery.json"
    payload_file.write_text(json.dumps(discovery(completeness="INCOMPLETE")), encoding="utf-8")

    code = cli.main([
        "prediction-arb",
        "relation-ingest",
        "--file",
        str(payload_file),
        "--data-dir",
        str(tmp_path / "data"),
    ])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_catalog_cleanup_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    catalog = RelationCatalog(tmp_path / "data")
    incomplete_id = catalog.ingest(discovery(completeness="INCOMPLETE"))["version_id"]
    catalog.ingest(discovery())

    code = cli.main([
        "prediction-arb",
        "catalog-cleanup",
        "--dry-run",
        "--data-dir",
        str(tmp_path / "data"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "matches: 1" in out
    assert incomplete_id in out
    assert catalog.review_rows()  # dry-run leaves rows untouched


def test_cli_catalog_cleanup_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    catalog = RelationCatalog(tmp_path / "data")
    incomplete_id = catalog.ingest(discovery(completeness="INCOMPLETE"))["version_id"]

    code = cli.main([
        "prediction-arb",
        "catalog-cleanup",
        "--apply",
        "--data-dir",
        str(tmp_path / "data"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "applied: 1" in out
    assert "REJECTED" in out

    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[incomplete_id]["status"] == "REJECTED"
    assert rows[incomplete_id]["activation"] == "REJECTED"


def test_concurrent_readers_on_shared_catalog_do_not_nest_transactions(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["version_id"]
    catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="a" * 40)

    errors: list[BaseException] = []

    def read_once() -> None:
        catalog.current_generation()
        catalog.generation_meta()
        _ = catalog._store["generation_number"]
        _ = catalog._store["versions"]

    def reader() -> None:
        for _ in range(100):
            try:
                read_once()
            except BaseException as exc:  # noqa: BLE001 - race must not leak
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in [pool.submit(reader) for _ in range(6)]:
            future.result()

    transaction_errors = [
        exc for exc in errors if isinstance(exc, sqlite3.OperationalError)
    ]
    assert not errors
    assert not transaction_errors
