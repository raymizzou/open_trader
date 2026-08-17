"""Issue #71: N>=3 no-submit validation harness tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_n_leg import fingerprint
from open_trader.prediction_n_leg_validation import (
    FailClosedExecution,
    build_report,
    frozen_snapshot_from_file,
    readonly_v2_relations,
    run_live,
    run_replay,
)
from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore


FIXTURE = Path(__file__).parent / "fixtures" / "prediction_n_leg_validation_frozen_n3.json"


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def live_books() -> dict[str, ThresholdOrderBook]:
    now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
    prices = {"a": "0.33", "b": "0.32", "c": "0.31"}
    return {
        contract: ThresholdOrderBook(
            contract,
            (BookLevel(Decimal(prices[contract]), Decimal("10")),),
            (),
            now,
        )
        for contract in prices
    }


def relation_payload(*, qualification: bool = False) -> dict[str, object]:
    fixture = load_fixture()
    problem = dict(fixture["problem"])
    if qualification:
        problem["qualification_constraints"] = [
            {
                "constraint_id": "min-profit",
                "rule_version": "v1",
                "metric": "GUARANTEED_PROFIT_UNITS",
                "comparison": "GREATER_THAN_OR_EQUAL",
                "threshold_numerator": 1,
                "threshold_denominator": 1,
            }
        ]
    return {
        "relation_type": "EXACTLY_ONE",
        "endpoints": [
            {"venue": "polymarket", "contract_id": contract}
            for contract in ("a", "b", "c")
        ],
        "model": {
            "terminal_states": [{}],
            "payouts": [{}],
            "capital_release": "2026-08-16T06:00:00Z",
            "problem": problem,
        },
    }


def seed_catalog(
    db_path: Path, *, activate: bool, qualification: bool = False
) -> None:
    catalog = RelationCatalogV2(SqliteCatalogStore(db_path))
    entry = catalog.ingest(relation_payload(qualification=qualification))
    catalog.approve(entry["version_id"], actor="test", git_sha="test")
    if activate:
        store = catalog.store
        store.begin_write()
        store["versions"][entry["version_id"]]["activation_status"] = "ACTIVE"
        store.commit_write()


def test_replay_n3_happy_path() -> None:
    report = run_replay(frozen_snapshot_from_file(FIXTURE))

    assert report["status"] == "PASS"
    assert report["legs"] == 3
    assert [q["action_id"] for q in report["quantities"] if q["quantity_lots"] > 0] == [
        "buy-yes-a",
        "buy-yes-b",
        "buy-yes-c",
    ]
    decision = report["execution_decision"]
    assert decision["order_ready"] is False
    assert decision["partial_fill_proof"] == "UNKNOWN"
    assert decision["reason"] == "PARTIAL_FILL_PROOF_REQUIRED"
    assert report["market"]["guaranteed_profit_units"] == 40_000
    assert report["oracle_differential"]["pass"] is True
    assert all(item["pass"] for item in report["oracle_differential"]["checks"])
    assert all(item["pass"] for item in report["expected_vs_actual"])


def test_replay_two_leg_snapshot_is_rejected(tmp_path: Path) -> None:
    data = load_fixture()
    problem = data["problem"]
    problem["actions"] = [
        action for action in problem["actions"] if action["action_id"] != "buy-yes-c"
    ]
    problem["terminal_state_sets"] = [
        state for state in problem["terminal_state_sets"] if state["market_contract_id"] != "c"
    ]
    problem["constraint_model"]["relations"][0]["contract_ids"] = ["a", "b"]
    data["expected"]["portfolio_actions"] = ["buy-yes-a", "buy-yes-b"]
    data["books"].pop("buy-yes-c")
    data["content_fingerprint"] = fingerprint({"problem": problem, "books": data["books"]})
    path = tmp_path / "two-leg.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    report = run_replay(frozen_snapshot_from_file(path))

    assert report["status"] == "FAIL"
    assert report["reason"] == "N_LESS_THAN_3"
    assert report["legs"] == 2


def test_frozen_snapshot_rejects_tampered_content(tmp_path: Path) -> None:
    data = load_fixture()
    data["books"]["buy-yes-a"]["asks"][0]["price"] = "0.99"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="content fingerprint mismatch"):
        frozen_snapshot_from_file(path)


def test_live_no_active_relation_is_blocked(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=False)
    exported = readonly_v2_relations(db)

    live = run_live(
        exported["rows"],
        book_source=lambda _: live_books(),
        data_dir=tmp_path / "run",
        catalog=exported,
    )

    assert live["status"] == "BLOCKED"
    assert live["reason"] == "NO_ACTIVE_N3_RELATION"
    assert live["zero_side_effects"]["submitted_orders"] == 0


def test_live_active_n3_relation_with_injected_books_qualifies(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)
    seam = FailClosedExecution()

    live = run_live(
        exported["rows"],
        book_source=lambda _: live_books(),
        data_dir=tmp_path / "run",
        catalog=exported,
        execution=seam,
    )

    assert live["status"] == "PASS"
    assert live["legs"] == 3
    assert live["qualified_verified"] is True
    assert live["execution_decision"]["order_ready"] is False
    assert live["execution_decision"]["reason"] == "PARTIAL_FILL_PROOF_REQUIRED"
    assert seam.submit_attempts == 0
    assert seam.mutation_attempts == 0
    assert live["zero_side_effects"]["submitted_orders"] == 0


def test_live_proven_no_qualified_opportunity_passes(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True, qualification=True)
    exported = readonly_v2_relations(db)

    def expensive_books(_: tuple[str, ...]) -> dict[str, ThresholdOrderBook]:
        now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
        return {
            contract: ThresholdOrderBook(
                contract,
                (BookLevel(Decimal("0.55"), Decimal("10")),),
                (),
                now,
            )
            for contract in ("a", "b", "c")
        }

    live = run_live(
        exported["rows"],
        book_source=expensive_books,
        data_dir=tmp_path / "run",
        catalog=exported,
    )

    assert live["status"] == "PASS"
    assert live["qualified_verified"] is False
    assert live["legs"] == 0
    assert live["execution_decision"] is None
    assert live["zero_side_effects"]["submitted_orders"] == 0


def test_live_missing_books_is_blocked(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)

    live = run_live(
        exported["rows"],
        book_source=lambda _: {},
        data_dir=tmp_path / "run",
        catalog=exported,
    )

    assert live["status"] == "BLOCKED"
    assert live["reason"] == "MISSING_BOOKS"


def test_fail_closed_seam_blocks_mutation() -> None:
    seam = FailClosedExecution()

    with pytest.raises(AssertionError, match="must never be reached"):
        seam.submit()
    with pytest.raises(AssertionError, match="must never be reached"):
        seam.mutate()

    assert seam.submit_attempts == 1
    assert seam.mutation_attempts == 1


def test_report_schema_and_fingerprints(tmp_path: Path) -> None:
    replay = run_replay(frozen_snapshot_from_file(FIXTURE))
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)
    live = run_live(
        exported["rows"],
        book_source=lambda _: live_books(),
        data_dir=tmp_path / "run",
        catalog=exported,
    )

    report = build_report(replay=replay, live=live, data_dir=tmp_path / "run")

    assert report["status"] == "PASS"
    assert report["schema_version"] == "open_trader.prediction_n_leg_validation.report.v1"
    assert isinstance(report["pid"], int) and report["pid"] > 0
    assert report["cwd"]
    assert report["git_sha"]
    assert report["captured_at"]
    assert report["zero_side_effects"]["submitted_orders"] == 0
    replay_fingerprints = report["replay"]["fingerprints"]
    assert replay_fingerprints["content"] == load_fixture()["content_fingerprint"]
    assert replay_fingerprints["structure"]
    assert report["replay"]["constraint_generation_rounds"]["master_rounds"] >= 0
    assert report["replay"]["timings"]["solve_seconds"] >= 0
    assert report["live"]["fingerprints"]["catalog_generation"] == exported["generation"]
