from __future__ import annotations

from pathlib import Path

from open_trader.relation_catalog import RelationCatalog


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


def test_catalog_ingests_exact_duplicate_as_evidence_not_a_second_pending_version(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)

    first = catalog.ingest(discovery(), git_sha="abc123")
    second = catalog.ingest({**discovery(), "discovered_at": "2026-08-15T02:33:00Z"}, git_sha="abc123")

    assert first["created"] is True
    assert second == {"created": False, "suppressed": False, "relation_version_id": first["relation_version_id"]}
    queue = catalog.list("pending")
    assert len(queue) == 1
    assert queue[0]["occurrences"] == 2
    assert queue[0]["markets"][0]["title"] == discovery()["markets"][0]["title"]


def test_complete_approval_publishes_atomic_generation_and_stale_decision_conflicts(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["relation_version_id"]
    detail = catalog.detail(version_id)
    expected = {key: detail[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}

    approved = catalog.approve(version_id, expected, actor="operator", git_sha="abc123")

    assert approved == {"relation_version_id": version_id, "approval_status": "APPROVED", "activation_status": "ACTIVE", "generation": 1}
    assert catalog.current_generation()["relations"][0]["relation_version_id"] == version_id
    try:
        catalog.reject(version_id, expected, reason="other", actor="operator", git_sha="abc123")
    except ValueError as error:
        assert "no longer pending" in str(error)
    else:
        raise AssertionError("a stale drawer must not overwrite approval")


def test_incomplete_approval_never_publishes_a_generation_and_rejected_exact_fingerprint_stays_suppressed(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    incomplete_id = catalog.ingest(discovery(complete=False))["relation_version_id"]
    detail = catalog.detail(incomplete_id)
    expected = {key: detail[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}

    assert catalog.approve(incomplete_id, expected, actor="operator", git_sha="abc") ["activation_status"] == "INCOMPLETE"
    assert catalog.current_generation() == {"generation": 0, "unknown_components": [], "relations": []}
    pending_id = catalog.ingest(discovery(title="Different source fact"))["relation_version_id"]
    pending = catalog.detail(pending_id)
    pending_expected = {key: pending[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}
    catalog.reject(pending_id, pending_expected, reason="source_evidence_insufficient", actor="operator", git_sha="abc")
    assert catalog.ingest(discovery(title="Different source fact"))["suppressed"] is True


def test_revoke_removes_only_the_active_relation_and_marks_its_component_unknown(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["relation_version_id"]
    detail = catalog.detail(version_id)
    expected = {key: detail[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}
    catalog.approve(version_id, expected, actor="operator", git_sha="abc")

    revoked = catalog.revoke(version_id, expected, reason="rules_changed", actor="operator", git_sha="abc")

    assert revoked["generation"] == 2
    assert catalog.current_generation() == {
        "generation": 2,
        "unknown_components": [["condition-a", "condition-b"]],
        "relations": [],
    }


def test_replacement_change_set_keeps_old_generation_when_candidate_cannot_activate(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    active_id = catalog.ingest(discovery())["relation_version_id"]
    active = catalog.detail(active_id)
    active_expected = {key: active[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}
    catalog.approve(active_id, active_expected, actor="operator", git_sha="abc")
    incomplete_id = catalog.ingest(discovery(complete=False))["relation_version_id"]
    candidate = catalog.detail(incomplete_id)
    candidate_expected = {key: candidate[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}
    catalog.approve(incomplete_id, candidate_expected, actor="operator", git_sha="abc")

    try:
        catalog.replace(active_expected, candidate_expected, reason="rules_changed", actor="operator", git_sha="abc")
    except ValueError as error:
        assert "not activatable" in str(error)
    else:
        raise AssertionError("change set must not partially revoke")

    assert catalog.current_generation()["relations"][0]["relation_version_id"] == active_id


def test_replacement_change_set_publishes_one_new_generation_without_intermediate_revocation(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    active_id = catalog.ingest(discovery())["relation_version_id"]
    active = catalog.detail(active_id)
    active_expected = {key: active[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}
    catalog.approve(active_id, active_expected, actor="operator", git_sha="abc")
    changed = discovery()
    changed["source_evidence"] = [{"source": "revised official rules", "quote": "resolves YES if..."}]
    candidate_id = catalog.ingest(changed)["relation_version_id"]
    candidate = catalog.detail(candidate_id)
    candidate_expected = {key: candidate[key] for key in (
        "relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint",
    )}

    assert catalog.approve(candidate_id, candidate_expected, actor="operator", git_sha="abc")["activation_status"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    result = catalog.replace(active_expected, candidate_expected, reason="rules_changed", actor="operator", git_sha="abc")

    assert result["generation"] == 2
    assert catalog.current_generation()["relations"][0]["relation_version_id"] == candidate_id
    assert catalog.detail(active_id)["approval_status"] == "REVOKED"
