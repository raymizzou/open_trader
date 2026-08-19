from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "cutover_us_tiger_to_futu.py"
)
SPEC = importlib.util.spec_from_file_location("cutover_us_tiger_to_futu", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
cutover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutover)


NOW = datetime(2026, 8, 19, 3, 0, 0, tzinfo=timezone.utc)

TIGER_REPORT = {
    "as_of_date": "2026-07-15",
    "generated_at": "2026-07-15T22:00:00+08:00",
    "signal_snapshots": {
        "candidates": [
            {"symbol": "AAPL", "right_side": True, "danger": False},
        ],
        "holdings": {
            "MSFT": {"symbol": "MSFT", "temperature": {"current": "温"}},
        },
        "real_holdings": {
            "AAPL": {
                "symbol": "AAPL",
                "temperature": {"current": "热", "previous": "温", "changed": True},
            },
            "SNOW": None,
        },
    },
}


def _seed_roots(tmp_path: Path) -> None:
    tiger_data = tmp_path / "data" / "trend_us_tiger"
    tiger_data.mkdir(parents=True)
    (tiger_data / "protection_state.json").write_text(
        json.dumps({"schema_version": 1, "symbols": ["AAPL"]}), encoding="utf-8"
    )
    (tiger_data / "real_protection_state.json").write_text(
        json.dumps({"schema_version": 1, "instruments": {}}), encoding="utf-8"
    )
    (tiger_data / "watch_events.jsonl").write_text(
        '{"kind": "attention", "symbol": "AAPL"}\n'
        '{"kind": "delivery", "date": "2026-08-14"}\n',
        encoding="utf-8",
    )
    (tiger_data / "daily_delivery").mkdir()
    (tiger_data / "daily_delivery" / "2026-08-15-ledger.json").write_text(
        json.dumps({"date": "2026-08-15"}), encoding="utf-8"
    )
    # Files the cutover must leave alone on the tiger side.
    (tiger_data / "old_baseline.json").write_text(
        json.dumps({"as_of_date": "2026-07-01"}), encoding="utf-8"
    )

    tiger_reports = tmp_path / "reports" / "trend_us_tiger"
    tiger_reports.mkdir(parents=True)
    (tiger_reports / "2026-07-14.json").write_text(
        json.dumps({
            "as_of_date": "2026-07-14",
            "signal_snapshots": {"candidates": [{"symbol": "QQQ"}]},
        }),
        encoding="utf-8",
    )
    (tiger_reports / "2026-07-15.json").write_text(
        json.dumps(TIGER_REPORT), encoding="utf-8"
    )

    # July-era legacy futu state that must be archived, not resurrected.
    legacy_data = tmp_path / "data" / "trend_us_futu"
    legacy_data.mkdir(parents=True)
    (legacy_data / "protection_state.json").write_text(
        json.dumps({"schema_version": 1, "legacy": True}), encoding="utf-8"
    )
    legacy_reports = tmp_path / "reports" / "trend_us_futu"
    legacy_reports.mkdir(parents=True)
    (legacy_reports / "2026-07-15.json").write_text(
        json.dumps({"as_of_date": "2026-07-15"}), encoding="utf-8"
    )


def _all_tiger_state_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        name: tmp_path / "data" / "trend_us_tiger" / name
        for name in (
            "protection_state.json",
            "real_protection_state.json",
            "watch_events.jsonl",
            "daily_delivery/2026-08-15-ledger.json",
            "old_baseline.json",
        )
    }


def test_dry_run_prints_plan_without_touching_filesystem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_roots(tmp_path)
    before = {
        path: path.read_bytes()
        for path in _all_tiger_state_paths(tmp_path).values()
    }

    return_code = cutover.main([
        "--dry-run",
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
    ])
    out = capsys.readouterr()

    assert return_code == 0
    assert "[dry-run]" in out.out
    assert "归档" in out.out
    assert "迁移 protection_state.json" in out.out
    assert "重建 attention_baseline.json" in out.out
    # Dry-run must never create anything.
    for path, body in before.items():
        assert path.read_bytes() == body
    assert not (tmp_path / "data" / "archive").exists()
    assert not (tmp_path / "reports" / "archive").exists()
    assert not (tmp_path / "data" / "trend_us_futu" / "attention_baseline.json").exists()
    assert not (tmp_path / "data" / "trend_us_futu" / ".cutover-complete.json").exists()
    assert not (tmp_path / "data" / "trend_us_futu" / "manifest.json").exists()


def test_dry_run_manifest_lists_planned_actions_and_validations(
    tmp_path: Path,
) -> None:
    _seed_roots(tmp_path)

    manifest = cutover._run(
        data_root=tmp_path / "data",
        reports_root=tmp_path / "reports",
        dry_run=True,
        now=NOW,
    )

    assert manifest["dry_run"] is True
    assert manifest["cutover"] == cutover.CUTOVER_ID
    assert manifest["archives"] == {
        "data": "archive/trend_us_futu-20260819T030000",
        "reports": "archive/trend_us_futu-20260819T030000",
    }
    assert manifest["migrated"] == {
        "protection_state.json": "dry-run",
        "real_protection_state.json": "dry-run",
        "watch_events.jsonl": "dry-run",
        "daily_delivery/": "dry-run",
    }
    actions = " ".join(str(item) for item in manifest["actions"])
    assert "归档" in actions
    assert "迁移 protection_state.json -> protection_state.json（dry-run）" in actions
    assert "重建 attention_baseline.json（来源 2026-07-15.json" in actions
    baseline = manifest["baseline"]
    assert isinstance(baseline, dict)
    assert baseline["as_of_date"] == "2026-07-15"
    assert baseline["source"] == "trend_us_tiger/2026-07-15.json"
    assert baseline["candidates"] == 1
    assert baseline["holdings"] == 1
    assert baseline["real_holdings"] == 2
    assert manifest["ledger"] == {
        "files": 1, "from": "2026-08-15", "to": "2026-08-15",
        "note": "投递去重账本，防切换日重发飞书",
    }
    assert all(item["ok"] is True for item in manifest["validations"])


def test_real_mode_migrates_archives_and_rebuilds_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_roots(tmp_path)

    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
    ])
    capsys.readouterr()

    assert return_code == 0
    futu_data = tmp_path / "data" / "trend_us_futu"
    for name in (
        "protection_state.json",
        "real_protection_state.json",
        "watch_events.jsonl",
        "daily_delivery/2026-08-15-ledger.json",
        "attention_baseline.json",
        ".cutover-complete.json",
    ):
        assert (futu_data / name).is_file(), name
    # Tiger side keeps only the files the cutover leaves behind.
    assert (tmp_path / "data" / "trend_us_tiger" / "old_baseline.json").is_file()
    assert not (tmp_path / "data" / "trend_us_tiger" / "protection_state.json").exists()
    # Tiger reports stay in place as read-only history.
    assert (tmp_path / "reports" / "trend_us_tiger" / "2026-07-15.json").is_file()

    data_archives = list((tmp_path / "data" / "archive").glob("trend_us_futu-*"))
    reports_archives = list((tmp_path / "reports" / "archive").glob("trend_us_futu-*"))
    assert len(data_archives) == 1
    assert len(reports_archives) == 1
    assert (data_archives[0] / "protection_state.json").is_file()
    assert (data_archives[0] / "manifest.json").is_file()
    assert (reports_archives[0] / "2026-07-15.json").is_file()

    baseline = json.loads(
        (futu_data / "attention_baseline.json").read_text(encoding="utf-8")
    )
    assert baseline["as_of_date"] == "2026-07-15"
    assert baseline["rebuilt_from"] == "trend_us_tiger/2026-07-15.json"
    snapshots = baseline["signal_snapshots"]
    assert snapshots["real_holdings"]["AAPL"]["temperature"] == {
        "current": "热", "previous": "温", "changed": True,
    }
    assert snapshots["real_holdings"]["SNOW"] is None

    marker = json.loads((futu_data / ".cutover-complete.json").read_text(encoding="utf-8"))
    assert marker["dry_run"] is False
    assert marker["executed_at"]
    # The marker/archive manifest copies are the manifest written before the
    # marker/archive keys are appended; the returned manifest carries them.
    archive_manifest = json.loads(
        (data_archives[0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert archive_manifest["cutover"] == cutover.CUTOVER_ID
    assert archive_manifest["dry_run"] is False

    # Re-running with the marker present is idempotent and exits 0.
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in futu_data.rglob("*")
        if path.is_file()
    }
    again = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
    ])
    out = capsys.readouterr()
    assert again == 0
    assert "已迁移" in out.out
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in futu_data.rglob("*")
        if path.is_file()
    } == before


def test_real_mode_manifest_records_ledger_range_before_move(
    tmp_path: Path,
) -> None:
    _seed_roots(tmp_path)

    manifest = cutover._run(
        data_root=tmp_path / "data",
        reports_root=tmp_path / "reports",
        dry_run=False,
        now=NOW,
    )

    # The ledger range is computed before daily_delivery/ is moved, so a real
    # run records the actual range instead of an always-empty ledger.
    assert manifest["dry_run"] is False
    assert manifest["ledger"] == {
        "files": 1, "from": "2026-08-15", "to": "2026-08-15",
        "note": "投递去重账本，防切换日重发飞书",
    }
    assert manifest["migrated"]["daily_delivery/"] == "moved"
    assert not (tmp_path / "data" / "trend_us_tiger" / "daily_delivery").exists()
    assert (tmp_path / "data" / "trend_us_futu" / "daily_delivery").is_dir()


def test_missing_tiger_state_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "FAIL: 缺少老虎数据目录" in err

    _seed_roots(tmp_path)
    (tmp_path / "data" / "trend_us_tiger" / "protection_state.json").unlink()
    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "FAIL: 迁移前校验失败" in err
