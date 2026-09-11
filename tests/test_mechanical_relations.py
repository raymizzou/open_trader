"""Mechanical relation codecs for native complements and NegRisk groups."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.relation_catalog import RelationCatalog
from open_trader.prediction_n_leg import (
    ActionQuantity,
    RelationKind,
    TerminalKind,
    problem_from_payload,
    validate_problem,
)
from open_trader.prediction_n_leg_oracle import (
    OracleBudget,
    enumerate_allowed_scenarios,
    evaluate_fixed_portfolio,
)
from open_trader.polymarket_relation_discovery import (
    NativeComplementMarket,
    NativeComplementRelation,
    NegriskGroupMarket,
    NegriskGroupRelation,
    discover_mechanical_relation_catalog,
)
from open_trader.prediction_relation_candidates import (
    prepare_mechanical_relation_candidates,
)


def discovery(
    *,
    relation_type: str = "NATIVE_COMPLEMENT",
    completeness: str = "INCOMPLETE",
    n: int = 2,
    contract_ids: tuple[str, ...] | None = None,
    problem: dict[str, object] | None = None,
) -> dict[str, object]:
    """A mechanical (VENUE_METADATA) v1 discovery payload over ``n`` markets."""
    contract_ids = contract_ids or tuple(f"token-{index}" for index in range(n))
    markets = []
    for index in range(n):
        markets.append({
            "venue": "Polymarket",
            "contract_id": contract_ids[index],
            "title": f"Market {index}",
            "market_date": "2026-08-15T00:00:00Z",
            "expires_at": "2026-12-31T17:00:00Z",
            "event_identity_basis": "event-1",
            "settlement_observation_key": "condition-1|Binance|2026-12-31T17:00:00Z|rules-1",
            "settlement_rules": "official index",
            "cancellation_rules": "not supplied by mechanical discovery",
        })
    model: dict[str, object] = {"completeness": completeness}
    if completeness == "COMPLETE":
        model.update({
            "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID", "REFUND", "SPLIT"],
            "payouts": {
                contract_ids[index]: {
                    "NORMAL_YES": 1, "NORMAL_NO": 0, "VOID": 0, "REFUND": 0, "SPLIT": 0,
                }
                for index in range(n)
            },
            "capital_release": "2026-12-31T17:00:00Z",
        })
        if problem is not None:
            model["problem"] = problem
    return {
        "discovery_source": "VENUE_METADATA",
        "discovered_at": "2026-08-15T02:32:00Z",
        "relation_type": relation_type,
        "semantics": {"statement": "exactly one of the mechanically bound contracts resolves YES"},
        "source_evidence": [{"event_id": "event-1", "relation_type": relation_type}],
        "model": model,
        "markets": markets,
    }


# Slice 1 (C1): the type tables accept NATIVE_COMPLEMENT and a two-endpoint
# payload ingests as PENDING with the sorted-token identity.

def test_native_complement_payload_ingests_pending_with_venue_metadata_row(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    result = catalog.ingest(
        discovery(contract_ids=("yes-1", "no-1"))
    )

    assert result["status"] == "PENDING"
    assert result["created"] is True
    assert result["identity"] == "NATIVE_COMPLEMENT|polymarket:no-1|polymarket:yes-1"
    assert catalog.current_generation() == {}

    rows = catalog.list("pending")
    assert len(rows) == 1
    assert rows[0]["identity"] == result["identity"]
    assert rows[0]["relation_type"] == "NATIVE_COMPLEMENT"
    assert rows[0]["discovery_source"] == "VENUE_METADATA"


def complement_relation() -> NativeComplementRelation:
    return NativeComplementRelation(
        event_id="event-1",
        market=NativeComplementMarket(
            event_id="event-1",
            market_id="market-1",
            condition_id="condition-1",
            question="Will the candidate win?",
            rules="official index",
            resolution_source="Binance",
            end_date="2026-12-31T17:00:00Z",
            yes_token_id="yes-1",
            no_token_id="no-1",
            rules_hash="rules-1",
        ),
    )


# Slice 2: the native codec compiles the YES/NO token pair to one native
# relation with three terminal kinds per contract and exactly three joint
# scenarios.

def test_complement_codec_allows_three_native_scenarios(
    tmp_path: Path,
) -> None:
    catalog = RelationCatalog(tmp_path)
    result = catalog.ingest_mechanical_relation(complement_relation())
    assert result["status"] == "PENDING"

    (row,) = catalog.review_rows()
    assert row["model"]["terminal_states"] == [
        "NORMAL_YES", "NORMAL_NO", "SPLIT",
    ]
    problem = problem_from_payload(row["model"]["problem"])
    assert validate_problem(problem) == ()

    by_contract = {
        state.market_contract_id: state for state in problem.terminal_state_sets
    }
    assert set(by_contract) == {"yes-1", "no-1"}
    for state in by_contract.values():
        assert {atom.kind for atom in state.atoms} == {
            TerminalKind.NORMAL_YES,
            TerminalKind.NORMAL_NO,
            TerminalKind.SPLIT,
        }

    (constraint,) = problem.constraint_model.relations
    assert constraint.kind == RelationKind.NATIVE_COMPLEMENT
    assert set(constraint.contract_ids) == {"yes-1", "no-1"}

    enumeration = enumerate_allowed_scenarios(problem, OracleBudget(1, 9, 1))
    assert enumeration.unknown_reason is None
    assert enumeration.raw_joint_state_count == 9
    assert len(enumeration.scenarios) == 3
    kind_by_atom = {
        atom.atom_id: atom.kind
        for state in problem.terminal_state_sets
        for atom in state.atoms
    }
    normal = [
        scenario for scenario in enumeration.scenarios
        if all(
            kind_by_atom[selected.atom_id]
            in {TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO}
            for selected in scenario.atoms
        )
    ]
    assert len(normal) == 2
    split_joint = [
        scenario for scenario in enumeration.scenarios
        if any(
            kind_by_atom[selected.atom_id] == TerminalKind.SPLIT
            for selected in scenario.atoms
        )
    ]
    assert len(split_joint) == 1


def test_complement_normal_scenarios_each_pay_exactly_one_lot(tmp_path: Path) -> None:
    # Token-level contract semantics: NORMAL_YES on either endpoint means
    # that endpoint's own contract settles (pays one lot) and NORMAL_NO pays
    # zero, identically for the YES token and the NO token of the pair.  Both
    # real settlement states (YES token settles / NO token settles) therefore
    # pay exactly one lot in total across one lot of each token.
    catalog = RelationCatalog(tmp_path)
    catalog.ingest_mechanical_relation(complement_relation())
    (row,) = catalog.review_rows()
    problem = problem_from_payload(row["model"]["problem"])
    assert validate_problem(problem) == ()

    enumeration = enumerate_allowed_scenarios(problem, OracleBudget(1, 9, 1))
    assert enumeration.unknown_reason is None
    atom_by_id = {
        atom.atom_id: atom
        for state in problem.terminal_state_sets
        for atom in state.atoms
    }
    normal = [
        scenario for scenario in enumeration.scenarios
        if all(
            atom_by_id[selected.atom_id].kind
            in {TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO}
            for selected in scenario.atoms
        )
    ]
    assert len(normal) == 2
    for scenario in normal:
        payout_total = sum(
            payout.payout_lower_bound_per_lot_units
            for selected in scenario.atoms
            for payout in atom_by_id[selected.atom_id].payouts
        )
        assert payout_total == 1_000_000


def test_native_complement_joint_settlement_payouts(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    catalog.ingest_mechanical_relation(complement_relation())
    (row,) = catalog.review_rows()
    problem = problem_from_payload(row["model"]["problem"])

    enumeration = enumerate_allowed_scenarios(problem, OracleBudget(1, 9, 1))
    assert enumeration.unknown_reason is None
    assert enumeration.raw_joint_state_count == 9
    assert enumeration.scenarios is not None
    assert len(enumeration.scenarios) == 3

    atom_by_id = {
        atom.atom_id: atom
        for state in problem.terminal_state_sets
        for atom in state.atoms
    }
    payouts = set()
    for scenario in enumeration.scenarios:
        by_action = {action_id: 0 for action_id in (
            "polymarket:yes-1", "polymarket:no-1"
        )}
        for selected in scenario.atoms:
            for payout in atom_by_id[selected.atom_id].payouts:
                by_action[payout.action_id] += payout.payout_lower_bound_per_lot_units
        payouts.add((by_action["polymarket:yes-1"], by_action["polymarket:no-1"]))

    assert payouts == {
        (1_000_000, 0),
        (0, 1_000_000),
        (500_000, 500_000),
    }

    full = evaluate_fixed_portfolio(
        problem,
        (
            ActionQuantity("polymarket:yes-1", 1),
            ActionQuantity("polymarket:no-1", 1),
        ),
        OracleBudget(1, 9, 1),
    )
    assert full.payout_lower_bound_units == 1_000_000
    for action_id in ("polymarket:yes-1", "polymarket:no-1"):
        single = evaluate_fixed_portfolio(
            problem,
            (ActionQuantity(action_id, 1),),
            OracleBudget(1, 9, 1),
        )
        assert single.payout_lower_bound_units == 0


def group_relation(n: int = 4) -> NegriskGroupRelation:
    return NegriskGroupRelation(
        event_id="event-group-1",
        markets=tuple(
            NegriskGroupMarket(
                event_id="event-group-1",
                market_id=f"market-{index}",
                condition_id=f"condition-{index}",
                question=f"Which outcome {index}?",
                rules="official index",
                resolution_source="Binance",
                end_date="2026-12-31T17:00:00Z",
                rules_hash=f"rules-{index}",
            )
            for index in range(n)
        ),
    )


# Slice 3 (A2): the negRisk exhaustive group codec compiles N BUY_YES contracts
# to one EXACTLY_ONE model; N=4 allows 5^4 - 2^4 + 4 = 613 scenarios with 4
# all-normal ones, and the approved candidate activates.

def test_negrisk_group_codec_allows_613_scenarios_and_activates(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    result = catalog.ingest_mechanical_relation(group_relation(4))
    assert result["status"] == "PENDING"

    (row,) = catalog.review_rows()
    assert row["relation_type"] == "EXACTLY_ONE"
    problem = problem_from_payload(row["model"]["problem"])
    assert validate_problem(problem) == ()
    assert {state.market_contract_id for state in problem.terminal_state_sets} == {
        "condition-0", "condition-1", "condition-2", "condition-3",
    }
    for state in problem.terminal_state_sets:
        assert {atom.kind for atom in state.atoms} == {
            TerminalKind.NORMAL_YES,
            TerminalKind.NORMAL_NO,
            TerminalKind.VOID,
            TerminalKind.REFUND,
            TerminalKind.SPLIT,
        }
    (constraint,) = problem.constraint_model.relations
    assert constraint.kind == RelationKind.EXACTLY_ONE
    assert constraint.contract_ids == (
        "condition-0", "condition-1", "condition-2", "condition-3",
    )

    enumeration = enumerate_allowed_scenarios(problem, OracleBudget(1, 625, 1))
    assert enumeration.unknown_reason is None
    assert enumeration.raw_joint_state_count == 625
    assert len(enumeration.scenarios) == 613
    kind_by_atom = {
        atom.atom_id: atom.kind
        for state in problem.terminal_state_sets
        for atom in state.atoms
    }
    normal = [
        scenario for scenario in enumeration.scenarios
        if all(
            kind_by_atom[selected.atom_id]
            in {TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO}
            for selected in scenario.atoms
        )
    ]
    assert len(normal) == 4

    approved = catalog.approve(
        result["version_id"],
        {"version_id": result["version_id"]},
        actor="tester",
        git_sha="test",
    )
    assert approved["status"] == "APPROVED"
    assert approved["activation"] == "ACTIVE"
    assert catalog.current_generation() != {}


# Slice 4 (C2 complement): an approved NATIVE_COMPLEMENT candidate with both
# token endpoints present activates and enters the compile seam generation.

def test_native_complement_approve_activates(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    result = catalog.ingest_mechanical_relation(complement_relation())
    approved = catalog.approve(
        result["version_id"],
        {"version_id": result["version_id"]},
        actor="tester",
        git_sha="test",
    )
    assert approved["status"] == "APPROVED"
    assert approved["activation"] == "ACTIVE"
    generation = catalog.current_generation()
    assert set(generation) == {result["identity"]}
    assert generation[result["identity"]]["activation"] == "ACTIVE"


# Slice 5 (C3): NATIVE_COMPLEMENT is a two-endpoint codec; a three-endpoint
# payload is rejected instead of being admitted.

def test_native_complement_three_endpoints_rejected(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    with pytest.raises(ValueError):
        catalog.ingest(
            discovery(
                relation_type="NATIVE_COMPLEMENT",
                n=3,
                contract_ids=("yes-1", "no-1", "side-1"),
            )
        )
    assert catalog.pending_count() == 0


def mechanical_market(
    market_id: str,
    *,
    condition_id: str | None = None,
    yes_token: str = "yes-m",
    no_token: str = "no-m",
    rules: str = "official index",
    source: str = "Binance",
    end_date: str = "2026-12-31T17:00:00Z",
    fees_enabled: bool | None = False,
) -> dict[str, object]:
    """One official Polymarket snapshot market with YES/NO outcome tokens.

    Issue #112: gamma rows carry the fee fields; the default fixture is a
    proven fee-free market and tests can override or drop the flag.
    """
    payload: dict[str, object] = {
        "id": market_id,
        "conditionId": condition_id or f"condition-{market_id}",
        "question": f"Will {market_id} happen?",
        "description": rules,
        "resolutionSource": source,
        "endDate": end_date,
        "outcomes": '["Yes", "No"]',
    }
    if fees_enabled is not None:
        payload["trading"] = {"feesEnabled": fees_enabled}
    if yes_token is not None and no_token is not None:
        payload["clobTokenIds"] = json.dumps([yes_token, no_token])
    return payload


def mechanical_event(
    *markets: dict[str, object],
    event_id: str = "event-1",
    active: bool = True,
    closed: bool = False,
    ended: bool = False,
    neg_risk: bool = False,
) -> dict[str, object]:
    """One official Polymarket snapshot event (flat keys, like the SDK dumps)."""
    return {
        "id": event_id,
        "title": "Which outcome resolves?",
        "active": active,
        "closed": closed,
        "ended": ended,
        "negRisk": neg_risk,
        "markets": list(markets),
    }


# Slice 6 (B1/B3 complement): the mechanical catalog derives one
# NATIVE_COMPLEMENT relation per eligible official market with YES/NO tokens;
# markets without tokens yield zero complement candidates and a rejection.

def test_mechanical_catalog_finds_complement_pair_from_venue_tokens(
    tmp_path: Path,
) -> None:
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("m1", yes_token="yes-m1", no_token="no-m1"),
        )
    ])
    assert result.events_seen == 1
    assert result.events_eligible == 1
    assert len(result.complements) == 1
    (complement,) = result.complements
    assert complement.relation_type == "NATIVE_COMPLEMENT"
    assert complement.event_id == "event-1"
    assert complement.market.condition_id == "condition-m1"
    assert complement.market.yes_token_id == "yes-m1"
    assert complement.market.no_token_id == "no-m1"

    catalog = RelationCatalog(tmp_path)
    ingested = catalog.ingest_mechanical_relation(complement)
    (row,) = catalog.review_rows()
    assert ingested["identity"] == "NATIVE_COMPLEMENT|polymarket:no-m1|polymarket:yes-m1"
    assert row["discovery_source"] == "VENUE_METADATA"
    assert row["relation_type"] == "NATIVE_COMPLEMENT"


def test_mechanical_catalog_skips_market_without_tokens() -> None:
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("m1", yes_token=None, no_token=None),
        )
    ])
    assert result.complements == ()
    assert result.rejection_counts["complement_unparseable"] == 1


def test_native_complement_missing_facts_stay_incomplete(tmp_path: Path) -> None:
    # The public venue-metadata entrance must drop a market whenever the
    # outcome-token pair, condition identity, settlement rules, or source is
    # incomplete. A repeated token pair may leave the first distinct market,
    # but never admits the duplicate endpoint as a second complete model.
    incomplete_markets = [
        mechanical_market("missing-yes", yes_token=None, no_token="no-1"),
        mechanical_market("missing-no", yes_token="yes-1", no_token=None),
        mechanical_market("missing-condition"),
        mechanical_market("missing-rules", rules=""),
        mechanical_market("missing-source", source=""),
    ]
    incomplete_markets[2]["conditionId"] = ""
    for market in incomplete_markets:
        result = discover_mechanical_relation_catalog([
            mechanical_event(market),
        ])
        assert result.complements == ()
        assert result.rejection_counts["complement_unparseable"] == 1
        catalog = RelationCatalog(tmp_path / str(market["id"]))
        assert catalog.current_generation() == {}

    duplicate = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("first", yes_token="yes-1", no_token="no-1"),
            mechanical_market("duplicate", yes_token="yes-1", no_token="no-1"),
        )
    ])
    assert len(duplicate.complements) == 1
    assert duplicate.complements[0].market.market_id == "first"
    assert duplicate.rejection_counts["duplicate_token"] == 1


def test_mechanical_catalog_rejects_complement_with_unparseable_end_date() -> None:
    # Fail closed on the compiler's fact set: the COMPLETE mechanical model
    # requires every endDate to parse as a release timestamp, so an
    # unparseable endDate must yield zero complement candidates instead of a
    # candidate whose identity/preparation would raise on every round.
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market(
                "m1",
                yes_token="yes-m1",
                no_token="no-m1",
                end_date="not-a-date",
            ),
        )
    ])
    assert result.complements == ()
    assert result.rejection_counts["complement_unparseable"] == 1


def test_mechanical_catalog_rejects_complement_with_naive_end_date() -> None:
    # Fail closed on the compiler's timezone requirement: the identity and
    # preparation steps reject timestamps without a timezone, so an endDate
    # with no offset ("2026-12-31T17:00:00") must yield zero complement
    # candidates instead of a candidate that raises on every later round.
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market(
                "m1",
                yes_token="yes-m1",
                no_token="no-m1",
                end_date="2026-12-31T17:00:00",
            ),
        )
    ])
    assert result.complements == ()
    assert result.rejection_counts["complement_unparseable"] == 1


def test_mechanical_catalog_rejects_complement_without_resolution_source() -> None:
    # The COMPLETE mechanical compiler requires a non-empty resolution source;
    # a market without one must not yield a complement candidate that can only
    # ever be INCOMPLETE (which would sit in the review queue forever).
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market(
                "m1",
                yes_token="yes-m1",
                no_token="no-m1",
                source="",
            ),
        )
    ])
    assert result.complements == ()
    assert result.rejection_counts["complement_unparseable"] == 1


def test_prepare_mechanical_candidates_completes_on_unparseable_end_date_snapshot(
    tmp_path: Path,
) -> None:
    # A snapshot containing an unparseable endDate must not crash preparation:
    # discovery fails the bad market closed, the healthy candidate is still
    # prepared, and no exception escapes (previously the identity step raised
    # ValueError on the bad endDate and the whole round died).
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market(
                "m0", yes_token="yes-m0", no_token="no-m0"
            ),
            mechanical_market(
                "m1",
                yes_token="yes-m1",
                no_token="no-m1",
                end_date="not-a-date",
            ),
        )
    ])
    assert result.rejection_counts["complement_unparseable"] == 1
    assert len(result.complements) == 1

    catalog = RelationCatalog(tmp_path)
    report = prepare_mechanical_relation_candidates(
        catalog, result.complements, result.groups
    )
    assert report["status"] == "PREPARED"
    assert report["prepared"] == 1
    assert report["components"][0]["event_id"] == "event-1"
    assert catalog.pending_count() == 1


# Slice 7 (B2/B3/B4 group): a negRisk event with an official event id yields
# one EXACTLY_ONE relation over its markets; a missing negRisk flag or event
# id yields zero group candidates, and more than seven markets are rejected.

def test_mechanical_catalog_groups_negrisk_event_markets(tmp_path: Path) -> None:
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            *(
                mechanical_market(
                    f"m{index}", yes_token=f"yes-m{index}", no_token=f"no-m{index}"
                )
                for index in range(4)
            ),
            event_id="event-group-1",
            neg_risk=True,
        )
    ])
    assert len(result.groups) == 1
    (group,) = result.groups
    assert group.relation_type == "EXACTLY_ONE"
    assert group.event_id == "event-group-1"
    assert [market.condition_id for market in group.markets] == [
        "condition-m0", "condition-m1", "condition-m2", "condition-m3",
    ]

    catalog = RelationCatalog(tmp_path)
    ingested = catalog.ingest_mechanical_relation(group)
    (row,) = catalog.review_rows()
    assert row["discovery_source"] == "VENUE_METADATA"
    assert row["relation_type"] == "EXACTLY_ONE"
    assert ingested["identity"].startswith("EXACTLY_ONE|polymarket:condition-")


def test_mechanical_catalog_skips_event_without_negrisk_flag() -> None:
    result = discover_mechanical_relation_catalog([
        mechanical_event(mechanical_market("m1"), neg_risk=False),
    ])
    assert result.groups == ()
    assert result.rejection_counts["group_ineligible"] == 1


def test_mechanical_catalog_skips_event_without_id() -> None:
    result = discover_mechanical_relation_catalog([
        mechanical_event(mechanical_market("m1"), event_id="", neg_risk=True),
    ])
    assert result.complements == ()
    assert result.groups == ()
    assert result.rejection_counts["event_ineligible"] == 1


def test_mechanical_catalog_rejects_group_over_seven_markets() -> None:
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            *(
                mechanical_market(
                    f"m{index}", yes_token=f"yes-m{index}", no_token=f"no-m{index}"
                )
                for index in range(8)
            ),
            neg_risk=True,
        )
    ])
    assert result.groups == ()
    assert result.rejection_counts["group_too_large"] == 1


def test_mechanical_catalog_rejects_group_with_unparseable_member() -> None:
    # Fail closed: the official guarantee is exactly one YES across the whole
    # negRisk event set, so a member market missing description/rules must
    # yield zero groups -- never a group over the remaining markets, which
    # would fabricate an EXACTLY_ONE constraint over a proper subset.
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("m0"),
            mechanical_market("m1", rules=""),
            mechanical_market("m2"),
            mechanical_market("m3"),
            event_id="event-group-1",
            neg_risk=True,
        )
    ])
    assert result.groups == ()
    assert result.rejection_counts["group_member_unparseable"] == 1


def test_mechanical_catalog_does_not_bypass_budget_via_unparseable_member() -> None:
    # An eight-market event with one unparseable member must be rejected as a
    # whole: dropping the member would otherwise leave seven markets and
    # bypass the seven-market group budget.
    markets = [
        mechanical_market(
            f"m{index}", yes_token=f"yes-m{index}", no_token=f"no-m{index}"
        )
        for index in range(8)
    ]
    del markets[3]["description"]
    result = discover_mechanical_relation_catalog([
        mechanical_event(*markets, event_id="event-group-1", neg_risk=True)
    ])
    assert result.groups == ()
    assert result.rejection_counts["group_member_unparseable"] == 1
    assert result.rejection_counts["group_too_large"] == 0


def test_mechanical_catalog_rejects_group_with_unparseable_member_end_date() -> None:
    # Fail closed on the compiler's fact set: an unparseable member endDate
    # must reject the whole negRisk event set -- never a group whose member
    # identity/preparation would raise on every round.
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("m0", yes_token="yes-m0", no_token="no-m0"),
            mechanical_market(
                "m1",
                yes_token="yes-m1",
                no_token="no-m1",
                end_date="not-a-date",
            ),
            mechanical_market("m2", yes_token="yes-m2", no_token="no-m2"),
            mechanical_market("m3", yes_token="yes-m3", no_token="no-m3"),
            event_id="event-group-1",
            neg_risk=True,
        )
    ])
    assert result.groups == ()
    assert result.rejection_counts["group_member_unparseable"] == 1


def test_mechanical_catalog_rejects_group_with_naive_member_end_date() -> None:
    # Fail closed on the compiler's timezone requirement: a member endDate
    # with no offset must reject the whole negRisk event set -- never a group
    # whose member identity/preparation would raise on every round.
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("m0", yes_token="yes-m0", no_token="no-m0"),
            mechanical_market(
                "m1",
                yes_token="yes-m1",
                no_token="no-m1",
                end_date="2026-12-31T17:00:00",
            ),
            mechanical_market("m2", yes_token="yes-m2", no_token="no-m2"),
            mechanical_market("m3", yes_token="yes-m3", no_token="no-m3"),
            event_id="event-group-1",
            neg_risk=True,
        )
    ])
    assert result.groups == ()
    assert result.rejection_counts["group_member_unparseable"] == 1


def test_mechanical_catalog_rejects_group_member_without_resolution_source() -> None:
    # The COMPLETE mechanical compiler requires a non-empty resolution source
    # per member; a member without one must reject the whole negRisk event set
    # instead of yielding a group that can only ever be INCOMPLETE.
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("m0", yes_token="yes-m0", no_token="no-m0", source=""),
            mechanical_market("m1", yes_token="yes-m1", no_token="no-m1"),
            mechanical_market("m2", yes_token="yes-m2", no_token="no-m2"),
            mechanical_market("m3", yes_token="yes-m3", no_token="no-m3"),
            event_id="event-group-1",
            neg_risk=True,
        )
    ])
    assert result.groups == ()
    assert result.rejection_counts["group_member_unparseable"] == 1


def test_mechanical_catalog_dedupes_duplicate_condition_in_negrisk_group(
    tmp_path: Path,
) -> None:
    # One negRisk event whose markets list repeats the same condition id must
    # not produce a group with duplicate endpoints, and the preparation flow
    # must not raise "market endpoints must be unique".
    result = discover_mechanical_relation_catalog([
        mechanical_event(
            mechanical_market("m0", condition_id="condition-shared"),
            mechanical_market("m0", condition_id="condition-shared"),
            mechanical_market("m1", condition_id="condition-m1"),
            event_id="event-group-1",
            neg_risk=True,
        )
    ])
    # One repeated condition is rejected twice: once in the complement pass
    # (condition+token dedupe) and once in the group pass (condition dedupe).
    assert result.rejection_counts["duplicate_condition"] == 2
    assert len(result.complements) == 1
    assert len(result.groups) == 1
    (group,) = result.groups
    assert [market.condition_id for market in group.markets] == [
        "condition-m1", "condition-shared",
    ]

    catalog = RelationCatalog(tmp_path)
    report = prepare_mechanical_relation_candidates(catalog, [], [group])
    assert report["status"] == "PREPARED"
    assert report["prepared"] == 1
    assert report["components"][0]["relation_type"] == "EXACTLY_ONE"


# --------------------------------------------------------------------------
# Issue #112 (S2): the catalog payload carries the per-market fee facts so the
# live resolver can gate on them; a fee-only change must rotate the version
# (otherwise the new payload would collide with the old version and never land).
# --------------------------------------------------------------------------


def test_mechanical_ingest_endpoints_carry_fee_fields(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    relation = replace(
        complement_relation(),
        market=replace(complement_relation().market, fees_enabled=False),
    )
    result = catalog.ingest_mechanical_relation(relation)
    approved = catalog.approve(
        result["version_id"],
        {"version_id": result["version_id"]},
        actor="tester",
        git_sha="test",
    )
    assert approved["activation"] == "ACTIVE"

    endpoints = catalog.current_generation()[result["identity"]]["endpoints"]
    assert len(endpoints) == 2
    for endpoint in endpoints:
        assert endpoint["fees_enabled"] is False
        assert endpoint["fee_rate"] is None


def test_mechanical_fee_only_change_rotates_the_version(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    free = complement_relation()
    charging = replace(
        free,
        market=replace(
            free.market, fees_enabled=True, fee_rate=Decimal("0.04")
        ),
    )
    first = catalog.ingest_mechanical_relation(free)
    second = catalog.ingest_mechanical_relation(charging)

    assert second["created"] is True
    assert second["version_id"] != first["version_id"]


def test_mechanical_group_endpoints_carry_fee_fields(tmp_path: Path) -> None:
    catalog = RelationCatalog(tmp_path)
    base = group_relation(2)
    charging, free = base.markets
    relation = replace(
        base,
        markets=(
            replace(charging, fees_enabled=True, fee_rate=Decimal("0.04")),
            replace(free, fees_enabled=False, fee_rate=None),
        ),
    )
    result = catalog.ingest_mechanical_relation(relation)
    approved = catalog.approve(
        result["version_id"],
        {"version_id": result["version_id"]},
        actor="tester",
        git_sha="test",
    )
    assert approved["activation"] == "ACTIVE"

    by_contract = {
        endpoint["contract_id"]: endpoint
        for endpoint in catalog.current_generation()[result["identity"]]["endpoints"]
    }
    assert by_contract["condition-0"]["fees_enabled"] is True
    assert by_contract["condition-0"]["fee_rate"] == "0.04"
    assert by_contract["condition-1"]["fees_enabled"] is False
    assert by_contract["condition-1"]["fee_rate"] is None
