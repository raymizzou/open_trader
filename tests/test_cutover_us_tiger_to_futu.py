from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from open_trader.daily_premarket import DailyPremarketConfig


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
# Production-like cutover moment: 2026-08-20 09:11 +08:00, i.e. after the
# 08-19 US buy window (16:00 ET) so the legacy cutover authorization window
# passes exactly like the manual production fix.
CUTOVER_NOW = datetime(2026, 8, 20, 9, 11, 35, tzinfo=timezone(timedelta(hours=8)))
# 2026-08-19 12:00 UTC = 08:00 ET: still inside the anchor execution day's US
# buy window (which closes 16:00 ET), so the authorization-window check must
# fail closed before any file is moved.
PRE_WINDOW_NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

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


def _seed_roots(
    tmp_path: Path,
    *,
    batch_cycles: tuple[tuple[str, str], ...] = (
        ("2026-08-15", "2026-08-14"),
        ("2026-08-18", "2026-08-17"),
        ("2026-08-19", "2026-08-18"),
    ),
    daily_dates: tuple[str, ...] = ("2026-08-18", "2026-08-19"),
) -> None:
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

    # Tiger-era US batch ledger (the durable-cycle source that must be
    # archived) and the daily close ledger (trading-day evidence).  The
    # anchor-date derivation only reads the date prefix of the filenames,
    # so the payloads carry the same shape but the dates are parameterized.
    batches = tmp_path / "data" / "trend_review" / "ledgers" / "US" / "batches"
    batches.mkdir(parents=True)
    for execution, as_of in batch_cycles:
        (batches / f"{execution}.json").write_text(
            json.dumps({
                "schema_version": "open_trader.trend_review.batch.v1",
                "market": "US",
                "execution_date": execution,
                "report_path": str(
                    tmp_path / "reports" / "trend_us_tiger" / f"{as_of}.json"
                ),
            }),
            encoding="utf-8",
        )
    daily = tmp_path / "data" / "trend_review" / "daily" / "US"
    daily.mkdir(parents=True)
    for trading_date in daily_dates:
        (daily / f"{trading_date}.json").write_text(
            json.dumps({"trading_date": trading_date}), encoding="utf-8"
        )


def _test_config(tmp_path: Path, *, executor_host: str | None = None) -> DailyPremarketConfig:
    """Config with the local host as trend executor, rooted in the tmp data."""
    return DailyPremarketConfig(
        repo=tmp_path,
        python=Path(sys.executable),
        timezone="Asia/Shanghai",
        deadline="09:30",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "data" / "logs",
        portfolio=tmp_path / "data" / "latest" / "portfolio.csv",
        trend_review_us_simulate_acc_id=0,
        trend_executor_host=executor_host or socket.gethostname(),
    )


def _write_env_file(tmp_path: Path, *, host: str | None = None) -> Path:
    env_path = tmp_path / "daily_premarket.env"
    env_path.write_text(
        f"OPEN_TRADER_TREND_EXECUTOR_HOST={host or socket.gethostname()}\n",
        encoding="utf-8",
    )
    return env_path


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
    batch_files = list(
        (tmp_path / "data" / "trend_review" / "ledgers" / "US" / "batches").glob("*.json")
    )
    before_batches = {path.name: path.read_bytes() for path in batch_files}

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
    # Hardening steps print their plan in dry-run.
    assert "批次账本归档：3 份（2026-08-15 至 2026-08-19）" in out.out
    assert "锚定切割前周期：as_of=2026-08-18 / execution=2026-08-19" in out.out
    # Dry-run must never create anything.
    for path, body in before.items():
        assert path.read_bytes() == body
    for name, body in before_batches.items():
        assert (
            tmp_path / "data" / "trend_review" / "ledgers" / "US" / "batches" / name
        ).read_bytes() == body
    assert not (tmp_path / "data" / "archive").exists()
    assert not (tmp_path / "reports" / "archive").exists()
    assert not (tmp_path / "data" / "trend_us_futu" / "attention_baseline.json").exists()
    assert not (tmp_path / "data" / "trend_us_futu" / ".cutover-complete.json").exists()
    assert not (tmp_path / "data" / "trend_us_futu" / "manifest.json").exists()
    assert not (tmp_path / "data" / "trend_controller").exists()


def test_dry_run_manifest_lists_planned_actions_and_validations(
    tmp_path: Path,
) -> None:
    _seed_roots(tmp_path)

    manifest = cutover._run(
        data_root=tmp_path / "data",
        reports_root=tmp_path / "reports",
        dry_run=True,
        now=NOW,
        anchor_as_of="2026-08-18",
        anchor_execution="2026-08-19",
    )

    assert manifest["dry_run"] is True
    assert manifest["cutover"] == cutover.CUTOVER_ID
    assert manifest["archives"] == {
        "data": "archive/trend_us_futu-20260819T030000",
        "reports": "archive/trend_us_futu-20260819T030000",
        "batches": "archive/trend_us_batches-tiger-era-20260819T030000",
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
    assert "归档老虎时代批次账本 3 份 -> " in actions
    assert "锚定切割前周期 as_of=2026-08-18 / execution=2026-08-19" in actions
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
    assert manifest["batch_archive"] == {
        "files": 3,
        "from": "2026-08-15",
        "to": "2026-08-19",
        "destination": "archive/trend_us_batches-tiger-era-20260819T030000",
        "note": "老虎时代批次账本归档（移动不删除），防 durable 周期回溯到 7 月",
    }
    assert manifest["anchor_cycle"] == {
        "market": "US",
        "as_of_date": "2026-08-18",
        "execution_date": "2026-08-19",
        "skipped": False,
        "legacy_cutover_path": None,
        "revision_request_path": None,
        "note": "锚定切割前最后一个老虎周期为已完成，防控制器回溯补产旧报告",
    }
    assert all(item["ok"] is True for item in manifest["validations"])


def test_real_mode_migrates_archives_and_rebuilds_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roots(tmp_path)
    monkeypatch.setattr(cutover, "_now", lambda: CUTOVER_NOW)

    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(_write_env_file(tmp_path)),
        "--actor", "test-actor",
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

    # Hardening steps ran through the CLI path as well: batch ledger archived,
    # revision request + legacy cutover anchored.
    batch_archives = list(
        (tmp_path / "data" / "archive").glob("trend_us_batches-tiger-era-*")
    )
    assert len(batch_archives) == 1
    assert sorted(
        path.name for path in batch_archives[0].glob("*.json")
        if path.name != "manifest.json"
    ) == ["2026-08-15.json", "2026-08-18.json", "2026-08-19.json"]
    assert not list(
        (tmp_path / "data" / "trend_review" / "ledgers" / "US" / "batches").glob("*.json")
    )
    assert (
        tmp_path / "data" / "trend_controller" / "US" / "revision_requests" / "2026-08-18.json"
    ).is_file()
    assert (
        tmp_path / "data" / "trend_controller" / "US" / "legacy_cutovers" / "2026-08-18.json"
    ).is_file()

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
        "--config", str(_write_env_file(tmp_path)),
        "--actor", "test-actor",
    ])
    out = capsys.readouterr()
    assert again == 0
    assert "已迁移" in out.out
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in futu_data.rglob("*")
        if path.is_file()
    } == before


def test_real_mode_archives_batches_and_anchors_last_cycle(
    tmp_path: Path,
) -> None:
    _seed_roots(tmp_path)

    manifest = cutover._run(
        data_root=tmp_path / "data",
        reports_root=tmp_path / "reports",
        dry_run=False,
        now=CUTOVER_NOW,
        anchor_as_of="2026-08-18",
        anchor_execution="2026-08-19",
        config=_test_config(tmp_path),
        actor="test-actor",
    )

    # Batch ledger archived (moved, not deleted) and source emptied.
    batch_source = tmp_path / "data" / "trend_review" / "ledgers" / "US" / "batches"
    batch_archives = list(
        (tmp_path / "data" / "archive").glob("trend_us_batches-tiger-era-*")
    )
    assert len(batch_archives) == 1
    assert sorted(
        path.name for path in batch_archives[0].glob("*.json")
        if path.name != "manifest.json"
    ) == ["2026-08-15.json", "2026-08-18.json", "2026-08-19.json"]
    assert not list(batch_source.glob("*.json"))
    assert (batch_archives[0] / "manifest.json").is_file()

    # Revision request with empty baseline for the anchored cycle.
    request_path = (
        tmp_path / "data" / "trend_controller" / "US" / "revision_requests" / "2026-08-18.json"
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["schema_version"] == "open_trader.trend_controller.revision_request.v1"
    assert request["market"] == "US"
    assert request["as_of_date"] == "2026-08-18"
    assert request["execution_date"] == "2026-08-19"
    assert request["baseline_report_path"] is None
    assert request["baseline_report_sha256"] is None
    assert request["baseline_revision"] == -1

    # report_missing legacy cutover anchored to that request.
    cutover_record = json.loads(
        (
            tmp_path / "data" / "trend_controller" / "US" / "legacy_cutovers" / "2026-08-18.json"
        ).read_text(encoding="utf-8")
    )
    assert cutover_record["schema_version"] == "open_trader.trend_controller.legacy_cutover.v1"
    assert cutover_record["market"] == "US"
    assert cutover_record["as_of_date"] == "2026-08-18"
    assert cutover_record["execution_date"] == "2026-08-19"
    assert cutover_record["report_missing"] is True
    assert cutover_record["report_path"] is None
    assert cutover_record["report_sha256"] is None
    assert cutover_record["actor"] == "test-actor"
    assert cutover_record["revision_request_sha256"] == hashlib.sha256(
        request_path.read_bytes()
    ).hexdigest()

    # Manifest records both hardening steps.
    assert manifest["batch_archive"] == {
        "files": 3,
        "from": "2026-08-15",
        "to": "2026-08-19",
        "destination": "archive/trend_us_batches-tiger-era-20260820T091135",
        "note": "老虎时代批次账本归档（移动不删除），防 durable 周期回溯到 7 月",
    }
    assert manifest["anchor_cycle"]["market"] == "US"
    assert manifest["anchor_cycle"]["as_of_date"] == "2026-08-18"
    assert manifest["anchor_cycle"]["execution_date"] == "2026-08-19"
    assert manifest["anchor_cycle"]["skipped"] is False
    assert manifest["anchor_cycle"]["legacy_cutover_path"] == (
        "trend_controller/US/legacy_cutovers/2026-08-18.json"
    )
    assert manifest["anchor_cycle"]["revision_request_path"] == (
        "trend_controller/US/revision_requests/2026-08-18.json"
    )


def test_real_mode_rerun_is_idempotent_for_batches_and_anchor(
    tmp_path: Path,
) -> None:
    _seed_roots(tmp_path)
    config = _test_config(tmp_path)

    first = cutover._run(
        data_root=tmp_path / "data",
        reports_root=tmp_path / "reports",
        dry_run=False,
        now=CUTOVER_NOW,
        anchor_as_of="2026-08-18",
        anchor_execution="2026-08-19",
        config=config,
        actor="test-actor",
    )
    assert first["anchor_cycle"]["skipped"] is False
    request_bytes = (
        tmp_path / "data" / "trend_controller" / "US" / "revision_requests" / "2026-08-18.json"
    ).read_bytes()
    cutover_bytes = (
        tmp_path / "data" / "trend_controller" / "US" / "legacy_cutovers" / "2026-08-18.json"
    ).read_bytes()

    # Simulate the recovery state of a second attempt: the hardening steps
    # already ran (batches archived, anchor written) while the tiger-side
    # files are back in place, so the pre-migration validations pass again.
    futu_data = tmp_path / "data" / "trend_us_futu"
    tiger_data = tmp_path / "data" / "trend_us_tiger"
    for name in (
        "protection_state.json",
        "real_protection_state.json",
        "watch_events.jsonl",
    ):
        shutil.copy2(futu_data / name, tiger_data / name)
    shutil.copytree(futu_data / "daily_delivery", tiger_data / "daily_delivery")

    # Re-running must not re-archive or re-anchor, and must not error.
    second = cutover._run(
        data_root=tmp_path / "data",
        reports_root=tmp_path / "reports",
        dry_run=False,
        now=CUTOVER_NOW,
        anchor_as_of="2026-08-18",
        anchor_execution="2026-08-19",
        config=config,
        actor="test-actor",
    )
    assert second["anchor_cycle"]["skipped"] is True
    assert second["batch_archive"]["files"] == 0
    assert not list(
        (tmp_path / "data" / "trend_review" / "ledgers" / "US" / "batches").glob("*.json")
    )
    batch_archives = list(
        (tmp_path / "data" / "archive").glob("trend_us_batches-tiger-era-*")
    )
    assert len(batch_archives) == 1
    assert (
        tmp_path / "data" / "trend_controller" / "US" / "revision_requests" / "2026-08-18.json"
    ).read_bytes() == request_bytes
    assert (
        tmp_path / "data" / "trend_controller" / "US" / "legacy_cutovers" / "2026-08-18.json"
    ).read_bytes() == cutover_bytes


def test_real_mode_manifest_records_ledger_range_before_move(
    tmp_path: Path,
) -> None:
    _seed_roots(tmp_path)

    manifest = cutover._run(
        data_root=tmp_path / "data",
        reports_root=tmp_path / "reports",
        dry_run=False,
        now=CUTOVER_NOW,
        anchor_as_of="2026-08-18",
        anchor_execution="2026-08-19",
        config=_test_config(tmp_path),
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
    env_file = _write_env_file(tmp_path)
    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(env_file),
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "FAIL: 缺少老虎数据目录" in err

    _seed_roots(tmp_path)
    (tmp_path / "data" / "trend_us_tiger" / "protection_state.json").unlink()
    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(env_file),
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "FAIL: 迁移前校验失败" in err


def test_real_mode_anchor_fails_closed_without_executor_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real mode aborts before touching anything when the config cannot prove
    the trend executor identity."""
    _seed_roots(tmp_path)
    env_file = _write_env_file(tmp_path, host="some-other-host.example")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(env_file),
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "FAIL: trend automation is readonly" in err
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_real_mode_anchor_fails_closed_without_config_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real mode fails closed when the env config file is missing."""
    _seed_roots(tmp_path)
    missing = tmp_path / "no-such.env"
    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(missing),
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "FAIL: 缺少配置文件" in err
    assert not (tmp_path / "data" / "archive").exists()


def test_real_mode_no_trading_day_evidence_fails_closed_before_any_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real mode fails closed on the anchor dates before any file is moved
    when neither the batch nor the daily close ledger has dated evidence."""
    _seed_roots(tmp_path, batch_cycles=(), daily_dates=())
    env_file = _write_env_file(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(env_file),
        "--actor", "test-actor",
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "均无交易日证据" in err
    # Fail-closed happened before any archive or migration: no archive
    # directory was created and no file moved.
    assert not (tmp_path / "data" / "archive").exists()
    assert not (tmp_path / "reports" / "archive").exists()
    assert (tmp_path / "data" / "trend_us_futu" / "protection_state.json").is_file()
    assert (tmp_path / "data" / "trend_us_tiger" / "protection_state.json").is_file()
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_real_mode_single_day_evidence_fails_closed_before_any_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real mode fails closed on the anchor dates before any file is moved
    when only one trading day is on record (no prior completed day)."""
    _seed_roots(
        tmp_path,
        batch_cycles=(("2026-08-19", "2026-08-18"),),
        daily_dates=(),
    )
    env_file = _write_env_file(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(env_file),
        "--actor", "test-actor",
    ])
    err = capsys.readouterr().err
    assert return_code == 1
    assert "之前没有已完成的交易日" in err
    # Fail-closed happened before any archive or migration: no archive
    # directory was created and no file moved.
    assert not (tmp_path / "data" / "archive").exists()
    assert not (tmp_path / "reports" / "archive").exists()
    assert (tmp_path / "data" / "trend_us_futu" / "protection_state.json").is_file()
    assert (tmp_path / "data" / "trend_us_tiger" / "protection_state.json").is_file()
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_real_mode_anchor_authorization_window_fails_closed_before_window_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real mode fails closed before any file is moved when the authorization
    moment is still inside the anchor execution day's US buy window (before
    16:00 ET), the untested branch of _check_anchor_authorization_window."""
    _seed_roots(tmp_path)
    monkeypatch.setattr(cutover, "_now", lambda: PRE_WINDOW_NOW)
    env_file = _write_env_file(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    return_code = cutover.main([
        "--data-root", str(tmp_path / "data"),
        "--reports-root", str(tmp_path / "reports"),
        "--config", str(env_file),
        "--actor", "test-actor",
    ])
    err = capsys.readouterr().err
    window_end = datetime.combine(
        date(2026, 8, 19),
        cutover.BUY_WINDOWS["US"][1],
        tzinfo=cutover.TIMEZONES["US"],
    )
    expected = (
        "FAIL: 锚定授权窗口未开启：legacy cutover 写入要求授权时刻晚于 "
        f"{window_end.isoformat()}（{cutover.TIMEZONES['US']}）且 as_of "
        f"2026-08-18 早于授权日；当前授权时刻 "
        f"{PRE_WINDOW_NOW.astimezone(cutover.TIMEZONES['US']).isoformat()}"
    )
    assert return_code == 1
    assert err.strip() == expected
    # Fail-closed happened before any archive or migration: no archive
    # directory was created and no file moved.
    assert not (tmp_path / "data" / "archive").exists()
    assert not (tmp_path / "reports" / "archive").exists()
    assert (tmp_path / "data" / "trend_us_futu" / "protection_state.json").is_file()
    assert (tmp_path / "data" / "trend_us_tiger" / "protection_state.json").is_file()
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before
