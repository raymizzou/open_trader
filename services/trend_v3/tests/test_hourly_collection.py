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
