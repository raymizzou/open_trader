"""Issue #60 slice 4: A8 terminal-state modeling verification tests.

The frozen production fixture (``a8_samples.v1``) carries, per sample, the
legacy LLM proof (``structured_result`` with ``relation`` +
``proof.excluded_state``) and the N_LEG side (``catalog`` with the canonical
compiled ``problem``).  The check must independently re-derive each proof's
excluded joint YES/NO state from the canonical N_LEG constraint model and
require 100% agreement — this run is the admission evidence for the frozen
fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

from open_trader.prediction_n_leg_terminal_check import (
    main,
    run_terminal_check,
)


FIXTURE = Path(__file__).parent / "fixtures" / "prediction_n_leg_cutover_a8_samples.json"


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def write_fixture(payload: dict[str, object], path: Path) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_full_fixture_all_samples_agree(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report = run_terminal_check(FIXTURE)
    report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")

    assert report["schema"] == "open_trader.prediction_n_leg_terminal_check.report.v1"
    # Independent source of truth: the fixture's own declared count.
    assert report["sample_count"] == load_fixture()["sample_count"] == 192
    assert report["agreed"] == 192
    assert report["disagreed"] == []
    assert report["errors"] == []
    assert report["source"]


def test_tampered_excluded_state_is_reported_and_cli_exits_2(
    tmp_path: Path, capsys
) -> None:
    payload = load_fixture()
    tampered_id = payload["samples"][0]["cache_key"]
    # Flip the proof's excluded state to the mirror joint state (A=YES,B=NO):
    # a well-formed label that no IMPLIES model excludes.
    payload["samples"][0]["structured_result"]["proof"]["excluded_state"] = (
        "A=YES,B=NO"
    )
    tampered = write_fixture(payload, tmp_path / "tampered.json")
    report_path = tmp_path / "report.json"

    exit_code = main(["--fixture", str(tampered), "--report", str(report_path)])

    assert exit_code == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sample_count"] == 192
    assert report["agreed"] == 191
    assert [row["sample_id"] for row in report["disagreed"]] == [tampered_id]
    assert report["errors"] == []
    # The CLI prints the report path it wrote.
    out = capsys.readouterr().out
    assert str(report_path) in out


def test_malformed_sample_without_problem_is_an_error_and_cli_exits_2(
    tmp_path: Path,
) -> None:
    payload = load_fixture()
    broken_id = payload["samples"][0]["cache_key"]
    del payload["samples"][0]["catalog"]["problem"]
    broken = write_fixture(payload, tmp_path / "broken.json")
    report_path = tmp_path / "report.json"

    exit_code = main(["--fixture", str(broken), "--report", str(report_path)])

    assert exit_code == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sample_count"] == 192
    assert report["agreed"] == 191
    assert [row["sample_id"] for row in report["errors"]] == [broken_id]
    assert report["disagreed"] == []


def test_malformed_relation_contract_id_shape_is_an_error_and_cli_exits_2(
    tmp_path: Path,
) -> None:
    payload = load_fixture()
    broken_id = payload["samples"][0]["cache_key"]
    # A contract_ids element that is not a string must be rejected as a
    # per-sample ValueError (SAMPLE_CHECK_FAILED), never leak a TypeError
    # from raw set() construction on the untrusted payload.
    payload["samples"][0]["catalog"]["problem"]["constraint_model"]["relations"][0][
        "contract_ids"
    ][0] = {"evil": True}
    broken = write_fixture(payload, tmp_path / "broken-contract-ids.json")
    report_path = tmp_path / "report.json"

    exit_code = main(["--fixture", str(broken), "--report", str(report_path)])

    assert exit_code == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sample_count"] == 192
    assert report["agreed"] == 191
    assert [row["sample_id"] for row in report["errors"]] == [broken_id]
    assert [row["reason"] for row in report["errors"]] == ["SAMPLE_CHECK_FAILED"]
    assert "contract_ids" in report["errors"][0]["detail"]
    assert report["disagreed"] == []
