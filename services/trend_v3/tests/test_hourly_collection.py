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


def threshold_report():
    """Independent transcribed official parent; no signal list selects the classes."""
    reference = json.loads(
        (Path(__file__).parent / "fixtures/financial_board_20261125.json").read_text()
    )
    report = {
        "date": "2026-11-25",
        "board_date": "2026-11-24",
        "board_version": "2026-11-25",
        "selection_mode": "threshold-v1",
        "cutoff": "2026-11-25T08:30:00+08:00",
        "collection": {
            "targets": {"BOARD": "2026-11-24", "US": "2026-11-24"},
            "worklist": [],
            "evidence": {},
            "verified_at": "2026-11-25T08:20:00+08:00",
        },
    }
    report["collection"]["parent_board"] = dict(
        state="verified",
        complete=True,
        source="independent-official-board.png",
        data_date="2026-11-24",
        version="2026-11-25",
        row_count=23,
        observed_at="2026-11-25T08:00:00+08:00",
        groups=[
            dict(name=name, strength=score)
            for name, score in reference["financial_assets"]
        ],
    )
    for direction, names in [
        ("long", reference["expected_long"]),
        ("short", reference["expected_short"]),
    ]:
        scores = dict(reference["financial_assets"])
        report[direction] = [
            dict(
                name=name,
                seat=i + 1,
                board_strength=str(scores[name]),
                data_date="2026-11-24",
                coverage={"state": "complete", "instrument_ids": []},
            )
            for i, name in enumerate(names)
        ]
    # Receipt shape from the legacy contract, literal expected groups above.
    api = control()
    for path, label, market, identity, value in api.required_facts(report):
        report["collection"]["worklist"].append(
            dict(path=path, label=label, market=market, target_date="2026-11-24")
        )
        report["collection"]["evidence"][path] = dict(
            state="verified",
            data_date="2026-11-24",
            version="2026-11-25",
            source="frozen-receipt",
            identity=identity,
            value=value,
            market=market,
        )
    from copy import deepcopy

    from test_signal_board_search import fixture

    payload = fixture()
    payload["board"] = deepcopy(report["collection"]["parent_board"])
    payload["group_markets"] = []
    payload["memberships"] = []
    payload["boards"][0]["members"] = []
    cold = deepcopy(payload["boards"][0])
    cold.update(
        board_id="cold-us", title="离线凉转寒榜", label="凉转寒", direction="short"
    )
    payload["boards"].append(cold)
    payload["search_coverage"].append(
        dict(
            payload["search_coverage"][0],
            direction="short",
            boards=[dict(board_id="cold-us", version="2026-11-25")],
        )
    )
    for side in ["long", "short"]:
        for group in report[side]:
            payload["group_markets"].append(
                dict(
                    group=group["name"],
                    state="verified",
                    complete=True,
                    source="synthetic-market-mapping",
                    markets=[dict(market="US", instrument_type="stock")],
                )
            )
            payload["memberships"].append(
                dict(
                    state="verified",
                    source="synthetic-membership",
                    complete=True,
                    data_date="2026-11-24",
                    version="2026-11-25",
                    observed_at="2026-11-25T08:00:00+08:00",
                    group=group["name"],
                    members=[],
                )
            )
    report["signal_search"] = payload
    return report


def test_threshold_report_checks_expected_groups(tmp_path):
    from copy import deepcopy

    api = control()
    data = threshold_report()
    assert api.assess(data)["complete"]
    html = api.render_html(data)
    assert html.count('class="class-row"') == 10
    assert "未上市公司" in html and "黄金" in html
    for side, index in [("long", 5), ("short", 3)]:
        missing = deepcopy(data)
        missing[side].pop(index)
        # Remove even its receipts: expected classes come from the separate parent.
        prefix = f"/{side}/{index}/"
        missing["collection"]["worklist"] = [
            x
            for x in missing["collection"]["worklist"]
            if not x["path"].startswith(prefix)
        ]
        assert not api.assess(missing)["complete"]
    state = tmp_path / "threshold-state.json"
    api.initialize(state, data)
    revised = deepcopy(data)
    revised["collection"]["parent_board"]["groups"].pop()
    with pytest.raises(ValueError, match="frozen"):
        api.initialize(state, revised)
    for field in ["source", "complete", "data_date"]:
        revised = deepcopy(data)
        revised["collection"]["parent_board"][field] = None
        assert not api.assess(revised)["complete"]
    from test_daily_report_layout import complete_report

    assert api.assess(complete_report())["complete"]

    absent = deepcopy(data)
    absent.pop("signal_search")
    assert not api.assess(absent)["complete"]
    assert "信号搜索覆盖待核" in api.render_html(absent)
    assert "截至 2026-11-25T08:30:00+08:00" in api.render_html(data)
    omitted = deepcopy(data)
    identity = dict(
        instrument_id="p1", code="ONE", market="US", instrument_type="stock"
    )
    omitted["signal_search"]["boards"][0]["members"] = [
        dict(identity, labels=["温转热"], removed=False)
    ]
    omitted["signal_search"]["memberships"][0]["members"] = [identity]
    assert not api.assess(omitted)["complete"]
    assert "已核实信号命中尚未入直属卡" in api.render_html(omitted)
    for partial in [
        None,
        dict(
            data["collection"]["parent_board"],
            complete=False,
            groups=data["collection"]["parent_board"]["groups"][:2],
            row_count=23,
        ),
    ]:
        initial = deepcopy(data)
        initial["collection"]["parent_board"] = partial
        path = tmp_path / (
            "null-state.json" if partial is None else "partial-state.json"
        )
        api.initialize(path, initial)
        assert api.claim_round(path, initial, "2026-11-25T08:30:00+08:00")["slot"] == 0
        # First parent verification in the same slot saves without replay.
        assert api.claim_round(path, data, "2026-11-25T08:40:00+08:00") is None
        assert (
            json.loads(path.read_text())["parent_board"]
            == data["collection"]["parent_board"]
        )
        invalid = deepcopy(data)
        invalid["collection"]["parent_board"]["groups"][0]["strength"] = 99
        with pytest.raises(ValueError, match="frozen"):
            api.initialize(path, invalid)


def add_threshold_hit(report):
    """One independently hand-defined US-stock hit; all quotes are synthetic."""
    from copy import deepcopy

    identity = dict(
        instrument_id="p1", code="ONE", market="US", instrument_type="stock"
    )
    group = report["long"][0]
    group["direct"] = [
        dict(
            identity,
            name="合成一号",
            data_date="2026-11-24",
            trend="热",
            signal_events=["温转热"],
            strength="90",
            local_strength="80",
            direct_verified=True,
            platform_match_verified=True,
            watch_eligible=False,
        )
    ]
    group["coverage"]["instrument_ids"] = ["p1"]
    report["collection"]["targets"]["US"] = "2026-11-24"
    report["signal_search"]["boards"][0]["members"] = [
        dict(identity, labels=["温转热"], removed=False)
    ]
    report["signal_search"]["memberships"][0]["members"] = [deepcopy(identity)]
    api = control()
    for path, label, market, owner, value in api.required_facts(report):
        manifest = report["collection"]
        if not any(item["path"] == path for item in manifest["worklist"]):
            manifest["worklist"].append(
                dict(path=path, label=label, market=market, target_date="2026-11-24")
            )
        manifest["evidence"][path] = dict(
            state="verified",
            data_date="2026-11-24",
            version="2026-11-25",
            source="synthetic-field",
            identity=owner,
            value=value,
            market=market,
            instrument_type="stock" if market == "US" else None,
        )
    return report


def test_signal_coverage_rejects_stale_empty_members_and_divergent_market_targets(
    tmp_path,
):
    from copy import deepcopy

    api = control()
    data = add_threshold_hit(threshold_report())
    assert api.assess(data)["complete"]
    for change in ["empty-members-day", "divergent-target"]:
        invalid = deepcopy(data)
        if change == "empty-members-day":
            assert invalid["signal_search"]["memberships"][1]["members"] == []
            invalid["signal_search"]["memberships"][1]["data_date"] = "2026-11-20"
        else:
            invalid["signal_search"]["targets"] = {"US": "2026-11-20"}
        assert not api.assess(invalid)["complete"], change
        state = tmp_path / (change + ".json")
        api.initialize(state, invalid)
        assert api.claim_round(state, invalid, "2026-11-25T08:30:00+08:00")["slot"] == 0
        assert api.claim_round(state, invalid, "2026-11-25T09:30:00+08:00")["slot"] == 1
        planned = api.report_signal_plan(invalid)
        assert planned["groups"][0]["hits"][0]["code"] == "ONE"
    holiday = threshold_report()
    holiday["collection"]["targets"]["US"] = "2026-11-20"
    holiday["signal_search"]["targets"] = {"US": "2026-11-20"}
    for container in [
        *holiday["signal_search"]["memberships"],
        *holiday["signal_search"]["boards"],
    ]:
        container["data_date"] = "2026-11-20"
    assert api.assess(holiday)["complete"]
