"""Public offline controls for scheduled report recovery; no live APIs."""

import importlib.util
import json
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / ".agents/skills/trend-v3-daily-report"


def control():
    spec = importlib.util.spec_from_file_location(
        "hourly_collection", SKILL / "collection.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hourly_slot_recovery_and_stop(tmp_path):
    from test_daily_report_layout import complete_report

    api = control()
    report = complete_report()
    state = tmp_path / "control.json"
    api.initialize(state, report)
    assert api.claim_round(state, report, "2026-11-23T08:29:00+08:00") is None
    first = api.claim_round(state, report, "2026-11-23T08:30:00+08:00")
    assert first["slot"] == 0
    # Data proof alone cannot finish monitoring before verified publication.
    assert api.claim_round(state, report, "2026-11-23T09:29:00+08:00") is None
    assert api.claim_round(state, report, "2026-11-23T09:30:00+08:00")["slot"] == 1
    assert control().claim_round(state, report, "2026-11-23T09:31:00+08:00") is None
    # Sleeping through hours skips every old slot; restart keeps original day.
    assert (
        control().claim_round(state, report, "2026-11-24T00:35:00+08:00")["slot"] == 16
    )
    assert api.claim_round(state, report, "2026-11-24T00:36:00+08:00") is None
    local = tmp_path / "report.html"
    cloud = tmp_path / "delivery.html"
    with pytest.raises((OSError, ValueError)):
        api.confirm_publication(state, report, local, cloud)
    assert api.claim_round(state, report, "2026-11-24T01:30:00+08:00")["slot"] == 17
    rendered = api.render_html(report)
    local.write_text(rendered)
    cloud.write_text("incorrect copy")
    with pytest.raises(ValueError):
        api.confirm_publication(state, report, local, cloud)
    cloud.write_text(rendered)
    api.confirm_publication(state, report, local, cloud)
    assert control().claim_round(state, report, "2026-11-24T02:30:00+08:00") is None
    # Corrections invalidate completion; unknown evidence cannot silently finish.
    report["collection"]["evidence"]["/board_date"]["state"] = "withdrawn"
    round_ = api.claim_round(state, report, "2026-11-24T02:30:00+08:00")
    assert round_["markets"] == ["BOARD"] and round_["changed"] == ["/board_date"]
    frozen = json.loads(state.read_text())
    report["collection"]["targets"]["CRYPTO"] = "2026-11-23"
    with pytest.raises(ValueError, match="frozen"):
        api.claim_round(state, report, "2026-11-24T03:30:00+08:00")
    assert json.loads(state.read_text()) == frozen


def test_target_dates_follow_market_calendar():
    api = control()
    calendars = {
        "CRYPTO": {"kind": "continuous", "source": "approved-day-boundary"},
        "US": {
            "kind": "equity",
            "sessions": ["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"],
            "covered_through": "2026-11-26",
            "source": "fixture-US-calendar",
        },
        "HK": {
            "kind": "equity",
            "sessions": [
                "2026-11-20",
                "2026-11-23",
                "2026-11-24",
                "2026-11-25",
                "2026-11-26",
            ],
            "covered_through": "2026-11-26",
            "source": "fixture-HK-calendar",
        },
        "UNKNOWN": {"kind": "equity"},
        "STALE-CALENDAR": {
            "kind": "equity",
            "sessions": ["2026-11-20"],
            "covered_through": "2026-11-20",
            "source": "old",
        },
    }
    assert api.target_dates("2026-11-23", calendars) == {
        "CRYPTO": "2026-11-22",
        "US": "2026-11-20",
        "HK": "2026-11-20",
        "UNKNOWN": None,
        "STALE-CALENDAR": None,
    }
    assert api.target_dates("2026-11-27", calendars) == {
        "CRYPTO": "2026-11-26",
        "US": "2026-11-25",
        "HK": "2026-11-26",
        "UNKNOWN": None,
        "STALE-CALENDAR": None,
    }
    assert api.target_dates("2026-11-28", calendars)["US"] is None


def test_late_calendar_resolves_unknown_target(tmp_path):
    from copy import deepcopy

    from test_daily_report_layout import complete_report

    api = control()
    initial = complete_report()
    initial["collection"]["targets"]["CRYPTO"] = None
    initial["collection"]["targets"].pop("US")
    state = tmp_path / "control.json"
    api.initialize(state, initial)
    assert "数据收集中" in api.render_html(initial)
    assert api.claim_round(state, initial, "2026-11-23T08:30:00+08:00")["slot"] == 0
    before = state.read_bytes()
    resolved = complete_report()
    with pytest.raises(ValueError):
        api.claim_round(state, resolved, "2026-11-23T09:30:00+08:00")
    assert state.read_bytes() == before
    proofs = {
        "CRYPTO": {
            "state": "verified",
            "report_date": "2026-11-23",
            "basis_date": "2026-11-22",
            "kind": "continuous",
            "source": "fixture-crypto-day-boundary",
        },
        "US": {
            "state": "verified",
            "report_date": "2026-11-23",
            "basis_date": "2026-11-22",
            "kind": "equity",
            "sessions": ["2026-11-19", "2026-11-20"],
            "covered_through": "2026-11-22",
            "source": "fixture-US-calendar",
        },
    }
    resolved["collection"]["target_evidence"] = proofs
    for label in ("wrong-day", "missing-source", "unverified", "future"):
        invalid = deepcopy(resolved)
        proof = invalid["collection"]["target_evidence"]["US"]
        if label == "wrong-day":
            proof["report_date"] = "2026-11-24"
        elif label == "missing-source":
            proof["source"] = ""
        elif label == "unverified":
            proof["state"] = "pending"
        else:
            invalid["collection"]["targets"]["US"] = "2026-11-24"
        assert "数据收集中" in api.render_html(invalid)
        with pytest.raises(ValueError):
            api.claim_round(state, invalid, "2026-11-23T09:30:00+08:00")
        assert state.read_bytes() == before

    round_ = api.claim_round(state, resolved, "2026-11-23T09:30:00+08:00")
    assert round_["slot"] == 1 and round_["markets"] == []
    assert "/long/0/searchable/0/trend" in round_["changed"]
    assert "/long/1/searchable/0/trend" in round_["changed"]
    assert round_["affected_groups"] == ["/long/0", "/long/1"]
    saved = json.loads(state.read_text())
    assert saved["date"] == "2026-11-23"
    assert saved["targets"] == {
        "BOARD": "2026-11-22",
        "US": "2026-11-20",
        "CRYPTO": "2026-11-22",
    }
    assert saved["target_evidence"] == proofs
    html = api.render_html(resolved)
    assert "数据已完备" in html
    local, delivered = tmp_path / "local.html", tmp_path / "delivered.html"
    local.write_text(html, encoding="utf-8")
    delivered.write_text(html, encoding="utf-8")
    api.confirm_publication(state, resolved, local, delivered)
    assert api.claim_round(state, resolved, "2026-11-23T10:30:00+08:00") is None

    for change in ("replace", "drop"):
        invalid = deepcopy(resolved)
        if change == "replace":
            invalid["collection"]["targets"]["US"] = "2026-11-19"
            invalid["collection"]["target_evidence"]["US"]["sessions"].pop()
        else:
            invalid["collection"]["targets"].pop("US")
        before = state.read_bytes()
        with pytest.raises(ValueError, match="frozen"):
            api.initialize(state, invalid)
        assert state.read_bytes() == before
    for label in ("future-target", "wrong-proof"):
        invalid = deepcopy(resolved)
        if label == "future-target":
            invalid["collection"]["targets"]["US"] = "2026-11-24"
        else:
            invalid["collection"]["target_evidence"]["US"]["sessions"].pop()
        rejected = tmp_path / f"{label}.json"
        with pytest.raises(ValueError):
            api.initialize(rejected, invalid)
        assert not rejected.exists()

    same_slot = tmp_path / "same-slot.json"
    api.initialize(same_slot, initial)
    assert api.claim_round(same_slot, initial, "2026-11-23T08:30:00+08:00")["slot"] == 0
    assert api.claim_round(same_slot, resolved, "2026-11-23T08:45:00+08:00") is None
    saved = json.loads(same_slot.read_text())
    assert saved["targets"] == resolved["collection"]["targets"]
    assert saved["target_evidence"] == proofs
    round_ = control().claim_round(same_slot, resolved, "2026-11-23T09:30:00+08:00")
    assert round_["slot"] == 1
    assert round_["affected_groups"] == ["/long/0", "/long/1"]
