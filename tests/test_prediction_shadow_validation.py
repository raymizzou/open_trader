from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

from open_trader import prediction_shadow_validation as validation
from open_trader.dashboard_web import create_dashboard_server
from tests.test_prediction_service import _Runtime, _response, _server
from tests.test_dashboard import dashboard_config


def _state(*, profit: str = "1.20", completed_at: str = "2026-08-10T00:00:00Z") -> dict[str, object]:
    return {
        "opportunities": [{
            "opportunity_id": "shared-1",
            "venue": "polymarket",
            "market_type": "threshold",
            "relation_direction": "A_IMPLIES_B",
            "legs": [{"market_id": "a", "side": "YES"}, {"market_id": "b", "side": "NO"}],
            "max_cost": "8.80",
            "fee": "0.00",
            "actionable": True,
            "eligibility_reason": "eligible",
            "profit": profit,
        }],
        "relation_discovery": {
            "activity": {"completed_at": completed_at},
            "codex_usage_24h": {"input_tokens": 3, "output_tokens": 2},
        },
        "heartbeat_at": completed_at,
        "csrf_token": "different-session",
    }


def test_compare_live_states_allows_process_and_time_differences() -> None:
    legacy = _state(completed_at="2026-08-10T00:00:00Z")
    shadow = _state(completed_at="2026-08-10T00:05:00Z")

    differences = validation._compare_live_states(legacy, shadow)

    assert [difference["classification"] for difference in differences] == [
        "isolated_state_difference"
    ]


def test_compare_live_states_classifies_sparse_operational_subtrees_as_isolated() -> None:
    legacy = _state()
    shadow = _state()
    legacy.update({
        "health": {"status": "healthy", "counter": 3},
        "venues": [{"venue": "polymarket", "status": "ready"}],
        "cross_venue": {
            "status": "ready",
            "funnel": {"matched_pairs": 2},
            "opportunities": [],
        },
    })
    shadow.update({
        "health": {},
        "venues": [],
        "cross_venue": {"status": "loading", "opportunities": []},
        "relation_discovery": {"activity": {"completed_at": ""}},
    })

    differences = validation._compare_live_states(legacy, shadow)

    assert {item["classification"] for item in differences} == {"isolated_state_difference"}
    assert {item.get("field") for item in differences} >= {"health", "venues", "cross_venue", "relation_discovery"}


def test_compare_live_states_flags_operational_type_shape_corruption() -> None:
    legacy = _state()
    shadow = _state()
    legacy["health"] = {"status": "healthy", "counter": 3}
    shadow["health"] = "healthy"

    differences = validation._compare_live_states(legacy, shadow)

    assert "semantic_difference" in {item["classification"] for item in differences}
    assert any(item.get("field") == "health" for item in differences)


def test_compare_live_states_allows_nullable_operational_values() -> None:
    legacy = _state()
    shadow = _state()
    legacy["venues"] = [{"venue": "polymarket", "last_success": "2026-08-10T00:00:00Z"}]
    shadow["venues"] = [{"venue": "polymarket", "last_success": None}]

    differences = validation._compare_live_states(legacy, shadow)

    assert {item["classification"] for item in differences} == {"isolated_state_difference"}


def test_compare_live_states_flags_deterministic_profit_formula_drift() -> None:
    differences = validation._compare_live_states(_state(), _state(profit="1.10"))

    assert differences == [{
        "classification": "semantic_difference",
        "opportunity_id": "shared-1",
        "field": "profit",
        "legacy": "1.20",
        "shadow": "1.10",
    }]


def test_compare_live_states_flags_recursive_schema_and_strict_type_drift() -> None:
    shadow = _state()
    shadow["opportunities"] = [
        dict(shadow["opportunities"][0], actionable=1, legs=[{"market_id": "a"}])
    ]  # type: ignore[index]

    differences = validation._compare_live_states(_state(), shadow)

    assert "semantic_difference" in {item["classification"] for item in differences}
    assert any(str(item["field"]).endswith("actionable") for item in differences)
    assert any(item["field"] == "schema" for item in differences)


def test_compare_live_states_never_uses_opportunity_array_position_for_schema() -> None:
    shadow = _state()
    shadow["opportunities"] = [{"opportunity_id": "sampled-only"}, *shadow["opportunities"]]  # type: ignore[operator]

    differences = validation._compare_live_states(_state(), shadow)

    assert differences == [{"classification": "sampling_difference", "opportunity_id": "sampled-only", "side": "shadow"}]


def test_compare_histories_classifies_isolated_contents() -> None:
    differences = validation._compare_histories(
        {"kind": "signals", "items": [{"signal_id": "legacy"}], "total": 1},
        {"kind": "signals", "items": [{"signal_id": "shadow"}], "total": 1},
    )

    assert differences == [{"classification": "isolated_state_difference", "field": "history.items"}]


def test_history_item_schema_drift_is_semantic_not_isolated() -> None:
    differences = validation._compare_histories(
        {"kind": "signals", "items": [{"signal_id": "same", "profit": "1"}], "total": 1},
        {"kind": "signals", "items": [{"signal_id": "same", "profit": 1}], "total": 1},
    )

    assert any(item["classification"] == "semantic_difference" for item in differences)


def test_shared_opportunity_missing_field_is_semantic_not_equal_to_none() -> None:
    shadow = _state()
    del shadow["opportunities"][0]["fee"]  # type: ignore[index]

    differences = validation._compare_live_states(_state(), shadow)

    assert any(item["classification"] == "semantic_difference" and item["field"] == "fee" for item in differences)


@contextmanager
def _legacy_endpoint(runtime: _Runtime, tmp_path: Path) -> Iterator[str]:
    server = create_dashboard_server(
        config=dashboard_config(tmp_path),
        host="127.0.0.1",
        port=0,
        prediction_store=runtime.store,
        prediction_monitor=runtime.monitor,
        prediction_execution_service=runtime.execution,
        cross_venue_monitor=runtime.cross_venue_monitor,
        prediction_session_token="legacy-session",
        prediction_csrf_token="legacy-csrf",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_frozen_legacy_and_shadow_http_endpoints_match_except_session(tmp_path: Path) -> None:
    runtime = _Runtime()
    with _legacy_endpoint(runtime, tmp_path) as legacy_base, _server(runtime) as shadow_base:
        legacy_state_status, legacy_state = _response(legacy_base + "/api/prediction-arbitrage/state")
        shadow_state_status, shadow_state = _response(shadow_base + "/api/prediction-arbitrage/state")
        legacy_history_status, legacy_history = _response(
            legacy_base + "/api/prediction-arbitrage/history?kind=signals&limit=100&offset=0"
        )
        shadow_history_status, shadow_history = _response(
            shadow_base + "/api/prediction-arbitrage/history?kind=signals&limit=100&offset=0"
        )

    assert legacy_state_status == shadow_state_status == 200
    assert legacy_history_status == shadow_history_status == 200
    assert validation._compare_frozen_payloads(legacy_state, shadow_state) == []
    assert validation._compare_frozen_payloads(legacy_history, shadow_history) == []


def test_install_shadow_passes_remaining_timeout_and_returns_verified_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[object] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append((command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 0, "installed", "")

    monkeypatch.setattr(validation.subprocess, "run", run)
    monkeypatch.setattr(validation.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(validation, "_service_evidence", lambda **_kwargs: {
        "pid": 42, "cwd": "repo", "git_sha": "sha", "label": "com.open-trader.prediction-service",
        "plist": "plist", "listener": "127.0.0.1:8769", "health": {"status": "running"},
    })

    evidence = validation._install_shadow(
        repo_root=tmp_path, runtime_root=tmp_path / "runtime",
        prediction_config_path=tmp_path / "config", deadline=25.0,
    )

    assert observed == [([
        str(tmp_path / "scripts/install_prediction_service_launchd.sh"), "--runtime-root", str(tmp_path / "runtime"),
        "--repo-root", str(tmp_path), "--python", validation.sys.executable, "--config", str(tmp_path / "config"),
        "--wait-seconds", "25",
    ], 25.0)]
    assert evidence["pid"] == 42
    assert evidence["health"] == {"status": "running"}


def test_sigterm_uses_cleanup_and_writes_report_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers: dict[int, object] = {}
    events: list[str] = []

    monkeypatch.setattr(validation.signal, "getsignal", lambda signum: f"old-{signum}")
    monkeypatch.setattr(validation.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    monkeypatch.setattr(validation, "_seed_shadow", lambda **_kwargs: handlers[signal.SIGTERM](signal.SIGTERM, None))
    monkeypatch.setattr(validation, "_uninstall_and_verify_absent", lambda **_kwargs: events.append("cleanup") or {"label_absent": True, "plist_absent": True, "listener_absent": True})
    monkeypatch.setattr(validation, "_write_report", lambda _path, report: events.append(f"report:{report['status']}") )
    monkeypatch.setattr(validation, "_git_sha", lambda _repo, **_kwargs: "sha")

    report = validation.run_shadow_validation(
        repo_root=tmp_path, source_data_dir=tmp_path / "source", runtime_root=tmp_path / "runtime",
        prediction_config_path=tmp_path / "config", legacy_url="http://127.0.0.1:8767",
        shadow_url="http://127.0.0.1:8769", timeout_seconds=10,
    )

    assert report["status"] == "BLOCKED"
    assert events == ["report:BLOCKED"]
    assert report["shutdown"]["cleanup_skipped"] == "not_owner"
    assert handlers == {signal.SIGINT: f"old-{signal.SIGINT}", signal.SIGTERM: f"old-{signal.SIGTERM}"}


def test_deadline_carries_deepseek_provider_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run_fake_validation("deadline_before_three_cycles", tmp_path, monkeypatch)

    assert report["provider_evidence"]["delta"]["deepseek"]["calls"] == 0
    assert report["status"] == "BLOCKED"


def test_usage_baseline_reads_persistent_seeded_store_before_install(tmp_path: Path) -> None:
    database = tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE llm_usage (usage_id TEXT, kind TEXT, status TEXT, payload TEXT, created_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO llm_usage VALUES (?, ?, ?, ?, ?)",
            [
                ("codex-1", "call", "success", json.dumps({"provider": "codex", "input_tokens": 11, "cached_input_tokens": 2, "output_tokens": 7, "reasoning_output_tokens": 3}), "2099-01-01T00:00:00Z"),
                ("deepseek-1", "call", "failed", json.dumps({"provider": "deepseek", "input_tokens": 5, "output_tokens": 4}), "2099-01-01T00:00:00Z"),
                ("cache-1", "cache_hit", "success", json.dumps({"provider": "deepseek"}), "2099-01-01T00:00:00Z"),
            ],
        )
        connection.commit()

    providers, tokens = validation._llm_usage_baseline(tmp_path)

    assert providers["codex"]["calls"] == 1
    assert providers["codex"]["input_tokens"] == 11
    assert providers["deepseek"]["calls"] == 1
    assert providers["deepseek"]["output_tokens"] == 4
    assert tokens == {
        "input_tokens": 16,
        "cached_input_tokens": 2,
        "output_tokens": 11,
        "reasoning_output_tokens": 3,
    }


def test_startup_provider_and_token_usage_is_retained_in_run_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_baseline = {
        "codex": {"calls": 3, "input_tokens": 8, "output_tokens": 5},
        "deepseek": {"calls": 1, "input_tokens": 4, "output_tokens": 2},
    }
    provider_current = {
        "codex": {"calls": 3, "input_tokens": 8, "output_tokens": 5},
        "deepseek": {"calls": 2, "input_tokens": 7, "output_tokens": 4},
    }
    monkeypatch.setattr(
        validation, "_llm_usage_baseline",
        lambda _runtime_root: (provider_baseline, {"input_tokens": 8, "output_tokens": 5}),
    )
    monkeypatch.setattr(validation, "_provider_evidence", lambda _state: provider_current)
    monkeypatch.setattr(validation, "_token_counts", lambda _state: {"input_tokens": 13, "output_tokens": 9})

    report = _run_fake_validation("timestamp_only", tmp_path, monkeypatch)

    assert report["status"] == "FAIL"
    assert report["provider_evidence"]["delta"]["deepseek"]["calls"] == 1
    assert report["token_counts"]["delta"] == {"input_tokens": 5, "output_tokens": 4}


def test_deadline_still_fails_when_observed_deepseek_calls_are_nonzero() -> None:
    status, _reason = validation._validation_status(
        semantic=[], health={}, activity=set(),
        codex={"same_venue": {"attempts": 0, "successes": 0}, "cross_venue": {"attempts": 0, "successes": 0}},
        deepseek_calls=1, deadline=True,
    )

    assert status == "FAIL"


def test_live_validation_does_not_require_cli_frozen_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_fake_validation("timestamp_only", tmp_path, monkeypatch)

    assert report["status"] == "PASS"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("timestamp_only", "PASS"),
        ("isolated_history", "PASS"),
        ("profit_formula_drift", "FAIL"),
        ("missing_same_venue_canary", "BLOCKED"),
        ("all_cross_canaries_failed", "BLOCKED"),
        ("mutation_attempt", "FAIL"),
        ("deadline_before_three_cycles", "BLOCKED"),
    ],
)
def test_shadow_validation_outcomes(
    case: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_fake_validation(case, tmp_path, monkeypatch)

    assert report["status"] == expected


def test_shadow_validation_report_has_required_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_fake_validation("timestamp_only", tmp_path, monkeypatch)

    assert set(report) >= {
        "status", "reason", "started_at", "ended_at", "urls", "git_sha", "seed",
        "cycles", "allowed_differences", "semantic_differences", "codex", "token_counts",
        "guard_attempts", "restart", "shutdown",
    }
    assert report["urls"] == {
        "legacy": "http://127.0.0.1:8767",
        "shadow": "http://127.0.0.1:8769",
    }
    assert (tmp_path / "runtime" / "shadow-validation.json").exists()


def _run_fake_validation(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    states = [_state(completed_at=f"2026-08-10T00:0{index}:00Z") for index in range(4)]
    if case == "profit_formula_drift":
        states[2] = _state(profit="1.10", completed_at="2026-08-10T00:02:00Z")
    if case == "deadline_before_three_cycles":
        states = states[:1]
    health = {
        "status": "running", "mode": "shadow", "production_owner": False, "mutations": "prohibited",
        "first_violation": {"kind": "mutation"} if case == "mutation_attempt" else None,
        "guard_attempts": [{"kind": "mutation"}] if case == "mutation_attempt" else [],
        "codex": {
            "relation": {"calls": 0 if case == "missing_same_venue_canary" else 3, "successes": 1},
            "cross_venue": {
                "calls": 3,
                "successes": 0 if case == "all_cross_canaries_failed" else 1,
            },
        },
    }
    calls = {"state": 0, "shadow_health": 0, "baseline_state": True}

    def fetch(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/healthz"):
            if url.startswith("http://127.0.0.1:8767"):
                return {**health, "mode": "legacy", "schema_version": "open_trader.legacy_dashboard.health.v1", "module": "legacy_dashboard"}
            if url.startswith("http://127.0.0.1:8769"):
                calls["shadow_health"] += 1
                if calls["shadow_health"] == 1:
                    return {**health, "codex": {"relation": {"calls": 0, "successes": 0}, "cross_venue": {"calls": 0, "successes": 0}}}
            return health
        if url.endswith("/state"):
            if url.startswith("http://127.0.0.1:8769") and calls["baseline_state"]:
                calls["baseline_state"] = False
                calls["state"] += 1
                return states[0]
            index = min(calls["state"] // 2, len(states) - 1)
            calls["state"] += 1
            if case == "profit_formula_drift" and url.startswith("http://127.0.0.1:8769") and index == 2:
                return states[index]
            if case == "profit_formula_drift" and index == 2:
                return _state(completed_at="2026-08-10T00:02:00Z")
            return states[index]
        return {"kind": "signals", "items": [{"isolated": url.startswith("http://127.0.0.1:8769")}], "total": 1}

    clock = [0.0]
    monkeypatch.setattr(validation, "_seed_shadow", lambda **_kwargs: {"sha256": "seed", "relation_state_rows": 1, "llm_cache_rows": 1})
    monkeypatch.setattr(validation, "_install_shadow", lambda **_kwargs: {"pid": 101, "cwd": "repo", "git_sha": "sha"})
    monkeypatch.setattr(validation, "_restart_shadow", lambda **_kwargs: {"pid": 102, "cwd": "repo", "git_sha": "sha"})
    monkeypatch.setattr(validation, "_uninstall_and_verify_absent", lambda **_kwargs: {"label_absent": True, "plist_absent": True, "listener_absent": True})
    monkeypatch.setattr(validation, "_fetch_json", fetch)
    monkeypatch.setattr(validation, "_git_sha", lambda _repo, **_kwargs: "sha")
    monkeypatch.setattr(validation.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(validation.time, "sleep", lambda _seconds: clock.__setitem__(0, clock[0] + 1))

    return validation.run_shadow_validation(
        repo_root=tmp_path / "repo",
        source_data_dir=tmp_path / "source",
        runtime_root=tmp_path / "runtime",
        prediction_config_path=tmp_path / "config.json",
        legacy_url="http://127.0.0.1:8767",
        shadow_url="http://127.0.0.1:8769",
        timeout_seconds=2 if case == "deadline_before_three_cycles" else 4,
    )
