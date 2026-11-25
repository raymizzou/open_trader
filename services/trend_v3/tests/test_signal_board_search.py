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


def test_distinct_market_days_merge_scoped_membership_coverage(tmp_path):
    from test_hourly_collection import control

    payload = fixture()
    payload["board"]["groups"] = [dict(name="甲", strength=100)]
    payload["targets"] = {"US": "2026-11-23", "HK": "2026-11-24"}
    us, hk = (
        dict(market="US", instrument_type="stock"),
        dict(market="HK", instrument_type="stock"),
    )
    payload["group_markets"] = [dict(payload["group_markets"][0], markets=[us, hk])]
    payload["boards"][0]["data_date"] = "2026-11-23"
    hong_kong = deepcopy(payload["boards"][0])
    hong_kong.update(
        board_id="warm-hk", title="港股温转热", data_date="2026-11-24", markets=[hk]
    )
    hong_kong["members"][0].update(instrument_id="p2", code="00001", market="HK")
    payload["boards"].append(hong_kong)
    payload["memberships"] = [
        dict(payload["memberships"][0], data_date="2026-11-23", markets=[us]),
        dict(
            payload["memberships"][0],
            data_date="2026-11-24",
            markets=[hk],
            members=[
                {
                    k: hong_kong["members"][0][k]
                    for k in ["instrument_id", "code", "market", "instrument_type"]
                }
            ],
        ),
    ]
    payload["search_coverage"][0]["boards"] = [
        dict(board_id="warm-us", version="2026-11-25", markets=[us]),
        dict(board_id="warm-hk", version="2026-11-25", markets=[hk]),
    ]

    def formal_assess(evidence, name):
        report = dict(
            date="2026-11-25",
            cutoff=payload["cutoff"],
            board_date="2026-11-24",
            board_version="2026-11-25",
            selection_mode="threshold-v1",
            signal_search=evidence,
            long=[
                dict(
                    name="甲",
                    seat=1,
                    board_strength="100",
                    data_date="2026-11-24",
                    coverage={"state": "complete", "instrument_ids": ["p1", "p2"]},
                    direct=[],
                )
            ],
            short=[],
            collection=dict(
                targets={"BOARD": "2026-11-24", **payload["targets"]},
                parent_board=dict(payload["board"], row_count=1),
                worklist=[],
                evidence={},
                verified_at="2026-11-25T08:20:00+08:00",
            ),
        )
        for identity, code, market, day in [
            ("p1", "ONE", "US", "2026-11-23"),
            ("p2", "00001", "HK", "2026-11-24"),
        ]:
            report["long"][0]["direct"].append(
                dict(
                    instrument_id=identity,
                    code=code,
                    name=code,
                    market=market,
                    instrument_type="stock",
                    data_date=day,
                    direct_verified=True,
                    platform_match_verified=True,
                    watch_eligible=False,
                    trend="热",
                    signal_events=["温转热"],
                    strength="90",
                    local_strength="80",
                )
            )
        for path, label, market, owner, value in control().required_facts(report):
            day = report["collection"]["targets"][market]
            report["collection"]["worklist"].append(
                dict(path=path, label=label, market=market, target_date=day)
            )
            report["collection"]["evidence"][path] = dict(
                state="verified",
                data_date=day,
                version="2026-11-25",
                source="synthetic-frozen-fact",
                identity=owner,
                value=value,
                market=market,
                instrument_type="stock" if market != "BOARD" else None,
            )
        path = tmp_path / (name + "-formal.json")
        path.write_text(json.dumps(report, ensure_ascii=False))
        result = subprocess.run(
            [sys.executable, str(SKILL / "collection.py"), "assess", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    result = search(tmp_path, payload)
    assert result["groups"][0]["status"] == "已命中"
    assert [
        (h["code"], h["signal_sources"][0]["data_date"])
        for h in result["groups"][0]["hits"]
    ] == [("ONE", "2026-11-23"), ("00001", "2026-11-24")]
    assert formal_assess(payload, "both-complete")["complete"]
    for change in [
        "missing-hk",
        "wrong-hk-day",
        "wrong-hk-scope",
        "wrong-catalog",
        "contradictory-member",
        "contradictory-board",
        "board-overclaim",
    ]:
        incomplete = deepcopy(payload)
        if change == "missing-hk":
            incomplete["memberships"].pop()
        elif change == "wrong-hk-day":
            incomplete["memberships"][1]["data_date"] = "2026-11-23"
        elif change == "wrong-hk-scope":
            incomplete["memberships"][1]["markets"] = [us]
        elif change == "contradictory-member":
            incomplete["memberships"][1].update(market="US", instrument_type="stock")
        elif change == "contradictory-board":
            incomplete["boards"][1].update(market="US", instrument_type="stock")
        elif change == "wrong-catalog":
            incomplete["search_coverage"][0]["boards"][0]["markets"] = [us, hk]
        else:
            incomplete["boards"][0]["markets"] = [us, hk]
            incomplete["boards"][0]["members"].append(
                dict(hong_kong["members"][0], data_date="2026-11-24")
            )
            incomplete["boards"].pop()
            incomplete["search_coverage"][0]["boards"] = [
                dict(board_id="warm-us", version="2026-11-25", markets=[us, hk])
            ]
        result = search(tmp_path, incomplete)["groups"][0]
        assert result["status"] == "待核", change
        assert result["hits"][0]["code"] == "ONE", change
        if change != "wrong-catalog":
            assert len(result["hits"]) == 1, change
        assert not formal_assess(incomplete, change)["complete"], change
    empty = deepcopy(payload)
    for container in [*empty["memberships"], *empty["boards"]]:
        container["members"] = []
    assert search(tmp_path, empty)["groups"][0]["status"] == "无命中"
    assert formal_assess(empty, "explicit-empty")["complete"]
    empty["memberships"][0].pop("markets")
    assert search(tmp_path, empty)["groups"][0]["status"] == "待核"
    assert not formal_assess(empty, "ambiguous-empty")["complete"]


def test_withdrawn_duplicate_is_order_independent_and_keeps_other_hits(tmp_path):
    import re

    from test_daily_report_layout import render
    from test_hourly_collection import add_threshold_hit, control, threshold_report

    report = add_threshold_hit(threshold_report())
    first = report["long"][0]
    good = dict(first["direct"][0], instrument_id="p2", code="TWO", name="Uncontested")
    first["direct"].append(good)
    first["coverage"]["instrument_ids"] = ["p1", "p2"]
    manifest = report["collection"]
    manifest["evidence"]["/long/0/coverage"]["value"] = deepcopy(first["coverage"])
    for field in ["trend", "signal_events", "strength", "local_strength"]:
        path = "/long/0/direct/1/" + field
        manifest["worklist"].append(
            dict(path=path, label="TWO " + field, market="US", target_date="2026-11-24")
        )
        manifest["evidence"][path] = dict(
            manifest["evidence"]["/long/0/direct/0/" + field],
            identity="p2",
            value=good[field],
        )
    payload = report["signal_search"]
    signal = payload["boards"][0]
    signal["members"][0]["state"] = "verified"
    signal["members"].append(dict(signal["members"][0], instrument_id="p2", code="TWO"))
    member = payload["memberships"][0]
    member["members"][0]["state"] = "verified"
    member["members"].append(dict(member["members"][0], instrument_id="p2", code="TWO"))
    assert control().assess(report)["complete"]
    for location in ["signal", "membership-row", "membership-container"]:
        observed = []
        for reverse in [False, True]:
            disputed = deepcopy(payload)
            if location == "signal":
                rows = disputed["boards"][0]["members"]
                pair = [deepcopy(rows[0]), dict(rows[0], state="withdrawn")]
                disputed["boards"][0]["members"] = (pair[::-1] if reverse else pair) + [
                    rows[1]
                ]
            elif location == "membership-row":
                rows = disputed["memberships"][0]["members"]
                pair = [deepcopy(rows[0]), dict(rows[0], state="withdrawn")]
                disputed["memberships"][0]["members"] = (
                    pair[::-1] if reverse else pair
                ) + [rows[1]]
            else:
                original = disputed["memberships"][0]
                withdrawn = dict(
                    original,
                    state="withdrawn",
                    members=[deepcopy(original["members"][0])],
                )
                disputed["memberships"] = (
                    [withdrawn, original] if reverse else [original, withdrawn]
                ) + disputed["memberships"][1:]
            result = search(tmp_path, disputed)["groups"][0]
            assert result["status"] == "待核", (location, reverse)
            assert [h["code"] for h in result["hits"]] == ["TWO"], (location, reverse)
            observed.append(result)
            projected = deepcopy(report)
            projected["signal_search"] = disputed
            assert not control().assess(projected)["complete"]
            html = render(tmp_path, projected, location + str(reverse))
            cards = re.findall(r"<article\b.*?</article>", html, re.S)
            contested = next(card for card in cards if "合成一号" in card)
            certain = next(card for card in cards if "Uncontested" in card)
            assert "信号榜：" not in contested and "信号榜：" in certain
        assert observed[0] == observed[1], location
    stable = deepcopy(payload)
    stable["boards"][0]["members"].insert(
        1, deepcopy(stable["boards"][0]["members"][0])
    )
    stable["boards"][0]["members"][1].pop("state")
    stable["memberships"][0]["members"].insert(
        1, deepcopy(stable["memberships"][0]["members"][0])
    )
    stable["memberships"][0]["members"][1].pop("state")
    result = search(tmp_path, stable)["groups"][0]
    assert result["status"] == "已命中"
    assert [h["code"] for h in result["hits"]] == ["ONE", "TWO"]
    # Another board's current positive is independent of this board's withdrawal.
    separate = deepcopy(payload)
    separate["boards"][0]["members"].insert(
        1, dict(separate["boards"][0]["members"][0], state="withdrawn")
    )
    other = deepcopy(payload["boards"][0])
    other.update(board_id="other-warm", title="独立温转热榜")
    other["members"][0]["removed_from_other_board"] = "冬至榜"
    separate["boards"].append(other)
    result = search(tmp_path, separate)["groups"][0]
    one = next(h for h in result["hits"] if h["code"] == "ONE")
    assert [s["board_id"] for s in one["signal_sources"]] == ["other-warm"]
    assert any(h["code"] == "TWO" for h in result["hits"])

    independent = deepcopy(payload)
    another = dict(
        deepcopy(independent["memberships"][0]), source="separate-independent-members"
    )
    independent["memberships"][0]["members"].insert(
        1, dict(independent["memberships"][0]["members"][0], state="withdrawn")
    )
    independent["memberships"].append(another)
    certain = search(tmp_path, independent)["groups"][0]
    assert certain["status"] == "待核"
    assert [h["code"] for h in certain["hits"]] == ["ONE", "TWO"]

    removed = deepcopy(payload)
    removed["memberships"][0]["members"][0]["removed"] = True
    remaining = search(tmp_path, removed)["groups"][0]
    assert remaining["status"] == "已命中"
    assert [h["code"] for h in remaining["hits"]] == ["TWO"]
