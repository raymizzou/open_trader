"""Issue #98 slice S2: incremental ``activate_many`` vs. the full ``replace`` oracle.

The v2 catalog's ``replace`` (change-set flow) is the independent truth for a
prospective generation: full per-component checks plus the whole-set compile
precheck. ``activate_many`` must reproduce the exact per-step judgment (status,
blocked reasons) and the final generation/approved state while only recomputing
the affected component via the in-process contract index.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import threading
import time
from datetime import datetime

import pytest

from open_trader.prediction_monitor_selection import relation_generation_problem
from open_trader.relation_catalog import RelationCatalog
from open_trader.relation_catalog_v2 import (
    RelationCatalogV2,
    SqliteCatalogStore,
    _canonical_endpoint,
    _canonicalize,
    _fp,
    _problem_observation_keys,
)
from test_relation_catalog import compiled_problem, compiled_relation_discovery
from test_relation_catalog_v2 import _endpoint, _payload

ACTOR = "auditor"
GIT_SHA = "a" * 40

# Fixed-seed matrix: every relation over a contract must agree on that
# contract's side, rule, as_of and release (otherwise the whole-set compile
# seam raises merge conflicts and the oracle blocks everything); rules are
# per-contract so the compile seam joins contracts only via relations, and
# each contract carries one stable event_identity_basis from two pools so
# cross-event components are constructible.
CONTRACT_POOL = [f"c{i}" for i in range(1, 121)]
SIDES = {contract: "BUY_YES" for contract in CONTRACT_POOL}
RULES = {contract: f"rule-{contract}" for contract in CONTRACT_POOL}
BASES = {
    contract: ("event-a" if index < 60 else "event-b")
    for index, contract in enumerate(CONTRACT_POOL)
}
AS_OF = "2026-08-15T00:00:00Z"
RELEASE = "2026-12-31T17:00:00Z"
SEED = 98


def _relation(relation_type: str, contracts: list[str]) -> dict[str, object]:
    endpoints = [
        dict(_endpoint(contract_id=contract, event_identity_basis=BASES[contract]))
        for contract in contracts
    ]
    return _payload(
        relation_type=relation_type,
        endpoints=endpoints,
        problem=compiled_problem(
            contracts, SIDES, as_of=AS_OF, release_at=RELEASE, rule=RULES
        ),
    )


def _matrix_payloads(rng: random.Random, catalog: RelationCatalogV2) -> list[dict[str, object]]:
    """One fixed-seed matrix step: a payload batch mixing normal satisfiable,
    cross-event, over-budget, unsatisfiable, same-identity-version-change,
    idempotent and shared-observation-key cases (the same generator both paths
    consume)."""
    pool_a = [c for c in CONTRACT_POOL if BASES[c] == "event-a"]
    pool_b = [c for c in CONTRACT_POOL if BASES[c] == "event-b"]
    case = rng.choices(
        [
            "normal", "cross_event", "oversized", "unsat", "same_identity",
            "idempotent", "shared_observation", "stale_as_of",
        ],
        weights=[45, 15, 12, 10, 15, 3, 8, 5],
        k=1,
    )[0]
    if case == "shared_observation":
        # One market with token-level and condition-level contract ids: the
        # token NATIVE_COMPLEMENT pair and the condition EXACTLY_ONE relation
        # are contract-disjoint, but both compiled problems carry the same
        # terminal-state settlement observation key (same rule, same as_of),
        # so only the observation-key dimension merges them. Dedicated ids
        # keep the pair isolated from the shared contract pool.
        market = rng.randrange(10**9)
        key = f"obs-market-{market}"
        token_ids = [f"tok-{market}-1", f"tok-{market}-2"]
        condition_ids = [f"cond-{market}-1", f"cond-{market}-2"]
        sides = {contract_id: "BUY_YES" for contract_id in token_ids + condition_ids}
        token_payload = _payload(
            relation_type="NATIVE_COMPLEMENT",
            endpoints=[
                dict(
                    _endpoint(
                        contract_id=contract_id,
                        event_identity_basis="event-a",
                        settlement_observation_key=key,
                    )
                )
                for contract_id in token_ids
            ],
            problem=compiled_problem(
                token_ids, sides, as_of=AS_OF, release_at=RELEASE, rule="rules-market"
            ),
        )
        condition_payload = _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                dict(
                    _endpoint(
                        contract_id=contract_id,
                        event_identity_basis="event-a",
                        settlement_observation_key=key,
                    )
                )
                for contract_id in condition_ids
            ],
            problem=compiled_problem(
                condition_ids, sides, as_of=AS_OF, release_at=RELEASE, rule="rules-market"
            ),
        )
        return [token_payload, condition_payload]
    if case == "stale_as_of":
        # One relation activates first with an early as_of and a late capital
        # release; a later contract-disjoint candidate with a later as_of and
        # the earliest release after its own as_of is globally stale: the
        # whole-set compile seam merges as_of=max(...) and validates every
        # terminal release against it, so the oracle blocks the candidate with
        # ACTIVATION_BLOCKED_INCONSISTENT although the two relations share no
        # contract and no observation key. Dedicated ids and rules keep both
        # payloads isolated from the shared contract pool.
        early = _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                dict(
                    _endpoint(
                        contract_id="stale-a",
                        event_identity_basis="event-a",
                    )
                ),
                dict(
                    _endpoint(
                        contract_id="stale-b",
                        event_identity_basis="event-a",
                    )
                ),
            ],
            problem=compiled_problem(
                ["stale-a", "stale-b"],
                {"stale-a": "BUY_YES", "stale-b": "BUY_YES"},
                as_of="2026-08-15T00:00:00Z",
                release_at="2027-12-31T17:00:00Z",
                rule="rules-stale-early",
            ),
        )
        later = _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                dict(
                    _endpoint(
                        contract_id="stale-c",
                        event_identity_basis="event-a",
                    )
                ),
                dict(
                    _endpoint(
                        contract_id="stale-d",
                        event_identity_basis="event-a",
                    )
                ),
            ],
            problem=compiled_problem(
                ["stale-c", "stale-d"],
                {"stale-c": "BUY_YES", "stale-d": "BUY_YES"},
                as_of="2028-06-01T00:00:00Z",
                release_at="2028-08-01T17:00:00Z",
                rule="rules-stale-late",
            ),
        )
        return [early, later]
    if case == "normal":
        relation_type = rng.choices(
            ["IMPLIES", "MUTUALLY_EXCLUSIVE", "EXACTLY_ONE", "NATIVE_COMPLEMENT"],
            weights=[3, 3, 3, 1],
        )[0]
        size = 2 if relation_type in ("IMPLIES", "NATIVE_COMPLEMENT") else rng.randint(2, 3)
        pool = rng.choice([pool_a, pool_b])
        return [_relation(relation_type, sorted(rng.sample(pool, size)))]
    if case == "cross_event":
        contracts = sorted([rng.choice(pool_a), rng.choice(pool_b)])
        relation_type = rng.choice(["EXACTLY_ONE", "IMPLIES", "MUTUALLY_EXCLUSIVE"])
        return [_relation(relation_type, contracts)]
    if case == "oversized":
        return [_relation("EXACTLY_ONE", sorted(rng.sample(CONTRACT_POOL, 8)))]
    if case == "unsat":
        pair = sorted(rng.sample(CONTRACT_POOL, 2))
        return [
            _relation("EXACTLY_ONE", pair),
            _relation("IMPLIES", pair),
            _relation("IMPLIES", list(reversed(pair))),
        ]
    active = list(catalog.store.get("generation", {}))
    if not active:
        return [_relation("EXACTLY_ONE", sorted(rng.sample(CONTRACT_POOL, 2)))]
    identity = rng.choice(active)
    version_id = catalog.store["generation"][identity]["version_id"]
    stored = catalog.store["versions"][version_id]["payload"]
    if case == "idempotent":
        return [dict(stored)]
    changed = dict(stored)
    changed["discovery_source"] = "exchange_metadata_v2"
    return [changed]


def _remaining_payloads(catalog: RelationCatalogV2) -> list[dict[str, object]]:
    return [
        catalog.store["versions"][entry["version_id"]]["payload"]
        for entry in catalog.store.get("generation", {}).values()
    ]


def _catalog(db_path: str) -> RelationCatalogV2:
    return RelationCatalogV2(SqliteCatalogStore(db_path))


def _recomputed_index(catalog: RelationCatalogV2) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for identity, entry in catalog.store["generation"].items():
        payload = catalog.store["versions"][entry["version_id"]]["payload"]
        for endpoint in payload["endpoints"]:
            index.setdefault(_canonical_endpoint(endpoint), set()).add(identity)
    return index


def _compiled_rows(catalog: RelationCatalogV2) -> dict[str, dict[str, object]]:
    """The current generation in the compile seam's row shape
    (``activation``/``model``), mirroring what ``_activate_many_locked``
    feeds ``relation_generation_problem``."""
    versions = catalog.store["versions"]
    return {
        identity: {
            "activation": "ACTIVE",
            "model": {
                name: versions[entry["version_id"]]["payload"].get(name)
                for name in ("terminal_states", "payouts", "capital_release", "problem")
            },
        }
        for identity, entry in catalog.store["generation"].items()
    }


def test_r11_batch_unsatisfiable_triple_matches_sequential(tmp_path) -> None:
    """R1.1 (review round 1): one batch of the unsat triple
    [EXACTLY_ONE(a,b), IMPLIES(a,b), IMPLIES(b,a)] judges each entry exactly
    like three sequential single batches: the first two activate, the third
    is BLOCKED because earlier batch members are visible inside the batch,
    and the published ACTIVE set compiles on the read path."""
    triple = [
        _relation("EXACTLY_ONE", ["c1", "c2"]),
        _relation("IMPLIES", ["c1", "c2"]),
        _relation("IMPLIES", ["c2", "c1"]),
    ]
    batched = _catalog(str(tmp_path / "batch.db"))
    sequential = _catalog(str(tmp_path / "seq.db"))

    outcome = batched.activate_many(triple, actor=ACTOR, git_sha=GIT_SHA)

    sequential_results: dict[str, dict[str, object]] = {}
    sequential_blocked: list[dict[str, str]] = []
    sequential_status = ""
    for payload in triple:
        identity = _canonicalize(payload)[0]
        result = sequential.activate_many([payload], actor=ACTOR, git_sha=GIT_SHA)
        sequential_results[identity] = result["results"][identity]
        sequential_blocked.extend(result["blocked"])
        sequential_status = str(result["status"])

    assert outcome["results"] == sequential_results
    assert outcome["blocked"] == sequential_blocked
    assert outcome["status"] == sequential_status
    assert outcome["results"][_canonicalize(triple[2])[0]]["status"] == "BLOCKED"
    assert batched.store["generation"] == sequential.store["generation"]
    assert batched.store["approved"] == sequential.store["approved"]

    problem, _ = relation_generation_problem(_compiled_rows(batched))
    assert problem is not None


def test_r16_scan_generation_is_lazy_and_correct(tmp_path, monkeypatch) -> None:
    """R1.6 (review round 1): ``_scan_generation`` must not materialize the
    whole generations history. With a small injected ``_ANCHOR_EVERY`` and
    many delta rows above the newest anchor, the DESC scan fetches only the
    deltas above the newest anchor plus the anchor row itself (the pre-fix
    ``fetchall()`` fetched every historical row), and the decoded generation
    is complete across anchors and deltas."""
    monkeypatch.setattr(SqliteCatalogStore, "_ANCHOR_EVERY", 5)
    n = 23  # anchors at changes 5, 10, 15, 20; three deltas above the newest anchor

    fetched: dict[str, int] = {"rows": 0}
    original_connection = SqliteCatalogStore._connection

    class CountingCursor:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __iter__(self) -> CountingCursor:
            return self

        def __next__(self) -> object:
            fetched["rows"] += 1
            return next(self._inner)  # type: ignore[arg-type]

        def fetchall(self) -> list[object]:
            rows = self._inner.fetchall()  # type: ignore[attr-defined]
            fetched["rows"] += len(rows)
            return rows

    class CountingConnection:
        """Duck-typed proxy: sqlite3.Connection is immutable, so the count
        wraps the store's thread-local connection object instead."""

        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql: object, *args: object, **kwargs: object) -> object:
            cursor = self._inner.execute(sql, *args, **kwargs)
            if "ORDER BY generation_id DESC" in str(sql):
                return CountingCursor(cursor)
            return cursor

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    def counting_connection(store: SqliteCatalogStore) -> sqlite3.Connection:
        return CountingConnection(original_connection(store))  # type: ignore[return-value]

    monkeypatch.setattr(SqliteCatalogStore, "_connection", counting_connection)

    catalog = _catalog(str(tmp_path / "db"))
    for index in range(n):
        contracts = [f"lazy-{index}-a", f"lazy-{index}-b"]
        payload = _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                dict(_endpoint(contract_id=contract, event_identity_basis="event-a"))
                for contract in contracts
            ],
            problem=compiled_problem(
                contracts,
                {contract: "BUY_YES" for contract in contracts},
                rule={contract: f"rule-{contract}" for contract in contracts},
            ),
        )
        result = catalog.activate_many([payload], actor=ACTOR, git_sha=GIT_SHA)
        assert result["status"] == "ACTIVE"

    before = fetched["rows"]
    reopened = _catalog(str(tmp_path / "db"))
    generation = reopened.current_generation()
    after = fetched["rows"]

    assert len(generation) == n
    assert after - before <= n % 5 + 1  # deltas above the newest anchor + the anchor row


def test_r12_batch_stale_pair_matches_sequential_and_read_path_compiles(tmp_path) -> None:
    """R1.2 (review round 1): one batch with two contract-disjoint relations
    whose as_of/release conflict globally (the second is stale against the
    first's terminal release) judges the second exactly like a sequential
    single batch: INCONSISTENT/BLOCKED, and the published generation compiles
    on the read path (no STALE_CAPITAL_RELEASE_AT)."""
    early = _payload(
        relation_type="EXACTLY_ONE",
        endpoints=[
            dict(_endpoint(contract_id="stale-a", event_identity_basis="event-a")),
            dict(_endpoint(contract_id="stale-b", event_identity_basis="event-a")),
        ],
        problem=compiled_problem(
            ["stale-a", "stale-b"],
            {"stale-a": "BUY_YES", "stale-b": "BUY_YES"},
            as_of="2026-08-15T00:00:00Z",
            release_at="2027-12-31T17:00:00Z",
            rule="rules-r12-early",
        ),
    )
    later = _payload(
        relation_type="EXACTLY_ONE",
        endpoints=[
            dict(_endpoint(contract_id="stale-c", event_identity_basis="event-a")),
            dict(_endpoint(contract_id="stale-d", event_identity_basis="event-a")),
        ],
        problem=compiled_problem(
            ["stale-c", "stale-d"],
            {"stale-c": "BUY_YES", "stale-d": "BUY_YES"},
            as_of="2028-06-01T00:00:00Z",
            release_at="2028-08-01T17:00:00Z",
            rule="rules-r12-late",
        ),
    )
    batched = _catalog(str(tmp_path / "batch.db"))
    sequential = _catalog(str(tmp_path / "seq.db"))

    outcome = batched.activate_many([early, later], actor=ACTOR, git_sha=GIT_SHA)

    sequential_results: dict[str, dict[str, object]] = {}
    sequential_blocked: list[dict[str, str]] = []
    for payload in (early, later):
        identity = _canonicalize(payload)[0]
        result = sequential.activate_many([payload], actor=ACTOR, git_sha=GIT_SHA)
        sequential_results[identity] = result["results"][identity]
        sequential_blocked.extend(result["blocked"])

    assert outcome["results"] == sequential_results
    assert outcome["blocked"] == sequential_blocked
    assert outcome["results"][_canonicalize(later)[0]]["status"] == "BLOCKED"
    assert batched.store["generation"] == sequential.store["generation"]
    assert batched.store["approved"] == sequential.store["approved"]

    # Read path: the published generation compiles (the pre-fix batch left
    # both members ACTIVE and the merged problem raised STALE_CAPITAL_RELEASE_AT).
    problem, _ = relation_generation_problem(_compiled_rows(batched))
    assert problem is not None


def test_r13_batch_valuation_unit_conflict_matches_sequential(tmp_path) -> None:
    """R1.3 (review round 1): one batch with two contract-disjoint relations
    that share no observation key but use different valuation units judges the
    second exactly like a sequential single batch: blocked (the whole-set
    merge predicate "compiled problems must share one valuation unit")."""
    usd = _payload(
        relation_type="EXACTLY_ONE",
        endpoints=[
            dict(_endpoint(contract_id="unit-a", event_identity_basis="event-a")),
            dict(_endpoint(contract_id="unit-b", event_identity_basis="event-a")),
        ],
        problem=compiled_problem(
            ["unit-a", "unit-b"],
            {"unit-a": "BUY_YES", "unit-b": "BUY_YES"},
            rule="rules-r13-usd",
        ),
    )
    eur = _payload(
        relation_type="EXACTLY_ONE",
        endpoints=[
            dict(_endpoint(contract_id="unit-c", event_identity_basis="event-a")),
            dict(_endpoint(contract_id="unit-d", event_identity_basis="event-a")),
        ],
        problem=compiled_problem(
            ["unit-c", "unit-d"],
            {"unit-c": "BUY_YES", "unit-d": "BUY_YES"},
            rule="rules-r13-eur",
        ),
    )
    # A valid EUR-compiled problem: the problem-level unit and every action's
    # unit move together, so the payload compiles alone and only the whole-set
    # merge predicate ("compiled problems must share one valuation unit")
    # blocks it against the USD ACTIVE set.
    eur["problem"]["valuation_unit_id"] = "EUR"
    for action in eur["problem"]["actions"]:
        action["valuation_unit_id"] = "EUR"

    batched = _catalog(str(tmp_path / "batch.db"))
    sequential = _catalog(str(tmp_path / "seq.db"))

    outcome = batched.activate_many([usd, eur], actor=ACTOR, git_sha=GIT_SHA)

    sequential_results: dict[str, dict[str, object]] = {}
    sequential_blocked: list[dict[str, str]] = []
    for payload in (usd, eur):
        identity = _canonicalize(payload)[0]
        result = sequential.activate_many([payload], actor=ACTOR, git_sha=GIT_SHA)
        sequential_results[identity] = result["results"][identity]
        sequential_blocked.extend(result["blocked"])

    assert outcome["results"] == sequential_results
    assert outcome["blocked"] == sequential_blocked
    assert outcome["results"][_canonicalize(eur)[0]]["status"] == "BLOCKED"
    assert batched.store["generation"] == sequential.store["generation"]
    assert batched.store["approved"] == sequential.store["approved"]

    problem, _ = relation_generation_problem(_compiled_rows(batched))
    assert problem is not None


def test_activate_many_matrix_matches_replace_oracle() -> None:
    """T2.1: 500 fixed-seed relation sets; path A is one activate_many per
    entry, path B replays replace(preserve_existing=True) with the remaining
    members plus the new entry. Final generation, approved (with fingerprints)
    and the per-entry blocked reason sequence must be identical.

    The matrix relies on one equivalence assumption: every production codec
    derives action/state/constraint ids from the contract ids (action_id
    ``polymarket:{contract_id}``, terminal state sets keyed by
    ``market_contract_id``, per-contract rule identity), so the contract
    closure covers id conflicts too and no global id index is needed; the
    whole-set compile precheck of replace() then differs from the incremental
    path only through the stale-capital and valuation-unit predicates, which
    the aggregate guard reproduces.
    """
    catalog_a = RelationCatalogV2(store={})
    catalog_b = RelationCatalogV2(store={})
    rng = random.Random(SEED)
    blocked_a: list[tuple[int, str, str, str]] = []
    blocked_b: list[tuple[int, str, str, str]] = []

    def record(seq: list[tuple[int, str, str, str]], blocked: list[dict]) -> None:
        seq.extend(
            (steps, entry["identity"], entry["reason"], entry.get("detail", ""))
            for entry in blocked
        )

    steps = 0
    while steps < 500:
        for payload in _matrix_payloads(rng, catalog_a):
            res_a = catalog_a.activate_many([payload], actor=ACTOR, git_sha=GIT_SHA)
            res_b = catalog_b.replace(
                _remaining_payloads(catalog_b) + [payload],
                actor=ACTOR,
                git_sha=GIT_SHA,
                preserve_existing=True,
            )
            assert res_a["status"] == res_b["status"], (
                f"step {steps}: status mismatch {res_a['status']!r} vs {res_b['status']!r}"
            )
            assert res_a["blocked"] == res_b["blocked"], (
                f"step {steps}: blocked mismatch {res_a['blocked']!r} vs {res_b['blocked']!r}"
            )
            record(blocked_a, res_a["blocked"])
            record(blocked_b, res_b["blocked"])
            steps += 1

    assert blocked_a == blocked_b
    assert catalog_a.store["generation"] == catalog_b.store["generation"]
    assert catalog_a.store["approved"] == catalog_b.store["approved"]


def test_activate_many_index_matches_generation_recompute() -> None:
    """T2.2: at intermediate points of the T2.1 path A, the in-process
    contract and compiled-problem-observation-key indexes plus the ACTIVE-set
    aggregates (max as_of / min terminal release / valuation units) equal the
    state recomputed from the current generation members' payloads."""
    catalog = RelationCatalogV2(store={})
    rng = random.Random(SEED)

    def recomputed() -> dict[str, set[str]]:
        index: dict[str, set[str]] = {}
        for identity, entry in catalog.store["generation"].items():
            payload = catalog.store["versions"][entry["version_id"]]["payload"]
            for endpoint in payload["endpoints"]:
                index.setdefault(_canonical_endpoint(endpoint), set()).add(identity)
        return index

    def recomputed_keys() -> dict[str, set[str]]:
        index: dict[str, set[str]] = {}
        for identity, entry in catalog.store["generation"].items():
            payload = catalog.store["versions"][entry["version_id"]]["payload"]
            problem = payload.get("problem")
            if not isinstance(problem, dict):
                continue
            for state in problem.get("terminal_state_sets", []):
                if not isinstance(state, dict):
                    continue
                key = state.get("settlement_observation_key")
                if isinstance(key, dict):
                    index.setdefault(_fp(key), set()).add(identity)
                elif isinstance(key, str) and key:
                    index.setdefault(key, set()).add(identity)
        return index

    def recomputed_aggregates() -> dict[str, object]:
        max_as_of: datetime | None = None
        min_release: datetime | None = None
        units: set[str] = set()
        for identity, entry in catalog.store["generation"].items():
            payload = catalog.store["versions"][entry["version_id"]]["payload"]
            problem = payload.get("problem")
            if not isinstance(problem, dict):
                continue
            as_of = datetime.fromisoformat(str(problem["as_of"]).replace("Z", "+00:00"))
            releases = [
                datetime.fromisoformat(str(atom["capital_release_at"]).replace("Z", "+00:00"))
                for state in problem.get("terminal_state_sets", [])
                if isinstance(state, dict)
                for atom in state.get("atoms", [])
                if isinstance(atom, dict) and atom.get("capital_release_at") is not None
            ]
            if not releases:
                continue
            max_as_of = as_of if max_as_of is None else max(max_as_of, as_of)
            min_release = (
                min(releases) if min_release is None else min(min_release, min(releases))
            )
            units.add(str(problem["valuation_unit_id"]))
        return {
            "max_as_of": max_as_of,
            "min_release": min_release,
            "units": frozenset(units),
        }

    steps = 0
    while steps < 300:
        for payload in _matrix_payloads(rng, catalog):
            catalog.activate_many([payload], actor=ACTOR, git_sha=GIT_SHA)
            steps += 1
            if steps % 25 == 0:
                assert catalog._contract_index == recomputed()
                assert catalog._key_index == recomputed_keys()
                assert catalog._aggregates == recomputed_aggregates()


def test_activate_many_cross_connection_rebuilds_index(tmp_path) -> None:
    """T2.3: two store connections over one sqlite file. After connection A
    approves a relation, connection B's next activate_many must rebuild its
    index from the current generation and reach the same judgment as a
    single-process run."""
    first = _relation("EXACTLY_ONE", ["c1", "c2"])
    second = _relation("EXACTLY_ONE", ["c2", "c3"])

    single = _catalog(str(tmp_path / "single.db"))
    single.approve(single.ingest(first)["version_id"], actor=ACTOR, git_sha=GIT_SHA)
    expected = single.activate_many([second], actor=ACTOR, git_sha=GIT_SHA)

    connection_a = _catalog(str(tmp_path / "two.db"))
    connection_b = _catalog(str(tmp_path / "two.db"))
    connection_a.approve(
        connection_a.ingest(first)["version_id"], actor=ACTOR, git_sha=GIT_SHA
    )
    result = connection_b.activate_many([second], actor=ACTOR, git_sha=GIT_SHA)

    assert result["status"] == expected["status"]
    assert result["blocked"] == expected["blocked"]
    assert result["results"] == expected["results"]
    assert connection_b._contract_index == _recomputed_index(connection_b)


def test_activate_many_survives_unrelated_active_payload_tamper(tmp_path) -> None:
    """T2.4: direct-SQL tamper of an ACTIVE payload endpoint (no generations
    row appended, so the index watermark does not move) must not disturb the
    next activate_many judgment for an adjacent relation, and the existing
    read path must still fail closed on current_generation()."""
    first = _relation("EXACTLY_ONE", ["c1", "c2"])
    second = _relation("EXACTLY_ONE", ["c3", "c4"])
    third = _relation("EXACTLY_ONE", ["c2", "c5"])

    control = _catalog(str(tmp_path / "control.db"))
    control.activate_many([first], actor=ACTOR, git_sha=GIT_SHA)
    control.activate_many([second], actor=ACTOR, git_sha=GIT_SHA)
    expected = control.activate_many([third], actor=ACTOR, git_sha=GIT_SHA)

    catalog = _catalog(str(tmp_path / "tamper.db"))
    catalog.activate_many([first], actor=ACTOR, git_sha=GIT_SHA)
    second_identity = _canonicalize(second)[0]
    second_result = catalog.activate_many([second], actor=ACTOR, git_sha=GIT_SHA)
    second_version_id = second_result["results"][second_identity]["version_id"]
    assert second_result["results"][second_identity]["status"] == "APPROVED"

    tampered = dict(catalog.store["versions"][second_version_id]["payload"])
    tampered["endpoints"][0] = dict(
        _endpoint(contract_id="c6", event_identity_basis="event-a")
    )
    with sqlite3.connect(str(tmp_path / "tamper.db")) as conn:
        conn.execute(
            "UPDATE catalog_v2_versions SET payload=? WHERE version_id=?",
            (json.dumps(tampered, sort_keys=True), second_version_id),
        )

    result = catalog.activate_many([third], actor=ACTOR, git_sha=GIT_SHA)
    assert result["status"] == expected["status"]
    assert result["blocked"] == expected["blocked"]
    assert result["results"] == expected["results"]
    assert result["results"][_canonicalize(third)[0]]["status"] == "APPROVED"

    with pytest.raises(ValueError):
        catalog.current_generation()


@pytest.mark.pressure
def test_10k_independent_relations_single_batch_activation(tmp_path, monkeypatch) -> None:
    """T5.1: 10,000 mutually independent relations publish in ONE S3 facade
    ``approve_many`` batch against a 10,000-relation ACTIVE baseline.

    The whole file is wall-clock capped (three minutes), so two non-semantic
    staging choices keep the ingest transactions cheap: the 10,000 PENDING
    relations are ingested while the versions/latest tables are still small
    (``SqliteCatalogStore._load_write_state`` reloads the version id set and
    the latest/approved tables per write transaction, so ingesting after the
    seed would cost ~100s of pure ingest), and the ACTIVE seed is published
    through the v2 batch activation seam (``activate_many`` self-ingests each
    payload in the same transaction; the facade ``approve_many`` seed path
    would need 10,000 individual ingest transactions). The judged batch is
    exactly the S3 facade ``approve_many`` over the 10,000 PENDING relations
    with a 10,000-member ACTIVE baseline.

    Assertions are correctness-only (never time thresholds): every result is
    ACTIVE with exact counts, a reopened ``RelationCatalog`` serves a 20,000-
    member generation whose sampled approved fingerprints match the closed
    catalog, and the ``catalog_v2_generations`` row increment stays within
    the membership changes. Timings are printed, not asserted.
    """
    size = 10_000
    seed_batch = 1_000
    original_connection = SqliteCatalogStore._connection

    def fast_connection(store: SqliteCatalogStore) -> sqlite3.Connection:
        # Throwaway temp DB only: skip fsync on the store's thread-local
        # connections; the pragma is per-connection and may only change
        # outside a transaction, so ignore the in-transaction re-run.
        connection = original_connection(store)
        try:
            connection.execute("PRAGMA synchronous=OFF")
        except sqlite3.OperationalError:
            pass
        return connection

    monkeypatch.setattr(SqliteCatalogStore, "_connection", fast_connection)

    def relation(prefix: str, index: int) -> dict[str, object]:
        """One mutually independent small v2 payload over two fresh contracts
        with a per-contract rule (unique problem observation keys), so the
        contract index, the observation-key index and the event gate all stay
        disjoint from every other relation in the test."""
        contracts = [f"{prefix}-{index}-a", f"{prefix}-{index}-b"]
        return _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                dict(_endpoint(contract_id=contract, event_identity_basis="event-a"))
                for contract in contracts
            ],
            problem=compiled_problem(
                contracts,
                {contract: "BUY_YES" for contract in contracts},
                as_of=AS_OF,
                release_at=RELEASE,
                rule={contract: f"rule-{contract}" for contract in contracts},
            ),
        )

    def discovery(prefix: str, index: int) -> dict[str, object]:
        """The facade discovery codec for the same independent relation."""
        contracts = [f"{prefix}-{index}-a", f"{prefix}-{index}-b"]
        return compiled_relation_discovery(
            contracts,
            {contract: "BUY_YES" for contract in contracts},
            relation_type="EXACTLY_ONE",
            as_of=AS_OF,
            release=RELEASE,
            rule={contract: f"rule-{contract}" for contract in contracts},
        )

    catalog = RelationCatalog(tmp_path)
    db_path = str(catalog.path)

    def generations_rows() -> int:
        with sqlite3.connect(db_path) as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM catalog_v2_generations").fetchone()[0]
            )

    started = time.perf_counter()
    pending_ids: list[str] = []
    for index in range(size):
        pending_ids.append(catalog.ingest(discovery("pending", index))["version_id"])
    ingest_seconds = time.perf_counter() - started
    assert len(pending_ids) == size
    rows_before_seed = generations_rows()  # 0: PENDING ingests never touch the generation

    seeder = RelationCatalogV2(SqliteCatalogStore(db_path))
    started = time.perf_counter()
    for batch in range(size // seed_batch):
        payloads = [
            relation("seed", batch * seed_batch + index)
            for index in range(seed_batch)
        ]
        seeded = seeder.activate_many(payloads, actor=ACTOR, git_sha=GIT_SHA)
        assert seeded["status"] == "ACTIVE"
        assert all(
            result["status"] == "APPROVED" for result in seeded["results"].values()
        )
    seed_seconds = time.perf_counter() - started

    started = time.perf_counter()
    outcome = catalog.approve_many(
        [{"version_id": version_id} for version_id in pending_ids],
        actor=ACTOR,
        git_sha=GIT_SHA,
    )
    batch_seconds = time.perf_counter() - started
    assert len(outcome["results"]) == size
    assert all(result["activation"] == "ACTIVE" for result in outcome["results"])
    assert outcome["counts"] == {
        "total": size,
        "active": size,
        "blocked": 0,
        "error": 0,
    }
    rows_after_batch = generations_rows()
    assert rows_after_batch - rows_before_seed <= 2 * size

    generation_before_close = catalog.current_generation()
    assert len(generation_before_close) == 2 * size
    approved_before_close = catalog._store["approved"]
    sampled = random.Random(1234).sample(sorted(approved_before_close), 50)
    fingerprints = {
        identity: approved_before_close[identity]["approved_fingerprints"]
        for identity in sampled
    }

    reopened = RelationCatalog(tmp_path)
    assert len(reopened.current_generation()) == 2 * size
    for identity in sampled:
        assert (
            reopened._store["approved"][identity]["approved_fingerprints"]
            == fingerprints[identity]
        )

    sample_times: list[float] = []
    for index in range(20):
        version_id = catalog.ingest(discovery("post", 1_000_000 + index))["version_id"]
        started = time.perf_counter()
        catalog.approve(version_id, {"version_id": version_id}, actor=ACTOR, git_sha=GIT_SHA)
        sample_times.append((time.perf_counter() - started) * 1000)

    print(f"[T5.1] ingest {size} PENDING: {ingest_seconds:.1f}s")
    print(f"[T5.1] seed {size} ACTIVE in {size // seed_batch} batches: {seed_seconds:.1f}s")
    print(f"[T5.1] single approve_many batch of {size}: {batch_seconds:.1f}s")
    print(
        f"[T5.1] 20 single approves after batch: "
        f"median {statistics.median(sample_times):.1f}ms"
    )


# -- R2 review fixes --------------------------------------------------------

def _r2_payload(tag: str, *, as_of: str = AS_OF, release: str = RELEASE) -> dict[str, object]:
    payload = compiled_relation_discovery(
        [f"r2-{tag}-a", f"r2-{tag}-b"],
        {f"r2-{tag}-a": "BUY_YES", f"r2-{tag}-b": "BUY_YES"},
        rule=f"rules-r2-{tag}",
        as_of=as_of,
        release=release,
    )
    for market in payload["markets"]:
        market["event_identity_basis"] = "E1"
    return payload


def test_r21_naive_or_unparseable_dates_block_inconsistent_and_preserve_batch(
    tmp_path,
) -> None:
    """R2.1: a candidate whose as_of / terminal capital_release_at is present
    but timezone-less (naive) or unparseable is a predicate failure: the whole
    batch returns normally, that entry is ACTIVATION_BLOCKED_INCONSISTENT
    (the old replace() oracle gives the same judgment on the same input) and
    the batch's other entries are unaffected. Fully absent dates are not part
    of this check (legacy skip behavior, covered by the matrix tests).
    """
    catalog = RelationCatalog(tmp_path)

    good_before = _r2_payload("good-before")
    naive_as_of = _r2_payload("naive-asof")
    naive_as_of["model"]["problem"]["as_of"] = "2029-01-01T00:00:00"  # no Z / offset
    naive_release = _r2_payload("naive-release")
    naive_release["model"]["problem"]["terminal_state_sets"][0]["atoms"][0][
        "capital_release_at"
    ] = "2029-01-01T00:00:00"
    garbage_as_of = _r2_payload("garbage-asof")
    garbage_as_of["model"]["problem"]["as_of"] = "not-a-date"
    good_after = _r2_payload("good-after")

    good_before_id = catalog.ingest(good_before)["version_id"]
    naive_as_of_id = catalog.ingest(naive_as_of)["version_id"]
    naive_release_id = catalog.ingest(naive_release)["version_id"]
    garbage_as_of_id = catalog.ingest(garbage_as_of)["version_id"]
    good_after_id = catalog.ingest(good_after)["version_id"]

    outcome = catalog.approve_many(
        [
            {"version_id": good_before_id},
            {"version_id": naive_as_of_id},
            {"version_id": naive_release_id},
            {"version_id": garbage_as_of_id},
            {"version_id": good_after_id},
        ],
        actor="op",
        git_sha="sha",
    )
    by_version = {result["version_id"]: result for result in outcome["results"]}
    for invalid_id in (naive_as_of_id, naive_release_id, garbage_as_of_id):
        assert by_version[invalid_id]["activation"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    assert by_version[good_before_id]["activation"] == "ACTIVE"
    assert by_version[good_after_id]["activation"] == "ACTIVE"
    # Batch siblings are unaffected: generation holds exactly the two goods.
    assert set(catalog.current_generation()) == {
        "EXACTLY_ONE|polymarket:r2-good-before-a|polymarket:r2-good-before-b",
        "EXACTLY_ONE|polymarket:r2-good-after-a|polymarket:r2-good-after-b",
    }

    # Old-path oracle: replace() over the same prospective set rejects the
    # same candidate via the compile seam.
    oracle = RelationCatalog(str(tmp_path / "oracle"))
    oracle_good = oracle.ingest(good_before)["version_id"]
    oracle.approve(oracle_good, {"version_id": oracle_good}, actor="op", git_sha="sha")
    oracle_naive = oracle.ingest(naive_as_of)["version_id"]
    oracle_result = oracle._catalog.replace(
        oracle._generation_change_set(oracle_naive),
        actor="system",
        git_sha="",
        preserve_existing=True,
    )
    assert oracle_result["status"] == "ACTIVATION_BLOCKED_INCONSISTENT"
    assert [entry["reason"] for entry in oracle_result["blocked"]] == [
        "ACTIVATION_BLOCKED_INCONSISTENT"
    ]


def test_r22_concurrent_approves_keep_guards_and_indexes_consistent(tmp_path) -> None:
    """R2.2: two threads approving independent relation sets concurrently
    (barrier start, 200 relations each, stale / marginal-stale / valuation-unit
    conflict shapes mixed in, reviewer-probe widening of the shared index
    read-modify-write) must reach exactly the sequential judgments — no stale
    or unit guard mis-approval — and the in-process indexes/aggregates must
    equal a recompute from the final generation (no lost update). The
    reviewer-probe assertion (no cross-thread overlap of index applies) is
    deterministically false on the unlocked code and deterministically true
    under the catalog-level index lock.
    """
    catalog = RelationCatalog(str(tmp_path / "concurrent"))

    def relation(tag: str, index: int, kind: str) -> dict[str, object]:
        if kind == "stale":
            # as_of after the ACTIVE-set min terminal release: always blocked.
            return _r2_payload(
                f"{tag}-{index}-stale",
                as_of="2027-06-15T00:00:00Z",
                release="2027-12-01T17:00:00Z",
            )
        if kind == "marginal":
            # as_of between the true ACTIVE min release (base 2026-12-31T17:00Z)
            # and the smallest normal release (2027-01-01T17:00Z): blocked by
            # the true aggregate, approvable only through a lost update.
            return _r2_payload(
                f"{tag}-{index}-marginal",
                as_of="2027-01-01T12:00:00Z",
                release="2027-12-01T17:00:00Z",
            )
        if kind == "unit":
            payload = _r2_payload(
                f"{tag}-{index}-unit",
                as_of="2026-08-16T00:00:00Z",
                release="2027-01-10T17:00:00Z",
            )
            problem = payload["model"]["problem"]
            problem["valuation_unit_id"] = "EUR"
            for action in problem["actions"]:
                action["valuation_unit_id"] = "EUR"
            return payload
        return _r2_payload(
            f"{tag}-{index}-normal",
            as_of=f"2026-08-{16 + index % 10:02d}T00:00:00Z",
            release=f"2027-01-{1 + index % 28:02d}T17:00:00Z",
        )

    kinds = ["normal"] * 190 + ["stale"] * 4 + ["marginal"] * 4 + ["unit"] * 2
    payloads: dict[str, list[dict[str, object]]] = {}
    for tag in ("A", "B"):
        payloads[tag] = [relation(tag, index, kind) for index, kind in enumerate(kinds)]
    assert len(payloads["A"]) == 200 and len(payloads["B"]) == 200
    # Final-pair shapes: B's final relation carries the smallest terminal
    # release of the whole set (2026-09-01, below the ACTIVE baseline's
    # 2026-12-31T17:00:00Z) and is ACTIVE, so the final aggregates must track
    # it — a non-redundant min contribution the recompute assertion checks.
    # A's final relation is ACTIVE in either judgment order and its own
    # contribution is aggregate-redundant (the baseline already dominates
    # min_release / max_as_of). The EUR unit shapes are blocked by the unit
    # guard, so a blocked final entry would never reach the sync apply.
    payloads["A"][199] = _r2_payload(
        "A-199-final",
        as_of="2026-08-20T00:00:00Z",
        release="2027-12-01T17:00:00Z",
    )
    payloads["B"][199] = _r2_payload(
        "B-199-final",
        as_of="2026-08-16T00:00:00Z",
        release="2026-09-01T00:00:00Z",
    )

    # Sequential oracle: the same payloads in the same order, one catalog.
    oracle = RelationCatalog(str(tmp_path / "oracle"))
    # The oracle mirrors the concurrent run's ACTIVE baseline (the base
    # member), so its per-entry judgments use the same aggregate guard.
    oracle_base = oracle.ingest(_r2_payload("base"))["version_id"]
    assert (
        oracle.approve(oracle_base, {"version_id": oracle_base}, actor="op", git_sha="sha")[
            "activation"
        ]
        == "ACTIVE"
    )
    expected: dict[str, str] = {}
    for tag in ("A", "B"):
        for payload in payloads[tag]:
            version_id = oracle.ingest(payload)["version_id"]
            expected[version_id] = oracle.approve(
                version_id, {"version_id": version_id}, actor="op", git_sha="sha"
            )["activation"]

    # ACTIVE baseline with an aggregate (max as_of 2026-08-15, min release
    # 2026-12-31T17:00Z, USD), shared by the concurrent run.
    base_id = catalog.ingest(_r2_payload("base"))["version_id"]
    assert (
        catalog.approve(base_id, {"version_id": base_id}, actor="op", git_sha="sha")["activation"]
        == "ACTIVE"
    )
    version_ids = {
        tag: [catalog.ingest(payload)["version_id"] for payload in payloads[tag]]
        for tag in ("A", "B")
    }

    # Reviewer-probe widening: the shared aggregate read-modify-write is
    # replicated with a stall between the stale read and the write (the
    # window the reviewer's probe demonstrated), and every apply records its
    # window. On the pre-fix (unlocked) code the two threads' applies — one
    # thread's post-commit sync apply against the other thread's
    # in-transaction publish — interleave (cross-thread overlap); under the
    # catalog-level index lock they are serialized, so the overlap assertion
    # below is a repeatable probe for the race.
    probe_lock = threading.Lock()
    apply_windows: list[tuple[float, float, str, bool]] = []
    original_apply = RelationCatalogV2._apply_index_entry

    def widened_apply(self, identity, contracts, keys, aggregate) -> None:
        t0 = time.perf_counter()
        for contract in contracts:
            self._contract_index.setdefault(contract, set()).add(identity)
        for key in keys:
            self._key_index.setdefault(key, set()).add(identity)
        if aggregate is not None:
            as_of, release, unit = aggregate
            state = self._aggregates
            stale_max = state["max_as_of"]
            stale_min = state["min_release"]
            stale_units = state["units"]
            time.sleep(0.003)  # widen the read-modify-write window
            state["max_as_of"] = as_of if stale_max is None else max(stale_max, as_of)
            state["min_release"] = (
                release if stale_min is None else min(stale_min, release)
            )
            state["units"] = frozenset(stale_units | {unit})
        t1 = time.perf_counter()
        in_txn = getattr(self.store._local, "overlay", None) is not None
        with probe_lock:
            apply_windows.append((t0, t1, threading.current_thread().name, in_txn))

    RelationCatalogV2._apply_index_entry = widened_apply
    outcomes: dict[str, dict[str, str]] = {"A": {}, "B": {}}
    windows: dict[str, tuple[float, float]] = {}
    start_barrier = threading.Barrier(2)
    try:
        def worker(tag: str) -> None:
            start = time.perf_counter()
            start_barrier.wait()
            for version_id in version_ids[tag]:
                result = catalog.approve(
                    version_id, {"version_id": version_id}, actor="op", git_sha="sha"
                )
                outcomes[tag][version_id] = result["activation"]
            windows[tag] = (start, time.perf_counter())

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        RelationCatalogV2._apply_index_entry = original_apply

    # Real concurrency happened: both threads' run windows overlap.
    assert windows["A"][0] < windows["B"][1] and windows["B"][0] < windows["A"][1]
    # Reviewer-probe assertion: the shared index/aggregate applies must never
    # interleave across threads. Any cross-thread overlap is a live
    # read-modify-write race on the shared aggregates/indexes (the defect the
    # index lock closes); it reproduces on the unlocked code and is
    # impossible under the catalog-level index lock.
    overlaps: list[tuple[str, bool, str, bool, float]] = []
    for i, (s1, e1, th1, tx1) in enumerate(apply_windows):
        for j, (s2, e2, th2, tx2) in enumerate(apply_windows[i + 1 :], i + 1):
            if th1 != th2 and s1 < e2 and s2 < e1:
                overlaps.append((th1, tx1, th2, tx2, min(e1, e2) - max(s1, s2)))
    assert not overlaps, (
        "cross-thread _apply_index_entry overlap on the shared indexes/"
        f"aggregates ({len(overlaps)} of {len(apply_windows)} applies);"
        f" first: {overlaps[0]}"
    )
    # Every guard judgment equals the sequential expectation (no mis-approval).
    for tag in ("A", "B"):
        for version_id, activation in outcomes[tag].items():
            assert activation == expected[version_id], (tag, version_id, activation)
    # Indexes/aggregates equal a recompute from the final generation.
    v2 = catalog._catalog
    assert v2._contract_index == _recomputed_index(v2)
    assert v2._key_index == _recomputed_keys(v2)
    assert v2._aggregates == _recomputed_aggregates(v2)


def _recomputed_keys(catalog: RelationCatalogV2) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for identity, entry in catalog.store["generation"].items():
        payload = catalog.store["versions"][entry["version_id"]]["payload"]
        for key in _problem_observation_keys(payload):
            index.setdefault(key, set()).add(identity)
    return index


def _recomputed_aggregates(catalog: RelationCatalogV2) -> dict[str, object]:
    max_as_of: datetime | None = None
    min_release: datetime | None = None
    units: set[str] = set()
    for identity, entry in catalog.store["generation"].items():
        payload = catalog.store["versions"][entry["version_id"]]["payload"]
        problem = payload.get("problem")
        if not isinstance(problem, dict):
            continue
        as_of = problem.get("as_of")
        if not isinstance(as_of, str):
            continue
        as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        releases = [
            datetime.fromisoformat(str(atom["capital_release_at"]).replace("Z", "+00:00"))
            for state in problem.get("terminal_state_sets", ())
            if isinstance(state, dict)
            for atom in state.get("atoms", ())
            if isinstance(atom, dict) and atom.get("capital_release_at") is not None
        ]
        if not releases:
            continue
        max_as_of = as_of_dt if max_as_of is None else max(max_as_of, as_of_dt)
        min_release = (
            min(releases) if min_release is None else min(min_release, min(releases))
        )
        units.add(str(problem["valuation_unit_id"]))
    return {
        "max_as_of": max_as_of,
        "min_release": min_release,
        "units": frozenset(units),
    }


def test_r23_facade_approve_bumps_generation_once_per_write_transaction(
    tmp_path,
) -> None:
    """R2.3: facade approve/approve_many write transactions bump
    generation_number exactly once each — "every write transaction +1",
    symmetric with ingest/reject/revoke — and readonly_v2_relations' catalog
    generation component follows.
    """
    catalog = RelationCatalog(str(tmp_path / "gen"))
    first = [catalog.ingest(_r2_payload(tag))["version_id"] for tag in ("one", "two", "three")]
    assert catalog.generation_meta()["generation"] == 3  # three ingest txns
    for version_id in first:
        catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
    assert catalog.generation_meta()["generation"] == 6  # three single-approve txns

    second = [catalog.ingest(_r2_payload(tag))["version_id"] for tag in ("four", "five", "six")]
    assert catalog.generation_meta()["generation"] == 9
    catalog.approve_many(
        [{"version_id": version_id} for version_id in second],
        actor="op",
        git_sha="sha",
    )
    assert catalog.generation_meta()["generation"] == 10  # one batch txn bumps once

    from open_trader.prediction_n_leg_validation import readonly_v2_relations

    exported = readonly_v2_relations(catalog.path)
    assert exported["generation"] == 10
