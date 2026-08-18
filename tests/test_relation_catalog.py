from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.relation_catalog import (
    RelationCatalog,
    _derive_statement,
    _threshold_complete_model,
    _threshold_discovery_payload,
)
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


def drifted_question(relation: object) -> object:
    return replace(
        relation,
        market_a=replace(relation.market_a, question=f"{relation.market_a.question} (edited)"),
        market_b=replace(relation.market_b, question=f"{relation.market_b.question} (edited)"),
    )


def test_dedup_complete_pending_keeps_latest_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    base = threshold_relation()
    first_id = catalog.ingest_threshold_relation(base)["version_id"]
    second_id = catalog.ingest_threshold_relation(drifted_question(base))["version_id"]
    assert catalog.pending_count() == 2

    matches = catalog.dedup_complete_pending(actor="cli", git_sha="", dry_run=True)
    assert catalog.pending_count() == 2
    assert len(matches) == 1
    assert matches[0]["kept_version_id"] == second_id
    assert matches[0]["reject_version_ids"] == [first_id]

    result = catalog.dedup_complete_pending(actor="cli", git_sha="", dry_run=False)
    assert result["applied"] == 1
    assert catalog.pending_count() == 1
    assert [row["version_id"] for row in catalog.list("pending")] == [second_id]
    assert {row["version_id"] for row in catalog.list("history")} == {first_id}
    assert len(catalog.review_rows()) == 2

    approved = catalog.approve(
        second_id, {"version_id": second_id}, actor="operator", git_sha="sha"
    )
    assert approved["status"] == "APPROVED"
    assert approved["activation"] == "ACTIVE"


def test_dedup_complete_pending_apply_is_bounded_and_rerunnable(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)

    def duplicate_pair(tag: str) -> tuple[object, object]:
        base = threshold_relation()
        retagged = replace(
            base,
            market_a=replace(base.market_a, condition_id=f"condition-{tag}-a"),
            market_b=replace(base.market_b, condition_id=f"condition-{tag}-b"),
        )
        return retagged, drifted_question(retagged)

    for pair in (duplicate_pair("x"), duplicate_pair("y")):
        catalog.ingest_threshold_relation(pair[0])
        catalog.ingest_threshold_relation(pair[1])
    assert catalog.pending_count() == 4

    bounded = catalog.dedup_complete_pending(
        actor="cli", git_sha="", dry_run=False, limit=1
    )
    assert bounded["applied"] == 1
    assert catalog.pending_count() == 3

    finished = catalog.dedup_complete_pending(actor="cli", git_sha="", dry_run=False)
    assert finished["applied"] == 1
    assert catalog.pending_count() == 2
    assert catalog.dedup_complete_pending(actor="cli", git_sha="", dry_run=True) == []


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


def test_cli_catalog_dedup_dry_run_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    catalog = RelationCatalog(tmp_path / "data")
    base = threshold_relation()
    first_id = catalog.ingest_threshold_relation(base)["version_id"]
    second_id = catalog.ingest_threshold_relation(drifted_question(base))["version_id"]

    code = cli.main([
        "prediction-arb",
        "catalog-dedup",
        "--dry-run",
        "--data-dir",
        str(tmp_path / "data"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "duplicates: 1" in out
    assert first_id in out
    assert second_id in out
    assert catalog.pending_count() == 2  # dry-run leaves rows untouched

    code = cli.main([
        "prediction-arb",
        "catalog-dedup",
        "--apply",
        "--data-dir",
        str(tmp_path / "data"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "applied: 1" in out

    # An externally-run CLI write is visible to a freshly opened catalog; the
    # pre-existing handle keeps its thread-local read cache by design (#91).
    reopened = RelationCatalog(tmp_path / "data")
    assert reopened.pending_count() == 1
    rows = {row["version_id"]: row for row in reopened.review_rows()}
    assert rows[first_id]["status"] == "REJECTED"
    assert rows[second_id]["status"] == "PENDING"


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


def _unique_discovery(suffix: str, *, completeness: str = "COMPLETE") -> dict[str, object]:
    payload = discovery(completeness=completeness, n=2)
    payload["markets"][0]["contract_id"] = f"condition-a-{suffix}"
    payload["markets"][1]["contract_id"] = f"condition-b-{suffix}"
    return payload


def _force_record(catalog: RelationCatalog, version_id: str, **overrides: object) -> None:
    record = dict(catalog._versions()[version_id])
    record.update(overrides)
    catalog._store_write({version_id: record})


def test_list_filters_the_six_review_state_views_and_counts_them(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)

    pending_id = catalog.ingest(
        _unique_discovery("p1", completeness="INCOMPLETE")
    )["version_id"]
    incomplete_id = catalog.ingest(
        _unique_discovery("i1", completeness="INCOMPLETE")
    )["version_id"]
    approved_incomplete = catalog.approve(
        incomplete_id, {"version_id": incomplete_id}, actor="op", git_sha="sha"
    )
    assert approved_incomplete["activation"] == "INCOMPLETE"

    active_id = catalog.ingest(_unique_discovery("a1"))["version_id"]
    catalog.approve(active_id, {"version_id": active_id}, actor="op", git_sha="sha")

    blocked_id = catalog.ingest(_unique_discovery("b1"))["version_id"]
    catalog.approve(blocked_id, {"version_id": blocked_id}, actor="op", git_sha="sha")
    _force_record(catalog, blocked_id, activation_status="ACTIVATION_BLOCKED_INCONSISTENT")

    size_blocked_id = catalog.ingest(_unique_discovery("b2"))["version_id"]
    catalog.approve(size_blocked_id, {"version_id": size_blocked_id}, actor="op", git_sha="sha")
    _force_record(catalog, size_blocked_id, activation_status="UNSUPPORTED_SIZE")

    compiled_id = catalog.ingest(_unique_discovery("c1"))["version_id"]
    catalog.approve(compiled_id, {"version_id": compiled_id}, actor="op", git_sha="sha")
    _force_record(catalog, compiled_id, activation_status="PENDING")

    superseded_id = catalog.ingest(_unique_discovery("s1"))["version_id"]
    catalog.approve(superseded_id, {"version_id": superseded_id}, actor="op", git_sha="sha")
    _force_record(catalog, superseded_id, activation_status="SUPERSEDED")

    rejected_id = catalog.ingest(
        _unique_discovery("r1", completeness="INCOMPLETE")
    )["version_id"]
    catalog.reject(
        rejected_id, {"version_id": rejected_id}, reason="other", actor="op", git_sha="sha"
    )

    views = {
        "pending_approval": [pending_id],
        "approved_model_incomplete": [incomplete_id],
        "compiled_pending_activation": [compiled_id],
        "activation_blocked": {blocked_id, size_blocked_id},
        "activated": [active_id],
        "source_changed_reapproval": [superseded_id],
    }
    for view, expected in views.items():
        rows = catalog.list(view)
        assert {row["version_id"] for row in rows} == set(expected)
        assert rejected_id not in {row["version_id"] for row in rows}

    assert catalog.review_counts() == {
        "counts": {
            "PENDING_APPROVAL": 1,
            "APPROVED_MODEL_INCOMPLETE": 1,
            "COMPILED_PENDING_ACTIVATION": 1,
            "ACTIVATION_BLOCKED": 2,
            "ACTIVATED": 1,
            "SOURCE_CHANGED_REAPPROVAL": 1,
        },
        "pending_count": 1,
    }


def test_list_legacy_view_aliases_keep_their_semantics(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)

    pending_id = catalog.ingest(
        _unique_discovery("p1", completeness="INCOMPLETE")
    )["version_id"]
    active_id = catalog.ingest(_unique_discovery("a1"))["version_id"]
    catalog.approve(active_id, {"version_id": active_id}, actor="op", git_sha="sha")

    base = _unique_discovery("a1")
    drifted = dict(base)
    drifted["markets"] = [dict(market) for market in base["markets"]]
    drifted["markets"][0]["title"] = "Market 0 (edited)"
    blocked_id = catalog.ingest(drifted)["version_id"]
    blocked = catalog.approve(
        blocked_id, {"version_id": blocked_id}, actor="op", git_sha="sha"
    )
    assert blocked["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"

    superseded_id = catalog.ingest(_unique_discovery("s1"))["version_id"]
    _force_record(
        catalog, superseded_id, status="APPROVED", activation_status="SUPERSEDED"
    )

    rejected_id = catalog.ingest(
        _unique_discovery("r1", completeness="INCOMPLETE")
    )["version_id"]
    catalog.reject(
        rejected_id, {"version_id": rejected_id}, reason="other", actor="op", git_sha="sha"
    )

    assert {row["version_id"] for row in catalog.list("pending")} == {pending_id}
    assert {row["version_id"] for row in catalog.list("pending_approval")} == {pending_id}
    assert {row["version_id"] for row in catalog.list("approved_active")} == {active_id}
    # Six-state semantics: blocked covers INCONSISTENT/UNSUPPORTED_SIZE; the
    # legacy INCOMPLETE rows now live in approved_model_incomplete.
    assert {row["version_id"] for row in catalog.list("activation_blocked")} == {blocked_id}
    assert {row["version_id"] for row in catalog.list("history")} == {
        rejected_id, superseded_id,
    }
    with pytest.raises(ValueError):
        catalog.list("nonsense")


def test_threshold_statement_is_derived_at_read_time_with_direction_code(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest_threshold_relation(threshold_relation())["version_id"]

    detail = catalog.detail(version_id)
    assert detail["direction_code"] == "B_IMPLIES_A"
    assert detail["statement"] == "B『BTC above $100000?』为 YES ⇒ A『BTC above $90000?』必须 YES"
    assert "B_IMPLIES_A" not in detail["statement"]

    row = catalog.list("pending")[0]
    assert row["version_id"] == version_id
    assert row["direction_code"] == "B_IMPLIES_A"
    assert row["statement"] == detail["statement"]


def test_list_and_detail_keep_full_statement_titles(
    tmp_path: Path,
) -> None:
    base = threshold_relation()
    relation = replace(
        base,
        market_a=replace(base.market_a, question="A" * 40),
        market_b=replace(base.market_b, question="B" * 5),
    )
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest_threshold_relation(relation)["version_id"]

    detail = catalog.detail(version_id)
    assert f"B『{'B' * 5}』为 YES ⇒ A『{'A' * 40}』必须 YES" == detail["statement"]

    row = catalog.list("pending")[0]
    assert row["statement"] == detail["statement"]
    assert "A" * 40 in row["statement"]
    assert "…" not in row["statement"]


def test_derive_statement_only_rewrites_direction_code_literals() -> None:
    assert _derive_statement("exactly one resolves YES", [{"title": "A"}, {"title": "B"}]) == (
        "exactly one resolves YES", "",
    )
    assert _derive_statement("A_IMPLIES_B", [{"title": "A"}]) == ("A_IMPLIES_B", "")
    # Direction codes without resolvable endpoint roles stay as the raw code;
    # guessing the antecedent from endpoint order is what flipped statements.
    assert _derive_statement("A_IMPLIES_B", [{"title": "A"}, {"title": "B"}]) == (
        "A_IMPLIES_B", "A_IMPLIES_B",
    )
    statement, code = _derive_statement(
        "A_TO_B", [{"title": "Q1"}, {"title": "Q2"}], roles=("A", "B")
    )
    assert statement == "A『Q1』为 YES ⇒ B『Q2』必须 YES"
    assert code == "A_TO_B"
    # Roles decide the antecedent, never the stored endpoint order.
    statement, code = _derive_statement(
        "A_TO_B", [{"title": "Q1"}, {"title": "Q2"}], roles=("B", "A")
    )
    assert statement == "A『Q2』为 YES ⇒ B『Q1』必须 YES"
    assert code == "A_TO_B"
    statement, code = _derive_statement(
        "B_IMPLIES_A", [{"title": "Q1"}, {"title": "Q2"}], roles=("B", "A")
    )
    assert statement == "B『Q1』为 YES ⇒ A『Q2』必须 YES"
    assert code == "B_IMPLIES_A"
    # Degenerate role payloads never derive.
    assert _derive_statement(
        "B_IMPLIES_A", [{"title": "Q1"}, {"title": "Q2"}], roles=("A", "A")
    ) == ("B_IMPLIES_A", "B_IMPLIES_A")
    assert _derive_statement(
        "B_IMPLIES_A", [{"title": "Q1"}, {"title": "Q2"}], roles=("", "B")
    ) == ("B_IMPLIES_A", "B_IMPLIES_A")


def _threshold_with_ids(
    condition_a_id: str, condition_b_id: str, *, relation_code: str
) -> object:
    base = threshold_relation()
    return replace(
        base,
        relation=relation_code,
        market_a=replace(base.market_a, condition_id=condition_a_id),
        market_b=replace(base.market_b, condition_id=condition_b_id),
    )


B_IMPLIES_A_ANTENECENT_LOW = (
    # B_IMPLIES_A: market_b (BTC above $100000) is the antecedent and its
    # condition_id sorts BELOW market_a's — the ordering that used to flip.
    _threshold_with_ids("0xbeef000000000001", "0x0aaa000000000001", relation_code="B_IMPLIES_A"),
    "B『BTC above $100000?』为 YES ⇒ A『BTC above $90000?』必须 YES",
    "BTC above $100000?",
    "B",
)
B_IMPLIES_A_ANTENECENT_HIGH = (
    # Same direction with the antecedent sorting above the consequent; the
    # pre-fix code only passed this case by contract_id coincidence.
    _threshold_with_ids("0x1aaa000000000001", "0x9fff000000000001", relation_code="B_IMPLIES_A"),
    "B『BTC above $100000?』为 YES ⇒ A『BTC above $90000?』必须 YES",
    "BTC above $100000?",
    "B",
)
A_IMPLIES_B_ANTENECENT_HIGH = (
    # A_IMPLIES_B: market_a (BTC above $90000) is the antecedent and its
    # condition_id sorts ABOVE market_b's — the ordering that used to flip.
    _threshold_with_ids("0xe111000000000001", "0x0bbb000000000001", relation_code="A_IMPLIES_B"),
    "A『BTC above $90000?』为 YES ⇒ B『BTC above $100000?』必须 YES",
    "BTC above $90000?",
    "A",
)
A_IMPLIES_B_ANTENECENT_LOW = (
    _threshold_with_ids("0x0ccc000000000001", "0xd222000000000001", relation_code="A_IMPLIES_B"),
    "A『BTC above $90000?』为 YES ⇒ B『BTC above $100000?』必须 YES",
    "BTC above $90000?",
    "A",
)


@pytest.mark.parametrize(
    "relation,expected_statement,antecedent_title,antecedent_letter",
    [
        B_IMPLIES_A_ANTENECENT_LOW,
        B_IMPLIES_A_ANTENECENT_HIGH,
        A_IMPLIES_B_ANTENECENT_HIGH,
        A_IMPLIES_B_ANTENECENT_LOW,
    ],
    ids=[
        "b_implies_a-antecedent-low",
        "b_implies_a-antecedent-high",
        "a_implies_b-antecedent-high",
        "a_implies_b-antecedent-low",
    ],
)
def test_threshold_statement_keeps_true_direction_regardless_of_contract_order(
    tmp_path: Path,
    relation: object,
    expected_statement: str,
    antecedent_title: str,
    antecedent_letter: str,
) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest_threshold_relation(relation)["version_id"]

    detail = catalog.detail(version_id)
    assert detail["statement"] == expected_statement

    # Endpoint roles are persisted on the stored payload and survive into rows.
    stored = catalog._versions()[version_id]["payload"]["endpoints"]
    roles = {str(endpoint["title"]): str(endpoint.get("role", "")) for endpoint in stored}
    assert roles[antecedent_title] == antecedent_letter
    assert sorted(roles.values()) == ["A", "B"]

    row = [item for item in catalog.list("pending") if item["version_id"] == version_id][0]
    assert row["statement"] == expected_statement
    row_roles = {
        str(endpoint["title"]): str(endpoint.get("role", ""))
        for endpoint in row["endpoints"]
    }
    assert row_roles[antecedent_title] == antecedent_letter


def _legacy_threshold_discovery(relation: object) -> dict[str, object]:
    """A pre-role-fields discovery payload, as written before this fix."""
    model = _threshold_complete_model(relation)
    payload = _threshold_discovery_payload(
        relation, model if model is not None else {"completeness": "INCOMPLETE"}
    )
    semantics = dict(payload["semantics"])
    semantics.pop("antecedent_contract_id", None)
    semantics.pop("consequent_contract_id", None)
    return {**payload, "semantics": semantics}


def test_legacy_row_without_roles_derives_direction_from_compiled_model(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    relation = B_IMPLIES_A_ANTENECENT_LOW[0]
    version_id = catalog.ingest(_legacy_threshold_discovery(relation))["version_id"]

    # No roles were persisted for this legacy row...
    stored = catalog._versions()[version_id]["payload"]["endpoints"]
    assert all("role" not in endpoint for endpoint in stored)

    # ...so the statement comes from the compiled IMPLIES constraint, whose
    # contract order is antecedent-first and order-preserving.
    detail = catalog.detail(version_id)
    assert detail["direction_code"] == "B_IMPLIES_A"
    assert detail["statement"] == B_IMPLIES_A_ANTENECENT_LOW[1]
    roles = {
        str(endpoint["title"]): str(endpoint.get("role", ""))
        for endpoint in detail["endpoints"]
    }
    assert roles["BTC above $100000?"] == "B"
    assert roles["BTC above $90000?"] == "A"


def test_legacy_row_without_model_shows_direction_code_without_role_labels(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    relation = replace(
        B_IMPLIES_A_ANTENECENT_LOW[0],
        market_a=replace(
            B_IMPLIES_A_ANTENECENT_LOW[0].market_a, resolution_source=""  # type: ignore[attr-defined]
        ),
    )
    version_id = catalog.ingest(_legacy_threshold_discovery(relation))["version_id"]

    # No roles, no compiled model: the raw direction code stays and no
    # antecedent/consequent roles are invented.
    detail = catalog.detail(version_id)
    assert detail["statement"] == "B_IMPLIES_A"
    assert detail["direction_code"] == "B_IMPLIES_A"
    assert all("role" not in endpoint for endpoint in detail["endpoints"])
    row = [item for item in catalog.list("pending") if item["version_id"] == version_id][0]
    assert row["statement"] == "B_IMPLIES_A"
    assert all("role" not in endpoint for endpoint in row["endpoints"])


def test_legacy_row_with_mismatched_model_constraint_never_guesses_roles(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    # The compiled problem speaks about condition-a/condition-b, but the row's
    # endpoints are different contracts; roles must not be inferred.
    payload = _legacy_threshold_discovery(B_IMPLIES_A_ANTENECENT_LOW[0])
    for market in payload["markets"]:
        market["contract_id"] = f"{market['contract_id']}-other"
    version_id = catalog.ingest(payload)["version_id"]

    detail = catalog.detail(version_id)
    assert detail["statement"] == "B_IMPLIES_A"
    assert detail["direction_code"] == "B_IMPLIES_A"
    assert all("role" not in endpoint for endpoint in detail["endpoints"])
