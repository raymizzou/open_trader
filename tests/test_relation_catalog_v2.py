"""Issue #78 step 1: v2 relation catalog invariant regression matrix (RED).

The minimal public API is ``RelationCatalogV2`` in
``src/open_trader/relation_catalog_v2.py``, which intentionally does not exist
yet. The module-level import below therefore fails; that import error is the
expected RED for this TDD step, and the test functions encode the nine
confirmed Issue #78 decisions as the implementation acceptance checklist.

Agreed minimal API (signatures follow the parent brief; the only addition is an
optional persistence seam so tests can tamper with or seed persisted state):

- ``RelationCatalogV2(store: MutableMapping | None = None)``
- ``ingest(payload) -> dict`` (returns identity, version_id, status, occurrence_count)
- ``approve(version_id, *, actor, git_sha) -> dict``
- ``reject(version_id, *, reason, actor, git_sha, note="") -> dict``
- ``revoke(version_id, *, actor, git_sha) -> dict``
- ``replace(change_set, *, actor, git_sha) -> dict``
  (atomic whole-generation swap; returns status/versions/blocked)
- ``current_generation() -> dict`` (keyed by identity -> {"version_id", "status"})
- ``admit(producer_facts) -> bool``
- ``authoritative_reconcile(producer, scope, complete_facts) -> dict``

Store layout assumed by the tamper/migration tests:
``store["versions"][version_id]["payload"]`` holds the persisted payload.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from open_trader.relation_catalog_v2 import RelationCatalogV2  # noqa: F401 - RED: module does not exist yet

# #49 full-matrix oracle budget reused by decision 6 (per-component size ceiling).
GROUP_BUDGET = 7  # ponytail: matches #49 scale_16 oracle limits; confirm exact ceiling at implementation


def _endpoint(
    venue: str = "polymarket",
    contract_id: str = "cX",
    **overrides: object,
) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "venue": venue,
        "contract_id": contract_id,
        "title": f"Will {contract_id} resolve?",
        "market_date": "2026-08-15",
        "expires_at": "2026-08-31T00:00:00Z",
        "settlement_observation_key": "obs-1",
        "settlement_rules": "resolves YES if the official source reports it",
        "cancellation_rules": "void cancels and refunds",
    }
    endpoint.update(overrides)
    return endpoint


def _payload(
    relation_type: str = "IMPLIES",
    endpoints: list[dict[str, object]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "relation_type": relation_type,
        "endpoints": endpoints
        if endpoints is not None
        else [
            _endpoint(venue="polymarket", contract_id="cY"),
            _endpoint(venue="predict.fun", contract_id="cX"),
        ],
        "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
        "payouts": {"cX": {"NORMAL_YES": 100, "NORMAL_NO": 0}},
        "capital_release": "2026-08-31T00:00:00Z",
        "discovery_source": "exchange_metadata",
        "discovered_at": "2026-08-15T00:00:00Z",
        "group_item_threshold": "0",
        "rules_hash": "a" * 64,
        "event_id": "event-1",
    }
    payload.update(overrides)
    return payload


def _catalog(store: dict[str, object] | None = None) -> RelationCatalogV2:
    return RelationCatalogV2(store=store if store is not None else {})


def _ingest(catalog: RelationCatalogV2, **overrides: object) -> dict[str, object]:
    return catalog.ingest(_payload(**overrides))


def _approve(catalog: RelationCatalogV2, payload: dict[str, object]) -> dict[str, object]:
    result = catalog.ingest(payload)
    catalog.approve(result["version_id"], actor="auditor", git_sha="a" * 40)
    return result


@pytest.fixture
def store() -> dict[str, object]:
    return {}


# Decision 1: identity = relation_type + venue-qualified canonical endpoint signature.

def test_identity_excludes_group_item_threshold_rules_hash_and_event_id(store) -> None:
    catalog = _catalog(store)
    common = {"group_item_threshold": "0", "rules_hash": "b" * 64, "event_id": "event-2"}
    identities = {
        _ingest(catalog, endpoints=[_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")], **common)["identity"],
        _ingest(catalog, endpoints=[_endpoint("polymarket", "cB"), _endpoint("predict.fun", "cC")], **common)["identity"],
        _ingest(catalog, endpoints=[_endpoint("polymarket", "cC"), _endpoint("predict.fun", "cD")], **common)["identity"],
    }
    assert len(identities) == 3


def test_identity_ignores_group_item_threshold_differences(store) -> None:
    catalog = _catalog(store)
    endpoints = [_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]
    assert _ingest(catalog, endpoints=endpoints, group_item_threshold="0")["identity"] == _ingest(
        catalog, endpoints=endpoints, group_item_threshold="1"
    )["identity"]


def test_identity_ignores_rules_hash_differences(store) -> None:
    catalog = _catalog(store)
    endpoints = [_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]
    assert _ingest(catalog, endpoints=endpoints, rules_hash="a" * 64)["identity"] == _ingest(
        catalog, endpoints=endpoints, rules_hash="b" * 64
    )["identity"]


def test_identity_ignores_event_id_differences(store) -> None:
    catalog = _catalog(store)
    endpoints = [_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]
    assert _ingest(catalog, endpoints=endpoints, event_id="event-1")["identity"] == _ingest(
        catalog, endpoints=endpoints, event_id="event-2"
    )["identity"]


def test_identity_orders_implies_endpoints(store) -> None:
    catalog = _catalog(store)
    forward = _ingest(catalog, endpoints=[_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")])
    backward = _ingest(catalog, endpoints=[_endpoint("predict.fun", "cB"), _endpoint("polymarket", "cA")])
    assert forward["identity"] != backward["identity"]


def test_identity_sorts_symmetric_relation_endpoints(store) -> None:
    catalog = _catalog(store)
    endpoints = [_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]
    ab = _ingest(catalog, relation_type="MUTUALLY_EXCLUSIVE", endpoints=endpoints)
    ba = _ingest(catalog, relation_type="MUTUALLY_EXCLUSIVE", endpoints=list(reversed(endpoints)))
    assert ab["identity"] == ba["identity"]


def test_identity_distinguishes_relation_kind(store) -> None:
    catalog = _catalog(store)
    endpoints = [_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]
    exactly_one = _ingest(catalog, relation_type="EXACTLY_ONE", endpoints=endpoints)
    mutually_exclusive = _ingest(catalog, relation_type="MUTUALLY_EXCLUSIVE", endpoints=endpoints)
    assert exactly_one["identity"] != mutually_exclusive["identity"]


def test_identity_qualifies_endpoint_venue(store) -> None:
    catalog = _catalog(store)
    poly = _ingest(catalog, endpoints=[_endpoint("polymarket", "cX"), _endpoint("predict.fun", "cY")])
    pred = _ingest(catalog, endpoints=[_endpoint("predict.fun", "cX"), _endpoint("polymarket", "cY")])
    assert poly["identity"] != pred["identity"]


def test_identity_fails_closed_on_unknown_venue(store) -> None:
    catalog = _catalog(store)
    with pytest.raises(ValueError):
        _ingest(catalog, endpoints=[_endpoint("manifold", "cX"), _endpoint("predict.fun", "cY")])


@pytest.mark.parametrize("relation_type", ["IMPLIES", "MUTUALLY_EXCLUSIVE", "EXACTLY_ONE"])
def test_identity_rejects_undersized_endpoint_tuple(store, relation_type) -> None:
    catalog = _catalog(store)
    with pytest.raises(ValueError):
        _ingest(catalog, relation_type=relation_type, endpoints=[_endpoint("polymarket", "cA")])


# Decision 2: version boundaries.

def test_version_bumps_on_capital_release_change(store) -> None:
    catalog = _catalog(store)
    assert _ingest(catalog)["version_id"] != _ingest(catalog, capital_release="2026-09-07T00:00:00Z")["version_id"]


def test_version_bumps_on_discovery_source_change(store) -> None:
    catalog = _catalog(store)
    assert _ingest(catalog, discovery_source="exchange_metadata")["version_id"] != _ingest(
        catalog, discovery_source="llm"
    )["version_id"]


def test_duplicate_observation_keeps_version_and_appends_occurrence(store) -> None:
    catalog = _catalog(store)
    first = _ingest(catalog)
    second = _ingest(catalog)
    assert second["version_id"] == first["version_id"]
    assert second["occurrence_count"] == first["occurrence_count"] + 1


def test_version_ignores_discovered_at_change(store) -> None:
    catalog = _catalog(store)
    assert _ingest(catalog, discovered_at="2026-08-15T00:00:00Z")["version_id"] == _ingest(
        catalog, discovered_at="2026-08-15T12:00:00Z"
    )["version_id"]


def test_version_bumps_on_market_identity_change(store) -> None:
    catalog = _catalog(store)
    changed_rules = [
        _endpoint("polymarket", "cY", settlement_rules="resolves on confirmed print"),
        _endpoint("predict.fun", "cX"),
    ]
    assert _ingest(catalog)["version_id"] != _ingest(catalog, endpoints=changed_rules)["version_id"]


# Decision 3: approval freeze with separately frozen fingerprints.

@pytest.mark.parametrize("field", ["settlement_rules", "payouts", "capital_release"])
def test_approval_freeze_fails_closed_on_tampered_payload(store, field) -> None:
    catalog = _catalog(store)
    version_id = catalog.ingest(_payload())["version_id"]
    catalog.approve(version_id, actor="auditor", git_sha="a" * 40)
    store["versions"][version_id]["payload"][field] = "tampered"
    with pytest.raises(ValueError):
        catalog.current_generation()  # read fails closed
    with pytest.raises(ValueError):
        catalog.replace([_payload()], actor="auditor", git_sha="a" * 40)  # activation fails closed
    assert catalog.admit(_payload()) is False  # admission fails closed, old approval not reused


# Decision 4: invalidation triggers.

def test_new_candidate_stays_pending_and_keeps_lkg(store) -> None:
    catalog = _catalog(store)
    approved = _approve(catalog, _payload())
    candidate = catalog.ingest(_payload(capital_release="2026-09-07T00:00:00Z"))
    assert candidate["status"] == "PENDING"
    generation = catalog.current_generation()
    assert generation[approved["identity"]]["version_id"] == approved["version_id"]
    assert generation[approved["identity"]]["status"] == "ACTIVE"


def test_authoritative_reconcile_marks_missing_relation_unknown(store) -> None:
    catalog = _catalog(store)
    approved = _approve(catalog, _payload())
    catalog.authoritative_reconcile("polymarket", "events/event-1", complete_facts=[])
    assert catalog.current_generation()[approved["identity"]]["status"] == "UNKNOWN"


def test_explicit_revoke_deactivates_relation(store) -> None:
    catalog = _catalog(store)
    approved = _approve(catalog, _payload())
    catalog.revoke(approved["version_id"], actor="auditor", git_sha="a" * 40)
    assert catalog.current_generation()[approved["identity"]]["status"] == "UNKNOWN"


def test_replacement_switches_generation_atomically(store) -> None:
    catalog = _catalog(store)
    old = _approve(catalog, _payload())
    new_payload = _payload(capital_release="2026-09-07T00:00:00Z")
    new_version_id = catalog.ingest(new_payload)["version_id"]
    result = catalog.replace([new_payload], actor="auditor", git_sha="a" * 40)
    assert result["status"] == "ACTIVE"
    generation = catalog.current_generation()
    assert generation[old["identity"]]["version_id"] == new_version_id
    assert generation[old["identity"]]["status"] == "ACTIVE"


# Decision 5: single cause ledger; component UNKNOWN iff an unresolved cause exists.

def test_relation_group_unknown_propagates_to_whole_group(store) -> None:
    catalog = _catalog(store)
    ab = _approve(
        catalog,
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cA"), _endpoint("polymarket", "cB")]),
    )
    bc = _approve(
        catalog,
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cB"), _endpoint("polymarket", "cC")]),
    )
    catalog.revoke(ab["version_id"], actor="auditor", git_sha="a" * 40)
    generation = catalog.current_generation()
    assert generation[ab["identity"]]["status"] == "UNKNOWN"
    assert generation[bc["identity"]]["status"] == "UNKNOWN"


def test_partial_cause_recovery_keeps_unknown_until_all_causes_resolved(store) -> None:
    catalog = _catalog(store)
    ab_payload = _payload(
        relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cA"), _endpoint("polymarket", "cB")]
    )
    bc_payload = _payload(
        relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cB"), _endpoint("polymarket", "cC")]
    )
    ab = _approve(catalog, ab_payload)
    bc = _approve(catalog, bc_payload)
    catalog.authoritative_reconcile("polymarket", "poly:all", complete_facts=[bc_payload])  # cause 1: A-B missing
    catalog.authoritative_reconcile("predict.fun", "pred:all", complete_facts=[ab_payload])  # cause 2: B-C missing
    generation = catalog.current_generation()
    assert generation[ab["identity"]]["status"] == "UNKNOWN"
    assert generation[bc["identity"]]["status"] == "UNKNOWN"
    catalog.authoritative_reconcile(
        "polymarket", "poly:all", complete_facts=[ab_payload, bc_payload]
    )  # only cause 1 cleared
    assert catalog.current_generation()[ab["identity"]]["status"] == "UNKNOWN"
    catalog.authoritative_reconcile(
        "predict.fun", "pred:all", complete_facts=[ab_payload, bc_payload]
    )  # cause 2 cleared
    generation = catalog.current_generation()
    assert generation[ab["identity"]]["status"] == "ACTIVE"
    assert generation[bc["identity"]]["status"] == "ACTIVE"


def test_unrelated_activation_does_not_clear_unknown(store) -> None:
    catalog = _catalog(store)
    ab = _approve(
        catalog,
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]),
    )
    cd = _approve(
        catalog,
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cC"), _endpoint("predict.fun", "cD")]),
    )
    catalog.revoke(ab["version_id"], actor="auditor", git_sha="a" * 40)
    generation = catalog.current_generation()
    assert generation[ab["identity"]]["status"] == "UNKNOWN"
    assert generation[cd["identity"]]["status"] == "ACTIVE"


# Decision 6: per-component satisfiability and budget.

def test_unsatisfiable_component_blocked_from_activation(store) -> None:
    catalog = _catalog(store)
    a_implies_b = _payload(
        relation_type="IMPLIES", endpoints=[_endpoint("polymarket", "cA"), _endpoint("polymarket", "cB")]
    )
    b_implies_a = _payload(
        relation_type="IMPLIES", endpoints=[_endpoint("polymarket", "cB"), _endpoint("polymarket", "cA")]
    )
    exactly_one = _payload(
        relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cA"), _endpoint("polymarket", "cB")]
    )
    result = catalog.replace([a_implies_b, b_implies_a, exactly_one], actor="auditor", git_sha="a" * 40)
    assert result["status"] == "ACTIVATION_BLOCKED_INCONSISTENT"


def test_group_over_budget_blocked_other_groups_unaffected(store) -> None:
    catalog = _catalog(store)
    oversized = [
        _payload(
            relation_type="IMPLIES",
            endpoints=[_endpoint("polymarket", f"c{i}"), _endpoint("polymarket", f"c{i + 1}")],
        )
        for i in range(GROUP_BUDGET)
    ]
    small = _payload(
        relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cZ"), _endpoint("predict.fun", "cW")]
    )
    big_identity = catalog.ingest(oversized[0])["identity"]
    small_identity = catalog.ingest(small)["identity"]
    result = catalog.replace(oversized + [small], actor="auditor", git_sha="a" * 40)
    blocked = {entry["identity"]: entry["reason"] for entry in result["blocked"]}
    assert blocked[big_identity] == "UNSUPPORTED_SIZE"
    assert small_identity not in blocked
    assert catalog.current_generation()[small_identity]["status"] == "ACTIVE"


# Decision 7: monitor admission is the only seam.

def test_admission_rejects_incomplete_facts(store) -> None:
    catalog = _catalog(store)
    _approve(catalog, _payload())
    assert catalog.admit(_payload(terminal_states=["NORMAL_YES"])) is False


def test_admission_rejects_unapproved_relation(store) -> None:
    catalog = _catalog(store)
    catalog.ingest(_payload())
    assert catalog.admit(_payload()) is False


def test_admission_rejects_facts_inconsistent_with_canonical(store) -> None:
    catalog = _catalog(store)
    _approve(catalog, _payload())
    assert catalog.admit(_payload(settlement_rules="different canonical facts")) is False


def test_admission_rejects_unknown_component(store) -> None:
    catalog = _catalog(store)
    approved = _approve(catalog, _payload())
    catalog.revoke(approved["version_id"], actor="auditor", git_sha="a" * 40)
    assert catalog.admit(_payload()) is False


def test_admission_accepts_fully_satisfied_relation(store) -> None:
    catalog = _catalog(store)
    _approve(catalog, _payload())
    assert catalog.admit(_payload()) is True


# Decision 8: v1 tables stay read-only audit; never promoted to v2 truth.

def test_v1_legacy_state_not_promoted_to_v2_current_generation(store) -> None:
    store["v1"] = {
        "approvals": [{"version_id": "legacy-1", "identity": "legacy:1"}],
        "active": ["legacy:1"],
        "unknown": ["legacy:2"],
    }
    catalog = _catalog(store)
    assert catalog.current_generation() == {}
    assert not hasattr(catalog, "legacy_repair")


# Decision 9: single-write mutation and single-read atomic snapshot.

def test_current_generation_observes_only_complete_generations_under_concurrency(store) -> None:
    catalog = _catalog(store)
    g1_payloads = [
        _payload(
            relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]
        ),
        _payload(
            relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cB"), _endpoint("predict.fun", "cC")]
        ),
    ]
    g2_payloads = [
        _payload(
            relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cD"), _endpoint("predict.fun", "cE")]
        ),
        _payload(
            relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cE"), _endpoint("predict.fun", "cF")]
        ),
    ]
    g1_ids = {_approve(catalog, payload)["identity"] for payload in g1_payloads}
    g2_ids = {_approve(catalog, payload)["identity"] for payload in g2_payloads}
    assert g1_ids.isdisjoint(g2_ids)

    snapshots: list[frozenset[str]] = []
    errors: list[BaseException] = []

    def writer() -> None:
        for _ in range(30):
            catalog.replace(g1_payloads, actor="auditor", git_sha="a" * 40)
            catalog.replace(g2_payloads, actor="auditor", git_sha="a" * 40)

    def reader() -> None:
        for _ in range(100):
            try:
                snapshots.append(frozenset(catalog.current_generation()))
            except BaseException as exc:  # pragma: no cover - atomicity must not tear
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in (pool.submit(reader), pool.submit(reader), pool.submit(writer)):
            future.result()

    assert not errors
    assert snapshots
    assert all(snapshot in (g1_ids, g2_ids, g1_ids | g2_ids) for snapshot in snapshots)
