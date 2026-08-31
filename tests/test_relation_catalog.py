from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.relation_catalog import (
    REVIEW_STATES,
    RelationCatalog,
    RelationConflictError,
    _derive_statement,
    _threshold_complete_model,
    _threshold_discovery_payload,
    default_catalog_path,
)
from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore
from open_trader.prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    RelationConstraint,
    RelationKind,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
)
from test_prediction_arbitrage import threshold_relation


PROBLEM = _threshold_complete_model(threshold_relation())["problem"]


def compiled_problem(
    contract_ids: list[str],
    sides: dict[str, str],
    *,
    as_of: str = "2026-08-15T00:00:00Z",
    release_at: str = "2026-12-31T17:00:00Z",
    rule: str | dict[str, str] = "rules-issue-99",
    problem_id: str = "issue-99",
) -> dict[str, object]:
    """A canonical compiled problem over ``contract_ids`` with per-contract sides.

    Mirrors ``_threshold_complete_model``: one EXACTLY_ONE constraint over the
    contracts, NORMAL_YES/NORMAL_NO/VOID atoms per contract releasing at
    ``release_at``, and a BUY_YES/BUY_NO action per contract at
    ``polymarket:{contract_id}``. ``rule`` may be a shared string or a
    per-contract dict, so tests can give each contract its own settlement
    observation key (disjoint observations within one explicit relation).
    """
    as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(UTC)
    release_dt = datetime.fromisoformat(release_at.replace("Z", "+00:00")).astimezone(UTC)
    shared_rule = rule[contract_ids[0]] if isinstance(rule, dict) else rule
    actions: list[CandidateAction] = []
    states: list[TerminalStateSet] = []
    for contract_id in contract_ids:
        contract_rule = rule[contract_id] if isinstance(rule, dict) else rule
        key = SettlementObservationKey(
            OBSERVATION_SCHEMA_V1,
            "oracle-issue-99",
            "indicator-issue-99",
            as_of_dt,
            as_of_dt,
            "UTC",
            contract_rule,
        )
        side = ActionSide(sides[contract_id])
        action_id = f"polymarket:{contract_id}"
        yes_payout = 1 if side == ActionSide.BUY_YES else 0
        no_payout = 0 if side == ActionSide.BUY_YES else 1
        actions.append(CandidateAction(
            action_id,
            venue_id="polymarket",
            account_id="catalog-v2",
            chain_id="polymarket",
            market_contract_id=contract_id,
            settlement_observation_key=key,
            side=side,
            lot_step_units=1,
            quantity_scale=1,
            min_quantity_lots=1,
            max_quantity_lots=1,
            settlement_asset_id="USD",
            valuation_unit_id="USD",
            asset_valuation_rule_id="usd-1:1-v1",
            cost_slices=(ExecutableCostSlice(1, 1, 0),),
        ))
        states.append(TerminalStateSet(
            contract_id,
            key,
            contract_rule,
            (
                TerminalAtom(
                    f"{contract_id}:NORMAL_YES", TerminalKind.NORMAL_YES, contract_rule,
                    (ActionPayout(action_id, yes_payout),), release_dt,
                ),
                TerminalAtom(
                    f"{contract_id}:NORMAL_NO", TerminalKind.NORMAL_NO, contract_rule,
                    (ActionPayout(action_id, no_payout),), release_dt,
                ),
                TerminalAtom(
                    f"{contract_id}:VOID", TerminalKind.VOID, contract_rule,
                    (ActionPayout(action_id, 0),), release_dt,
                ),
            ),
        ))
    problem = ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        problem_id,
        as_of_dt,
        "USD",
        tuple(actions),
        tuple(states),
        ConstraintModel(
            (
                RelationConstraint(
                    f"exactly-one:{':'.join(contract_ids)}",
                    RelationKind.EXACTLY_ONE,
                    tuple(contract_ids),
                    shared_rule,
                ),
            ),
            (),
        ),
        (),
    )
    return canonical_payload(problem)


def compiled_relation_discovery(
    contract_ids: list[str],
    sides: dict[str, str],
    *,
    relation_type: str = "EXACTLY_ONE",
    as_of: str = "2026-08-15T00:00:00Z",
    release: str = "2026-12-31T17:00:00Z",
    rule: str | dict[str, str] = "rules-issue-99",
) -> dict[str, object]:
    """A facade discovery payload carrying a compiled problem over contracts."""
    payload = discovery(
        relation_type=relation_type,
        n=len(contract_ids),
        problem=compiled_problem(
            contract_ids, sides, as_of=as_of, release_at=release, rule=rule
        ),
    )
    for index, contract_id in enumerate(contract_ids):
        payload["markets"][index]["contract_id"] = contract_id
    payload["model"]["capital_release"] = release
    return payload


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


def test_review_counts_are_generation_pure_per_identity(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)

    # Identity X: v1 activated historically, then replaced in the generation
    # by v2. The #60 cutover left such historical records APPROVED+ACTIVE
    # even though the generation moved on, which is exactly the shape the
    # generation-pure counts must stop double-counting.
    x_v1 = catalog.ingest(_unique_discovery("gx"))["version_id"]
    catalog.approve(x_v1, {"version_id": x_v1}, actor="op", git_sha="sha")
    x_v2_payload = _unique_discovery("gx")
    x_v2_payload["markets"][0]["title"] = "Market 0 (edited)"
    x_v2 = catalog.ingest(x_v2_payload)["version_id"]
    catalog.approve(x_v2, {"version_id": x_v2}, actor="op", git_sha="sha")
    catalog.replace(
        {"version_id": x_v1},
        {"version_id": x_v2},
        reason="rules_changed",
        actor="op",
        git_sha="sha",
    )
    _force_record(catalog, x_v1, status="APPROVED", activation_status="ACTIVE")

    # Identity Y: an APPROVED+ACTIVE record that is not a generation member.
    y_v1 = catalog.ingest(_unique_discovery("gy"))["version_id"]
    _force_record(catalog, y_v1, status="APPROVED", activation_status="ACTIVE")

    counts = catalog.review_counts()["counts"]
    assert counts["ACTIVATED"] == 1
    active_rows = catalog.list("approved_active")
    assert len(active_rows) == 1
    assert active_rows[0]["version_id"] == x_v2


def test_review_counts_classify_only_the_latest_version_per_identity(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)

    # One identity: v1 activated, v2 approval blocked, v3 (latest) pending.
    activated = catalog.ingest(_unique_discovery("gz"))["version_id"]
    catalog.approve(activated, {"version_id": activated}, actor="op", git_sha="sha")
    blocked_payload = _unique_discovery("gz")
    blocked_payload["markets"][0]["title"] = "Market 0 (edited)"
    blocked = catalog.ingest(blocked_payload)["version_id"]
    blocked_result = catalog.approve(
        blocked, {"version_id": blocked}, actor="op", git_sha="sha"
    )
    assert blocked_result["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    relisted_payload = _unique_discovery("gz")
    relisted_payload["markets"][0]["title"] = "Market 0 (relisted)"
    catalog.ingest(relisted_payload)

    counts = catalog.review_counts()["counts"]
    assert counts["PENDING_APPROVAL"] == 1
    assert counts["ACTIVATION_BLOCKED"] == 0
    assert catalog.pending_count() == 1


def test_review_counts_exclude_terminal_history_from_every_state(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)

    rejected = catalog.ingest(
        _unique_discovery("gr", completeness="INCOMPLETE")
    )["version_id"]
    catalog.reject(
        rejected, {"version_id": rejected}, reason="other", actor="op", git_sha="sha"
    )
    revoked = catalog.ingest(_unique_discovery("gv"))["version_id"]
    catalog.approve(revoked, {"version_id": revoked}, actor="op", git_sha="sha")
    catalog.revoke(
        revoked, {"version_id": revoked}, reason="rules_changed", actor="op", git_sha="sha"
    )
    expired = catalog.ingest(_unique_discovery("ge"))["version_id"]
    catalog.approve(expired, {"version_id": expired}, actor="op", git_sha="sha")
    catalog.expire_stale_members(now="2027-01-01T00:00:00Z", actor="op", git_sha="sha")

    assert catalog.review_counts()["counts"] == {state: 0 for state in REVIEW_STATES}
    assert catalog.pending_count() == 0


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


# Issue #99: activation gate compile precheck (facade level).

def test_activation_gate_blocks_compile_conflict_candidate(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    first = compiled_relation_discovery(
        ["condition-a", "condition-b", "condition-z"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
    )
    second = compiled_relation_discovery(
        ["condition-a", "condition-c", "condition-w"],
        {"condition-a": "BUY_NO", "condition-c": "BUY_YES", "condition-w": "BUY_YES"},
    )
    first_id = catalog.ingest_controlled(first)["version_id"]
    assert catalog.approve(first_id, {"version_id": first_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    before = dict(catalog.current_generation())
    assert len(before) == 1
    second_id = catalog.ingest_controlled(second)["version_id"]
    blocked = catalog.approve(second_id, {"version_id": second_id}, actor="op", git_sha="sha")
    assert blocked["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    assert catalog.current_generation() == before
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[second_id]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    assert rows[second_id]["status"] == "APPROVED"


def test_activation_gate_blocks_stale_capital_release_candidate(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    first = compiled_relation_discovery(
        ["condition-a", "condition-b", "condition-z"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
        as_of="2026-08-15T00:00:00Z",
        release="2026-12-31T17:00:00Z",
    )
    later = compiled_relation_discovery(
        ["condition-c", "condition-d", "condition-w"],
        {"condition-c": "BUY_YES", "condition-d": "BUY_YES", "condition-w": "BUY_YES"},
        as_of="2027-03-01T00:00:00Z",
        release="2027-06-01T17:00:00Z",
    )
    first_id = catalog.ingest_controlled(first)["version_id"]
    assert catalog.approve(first_id, {"version_id": first_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    before = dict(catalog.current_generation())
    later_id = catalog.ingest_controlled(later)["version_id"]
    blocked = catalog.approve(later_id, {"version_id": later_id}, actor="op", git_sha="sha")
    assert blocked["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    assert catalog.current_generation() == before


def test_activation_gate_accepts_compile_compatible_candidate(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    first = compiled_relation_discovery(
        ["condition-a", "condition-b", "condition-z"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES", "condition-z": "BUY_YES"},
    )
    compatible = compiled_relation_discovery(
        ["condition-a", "condition-c", "condition-w"],
        {"condition-a": "BUY_YES", "condition-c": "BUY_YES", "condition-w": "BUY_YES"},
    )
    first_id = catalog.ingest_controlled(first)["version_id"]
    assert catalog.approve(first_id, {"version_id": first_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    compatible_id = catalog.ingest_controlled(compatible)["version_id"]
    result = catalog.approve(compatible_id, {"version_id": compatible_id}, actor="op", git_sha="sha")
    assert result["activation"] == "ACTIVE"
    generation = catalog.current_generation()
    assert len(generation) == 2
    assert all(row["activation"] == "ACTIVE" for row in generation.values())


# Issue #102: same-event activation gate (facade level).

def _legacy_v2_payload() -> dict[str, object]:
    """A stored v2 payload as written before issue #102: no event_identity_basis."""
    return {
        "relation_type": "EXACTLY_ONE",
        "endpoints": [
            {
                "venue": "polymarket", "contract_id": f"condition-{letter}",
                "title": f"Market {letter}",
                "market_date": "2026-08-15T00:00:00Z",
                "expires_at": "2026-12-31T17:00:00Z",
                "settlement_observation_key": "btc-usd",
                "settlement_rules": "official index",
                "cancellation_rules": "void refunds",
            }
            for letter in ("a", "b")
        ],
        "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
        "payouts": {
            f"condition-{letter}": {"NORMAL_YES": 1, "NORMAL_NO": 0, "VOID": 0}
            for letter in ("a", "b")
        },
        "capital_release": "2026-12-31T17:00:00Z",
        "discovery_source": "exchange_metadata",
        "discovered_at": "2026-08-15T02:32:00Z",
        "problem": compiled_problem(
            ["condition-a", "condition-b"],
            {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
        ),
    }


def test_activation_blocks_legacy_version_without_event_identity_basis(
    tmp_path: Path,
) -> None:
    default_catalog_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    store = SqliteCatalogStore(str(default_catalog_path(tmp_path)))
    legacy_id = RelationCatalogV2(store).ingest(_legacy_v2_payload())["version_id"]

    catalog = RelationCatalog(tmp_path)
    result = catalog.approve(legacy_id, {"version_id": legacy_id}, actor="op", git_sha="sha")
    assert result["activation"] == "ACTIVATION_BLOCKED_EVENT_IDENTITY_MISSING"
    assert result["activation_diagnostic"].startswith(
        "ACTIVATION_BLOCKED_EVENT_IDENTITY_MISSING"
    )
    assert "condition-a" in result["activation_diagnostic"]
    assert "condition-b" in result["activation_diagnostic"]
    assert catalog.current_generation() == {}


def test_activation_gate_blocks_cross_event_observation_join(tmp_path: Path) -> None:
    """Two legal relations whose contracts share one settlement observation
    key are merged by the solver into one component; a second event_identity
    basis inside that component blocks only the new identity."""
    catalog = RelationCatalog(tmp_path)
    first = compiled_relation_discovery(
        ["condition-a", "condition-b"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
    )
    for market in first["markets"]:
        market["event_identity_basis"] = "E1"
    second = compiled_relation_discovery(
        ["condition-c", "condition-d"],
        {"condition-c": "BUY_YES", "condition-d": "BUY_YES"},
    )
    for market in second["markets"]:
        market["event_identity_basis"] = "E2"

    first_id = catalog.ingest(first)["version_id"]
    assert catalog.approve(first_id, {"version_id": first_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    before = dict(catalog.current_generation())
    assert len(before) == 1

    second_id = catalog.ingest(second)["version_id"]
    blocked = catalog.approve(second_id, {"version_id": second_id}, actor="op", git_sha="sha")
    assert blocked["activation"] == "ACTIVATION_BLOCKED_CROSS_EVENT"
    assert "E1" in blocked["activation_diagnostic"]
    assert "E2" in blocked["activation_diagnostic"]
    assert catalog.current_generation() == before  # store rolled back
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[second_id]["activation"] == "ACTIVATION_BLOCKED_CROSS_EVENT"
    assert rows[second_id]["status"] == "APPROVED"


def test_activation_gate_blocks_every_new_relation_in_the_violating_component(
    tmp_path: Path,
) -> None:
    """Cross-batch joint blocking: two later relations that land in the same
    violating component are each blocked, while a clean component's relation
    activates normally."""
    catalog = RelationCatalog(tmp_path)
    active = compiled_relation_discovery(
        ["condition-a", "condition-b"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
    )
    for market in active["markets"]:
        market["event_identity_basis"] = "E1"
    active_id = catalog.ingest(active)["version_id"]
    assert catalog.approve(active_id, {"version_id": active_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"

    r1 = compiled_relation_discovery(
        ["condition-x", "condition-y"],
        {"condition-x": "BUY_YES", "condition-y": "BUY_YES"},
    )
    for market in r1["markets"]:
        market["event_identity_basis"] = "E2"
    r1_id = catalog.ingest(r1)["version_id"]
    first_blocked = catalog.approve(r1_id, {"version_id": r1_id}, actor="op", git_sha="sha")
    assert first_blocked["activation"] == "ACTIVATION_BLOCKED_CROSS_EVENT"

    r2 = compiled_relation_discovery(
        ["condition-y", "condition-z"],
        {"condition-y": "BUY_YES", "condition-z": "BUY_YES"},
    )
    for market in r2["markets"]:
        market["event_identity_basis"] = "E2"
    r2_id = catalog.ingest(r2)["version_id"]
    second_blocked = catalog.approve(r2_id, {"version_id": r2_id}, actor="op", git_sha="sha")
    assert second_blocked["activation"] == "ACTIVATION_BLOCKED_CROSS_EVENT"

    clean = compiled_relation_discovery(
        ["condition-m", "condition-n"],
        {"condition-m": "BUY_YES", "condition-n": "BUY_YES"},
        rule="rules-clean",
    )
    for market in clean["markets"]:
        market["event_identity_basis"] = "E3"
    clean_id = catalog.ingest(clean)["version_id"]
    result = catalog.approve(clean_id, {"version_id": clean_id}, actor="op", git_sha="sha")
    assert result["activation"] == "ACTIVE"
    generation = catalog.current_generation()
    assert len(generation) == 2  # only the first active and the clean relation
    active_row = next(row for row in catalog.review_rows() if row["version_id"] == active_id)
    clean_row = next(row for row in catalog.review_rows() if row["version_id"] == clean_id)
    assert set(generation) == {active_row["identity"], clean_row["identity"]}
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    for blocked_id in (r1_id, r2_id):
        assert rows[blocked_id]["activation"] == "ACTIVATION_BLOCKED_CROSS_EVENT"
        assert rows[blocked_id]["status"] == "APPROVED"
        assert rows[blocked_id]["identity"] not in generation


def test_activation_gate_allows_same_observation_relations_with_one_basis(
    tmp_path: Path,
) -> None:
    """Legal pass: two relations observing the same event (identical settlement
    observation key) merge into one component; a single shared event_identity
    basis keeps the component legal, so both activate."""
    catalog = RelationCatalog(tmp_path)
    first = compiled_relation_discovery(
        ["condition-a", "condition-b"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
    )
    for market in first["markets"]:
        market["event_identity_basis"] = "E1"
    second = compiled_relation_discovery(
        ["condition-c", "condition-d"],
        {"condition-c": "BUY_YES", "condition-d": "BUY_YES"},
    )
    for market in second["markets"]:
        market["event_identity_basis"] = "E1"

    first_id = catalog.ingest(first)["version_id"]
    assert catalog.approve(first_id, {"version_id": first_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    second_id = catalog.ingest(second)["version_id"]
    assert catalog.approve(second_id, {"version_id": second_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    generation = catalog.current_generation()
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert set(generation) == {rows[first_id]["identity"], rows[second_id]["identity"]}
    assert rows[first_id]["activation"] == "ACTIVE"
    assert rows[second_id]["activation"] == "ACTIVE"


def test_activation_gate_allows_negrisk_component_with_distinct_observation_keys(
    tmp_path: Path,
) -> None:
    """Legal pass: a NegRisk-shaped component of three contracts, each with its
    own settlement observation key, joined by the explicit EXACTLY_ONE relation
    and sharing one event_identity basis, activates."""
    catalog = RelationCatalog(tmp_path)
    negrisk = compiled_relation_discovery(
        ["condition-n1", "condition-n2", "condition-n3"],
        {
            "condition-n1": "BUY_YES",
            "condition-n2": "BUY_YES",
            "condition-n3": "BUY_YES",
        },
        rule={
            "condition-n1": "negrisk-observer-a",
            "condition-n2": "negrisk-observer-b",
            "condition-n3": "negrisk-observer-c",
        },
    )
    for market in negrisk["markets"]:
        market["event_identity_basis"] = "E1"
    version_id = catalog.ingest(negrisk)["version_id"]
    result = catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
    assert result["activation"] == "ACTIVE"
    generation = catalog.current_generation()
    assert len(generation) == 1
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[version_id]["identity"] in generation


def test_activation_gate_blocks_component_with_multiple_venues(
    tmp_path: Path,
) -> None:
    """The venue check runs inside the same component check: two relations
    joined by one settlement observation key but traded on different venues
    block the new identity; the already-active one stays."""
    catalog = RelationCatalog(tmp_path)
    first = compiled_relation_discovery(
        ["condition-a", "condition-b"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
    )
    for market in first["markets"]:
        market["event_identity_basis"] = "E1"
    second = compiled_relation_discovery(
        ["condition-c", "condition-d"],
        {"condition-c": "BUY_YES", "condition-d": "BUY_YES"},
    )
    for market in second["markets"]:
        market["event_identity_basis"] = "E1"
        market["venue"] = "Predict.fun"

    first_id = catalog.ingest(first)["version_id"]
    assert catalog.approve(first_id, {"version_id": first_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    before = dict(catalog.current_generation())
    assert len(before) == 1

    second_id = catalog.ingest(second)["version_id"]
    blocked = catalog.approve(second_id, {"version_id": second_id}, actor="op", git_sha="sha")
    assert blocked["activation"] == "ACTIVATION_BLOCKED_CROSS_EVENT"
    # venues are stored casefolded by the v2 conversion (_converted)
    assert "polymarket" in blocked["activation_diagnostic"]
    assert "predict.fun" in blocked["activation_diagnostic"]
    assert catalog.current_generation() == before  # rollback: only the first stays
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[second_id]["activation"] == "ACTIVATION_BLOCKED_CROSS_EVENT"
    assert rows[second_id]["status"] == "APPROVED"


def test_activation_gate_keeps_similar_title_relations_in_separate_components(
    tmp_path: Path,
) -> None:
    """Similar titles never merge components: relations that share no
    observation key and no event_identity basis each activate independently."""
    catalog = RelationCatalog(tmp_path)
    title = "Will Bitcoin trade above $100,000 before December 31, 2026?"
    first = compiled_relation_discovery(
        ["condition-a", "condition-b"],
        {"condition-a": "BUY_YES", "condition-b": "BUY_YES"},
    )
    for market in first["markets"]:
        market["event_identity_basis"] = "E1"
        market["title"] = title
    second = compiled_relation_discovery(
        ["condition-c", "condition-d"],
        {"condition-c": "BUY_YES", "condition-d": "BUY_YES"},
        rule="rules-other-event",
    )
    for market in second["markets"]:
        market["event_identity_basis"] = "E2"
        market["title"] = title

    first_id = catalog.ingest(first)["version_id"]
    assert catalog.approve(first_id, {"version_id": first_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    second_id = catalog.ingest(second)["version_id"]
    assert catalog.approve(second_id, {"version_id": second_id}, actor="op", git_sha="sha")["activation"] == "ACTIVE"
    generation = catalog.current_generation()
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert set(generation) == {rows[first_id]["identity"], rows[second_id]["identity"]}
    assert rows[first_id]["activation"] == "ACTIVE"
    assert rows[second_id]["activation"] == "ACTIVE"


# -- issue #98 S3: approve_many batch confirmation -------------------------


def _issue98_fixture(catalog: RelationCatalog) -> dict[str, object]:
    """Ingest the six T3.1 relations and approve case 3's old version first.

    Each case gets its own rule (distinct observation key) so the compile
    seam never merges relations that do not share contracts, keeping the
    single-approve path's per-component judgments identical to the batch
    path's affected-component judgments. Case 6 ingests v1 then v2 of one
    identity, so v2 is the latest version and only v2 can pass the chain.
    """
    normal = compiled_relation_discovery(
        ["s3-a", "s3-b"],
        {"s3-a": "BUY_YES", "s3-b": "BUY_YES"},
    )
    for market in normal["markets"]:
        market["event_identity_basis"] = "E1"
    normal_id = catalog.ingest(normal)["version_id"]

    incomplete_id = catalog.ingest(
        _unique_discovery("i2", completeness="INCOMPLETE")
    )["version_id"]

    old = compiled_relation_discovery(
        ["s3-c", "s3-d"],
        {"s3-c": "BUY_YES", "s3-d": "BUY_YES"},
        rule="rules-s3c",
    )
    for market in old["markets"]:
        market["event_identity_basis"] = "E3"
    old_id = catalog.ingest(old)["version_id"]
    assert catalog.approve(
        old_id, {"version_id": old_id}, actor="op", git_sha="sha"
    )["activation"] == "ACTIVE"
    drifted = dict(old)
    drifted["markets"] = [dict(market) for market in old["markets"]]
    drifted["markets"][0]["title"] = "Market 0 (edited)"
    drifted_id = catalog.ingest(drifted)["version_id"]

    oversized = compiled_relation_discovery(
        [f"s3-e{i}" for i in range(8)],
        {f"s3-e{i}": "BUY_YES" for i in range(8)},
        rule="rules-s3e",
    )
    for market in oversized["markets"]:
        market["event_identity_basis"] = "E4"
    oversized_id = catalog.ingest(oversized)["version_id"]

    cross = compiled_relation_discovery(
        ["s3-f", "s3-g"],
        {"s3-f": "BUY_YES", "s3-g": "BUY_YES"},
        rule="rules-s3f",
    )
    cross["markets"][0]["event_identity_basis"] = "E5a"
    cross["markets"][1]["event_identity_basis"] = "E5b"
    cross_id = catalog.ingest(cross)["version_id"]

    base = compiled_relation_discovery(
        ["s3-h", "s3-i"],
        {"s3-h": "BUY_YES", "s3-i": "BUY_YES"},
        rule="rules-s3h",
    )
    for market in base["markets"]:
        market["event_identity_basis"] = "E6"
    v1_id = catalog.ingest(base)["version_id"]
    newer = dict(base)
    newer["markets"] = [dict(market) for market in base["markets"]]
    newer["markets"][0]["title"] = "Market 0 (edited)"
    v2_id = catalog.ingest(newer)["version_id"]

    versions = catalog._versions()
    return {
        "normal": (normal_id, str(versions[normal_id]["identity"])),
        "incomplete": (incomplete_id, str(versions[incomplete_id]["identity"])),
        "old": (old_id, str(versions[old_id]["identity"])),
        "drifted": (drifted_id, str(versions[drifted_id]["identity"])),
        "oversized": (oversized_id, str(versions[oversized_id]["identity"])),
        "cross": (cross_id, str(versions[cross_id]["identity"])),
        "v1": (v1_id, str(versions[v1_id]["identity"])),
        "v2": (v2_id, str(versions[v2_id]["identity"])),
        "batch": [
            normal_id,
            incomplete_id,
            drifted_id,
            oversized_id,
            cross_id,
            v1_id,
            v2_id,
        ],
    }


def test_approve_many_matches_single_approve_chain(tmp_path: Path) -> None:
    """T3.1: approve_many over a mixed batch is per-item identical to the
    single-approve worked example (returns, conflict error messages) and
    leaves the full store state (versions, generation, approved, latest)
    identical."""
    catalog_a = RelationCatalog(tmp_path / "a")
    catalog_b = RelationCatalog(tmp_path / "b")
    fx_a = _issue98_fixture(catalog_a)
    fx_b = _issue98_fixture(catalog_b)

    # Path A: one single approve per batch item; conflicts recorded as
    # exceptions (with the identity the batch error entry also carries).
    path_a: list[dict[str, object]] = []
    for version_id in fx_a["batch"]:
        try:
            result = catalog_a.approve(
                version_id, {"version_id": version_id}, actor="op", git_sha="sha"
            )
        except ValueError as exc:
            identity = fx_a["v1"][1] if version_id == fx_a["v1"][0] else None
            path_a.append({
                "version_id": version_id,
                **({"identity": identity} if identity is not None else {}),
                "error": str(exc),
            })
        else:
            path_a.append(result)

    outcome = catalog_b.approve_many(
        [{"version_id": version_id} for version_id in fx_b["batch"]],
        actor="op",
        git_sha="sha",
    )
    assert outcome["counts"] == {"total": 7, "active": 2, "blocked": 4, "error": 1}
    assert outcome["results"] == path_a

    # Spot-check the two hard semantic cases: the stale same-identity version
    # errors with the single-approve message and the latest one activates.
    assert outcome["results"][5] == {
        "version_id": fx_b["v1"][0],
        "identity": fx_b["v1"][1],
        "error": "relation version changed; refresh before deciding",
    }
    assert outcome["results"][6] == {
        "version_id": fx_b["v2"][0],
        "identity": fx_b["v2"][1],
        "status": "APPROVED",
        "activation": "ACTIVE",
    }

    def snapshot(catalog: RelationCatalog) -> dict[str, object]:
        versions = {
            version_id: {
                key: value
                for key, value in record.items()
                if key not in {"created_at", "updated_at"}
            }
            for version_id, record in catalog._store["versions"].items()
        }
        return {
            "versions": versions,
            "generation": catalog.current_generation(),
            "approved": dict(catalog._store["approved"]),
            "latest": dict(catalog._store["latest"]),
        }

    assert snapshot(catalog_a) == snapshot(catalog_b)


def test_approve_many_conflicts_do_not_interrupt_batch(tmp_path: Path) -> None:
    """T3.2: not-pending and missing-version entries become per-entry error
    results, the remaining entries still take effect, and counts are exact."""
    catalog = RelationCatalog(tmp_path)
    active = compiled_relation_discovery(
        ["t32-a", "t32-b"],
        {"t32-a": "BUY_YES", "t32-b": "BUY_YES"},
    )
    for market in active["markets"]:
        market["event_identity_basis"] = "E1"
    active_id = catalog.ingest(active)["version_id"]

    stale = compiled_relation_discovery(
        ["t32-c", "t32-d"],
        {"t32-c": "BUY_YES", "t32-d": "BUY_YES"},
        rule="rules-t32",
    )
    for market in stale["markets"]:
        market["event_identity_basis"] = "E2"
    stale_id = catalog.ingest(stale)["version_id"]
    assert catalog.approve(
        stale_id, {"version_id": stale_id}, actor="op", git_sha="sha"
    )["activation"] == "ACTIVE"

    incomplete_id = catalog.ingest(
        _unique_discovery("t32i", completeness="INCOMPLETE")
    )["version_id"]
    missing_id = "v-" + "0" * 64

    outcome = catalog.approve_many(
        [
            {"version_id": active_id},
            {"version_id": stale_id},
            {"version_id": missing_id},
            {"version_id": incomplete_id},
        ],
        actor="op",
        git_sha="sha",
    )
    assert outcome["counts"] == {"total": 4, "active": 1, "blocked": 1, "error": 2}

    by_id = {result["version_id"]: result for result in outcome["results"]}
    assert by_id[active_id]["activation"] == "ACTIVE"
    assert by_id[active_id]["status"] == "APPROVED"
    assert by_id[stale_id] == {
        "version_id": stale_id,
        "identity": str(catalog._versions()[stale_id]["identity"]),
        "error": "relation version is no longer pending",
    }
    assert by_id[missing_id] == {
        "version_id": missing_id,
        "error": "relation version not found",
    }
    assert by_id[incomplete_id]["activation"] == "INCOMPLETE"

    generation = catalog.current_generation()
    assert set(generation) == {
        str(catalog._versions()[active_id]["identity"]),
        str(catalog._versions()[stale_id]["identity"]),
    }
    rows = {row["version_id"]: row for row in catalog.review_rows()}
    assert rows[active_id]["activation"] == "ACTIVE"
    assert rows[active_id]["status"] == "APPROVED"
    assert rows[incomplete_id]["activation"] == "INCOMPLETE"
    assert rows[stale_id]["activation"] == "ACTIVE"  # untouched by the batch


def test_r14_approve_many_batch_internal_visibility_matches_sequential_approve(tmp_path: Path) -> None:
    """R1.4 (review round 1): the R1.1-R1.3 shapes through the facade —
    the unsat triple, the globally stale pair and the valuation-unit conflict
    — judged by one ``approve_many`` batch are per-item identical to
    sequential single ``approve`` calls, and the final store state is
    identical (the batch must see earlier batch members)."""

    def discovery(contracts: list[str], relation_type: str, *, rule: str, **kwargs: object) -> dict[str, object]:
        payload = compiled_relation_discovery(
            contracts,
            {contract: "BUY_YES" for contract in contracts},
            relation_type=relation_type,
            rule=rule,
            **kwargs,
        )
        for market in payload["markets"]:
            market["event_identity_basis"] = "E1"
        return payload

    # The facade identity normalizes IMPLIES endpoints by contract id, so a
    # reversed IMPLIES is not representable; the unsat triple is three
    # EXACTLY_ONE relations over a contract triangle (pairwise satisfiable,
    # jointly unsatisfiable).
    unsat_triple = [
        discovery(["r14-a", "r14-b"], "EXACTLY_ONE", rule="rules-r14"),
        discovery(["r14-b", "r14-c"], "EXACTLY_ONE", rule="rules-r14"),
        discovery(["r14-a", "r14-c"], "EXACTLY_ONE", rule="rules-r14"),
    ]
    stale_pair = [
        discovery(
            ["r14-e-a", "r14-e-b"], "EXACTLY_ONE",
            rule="rules-r14-early",
            as_of="2026-08-15T00:00:00Z",
            release="2027-12-31T17:00:00Z",
        ),
        discovery(
            ["r14-l-a", "r14-l-b"], "EXACTLY_ONE",
            rule="rules-r14-late",
            as_of="2028-06-01T00:00:00Z",
            release="2028-08-01T17:00:00Z",
        ),
    ]
    usd = discovery(["r14-u-a", "r14-u-b"], "EXACTLY_ONE", rule="rules-r14-usd")
    eur = discovery(["r14-u-c", "r14-u-d"], "EXACTLY_ONE", rule="rules-r14-eur")
    eur["model"]["problem"]["valuation_unit_id"] = "EUR"
    for action in eur["model"]["problem"]["actions"]:
        action["valuation_unit_id"] = "EUR"
    unit_conflict = [usd, eur]

    for label, shape in (
        ("unsat_triple", unsat_triple),
        ("stale_pair", stale_pair),
        ("unit_conflict", unit_conflict),
    ):
        batched = RelationCatalog(tmp_path / f"batch-{label}")
        sequential = RelationCatalog(tmp_path / f"seq-{label}")
        batch_ids = [batched.ingest(payload)["version_id"] for payload in shape]
        seq_ids = [sequential.ingest(payload)["version_id"] for payload in shape]

        outcome = batched.approve_many(
            [{"version_id": version_id} for version_id in batch_ids],
            actor="op",
            git_sha="sha",
        )
        per_item = [
            sequential.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
            for version_id in seq_ids
        ]

        assert outcome["results"] == per_item, label
        assert outcome["counts"] == {
            "total": len(shape),
            "active": sum(1 for result in per_item if result["activation"] == "ACTIVE"),
            "blocked": sum(
                1 for result in per_item
                if result.get("activation") not in (None, "ACTIVE")
            ),
            "error": 0,
        }, label
        assert per_item[-1]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT", label

        def snapshot(catalog: RelationCatalog) -> dict[str, object]:
            versions = {
                version_id: {
                    key: value
                    for key, value in record.items()
                    if key not in {"created_at", "updated_at"}
                }
                for version_id, record in catalog._store["versions"].items()
            }
            return {
                "versions": versions,
                "generation": catalog.current_generation(),
                "approved": dict(catalog._store["approved"]),
            }

        assert snapshot(batched) == snapshot(sequential), label


def test_r15_duplicate_version_in_batch_is_entry_conflict(tmp_path: Path) -> None:
    """R1.5 (review round 1): the same version_id twice in one approve_many
    batch takes effect once; the second occurrence is a per-entry conflict
    with the single-approve message (the pre-fix batch crashed with an
    uncaught KeyError('reason')), counts are exact, and the sequential
    control (first approve ACTIVE, second raises RelationConflictError) is
    equivalent."""
    payload = compiled_relation_discovery(
        ["r15-a", "r15-b"],
        {"r15-a": "BUY_YES", "r15-b": "BUY_YES"},
        rule="rules-r15",
    )
    for market in payload["markets"]:
        market["event_identity_basis"] = "E1"
    batched = RelationCatalog(tmp_path / "batch")
    sequential = RelationCatalog(tmp_path / "control")
    batch_id = batched.ingest(payload)["version_id"]
    control_id = sequential.ingest(payload)["version_id"]

    outcome = batched.approve_many(
        [{"version_id": batch_id}, {"version_id": batch_id}],
        actor="op",
        git_sha="sha",
    )
    assert outcome["counts"] == {"total": 2, "active": 1, "blocked": 0, "error": 1}
    assert outcome["results"][0]["activation"] == "ACTIVE"
    assert outcome["results"][0]["status"] == "APPROVED"
    assert outcome["results"][1] == {
        "version_id": batch_id,
        "identity": str(batched._versions()[batch_id]["identity"]),
        "error": "relation version is no longer pending",
    }

    first = sequential.approve(
        control_id, {"version_id": control_id}, actor="op", git_sha="sha"
    )
    assert first["activation"] == "ACTIVE"
    with pytest.raises(
        RelationConflictError, match="relation version is no longer pending"
    ):
        sequential.approve(control_id, {"version_id": control_id}, actor="op", git_sha="sha")

    assert batched.current_generation() == sequential.current_generation()
