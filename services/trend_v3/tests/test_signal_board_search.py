"""Hand-frozen search/identity fixtures exercise the network-free published CLI."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".agents/skills/trend-v3-daily-report"


def search(tmp_path, payload):
    path = tmp_path / "search.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    result = subprocess.run(
        [sys.executable, str(SKILL / "collection.py"), "search-plan", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def fixture():
    identity = dict(
        instrument_id="p1", code="ONE", market="US", instrument_type="stock"
    )
    return {
        "cutoff": "2026-11-25T08:30:00+08:00",
        "board": dict(
            state="verified",
            complete=True,
            source="frozen-parent",
            data_date="2026-11-24",
            version="2026-11-25",
            observed_at="2026-11-25T08:00:00+08:00",
            groups=[dict(name="甲", strength=100), dict(name="乙", strength=80)],
        ),
        "group_markets": [
            dict(
                group=g,
                state="verified",
                source="mapping",
                complete=True,
                markets=[dict(market="US", instrument_type="stock")],
            )
            for g in ["甲", "乙"]
        ],
        "boards": [
            dict(
                board_id="warm-us",
                title="美股温转热",
                direction="long",
                label="温转热",
                version="2026-11-25",
                data_date="2026-11-24",
                observed_at="2026-11-25T08:10:00+08:00",
                state="verified",
                source="signal-snapshot",
                complete=True,
                markets=[dict(market="US", instrument_type="stock")],
                members=[dict(identity, labels=["温转热"], removed=False)],
            )
        ],
        "search_coverage": [
            dict(
                direction="long",
                state="verified",
                complete=True,
                source="search-results",
                data_date="2026-11-24",
                version="2026-11-25",
                observed_at="2026-11-25T08:08:00+08:00",
                boards=[dict(board_id="warm-us", version="2026-11-25")],
            )
        ],
        "memberships": [
            dict(
                group=g,
                state="verified",
                source="independent-members",
                version="2026-11-25",
                data_date="2026-11-24",
                observed_at="2026-11-25T08:05:00+08:00",
                complete=True,
                members=[deepcopy(identity)],
            )
            for g in ["甲", "乙"]
        ],
    }


def test_shared_board_read_once_for_two_groups(tmp_path):
    value = search(tmp_path, fixture())
    assert value["searches"] == ["温转热"]
    assert value["boards_to_read"] == []
    assert value["metrics"]["unique_boards_read"] == 1
    assert [(g["name"], [h["code"] for h in g["hits"]]) for g in value["groups"]] == [
        ("甲", ["ONE"]),
        ("乙", ["ONE"]),
    ]
    assert value["groups"][0]["hits"][0]["signal_sources"] == [
        {
            "board_id": "warm-us",
            "title": "美股温转热",
            "label": "温转热",
            "data_date": "2026-11-24",
            "version": "2026-11-25",
        }
    ]

    payload = fixture()
    payload["search_coverage"][0]["boards"].append(
        dict(
            board_id="cn-missing",
            version="2026-11-25",
            markets=[dict(market="CN", instrument_type="ETF")],
        )
    )
    scoped = search(tmp_path, payload)
    assert scoped["boards_to_read"] == []
    assert scoped["groups"][0]["status"] == "已命中"


def test_long_short_intersections_ignore_related_board_rank(tmp_path):
    payload = fixture()
    payload["board"]["groups"] += [
        dict(name="弱", strength=20),
        dict(name="零", strength=0),
        dict(name="中", strength=79),
        dict(name="次弱", strength=21),
    ]
    for name in ["弱", "零"]:
        payload["group_markets"].append(dict(payload["group_markets"][0], group=name))
        payload["memberships"].append(dict(payload["memberships"][0], group=name))
    cold = deepcopy(payload["boards"][0])
    cold.update(
        board_id="cold-us", title="美股凉转寒", direction="short", label="凉转寒"
    )
    cold["members"][0]["labels"] = ["凉转寒"]
    payload["boards"].append(cold)
    value = search(tmp_path, payload)
    assert value["searches"] == ["温转热", "凉转寒"]
    assert [(g["name"], g["direction"], len(g["hits"])) for g in value["groups"]] == [
        ("甲", "long", 1),
        ("乙", "long", 1),
        ("弱", "short", 1),
        ("零", "short", 1),
    ]


def test_identity_dates_and_removed_rows(tmp_path):
    base = fixture()
    for change in [
        "wrong-type",
        "wrong-id",
        "wrong-code",
        "wrong-date",
        "late",
        "removed",
        "no-label",
        "wrong-edition",
        "membership-source",
        "mapping-missing",
    ]:
        payload = deepcopy(base)
        row = payload["boards"][0]["members"][0]
        if change == "wrong-type":
            row["instrument_type"] = "future"
        elif change == "wrong-id":
            row["instrument_id"] = "different"
        elif change == "wrong-code":
            row["code"] = "OTHER"
        elif change == "wrong-date":
            payload["boards"][0]["data_date"] = "2026-11-23"
        elif change == "late":
            payload["boards"][0]["observed_at"] = "2026-11-25T20:00:00+08:00"
        elif change == "removed":
            row["removed"] = True
        elif change == "no-label":
            row["labels"] = ["热转沸"]
        elif change == "wrong-edition":
            payload["memberships"][0]["version"] = "old"
        elif change == "membership-source":
            payload["memberships"][0]["source"] = "signal-snapshot"
        else:
            payload["group_markets"] = []
        result = search(tmp_path, payload)
        assert result["groups"][0]["hits"] == [], change
    payload = deepcopy(base)
    payload["boards"][0]["members"][0]["removed_from_other_board"] = "冬至榜"
    # A second source merges into one class card; cross-class membership stays separate.
    second = deepcopy(payload["boards"][0])
    second.update(board_id="warm-second", title="温转热第二榜")
    payload["boards"] += [second, deepcopy(second)]
    result = search(tmp_path, payload)
    assert [len(g["hits"]) for g in result["groups"]] == [1, 1]
    assert len(result["groups"][0]["hits"][0]["signal_sources"]) == 2
    assert result["metrics"]["unique_boards_read"] == 2

    payload = fixture()
    for row in [
        payload["boards"][0]["members"][0],
        *[m["members"][0] for m in payload["memberships"]],
    ]:
        row["currency"] = "USD"
    assert search(tmp_path, payload)["groups"][0]["hits"][0]["currency"] == "USD"
    # A market holiday has an independently supplied applicable data day.
    payload["targets"] = {"US": "2026-11-20"}
    for container in [payload["boards"][0], *payload["memberships"]]:
        container["data_date"] = "2026-11-20"
    hit = search(tmp_path, payload)["groups"][0]["hits"][0]
    assert hit["signal_sources"][0]["data_date"] == "2026-11-20"


def test_incomplete_coverage_keeps_known_hits(tmp_path):
    payload = fixture()
    payload["boards"][0]["complete"] = False
    result = search(tmp_path, payload)
    assert result["groups"][0]["status"] == "待核"
    assert [h["code"] for h in result["groups"][0]["hits"]] == ["ONE"]
    assert result["boards_to_read"] == [dict(board_id="warm-us", version="2026-11-25")]
    assert result["groups"][0]["gaps"]
    payload["boards"][0]["members"] = []
    result = search(tmp_path, payload)
    assert result["groups"][0]["status"] == "待核"
    payload["boards"][0]["complete"] = True
    assert search(tmp_path, payload)["groups"][0]["status"] == "无命中"
    payload["memberships"][0]["complete"] = False
    assert search(tmp_path, payload)["groups"][0]["status"] == "待核"
    payload["boards"] = []
    assert search(tmp_path, payload)["groups"][0]["status"] == "待核"

    # Per-row unknowns cannot prove absence, and exact label arrays are required.
    for change in [
        "stale-signal",
        "stale-member",
        "substring",
        "identity",
        "removal",
        "shared-source",
    ]:
        value = fixture()
        signal = value["boards"][0]["members"][0]
        if change == "stale-signal":
            signal["data_date"] = "2026-11-20"
        elif change == "stale-member":
            value["memberships"][0]["members"][0]["data_date"] = "2026-11-20"
        elif change == "substring":
            signal["labels"] = "非温转热"
        elif change == "identity":
            signal.pop("instrument_id")
        elif change == "removal":
            signal.pop("removed")
        else:
            for membership in value["memberships"]:
                membership["source"] = value["boards"][0]["source"]
        group = search(tmp_path, value)["groups"][0]
        assert group["hits"] == [] and group["status"] == "待核", change


def test_each_expected_board_covers_its_declared_scope(tmp_path):
    from test_hourly_collection import add_threshold_hit, control, threshold_report

    payload = fixture()
    bad = deepcopy(payload["boards"][0])
    bad.update(
        board_id="second-us",
        markets=[dict(market="HK", instrument_type="stock")],
        members=[],
    )
    payload["boards"].append(bad)
    payload["search_coverage"][0]["boards"].append(
        dict(
            board_id="second-us",
            version="2026-11-25",
            markets=[dict(market="US", instrument_type="stock")],
        )
    )
    result = search(tmp_path, payload)
    assert result["groups"][0]["hits"][0]["code"] == "ONE"
    assert result["groups"][0]["status"] == "待核"
    assert result["boards_to_read"] == [
        dict(board_id="second-us", version="2026-11-25")
    ]
    report = add_threshold_hit(threshold_report())
    report["signal_search"]["boards"].append(bad)
    report["signal_search"]["search_coverage"][0]["boards"].append(
        payload["search_coverage"][0]["boards"][-1]
    )
    assert not control().assess(report)["complete"]
    # A CN-only catalogue entry remains irrelevant even with a missing readback.
    report["signal_search"]["search_coverage"][0]["boards"][-1]["markets"] = [
        dict(market="CN", instrument_type="ETF")
    ]
    assert control().assess(report)["complete"]
