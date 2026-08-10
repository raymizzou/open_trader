from __future__ import annotations

from pathlib import Path

import pytest

from open_trader import prediction_shadow_validation as validation


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
    states = [_state(completed_at=f"2026-08-10T00:0{index}:00Z") for index in range(3)]
    if case == "profit_formula_drift":
        states[-1] = _state(profit="1.10", completed_at="2026-08-10T00:02:00Z")
    if case == "deadline_before_three_cycles":
        states = states[:1]
    health = {
        "status": "running", "mode": "shadow", "mutations": "prohibited",
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
    calls = {"state": 0}

    def fetch(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/healthz"):
            return health
        if url.endswith("/state"):
            index = min(calls["state"] // 2, len(states) - 1)
            calls["state"] += 1
            if case == "profit_formula_drift" and url.startswith("http://127.0.0.1:8769") and index == 2:
                return states[index]
            if case == "profit_formula_drift" and index == 2:
                return _state(completed_at="2026-08-10T00:02:00Z")
            return states[index]
        return {"kind": "signals", "items": [{"isolated": url.startswith("http://127.0.0.1:8769")}], "total": 1}

    monotonic = iter(range(100))
    monkeypatch.setattr(validation, "seed_shadow_store", lambda **_kwargs: {"sha256": "seed", "relation_state_rows": 1, "llm_cache_rows": 1})
    monkeypatch.setattr(validation, "_install_shadow", lambda **_kwargs: {"pid": 101, "cwd": "repo", "git_sha": "sha"})
    monkeypatch.setattr(validation, "_restart_shadow", lambda **_kwargs: {"pid": 102, "cwd": "repo", "git_sha": "sha"})
    monkeypatch.setattr(validation, "_uninstall_and_verify_absent", lambda **_kwargs: {"label_absent": True, "plist_absent": True, "listener_absent": True})
    monkeypatch.setattr(validation, "_fetch_json", fetch)
    monkeypatch.setattr(validation, "_git_sha", lambda _repo: "sha")
    monkeypatch.setattr(validation.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(validation.time, "sleep", lambda _seconds: None)

    return validation.run_shadow_validation(
        repo_root=tmp_path / "repo",
        source_data_dir=tmp_path / "source",
        runtime_root=tmp_path / "runtime",
        prediction_config_path=tmp_path / "config.json",
        legacy_url="http://127.0.0.1:8767",
        shadow_url="http://127.0.0.1:8769",
        timeout_seconds=2 if case == "deadline_before_three_cycles" else 4,
    )
