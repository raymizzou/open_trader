from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

from open_trader import prediction_shadow_validation as validation
from open_trader.prediction_read_model import prediction_state_payload
from tests.test_prediction_service import _Runtime, _response, _server


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
    shadow["opportunities"] = [dict(shadow["opportunities"][0], actionable=1)]  # type: ignore[index]
    shadow["relation_discovery"] = {"activity": []}

    differences = validation._compare_live_states(_state(), shadow)

    assert {item["classification"] for item in differences} == {"semantic_difference"}
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
def _legacy_endpoint(runtime: _Runtime) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            payload = prediction_state_payload(
                store=runtime.store, monitor=runtime.monitor, execution=runtime.execution,
                csrf_token="legacy-session", cross_venue_monitor=runtime.cross_venue_monitor,
            )
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_frozen_legacy_and_shadow_http_endpoints_match_except_session() -> None:
    runtime = _Runtime()
    with _legacy_endpoint(runtime) as legacy_base, _server(runtime) as shadow_base:
        legacy_status, legacy = _response(legacy_base + "/api/prediction-arbitrage/state")
        status, shadow = _response(shadow_base + "/api/prediction-arbitrage/state")

    assert legacy_status == status == 200
    assert validation._compare_frozen_payloads(legacy, shadow) == []


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


def test_deadline_still_fails_when_observed_deepseek_calls_are_nonzero() -> None:
    status, _reason = validation._validation_status(
        semantic=[], health={}, activity=set(),
        codex={"same_venue": {"attempts": 0, "successes": 0}, "cross_venue": {"attempts": 0, "successes": 0}},
        deepseek_calls=1, deadline=True,
    )

    assert status == "FAIL"


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
        states[-1] = _state(profit="1.10", completed_at="2026-08-10T00:02:00Z")
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
                return {**health, "mode": "legacy"}
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
