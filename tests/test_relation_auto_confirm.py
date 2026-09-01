"""Issue #96 Tier 1 auto-confirm policy, round runner, and lifecycle tests.

Cases 6-9 cover the config-driven whitelist selection, its round runner
(error-rate breaker, all-blocked alert-only), and configuration fail-closed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from contextlib import contextmanager
import threading

from open_trader.prediction_service import create_prediction_server
from open_trader.relation_auto_confirm import (
    RelationAutoConfirmRunner,
    load_auto_confirm_policy,
    load_auto_confirm_policy_file,
    select_auto_confirm_items,
)
from open_trader.relation_catalog import RelationCatalog
from test_relation_catalog import compiled_relation_discovery
from test_prediction_arbitrage import threshold_relation


TIER1_NAME = "tier1-venue-metadata"

TIER1_MATCH = [
    {"discovery_source": "VENUE_METADATA", "relation_type": "NATIVE_COMPLEMENT"},
    {"discovery_source": "VENUE_METADATA", "relation_type": "EXACTLY_ONE"},
]

TIER1_POLICY_DOCUMENT = {
    "tiers": [
        {
            "name": TIER1_NAME,
            "mode": "active",
            "max_per_round": 4,
            "match": TIER1_MATCH,
        }
    ]
}


class StubNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.calls.append((title, message))


def venue_candidate(
    tag: str,
    *,
    relation_type: str = "NATIVE_COMPLEMENT",
    discovered_at: str = "2026-07-01T00:00:00Z",
    as_of: str = "2026-08-15T00:00:00Z",
    release: str = "2026-12-31T17:00:00Z",
) -> dict[str, object]:
    contract_ids = (
        [f"{tag}-yes", f"{tag}-no"]
        if relation_type == "NATIVE_COMPLEMENT"
        else [f"{tag}-a", f"{tag}-b", f"{tag}-c"]
    )
    sides = {contract_id: "BUY_YES" for contract_id in contract_ids}
    payload = compiled_relation_discovery(
        contract_ids, sides,
        relation_type=relation_type, as_of=as_of, release=release,
        rule=f"rules-{tag}",
    )
    payload["discovery_source"] = "VENUE_METADATA"
    payload["discovered_at"] = discovered_at
    return payload


def _drifted(payload: dict[str, object], statement: str) -> dict[str, object]:
    return {**payload, "semantics": {**payload["semantics"], "statement": statement}}


def tier1_policy(**overrides: object):
    document = {"tiers": [{**TIER1_POLICY_DOCUMENT["tiers"][0], **overrides}]}
    return load_auto_confirm_policy(document)


def approvals_by_version(catalog: RelationCatalog) -> dict[str, str]:
    connection = sqlite3.connect(catalog.path)
    try:
        rows = connection.execute(
            "SELECT version_id, actor FROM catalog_v2_approvals"
        ).fetchall()
    finally:
        connection.close()
    return {str(version_id): str(actor) for version_id, actor in rows}


def test_tier1_round_approves_only_whitelisted_matches_oldest_first(
    tmp_path: Path,
) -> None:
    """Case 6: one round approves exactly the Tier-1 matches — VENUE_METADATA
    x NATIVE_COMPLEMENT / EXACTLY_ONE pending items, oldest ``discovered_at``
    first truncated at ``max_per_round`` — inside a single transaction under
    the ``auto-confirm:<tier>`` actor; deterministic-rule IMPLIES candidates
    receive zero action."""
    catalog = RelationCatalog(tmp_path)
    survivor_id = catalog.ingest(venue_candidate("survivor-base"))["version_id"]
    approved = catalog.approve(survivor_id, {"version_id": survivor_id}, actor="op", git_sha="sha")
    assert approved["activation"] == "ACTIVE"  # already-clean generation

    pending = {
        "vn-old": catalog.ingest(venue_candidate(
            "vn-old", relation_type="NATIVE_COMPLEMENT", discovered_at="2026-07-01T00:00:00Z",
        ))["version_id"],
        "vn-mid": catalog.ingest(venue_candidate(
            "vn-mid", relation_type="NATIVE_COMPLEMENT", discovered_at="2026-07-02T00:00:00Z",
        ))["version_id"],
        "vn-new": catalog.ingest(venue_candidate(
            "vn-new", relation_type="NATIVE_COMPLEMENT", discovered_at="2026-07-03T00:00:00Z",
        ))["version_id"],
        "ve-old": catalog.ingest(venue_candidate(
            "ve-old", relation_type="EXACTLY_ONE", discovered_at="2026-06-30T00:00:00Z",
        ))["version_id"],
        "ve-late": catalog.ingest(venue_candidate(
            "ve-late", relation_type="EXACTLY_ONE", discovered_at="2026-07-04T00:00:00Z",
        ))["version_id"],
    }
    implies_id = catalog.ingest_threshold_relation(threshold_relation())["version_id"]

    policy = tier1_policy()
    assert policy.tiers[0].enabled
    expected_selected = [
        pending["ve-old"], pending["vn-old"], pending["vn-mid"], pending["vn-new"],
    ]
    selected = select_auto_confirm_items(
        catalog.list("pending_approval"), policy.tiers[0]
    )
    assert selected == expected_selected

    notifier = StubNotifier()
    runner = RelationAutoConfirmRunner(catalog, policy=policy, notifier=notifier)
    report = runner.run_round(git_sha="sha-ac")

    tier_report = report["tiers"][0]
    assert tier_report["tier"] == TIER1_NAME
    assert tier_report["actor"] == f"auto-confirm:{TIER1_NAME}"
    assert tier_report["selected"] == 4
    assert tier_report["active"] == 4
    assert tier_report["blocked"] == 0
    assert tier_report["error"] == 0
    assert tier_report["halted"] is False
    assert report["halted"] is False

    generation = catalog.current_generation()
    for version_id in expected_selected:
        identity = catalog.detail(version_id)["identity"]
        assert identity in generation
        assert generation[identity]["activation"] == "ACTIVE"
    assert catalog.detail(pending["ve-late"])["status"] == "PENDING"
    assert catalog.detail(pending["ve-late"])["identity"] not in generation
    assert catalog.detail(implies_id)["status"] == "PENDING"
    assert catalog.detail(implies_id)["identity"] not in generation

    actors = approvals_by_version(catalog)
    for version_id in expected_selected:
        assert actors[version_id] == f"auto-confirm:{TIER1_NAME}"
    assert notifier.calls == []


def test_error_rate_breaker_halts_tier_and_alert_only_blocked_rounds(
    tmp_path: Path,
) -> None:
    """Case 7: a round with unexpected-error share above the breaker threshold
    halts the tier (later rounds become no-ops) and notifies; an all-blocked
    round with zero unexpected errors does not halt but does notify."""
    # (a) unexpected errors -> halt, subsequent rounds are frozen no-ops
    catalog = RelationCatalog(tmp_path)
    base = venue_candidate("zombie", discovered_at="2026-06-01T00:00:00Z")
    zombie_id = catalog.ingest(base)["version_id"]
    # The superseding version must steal ``latest`` WITHOUT being a Tier-1
    # match itself (discovery_source never affects identity), otherwise it
    # would enter the round as a legitimately selectable row.
    superseding = _drifted(base, "superseding the zombie")
    superseding["discovery_source"] = "deterministic_rule"
    catalog.ingest(superseding)
    healthy_id = catalog.ingest(venue_candidate(
        "healthy", discovered_at="2026-07-01T00:00:00Z"
    ))["version_id"]

    notifier = StubNotifier()
    runner = RelationAutoConfirmRunner(catalog, policy=tier1_policy(), notifier=notifier)
    halt_report = runner.run_round(git_sha="sha-brk")

    tier_report = halt_report["tiers"][0]
    assert tier_report["selected"] == 2
    assert tier_report["error"] == 1  # the superseded zombie can never approve
    assert tier_report["active"] == 1
    assert tier_report["halted"] is True
    assert halt_report["halted"] is True
    assert TIER1_NAME in runner.halted_tiers
    assert len(notifier.calls) == 1
    assert TIER1_NAME in notifier.calls[0][0] + notifier.calls[0][1]
    assert "halt" in (notifier.calls[0][0] + notifier.calls[0][1]).lower()
    pending_after_halt = {row["version_id"] for row in catalog.list("pending_approval")}

    noop_report = runner.run_round(git_sha="sha-brk")
    assert noop_report["selected"] == 0
    assert noop_report["tiers"][0]["halted"] is True
    assert len(notifier.calls) == 1  # the freeze itself stays silent
    assert {row["version_id"] for row in catalog.list("pending_approval")} == pending_after_halt

    # (b) all-blocked, zero unexpected errors -> alert-only, tier keeps running
    pinned_catalog = RelationCatalog(tmp_path / "pinned")
    pinned_payload = venue_candidate("pin", release="2026-08-31T20:00:00Z")
    pinned_member_id = pinned_catalog.ingest(pinned_payload)["version_id"]
    approved = pinned_catalog.approve(
        pinned_member_id, {"version_id": pinned_member_id}, actor="op", git_sha="sha"
    )
    assert approved["activation"] == "ACTIVE"
    # Issue #110: a disjoint-timeline candidate would now approve, so the
    # blocked round uses the still-fatal supersession shape: a drifted
    # rediscovery of the pinned member itself (same identity, new version).
    drifted = _drifted(pinned_payload, "superseding the pinned member")
    late_candidate = pinned_catalog.ingest(drifted)["version_id"]

    block_notifier = StubNotifier()
    block_runner = RelationAutoConfirmRunner(
        pinned_catalog, policy=tier1_policy(max_per_round=4), notifier=block_notifier
    )
    blocked_report = block_runner.run_round(git_sha="sha-brk")
    blocked_tier = blocked_report["tiers"][0]
    assert blocked_tier["blocked"] == 1
    assert blocked_tier["error"] == 0
    assert blocked_tier["halted"] is False
    assert blocked_report["halted"] is False
    assert len(block_notifier.calls) == 1
    assert TIER1_NAME in block_notifier.calls[0][0] + block_notifier.calls[0][1]

    later_report = block_runner.run_round(git_sha="sha-brk")  # not halted; queue drained
    assert later_report["tiers"][0]["halted"] is False
    assert later_report["selected"] == 0
    assert len(block_notifier.calls) == 1  # a silent empty round alerts nobody
    assert TIER1_NAME not in block_runner.halted_tiers


def test_policy_configuration_error_disables_only_the_broken_tier(
    tmp_path: Path,
) -> None:
    """Case 9: a tier entry with an unknown ``mode`` (and one with a malformed
    ``match``) is fail-closed to disabled and its configuration error is
    surfaced on every round report; the valid tier still runs."""
    document = {
        "tiers": [
            {"name": "broken-mode", "mode": "pilot", "max_per_round": 100, "match": TIER1_MATCH},
            {
                "name": "broken-match",
                "mode": "active",
                "max_per_round": 100,
                "match": [{"discovery_source": "VENUE_METADATA"}],
            },
            {**TIER1_POLICY_DOCUMENT["tiers"][0], "name": "healthy"},
        ]
    }
    policy = load_auto_confirm_policy(document)
    by_name = {tier.name: tier for tier in policy.tiers}
    assert not by_name["broken-mode"].enabled
    assert not by_name["broken-match"].enabled
    assert by_name["healthy"].enabled
    assert any("mode" in error for error in by_name["broken-mode"].config_errors)
    assert any("relation_type" in error for error in by_name["broken-match"].config_errors)

    catalog = RelationCatalog(tmp_path)
    candidate_id = catalog.ingest(venue_candidate("valid-tier-item"))["version_id"]
    catalog.approve(candidate_id, {"version_id": candidate_id}, actor="op", git_sha="sha")
    # seed one pending Tier-1 item for the healthy tier
    pending_item = catalog.ingest(venue_candidate("pending-item"))["version_id"]

    runner = RelationAutoConfirmRunner(catalog, policy=policy, notifier=StubNotifier())
    report = runner.run_round(git_sha="sha-cfg")

    assert report["configuration_errors"] == [
        {"tier": "broken-mode", "errors": list(by_name["broken-mode"].config_errors)},
        {"tier": "broken-match", "errors": list(by_name["broken-match"].config_errors)},
    ]
    ran = [entry for entry in report["tiers"] if entry["tier"] == "healthy"]
    assert len(ran) == 1
    assert ran[0]["selected"] == 1
    assert ran[0]["active"] == 1
    assert all(entry["tier"] != "broken-mode" for entry in report["tiers"])
    assert all(entry["tier"] != "broken-match" for entry in report["tiers"])
    assert catalog.detail(pending_item)["identity"] in catalog.current_generation()


def _production_runtime(catalog: RelationCatalog, *, runner: object | None = None):
    from test_relation_catalog_service import _Runtime

    runtime = _Runtime(catalog)
    if runner is not None:
        runtime.relation_auto_confirm_runner = runner
    return runtime


@contextmanager
def running_with_runtime(runtime_obj):
    """``running()`` from the service harness wraps its own fresh ``_Runtime``,
    so runner-carrying runtimes need this variant that serves the given one."""
    server = create_prediction_server(
        runtime=runtime_obj,
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


def _http_view(base: str, view: str) -> tuple[set[str], dict[str, object]]:
    from test_relation_catalog_service import response as http_response

    status, page = http_response(
        f"{base}/api/prediction-arbitrage/relations?view={view}"
    )
    assert status == 200
    return {str(item["version_id"]) for item in page["items"]}, page


def _http_detail(base: str, version_id: str) -> dict[str, object]:
    from test_relation_catalog_service import response as http_response

    status, detail = http_response(
        f"{base}/api/prediction-arbitrage/relations/{version_id}"
    )
    assert status == 200
    return detail


def test_relations_endpoints_follow_the_approve_batch_contract(
    tmp_path: Path,
) -> None:
    """Case 8: ``auto-confirm-round``, ``revoke-batch`` and ``stale-reject``
    enforce the exact ``approve-batch`` confirm/error contract; with confirm
    they execute (round report, one-transaction revocation, bounded stale
    exit)."""
    from open_trader.relation_auto_confirm import RelationAutoConfirmRunner
    from test_relation_catalog_service import (
        mutation as http_mutation,
        response as http_response,
        running,
    )

    catalog = RelationCatalog(tmp_path)
    active_a = catalog.ingest(venue_candidate("revoke-a"))["version_id"]
    active_b = catalog.ingest(venue_candidate(
        "revoke-b", relation_type="EXACTLY_ONE"
    ))["version_id"]
    for version_id in (active_a, active_b):
        approved = catalog.approve(version_id, {"version_id": version_id}, actor="op", git_sha="sha")
        assert approved["activation"] == "ACTIVE"

    runner_policy = tier1_policy(max_per_round=10)
    runner = RelationAutoConfirmRunner(catalog, policy=runner_policy)

    base_payloads = [
        ("/api/prediction-arbitrage/relations/auto-confirm-round", {"confirm": False}),
        ("/api/prediction-arbitrage/relations/revoke-batch", {
            "items": [{"version_id": active_a}], "reason": "rules_changed", "confirm": False,
        }),
        ("/api/prediction-arbitrage/relations/stale-reject", {"confirm": False}),
        # approve-batch baseline under the identical conditions
        ("/api/prediction-arbitrage/relations/approve-batch", {
            "items": [{"version_id": active_a}], "confirm": False,
        }),
    ]
    empty_payloads = [
        "/api/prediction-arbitrage/relations/auto-confirm-round",
        "/api/prediction-arbitrage/relations/revoke-batch",
        "/api/prediction-arbitrage/relations/stale-reject",
        "/api/prediction-arbitrage/relations/approve-batch",
    ]
    with running_with_runtime(_production_runtime(catalog, runner=runner)) as base:
        for path, payload in base_payloads:
            status, denied = http_response(http_mutation(base, path, payload))
            assert status == 400, (path, payload)
            assert denied["message"] == "confirm must be true", (path, payload)
        for path in empty_payloads:
            status, denied = http_response(http_mutation(base, path, {}))
            assert status == 400
            assert denied["message"] != "confirm must be true", path

        # with confirm: one round report including required keys and tier name
        pending_id = catalog.ingest(venue_candidate("round-item"))["version_id"]
        status, report = http_response(http_mutation(
            base,
            "/api/prediction-arbitrage/relations/auto-confirm-round",
            {"confirm": True},
        ))
        assert status == 200
        for key in ("selected", "active", "blocked", "error", "halted"):
            assert key in report
        tiers = [entry["tier"] for entry in report["tiers"]]
        assert TIER1_NAME in tiers
        # Cross-thread state reads must go through HTTP: each thread keeps its
        # own committed store snapshot.
        assert _http_detail(base, pending_id)["activation"] == "ACTIVE"

        # with confirm: revoke-batch revokes listed actives in one transaction,
        # continuing past an inactive entry
        status, revoked = http_response(http_mutation(
            base,
            "/api/prediction-arbitrage/relations/revoke-batch",
            {
                "items": [{"version_id": active_a}, {"version_id": active_b}],
                "reason": "rules_changed",
                "note": "operator rollback",
                "confirm": True,
            },
        ))
        assert status == 200
        assert revoked["counts"]["revoked"] == 2
        assert _http_detail(base, active_a)["status"] == "REVOKED"
        assert _http_detail(base, active_b)["status"] == "REVOKED"
        activated_ids, _ = _http_view(base, "activated")
        # Mirrors single revoke: revoked members leave the ACTIVE review view.
        assert active_a not in activated_ids
        assert active_b not in activated_ids

        # with confirm: stale-reject exits zombies through the same contract
        zombie_base = venue_candidate("stale-zombie")
        zombie_pending_id = catalog.ingest(zombie_base)["version_id"]
        superseding = _drifted(zombie_base, "supersede the zombie")
        superseding["discovery_source"] = "deterministic_rule"
        catalog.ingest(superseding)
        status, stale = http_response(http_mutation(
            base,
            "/api/prediction-arbitrage/relations/stale-reject",
            {"confirm": True},
        ))
        assert status == 200
        assert stale["applied"] >= 1
        assert all(
            row["activation_diagnostic"] == "STALE_NON_LATEST"
            for row in stale["rejected"]
        )
        assert _http_detail(base, zombie_pending_id)["status"] == "REJECTED"
        pending_after, _ = _http_view(base, "pending_approval")
        assert zombie_pending_id not in pending_after


def test_lifecycle_coordinator_rotates_then_confirms_and_isolates_failures(
    tmp_path: Path,
) -> None:
    """Wiring seam: one coordinator call expires the stale generation first,
    then runs the auto-confirm round against the rotated generation; an
    expiry failure is recorded and must not stop the auto-confirm step."""
    from open_trader.relation_auto_confirm import run_relation_lifecycle

    class FailingExpiryCatalog:
        def __init__(self, inner: RelationCatalog) -> None:
            self._inner = inner

        def expire_stale_members(self, **kwargs):
            raise RuntimeError("simulated expiry outage")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    catalog = RelationCatalog(tmp_path)
    stale_id = catalog.ingest(compiled_relation_discovery(
        ["lc-stale-a", "lc-stale-b"],
        {"lc-stale-a": "BUY_YES", "lc-stale-b": "BUY_YES"},
        as_of="2026-08-15T00:00:00Z",
        release="2026-08-31T20:00:00Z",
        rule="rules-lc",
    ))["version_id"]
    catalog.approve(stale_id, {"version_id": stale_id}, actor="op", git_sha="sha")
    candidate_id = catalog.ingest(venue_candidate(
        "lc-candidate", discovered_at="2026-07-01T00:00:00Z",
        as_of="2026-11-01T00:00:00Z", release="2026-12-24T17:00:00Z",
    ))["version_id"]

    runner = RelationAutoConfirmRunner(catalog, policy=tier1_policy())
    report = run_relation_lifecycle(
        catalog, runner,
        clock=lambda: "2026-09-15T00:00:00Z",
        git_sha="sha-lc",
    )

    assert report["expiry"]["dropped"][0]["version_id"] == stale_id
    assert report["auto_confirm"]["tiers"][0]["active"] >= 1
    assert _identity_of(catalog, candidate_id) in catalog.current_generation()

    isolated = run_relation_lifecycle(
        FailingExpiryCatalog(catalog), runner,
        clock=lambda: "2026-09-16T00:00:00Z",
        git_sha="sha-lc2",
    )
    assert "expiry_error" in isolated
    assert "auto_confirm" in isolated


def test_policy_file_with_a_broken_only_tier_still_wires_the_runner(
    tmp_path: Path,
) -> None:
    """Repair round 1 finding 2: a policy file whose only tier carries an
    invalid mode still wires the runner, so its configuration error surfaces
    on the manual round report instead of Tier 1 silently going dark; only a
    missing file switches the feature off entirely."""
    from open_trader.prediction_runtime import PredictionRuntime

    class RecordingMonitor:
        def __init__(self) -> None:
            self.observers: list[object] = []

        def set_relation_lifecycle_observer(self, observer: object) -> None:
            self.observers.append(observer)

    def wired_runtime(config_dir: Path, monitor: RecordingMonitor, catalog: RelationCatalog):
        config_dir.mkdir(exist_ok=True)
        config_path = config_dir / "prediction_production.toml"
        config_path.write_text("", encoding="utf-8")
        runtime = PredictionRuntime(
            data_dir=config_dir / "data",
            prediction_config_path=config_path,
            dashboard_url="http://127.0.0.1:9",
            # never started; production start() assigns catalog/monitor
            # before calling _wire_relation_lifecycle — mirrored below
            mode="shadow",
            git_sha="sha-wire",
        )
        runtime.relation_catalog = catalog
        runtime.monitor = monitor
        return runtime

    broken_document = {
        "tiers": [
            {
                "name": TIER1_NAME,
                "mode": "pilot",  # unknown mode → fail-closed disabled
                "max_per_round": 100,
                "match": TIER1_MATCH,
            }
        ]
    }
    wired_dir = tmp_path / "wired"
    # The policy lives next to the prediction config file, as in production.
    wired_config_dir = wired_dir / "config"
    wired_config_dir.mkdir(parents=True)
    (wired_config_dir / "relation_auto_confirm.json").write_text(
        json.dumps(broken_document), encoding="utf-8"
    )
    catalog = RelationCatalog(wired_dir / "catalog")
    item_id = catalog.ingest(venue_candidate("wired-item"))["version_id"]
    monitor = RecordingMonitor()
    runtime = wired_runtime(wired_dir / "config", monitor, catalog)

    runtime._wire_relation_lifecycle()

    runner = getattr(runtime, "relation_auto_confirm_runner", None)
    assert runner is not None  # zero enabled tiers must not unwire the feature
    assert len(monitor.observers) == 1

    report = runner.run_round(git_sha="sha-wire")
    assert len(report["configuration_errors"]) == 1
    error_entry = report["configuration_errors"][0]
    assert error_entry["tier"] == TIER1_NAME
    assert any("unknown mode" in message for message in error_entry["errors"])
    assert report["tiers"] == []
    assert report["selected"] == 0
    assert report["halted"] is False
    assert catalog.detail(item_id)["status"] == "PENDING"  # nothing acted silently

    bare_dir = tmp_path / "bare" / "config"
    bare_monitor = RecordingMonitor()
    bare_runtime = wired_runtime(
        bare_dir, bare_monitor, RelationCatalog(tmp_path / "bare" / "catalog")
    )
    bare_runtime._wire_relation_lifecycle()
    assert getattr(bare_runtime, "relation_auto_confirm_runner", None) is None
    assert bare_monitor.observers == []  # missing file = feature off


def test_concurrent_rounds_approve_each_selection_exactly_once(
    tmp_path: Path,
) -> None:
    """Repair round 2 finding 2: a manual ``auto-confirm-round`` overlapping
    the monitor observer's round must not double-select the same pending
    items — both callers select them, the loser's approvals all return
    "no longer pending", and a 1.0 unexpected-error rate permanently halts the
    tier and pages from a benign overlap. Rounds are serialized instead: one
    approves each selected item exactly once, the other sees the drained
    selection as a normal empty report, and no halt or page occurs."""
    catalog = RelationCatalog(tmp_path)
    expected = [
        catalog.ingest(venue_candidate(
            f"race-{index}",
            discovered_at=f"2026-07-0{index + 1}T00:00:00Z",
        ))["version_id"]
        for index in range(4)
    ]

    notifier = StubNotifier()
    runner = RelationAutoConfirmRunner(
        catalog, policy=tier1_policy(max_per_round=10), notifier=notifier
    )
    reports: list[dict[str, object]] = []
    failures: list[BaseException] = []
    start = threading.Barrier(2)

    def worker() -> None:
        try:
            start.wait(timeout=10)
            reports.append(runner.run_round(git_sha="sha-race"))
        except BaseException as exc:  # surfaced below, never swallowed
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    assert len(reports) == 2

    # No breaker trip anywhere: neither report halted, tier still runnable.
    assert all(report["halted"] is False for report in reports)
    assert runner.halted_tiers == frozenset()
    assert notifier.calls == []

    # Combined accounting equals exactly one approval per selected item —
    # no double-approve errors from the overlap.
    assert sum(int(report["selected"]) for report in reports) == len(expected)
    assert sum(int(report["active"]) for report in reports) == len(expected)
    assert sum(int(report["blocked"]) for report in reports) == 0
    assert sum(int(report["error"]) for report in reports) == 0

    # Exactly one approval row per version, all under the tier actor.
    actors = approvals_by_version(catalog)
    assert sorted(actors) == sorted(expected)
    assert set(actors.values()) == {f"auto-confirm:{TIER1_NAME}"}

    # Every selected item is ACTIVE through a fresh cross-thread reader.
    reader = RelationCatalog(tmp_path)
    generation = reader.current_generation()
    for version_id in expected:
        identity = reader.detail(version_id)["identity"]
        assert generation[identity]["activation"] == "ACTIVE"


def test_policy_file_with_invalid_utf8_loads_disabled_with_config_error(
    tmp_path: Path,
) -> None:
    """Repair round 2 finding 3: a binary-corrupted policy file (invalid
    UTF-8) fails closed exactly like invalid JSON — the tier loads disabled
    with a surfaced configuration error instead of the decode error escaping
    ``_wire_relation_lifecycle`` and taking the prediction runtime down at
    boot. The empty-file reading stays feature-off."""
    policy_path = tmp_path / "relation_auto_confirm.json"
    policy_path.write_bytes(b"\x80\x81\xff broken binary payload")

    policy = load_auto_confirm_policy_file(policy_path)  # must not raise

    assert len(policy.tiers) == 1
    tier = policy.tiers[0]
    assert tier.enabled is False
    assert any("UTF-8" in error for error in tier.config_errors)
    assert policy.configuration_errors()  # surfaced on every round report


def _identity_of(catalog: RelationCatalog, version_id: str) -> str:
    return catalog.detail(version_id)["identity"]
