import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".agents/skills/trend-v3-daily-report"
REPORT_DAY = "2025-02-07"
DATA_DAY = "2025-02-06"
CUTOFF = f"{REPORT_DAY}T08:30:00+08:00"


def instrument(code, pid):
    return {
        "code": code,
        "instrument_id": pid,
        "market": "X",
        "instrument_type": "stock",
        "data_date": DATA_DAY,
        "strength": 97,
        "local_strength": 12,
    }


def member(code, pid, score, position, *, version="component-edition"):
    return instrument(code, pid) | {
        "state": "verified",
        "removed": False,
        "class_page_strength": score,
        "class_page_position": position,
        "class_page_version": version,
        "class_page_source": f"frames/component-{position}.png",
    }


def capture(group, direction, members, *, version="component-edition"):
    return {
        "group": group,
        "state": "verified",
        "version": "board-edition",
        "observed_at": f"{REPORT_DAY}T08:25:00+08:00",
        "source": "frames/component-head.png",
        "component_page": {
            "kind": "class_components",
            "group": group,
            "direction": direction,
            "version": version,
            "report_date": REPORT_DAY,
            "column": "强度",
        },
        "members": members,
    }


def report():
    long_members = [
        member("FIRST", "x1", "52.20", 1),
        member("SECOND", "x2", "52.20", 4),
    ]
    short_members = [member("ZERO", "x3", 0, 1), member("LOW", "x4", "0.15", 2)]
    return {
        "date": REPORT_DAY,
        "cutoff": CUTOFF,
        "board_version": "board-edition",
        "collection": {"targets": {"X": DATA_DAY}},
        "long": [
            {
                "name": "Long group",
                "direct": [
                    instrument(row["code"], row["instrument_id"])
                    for row in long_members
                ],
            }
        ],
        "short": [
            {
                "name": "Short group",
                "direct": [
                    instrument(row["code"], row["instrument_id"])
                    for row in short_members
                ],
            }
        ],
        "signal_search": {
            "component_pages": [
                capture("Long group", "long", long_members[::-1]),
                capture("Short group", "short", short_members),
            ]
        },
    }


def collect(tmp_path, projection):
    source = tmp_path / "reader-projection.json"
    output = tmp_path / "component-observations.json"
    raw = json.dumps(projection)
    source.write_text(raw)
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL / "collection.py"),
            "component-observations",
            str(source),
            "--state",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert source.read_text() == raw
    return json.loads(output.read_bytes())


def test_component_observations_keep_exact_scores_and_source_tie_position(tmp_path):
    data = collect(tmp_path, report())

    assert data["schema"] == "private-visual-observations/v1"
    observed = {row["code"]: row for row in data["records"]}
    assert {
        code: (row["direction"], row["class_page_strength"], row["class_page_position"])
        for code, row in observed.items()
    } == {
        "FIRST": ("long", "52.20", 1),
        "SECOND": ("long", "52.20", 4),
        "ZERO": ("short", 0, 1),
        "LOW": ("short", "0.15", 2),
    }
    assert observed["ZERO"] == {
        "group": "Short group",
        "code": "ZERO",
        "identity": "x3",
        "market": "X",
        "instrument_type": "stock",
        "direction": "short",
        "report_date": REPORT_DAY,
        "data_date": DATA_DAY,
        "state": "verified",
        "class_page_strength": 0,
        "class_page_position": 1,
        "class_page_version": "component-edition",
        "source": "frames/component-1.png",
        "class_page_source": "frames/component-head.png",
    }


def test_component_observations_reject_inapplicable_rows_without_substitution(tmp_path):
    for updates, page_updates in (
        ({"data_date": "2025-02-05"}, {}),
        ({"data_date": None}, {}),
        ({"removed": True}, {}),
        ({"removed": None}, {}),
        ({"instrument_id": None}, {}),
        ({"instrument_id": "conflicting-id"}, {}),
        ({"code": "ANOTHER"}, {}),
        ({"market": "other"}, {}),
        ({"instrument_type": "option"}, {}),
        ({"class_page_version": "other-page-edition"}, {}),
        ({"class_page_position": None}, {}),
        ({"class_page_source": None}, {}),
        ({"class_page_strength": None}, {}),
        ({}, {"group": "Unrelated class"}),
        ({}, {"direction": "short"}),
        ({}, {"report_date": "2025-02-06"}),
        ({}, {"kind": "direction_signal"}),
    ):
        projection = report()
        entry = projection["signal_search"]["component_pages"][0]
        entry["members"][0].update(updates)
        entry["component_page"].update(page_updates)
        # Even an exact matching signal row with another strength cannot fill.
        projection["signal_search"]["boards"] = [
            {
                "members": [member("SECOND", "x2", 77, 9)],
            }
        ]

        data = collect(tmp_path, projection)
        assert {row["code"] for row in data["records"]} == (
            {"ZERO", "LOW"} if page_updates else {"FIRST", "ZERO", "LOW"}
        ), (updates, page_updates)


def test_component_observations_keep_overlap_and_quarantine_conflicts(tmp_path):
    for overlap in (
        {"class_page_source": "frames/component-overlap.png"},
        {"class_page_strength": "53.20"},
        {"class_page_position": 3},
        {"removed": True},
    ):
        projection = report()
        first = projection["signal_search"]["component_pages"][0]
        same_row = first["members"][0] | overlap
        first["members"].append(same_row)

        data = collect(tmp_path, projection)
        rows = [row for row in data["records"] if row["code"] == "SECOND"]
        assert len(rows) == (1 if "class_page_source" in overlap else 0), overlap
        assert {"FIRST", "LOW", "ZERO"} <= {row["code"] for row in data["records"]}

    projection = report()
    changed = capture(
        "Long group",
        "long",
        [
            member("SECOND", "x2", "52.20", 4, version="new-edition"),
        ],
        version="new-edition",
    )
    projection["signal_search"]["component_pages"].append(changed)
    data = collect(tmp_path, projection)
    assert {row["code"] for row in data["records"]} == {"LOW", "ZERO"}


def test_component_observations_use_one_based_position_and_exact_row_frame(tmp_path):
    projection = report()
    second, first = projection["signal_search"]["component_pages"][0]["members"]
    second["class_page_position"] = 0
    first["class_page_strength"] = 0
    other = member("LOW", "x4", "0.15", -1)
    projection["signal_search"]["component_pages"][1]["members"] = [other]

    data = collect(tmp_path, projection)
    assert [
        (record["code"], record["class_page_strength"]) for record in data["records"]
    ] == [
        ("FIRST", 0),
    ]
    [first_record] = data["records"]
    assert first_record["class_page_position"] == 1
    assert first_record["source"] == "frames/component-1.png"
    assert first_record["class_page_source"] == "frames/component-head.png"
