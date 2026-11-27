import json
import subprocess
import sys
from pathlib import Path

DAY = "2025-01-02"
BOARD_DAY = "2025-01-01"
EDITION = "morning"
LAUNCH = "a" * 32
OBSERVED_AT = "2025-01-02T08:28:00+08:00"
LAUNCHED_AT = "2025-01-02T08:25:00+08:00"
SKILL = Path(__file__).resolve().parents[1] / ".agents/skills/trend-v3-daily-report"


def visual_row(name, strength):
    return {"name": name, "strength": strength, "row_date": BOARD_DAY}


def visual_proof():
    return {
        "mode": "fresh",
        "report_date": DAY,
        "launch": LAUNCH,
        "attempted": True,
        "app": "趋势动物Pro",
        "page": "大类资产",
        "page_evidence": "screens/board-top.png",
        "group": "金融资产",
        "page_timestamp": "01-02 08:00",
        "observed_at": OBSERVED_AT,
        "board_date": BOARD_DAY,
        "board_version": EDITION,
        "header_count": 2,
        "tail_seen": True,
        "tail_evidence": "screens/board-tail.png",
        "rows": [visual_row("Class A", 0), visual_row("Class B", "82.70")],
        "board_api_calls": 0,
        "coverage": "complete",
    }


def closeout(tmp_path, proof=None, *, context=None, reuse_sha=None):
    metadata = tmp_path / "context.json"
    metadata.write_text(json.dumps({"mode": "fresh"} | (context or {})))
    receipt = tmp_path / "daily-completion.json"
    command = [
        sys.executable, str(SKILL / "collection.py"), "closeout", str(metadata),
        "--day", DAY, "--launch", LAUNCH, "--launched-at", LAUNCHED_AT,
        "--state", str(receipt),
    ]
    if proof is not None:
        path = tmp_path / "board.json"
        data = proof if isinstance(proof, bytes) else json.dumps(proof).encode()
        path.write_bytes(data)
        command.extend(("--proof", str(path)))
    if reuse_sha is not None:
        command.extend(("--reuse-sha256", reuse_sha, "--reuse-revision", "v01"))
    result = subprocess.run(command, capture_output=True, text=True)
    return result, receipt


def test_complete_visual_board_includes_zero_and_needs_no_api(tmp_path):
    proof = visual_proof()
    result, receipt = closeout(tmp_path, proof)

    assert result.returncode == 0, result.stderr
    saved = json.loads(receipt.read_bytes())
    assert saved["launch"] == LAUNCH and saved["report_date"] == DAY
    assert saved["collection"] == "complete"
    acquisition = saved["acquisition"]
    assert acquisition["method"] == "visual"
    assert acquisition["board_api_calls"] == 0
    assert acquisition["visual_rows"] == proof["rows"]
    assert acquisition["rows"] == [
        visual_row("Class A", 0), visual_row("Class B", "82.70"),
    ]
    assert acquisition["board_version"] == EDITION


def test_unattempted_fresh_board_records_workflow_failure(tmp_path):
    proof = visual_proof() | {
        "attempted": False,
        "blocking_step": "old bulk-history pause",
        "restriction": {"source": "E17"},
    }
    result, receipt = closeout(
        tmp_path,
        proof,
        context={"collection": "complete", "sync": "uploaded", "revision": "v03"},
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["reason"] == "visual_attempt_required"
    saved = json.loads(receipt.read_bytes())
    assert saved["collection"] == "workflow_failed"
    assert saved["sync"] == "uploaded"
    assert saved["revision"] == "v03"

    result, _ = closeout(
        tmp_path,
        proof | {
            "rows": [],
            "coverage": "unavailable",
            "restriction": {
                "applies_to": "scheduled_daily_financial_board",
                "source": "current official provider policy",
                "evidence": "private/provider-policy-record",
            },
        },
    )
    assert result.returncode == 0
    saved = json.loads(receipt.read_bytes())
    assert saved["collection"] == "unavailable"
    assert saved["acquisition"]["reason"] == "applicable_restriction"


def test_partial_board_preserves_rows_and_reports_incomplete(tmp_path):
    proof = visual_proof() | {
        "tail_seen": False,
        "tail_evidence": None,
        "rows": [visual_row("Class A", 0), visual_row("Class B", None)],
        "blocking_step": "could not read the second strength or confirm the tail",
        "coverage": "partial",
    }
    result, receipt = closeout(tmp_path, proof, context={"sync": "uploaded"})

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "collection": "partial", "reason": "visual_coverage_incomplete",
    }
    saved = json.loads(receipt.read_bytes())
    assert saved["collection"] == "partial"
    assert saved["acquisition"]["rows"] == [visual_row("Class A", 0)]
    assert saved["sync"] == "uploaded"

    # Checking the app can be a real attempt even when the board never opens.
    blocked = {
        "mode": "fresh",
        "report_date": DAY,
        "launch": LAUNCH,
        "attempted": True,
        "observed_at": OBSERVED_AT,
        "blocking_step": "official WeChat displayed a login prompt",
        "board_api_calls": 0,
        "coverage": "unavailable",
    }
    result, _ = closeout(tmp_path, blocked)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["collection"] == "unavailable"


def test_fallback_fills_only_proven_gaps(tmp_path):
    gap = {"name": "Class B", "row_date": BOARD_DAY, "field": "strength"}
    fallback = {
        "state": "verified",
        "source": "private/official-board-response.json",
        "authorization": "private/confirmed-free",
        "group": "金融资产",
        "version": EDITION,
        "observed_at": "2025-01-02T08:29:00+08:00",
        "rows": [gap | {"strength": "82.70"}],
    }
    proof = visual_proof() | {
        "rows": [visual_row("Class A", 0), visual_row("Class B", None)],
        "gaps": [gap],
        "fallback": fallback,
        "board_api_calls": 1,
        "coverage": "partial",
    }

    result, receipt = closeout(tmp_path, proof)

    assert result.returncode == 0, result.stderr
    saved = json.loads(receipt.read_bytes())
    assert saved["collection"] == "complete"
    assert saved["acquisition"]["method"] == "visual_api"
    assert saved["acquisition"]["visual_rows"] == [visual_row("Class A", 0)]
    assert saved["acquisition"]["rows"] == [
        visual_row("Class A", 0), visual_row("Class B", "82.70"),
    ]

    for invalid in (
        {"version": "other"},
        {"rows": [gap | {"row_date": DAY, "strength": 93}]},
        {"rows": [gap | {"field": "temperature", "strength": 82}]},
        {"rows": [gap | {"name": "Class A", "strength": 93}]},
    ):
        closeout(tmp_path, proof | {"fallback": fallback | invalid})
        saved = json.loads(receipt.read_bytes())
        assert saved["collection"] != "complete"
        assert saved["acquisition"]["rows"] == [visual_row("Class A", 0)]


def test_invalid_complete_claim_cannot_close_fresh_run(tmp_path):
    proof = visual_proof()
    for invalid in (
        None,
        [],
        b"{",
        proof | {"tail_seen": False},
        proof | {"tail_evidence": ""},
        proof | {"header_count": 3},
        proof | {"header_count": True},
        proof | {"header_count": 1, "rows": [
            visual_row("Class A", 0), visual_row("Class B", None),
        ]},
        proof | {"rows": [visual_row("Class A", 0), visual_row("Class A", 80)]},
        proof | {"rows": [visual_row("Class A", False), visual_row("Class B", 80)]},
        proof | {"rows": [visual_row("Class A", "NaN"), visual_row("Class B", 80)]},
        proof | {"rows": [visual_row("Class A", 101), visual_row("Class B", 80)]},
        proof | {"board_api_calls": 1},
        proof | {"board_api_calls": False},
        proof | {"app": "WeChat"},
        proof | {"group": "other"},
        proof | {"observed_at": "2025-01-02"},
        proof | {"observed_at": "2025-01-02T08:24:00+08:00"},
        proof | {"launch": "b" * 32},
    ):
        result, receipt = closeout(
            tmp_path, invalid, context={"collection": "complete"},
        )
        assert result.returncode == 1
        assert json.loads(receipt.read_bytes())["collection"] == "workflow_failed"

    # Readback of the published page's raw date text and row-specific dates is
    # sufficient for the acquisition guard. No fabricated board-wide data day.
    proof.pop("board_date")
    proof.pop("board_version")
    proof["head_evidence"] = proof.pop("page_evidence")
    proof["rows"][1]["row_date"] = DAY
    result, receipt = closeout(tmp_path, proof)
    assert result.returncode == 0, result.stderr
    assert json.loads(receipt.read_bytes())["acquisition"]["rows"] == proof["rows"]


def test_offline_and_verified_reuse_avoid_new_acquisition(tmp_path):
    report = tmp_path / f"{DAY}.html"
    report.write_bytes(b"original known HTML")
    digest = "0" * 64
    offline, receipt = closeout(tmp_path, [], context={"mode": "offline"})
    assert offline.returncode == 0, offline.stderr
    assert json.loads(offline.stdout) == {"collection": "not_applicable"}
    assert not receipt.exists()

    context = {"mode": "verified_reuse", "revision": "v01", "reused_sha256": digest}
    reused, _ = closeout(tmp_path, [], reuse_sha=digest, context=context)
    assert reused.returncode == 0, reused.stderr
    saved = json.loads(receipt.read_bytes())
    assert saved["collection"] == "complete"
    assert saved["acquisition"] == {
        "status": "not_applicable", "method": "verified_reuse",
    }
    assert report.read_bytes() == b"original known HTML"

    refused, _ = closeout(tmp_path, reuse_sha="1" * 64, context=context)
    assert refused.returncode == 1
    assert json.loads(receipt.read_bytes())["collection"] == "workflow_failed"
    mismatch, _ = closeout(
        tmp_path, reuse_sha=digest, context=context | {"revision": "v02"},
    )
    assert mismatch.returncode == 1


def test_unavailable_visual_only_completes_with_independent_api_board(tmp_path):
    fallback = {
        "state": "verified",
        "source": "private/official-board-response.json",
        "group": "金融资产",
        "version": EDITION,
        "observed_at": "2025-01-02T08:29:00+08:00",
        "complete": True,
        "row_count": 2,
        "tail_evidence": "private/official-last-page",
        "authorization": "private/confirmed-free",
        "rows": visual_proof()["rows"],
    }
    proof = {
        "mode": "fresh", "report_date": DAY, "launch": LAUNCH, "attempted": True,
        "observed_at": OBSERVED_AT, "blocking_step": "app access rejected",
        "coverage": "unavailable", "board_api_calls": 1, "board_version": EDITION,
        "fallback": fallback,
    }
    result, receipt = closeout(tmp_path, proof)
    assert result.returncode == 0, result.stderr
    saved = json.loads(receipt.read_bytes())
    assert saved["collection"] == "complete"
    assert saved["acquisition"]["visual_status"] == "unavailable"
    assert saved["acquisition"]["visual_rows"] == []
    assert saved["acquisition"]["rows"] == fallback["rows"]

    for invalid in ({"authorization": ""}, {"row_count": 3}, {"version": "other"}):
        closeout(tmp_path, proof | {"fallback": fallback | invalid})
        assert json.loads(receipt.read_bytes())["collection"] == "unavailable"
