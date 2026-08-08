from __future__ import annotations

import copy
import json
from pathlib import Path

import scripts.validate_trend_candidate_replay as replay_validator
from scripts.validate_trend_candidate_replay import (
    _selected_evidence,
    compare_reports,
)


def _report() -> dict[str, object]:
    return {
        "signal_snapshots": {
            "candidates": [
                {"symbol": "QUALIFIED", "eligible": True},
                {"symbol": "SKIPPED", "eligible": False},
            ]
        },
        "strategy_judgments": {
            "holding_decisions": [
                {"symbol": "HELD", "action": "HOLD", "reason": "trend_intact"}
            ],
            "formal_actions": [
                {"symbol": "EXIT", "action": "SELL_ALL", "reason": "danger"}
            ],
            "simulate_rotation_comparisons": [
                {
                    "pair_index": 0,
                    "strength_basis": "local",
                    "threshold": "20",
                    "outcome": "planned",
                }
            ],
        },
        "protection_state": {
            "schema_version": 1,
            "positions": {"HELD": {"active_line": "10"}},
        },
        "risk_summary": {
            "risk_formula": "atr14 * 2",
            "portfolio_risk_limit": "100",
            "single_entry_risk_limit": "10",
            "normal_cost_rate": "0.001",
        },
    }


def test_compare_reports_allows_candidate_order_buy_priority_audit_cost_and_metadata() -> None:
    old = _report()
    new = copy.deepcopy(old)
    candidates = new["signal_snapshots"]["candidates"]  # type: ignore[index]
    assert isinstance(candidates, list)
    candidates.reverse()
    new["strategy_judgments"]["formal_actions"] = [  # type: ignore[index]
        {"symbol": "EXIT", "action": "SELL_ALL", "reason": "danger"},
        {"symbol": "NEW", "action": "BUY", "target_weight": "0.06"}
    ]
    new["strategy_judgments"]["risk_skips"] = [  # type: ignore[index]
        {"symbol": "SKIPPED", "reason": "missing global strength"}
    ]
    new["strategy_snapshot"] = {"strategy_version": "v13", "process_version": "new"}
    new["estimated_api_cost"] = "0.7"
    new["actual_api_cost"] = "0.6"
    new["generated_at"] = "2026-08-08T00:00:00+08:00"
    new["report_sha256"] = "b" * 64

    assert compare_reports(old, new).errors == ()


def test_compare_reports_rejects_changed_discipline_qualified_set() -> None:
    old = _report()
    new = copy.deepcopy(old)
    new["signal_snapshots"]["candidates"] = [  # type: ignore[index]
        {"symbol": "OTHER", "eligible": True},
        {"symbol": "SKIPPED", "eligible": False},
    ]

    assert compare_reports(old, new).errors == ("discipline_qualified_set changed",)


def test_compare_reports_rejects_frozen_decisions_and_contracts() -> None:
    fields = (
        ("holding_decisions", ("strategy_judgments", "holding_decisions"), {"action": "SELL_ALL"}),
        ("exit", ("strategy_judgments", "formal_actions"), {"reason": "left_right_side"}),
        ("protection_state", ("protection_state",), {"schema_version": 2}),
        ("risk_formula_or_limit", ("risk_summary",), {"risk_formula": "close * 3"}),
    )
    for label, path, changes in fields:
        old = _report()
        new = copy.deepcopy(old)
        target: object = new
        for key in path[:-1]:
            assert isinstance(target, dict)
            target = target[key]
        if path[-1] == "protection_state":
            assert isinstance(target, dict)
            target[path[-1]] = {**target[path[-1]], **changes}  # type: ignore[index]
        elif path[-1] == "risk_summary":
            assert isinstance(target, dict)
            target[path[-1]] = {**target[path[-1]], **changes}  # type: ignore[index]
        else:
            assert isinstance(target, dict)
            rows = target[path[-1]]
            assert isinstance(rows, list)
            rows[0] = {**rows[0], **changes}
        errors = compare_reports(old, new).errors
        assert errors, label


def test_compare_reports_rejects_rotation_threshold_or_basis() -> None:
    old = _report()
    new = copy.deepcopy(old)
    new["strategy_judgments"]["simulate_rotation_comparisons"][0]["threshold"] = (
        "25"
    )  # type: ignore[index]
    assert compare_reports(old, new).errors == ("rotation_threshold_or_basis changed",)

    new = copy.deepcopy(old)
    new["strategy_judgments"]["simulate_rotation_comparisons"][0]["strength_basis"] = (
        "global"
    )  # type: ignore[index]
    assert compare_reports(old, new).errors == ("rotation_threshold_or_basis changed",)


def test_compare_reports_fails_closed_when_risk_or_rotation_facts_are_missing() -> None:
    old = _report()
    new = copy.deepcopy(old)
    del new["risk_summary"]
    del new["strategy_judgments"]["simulate_rotation_comparisons"]

    errors = compare_reports(old, new).errors
    assert "risk_formula_or_limit unavailable" in errors
    assert "rotation_threshold_or_basis unavailable" in errors


def test_compare_reports_fails_closed_on_malformed_rows() -> None:
    old = _report()
    new = copy.deepcopy(old)
    new["signal_snapshots"]["candidates"] = ["not a row"]  # type: ignore[index]
    new["strategy_judgments"]["holding_decisions"] = ["not a row"]  # type: ignore[index]
    new["strategy_judgments"]["simulate_rotation_comparisons"] = [  # type: ignore[index]
        "not a row"
    ]

    errors = compare_reports(old, new).errors
    assert "discipline_qualified_set unavailable" in errors
    assert "holding_decisions unavailable" in errors
    assert "rotation_threshold_or_basis unavailable" in errors


def test_selected_evidence_chooses_latest_complete_revision(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        replay_validator, "rebuild_trend_report_from_evidence", lambda _payload: {}
    )
    root = tmp_path / "trend_review" / "evidence" / "CN"
    root.mkdir(parents=True)

    def payload(generated_at: str, candidates: object) -> dict[str, object]:
        return {
            "schema_version": "open_trader.trend_review.evidence.v1",
            "market": "CN",
            "report_id": "2026-08-07",
            "strategy_snapshot": {
                "market": "CN",
                "strategy_version": "v12",
                "parameters": {"candidate_pool_ids": [1]},
            },
            "rebuild_inputs": {
                "generated_at": generated_at,
                "as_of_date": "2026-08-07",
                "execution_date": "2026-08-10",
                "account": {},
                "candidates": candidates,
                "holding_snapshots": {},
                "bars_by_symbol": {},
                "prior_state": {},
                "watch_events": [],
                "market": "CN",
                "candidate_pool_ids": [1],
                "metadata": {},
                "price_fx_to_account_currency": "1",
            },
        }

    old = payload(
        "2026-08-07T16:00:00+08:00",
        [{"symbol": "A", "as_of_date": "2026-08-07"}],
    )
    malformed_latest = payload("2026-08-07T17:00:00+08:00", ["bad row"])
    (root / "old.json").write_text(json.dumps(old), encoding="utf-8")
    (root / "latest-but-incomplete.json").write_text(
        json.dumps(malformed_latest), encoding="utf-8"
    )

    selected = _selected_evidence(tmp_path, "CN", 1)
    assert len(selected) == 1
    assert selected[0][1].name == "old.json"


def test_cli_emits_zero_paid_calls_for_empty_data(tmp_path: Path) -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_trend_candidate_replay.py",
            "--data-dir",
            str(tmp_path),
            "--days",
            "20",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAIL"
    assert payload["paid_api_calls"] == 0
    assert payload["days_per_market"] == {"CN": 0, "HK": 0, "US": 0}
