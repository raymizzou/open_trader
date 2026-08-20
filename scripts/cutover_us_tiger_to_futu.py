#!/usr/bin/env python3
"""US trend account cutover: tiger -> futu state migration (2026-08-19).

The US trend real-holding role moved from the tiger account to the futu
account; code now reads ``data/trend_us_futu`` and ``reports/trend_us_futu``
exclusively.  This script performs the one-shot data migration at the cutover
window.  It is idempotent and auditable, and supports ``--dry-run`` (print the
full plan and validations without touching the filesystem).

What it does:

1. Archive (never delete) the legacy July-era futu state directories
   ``data/trend_us_futu/`` and ``reports/trend_us_futu/`` into timestamped
   directories under ``data/archive/`` and ``reports/archive/``.  Those files
   predate the 07-16 tiger cutover and must not resurrect stale protection
   lines or July reports that would produce fake attention diffs.
2. Move the tiger state that must carry over into ``data/trend_us_futu/``:
   ``protection_state.json``, ``real_protection_state.json``,
   ``watch_events.jsonl`` and the ``daily_delivery/`` delivery-dedup ledger
   (prevents re-sending Feishu deliveries on cutover day).
3. Rebuild ``data/trend_us_futu/attention_baseline.json`` from the LAST
   ``reports/trend_us_tiger/*.json`` report's ``signal_snapshots`` (including
   the ``real_holdings`` key the option-attention real-holding rows require),
   never from the July legacy baseline, so the first futu report diffs against
   the most recent tiger view instead of month-old data.
4. ``reports/trend_us_tiger/`` stays in place as read-only history: it is
   neither migrated nor deleted.
5. Archive (move, never delete) the tiger-era US batch ledger
   ``data/trend_review/ledgers/US/batches/*.json`` into a timestamped
   directory under ``data/archive/``.  The controller's durable-cycle
   discovery reads that ledger; leaving it in place would make the futu
   controller treat July tiger-era cycles as unfinished and try to backfill
   reports that predate the cutover.
6. Anchor the last pre-cutover cycle (the one immediately before the
   controller's current cycle, in production as_of 2026-08-18 / execution
   2026-08-19): write a revision request with an empty baseline plus a
   ``report_missing`` legacy cutover record so that cycle counts as
   completed without a futu-identity report.  Idempotent: a cycle that
   already has a legacy cutover record is left untouched; dry-run only
   prints the plan (anchor dates, batch archive count) without writing.

The anchor step needs the trend executor identity: real mode reads
``OPEN_TRADER_TREND_EXECUTOR_HOST`` from ``--config`` (default
``config/daily_premarket.env``) and aborts (fail-closed) unless the local
host is the trend executor; ``--dry-run`` never touches the controller
functions.

Completion is recorded in ``data/trend_us_futu/.cutover-complete.json``; a
re-run with that marker present exits successfully without repeating any step.

Run by the operator at the cutover window (main agent).  Never auto-run by
services.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import shutil
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

from open_trader.daily_premarket import DailyPremarketConfig, require_trend_executor
from open_trader.trend_market_controller import (
    BUY_WINDOWS,
    TIMEZONES,
    ControllerCycle,
    _record_legacy_cycle_cutover,
    _request_revision,
)

CUTOVER_ID = "futu-us-trend-cutover-2026-08-19"
MARKER_NAME = ".cutover-complete.json"
MANIFEST_NAME = "manifest.json"
BASELINE_NAME = "attention_baseline.json"
TIGER_DATA_DIR = "trend_us_tiger"
FUTU_DATA_DIR = "trend_us_futu"
TIGER_REPORTS_DIR = "trend_us_tiger"
FUTU_REPORTS_DIR = "trend_us_futu"
MIGRATED_FILES = (
    "protection_state.json",
    "real_protection_state.json",
    "watch_events.jsonl",
)
MIGRATED_DIRECTORIES = ("daily_delivery",)
BATCH_LEDGER_REL = Path("trend_review") / "ledgers" / "US" / "batches"
DAILY_CLOSE_REL = Path("trend_review") / "daily" / "US"
BATCH_ARCHIVE_STEM = "trend_us_batches-tiger-era"
ANCHOR_MARKET = "US"
ANCHOR_REASON = (
    "US trend account cutover tiger->futu: anchor last tiger-era cycle as "
    "complete; history preserved in reports/trend_us_tiger, tiger-era "
    "batches archived"
)
_DAILY_LEDGER_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _json_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} 不是可解析的 JSON：{path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} 不是 JSON 对象：{path}")
    return payload


def _read_jsonl_lines(path: Path) -> list[Mapping[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label_for(path)} 不可读：{path}") from exc
    rows: list[Mapping[str, object]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} 第 {number} 行不是 JSON") from exc
        if not isinstance(row, Mapping):
            raise ValueError(f"{path} 第 {number} 行不是 JSON 对象")
        rows.append(row)
    return rows


def label_for(path: Path) -> str:
    return path.name


def _latest_tiger_report(tiger_reports: Path) -> tuple[Path, Mapping[str, object]] | None:
    """Return the newest tiger report JSON by (as_of_date, generated_at, stem)."""
    if not tiger_reports.is_dir():
        return None
    candidates: list[tuple[object, object, str, Path, Mapping[str, object]]] = []
    for path in sorted(tiger_reports.glob("*.json")):
        try:
            payload = _json_object(path, label_for(path))
        except ValueError:
            continue
        snapshots = payload.get("signal_snapshots")
        if not isinstance(snapshots, Mapping):
            continue
        as_of_date = payload.get("as_of_date")
        candidates.append(
            (as_of_date, payload.get("generated_at"), path.name, path, payload)
        )
    if not candidates:
        return None
    _, _, _, path, payload = max(
        candidates, key=lambda item: (str(item[0]), str(item[1]), item[2])
    )
    return path, payload


def _baseline_payload(report: Mapping[str, object]) -> dict[str, object]:
    """Extract the attention baseline from a report payload.

    The baseline keeps the report's ``signal_snapshots`` underlying rows (the
    shape ``_attention_rows`` requires: candidates list, holdings and
    real_holdings symbol maps with ``None``/row values) and the ``as_of_date``
    so ``_previous_attention_rows`` can diff the first futu report against the
    most recent tiger view.
    """
    snapshots = report.get("signal_snapshots")
    if not isinstance(snapshots, Mapping):
        raise ValueError("最后一份老虎报告中 signal_snapshots 缺失")
    rebuilt: dict[str, object] = dict(snapshots)
    if not isinstance(rebuilt.get("candidates"), list) or not all(
        isinstance(row, Mapping) for row in rebuilt["candidates"]
    ):
        raise ValueError("最后一份老虎报告 signal_snapshots.candidates 形状无效")
    for key in ("holdings", "real_holdings"):
        values = rebuilt.get(key, {})
        if not isinstance(values, Mapping) or not all(
            row is None or isinstance(row, Mapping) for row in values.values()
        ):
            rebuilt[key] = {}
    as_of_date = report.get("as_of_date")
    if not isinstance(as_of_date, str):
        raise ValueError("最后一份老虎报告缺少 as_of_date")
    return {
        "as_of_date": as_of_date,
        "signal_snapshots": rebuilt,
        "rebuilt_from": f"{TIGER_REPORTS_DIR}/<latest>.json",
    }


def _ledger_range(directory: Path) -> tuple[int, str, str]:
    dates: list[str] = []
    if directory.is_dir():
        for path in directory.glob("*.json"):
            match = _DAILY_LEDGER_DATE.match(path.name)
            if match:
                dates.append(match.group(1))
    if not dates:
        return 0, "", ""
    return len(dates), min(dates), max(dates)


def _unique_archive_path(archive_root: Path, stem: str, now: datetime) -> Path:
    # Pure path computation: dry-run must never touch the filesystem, and the
    # real-mode move creates the archive root right before the move.
    base = f"{stem}-{now.strftime('%Y%m%dT%H%M%S')}"
    candidate = archive_root / base
    suffix = 1
    while candidate.exists():
        candidate = archive_root / f"{base}-{suffix}"
        suffix += 1
    return candidate


def _move(source: Path, target: Path) -> str:
    if target.exists():
        return "already-present"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return "moved"


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _read_env_values(path: Path) -> dict[str, str]:
    """Parse the daily_premarket env file (mirrors daily_premarket._read_env_file)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _controller_config(
    data_root: Path,
    reports_root: Path,
    env_values: Mapping[str, str],
) -> DailyPremarketConfig:
    """Build the controller config the anchor step needs.

    Only the fields the controller's revision/cutover helpers consume are
    sourced from the env file; data/reports roots always come from the CLI so
    dry-run/real mode stay consistent with ``--data-root``/``--reports-root``.
    """
    repo = Path(env_values.get("OPEN_TRADER_REPO", str(data_root.parent))).expanduser()
    return DailyPremarketConfig(
        repo=repo,
        python=Path(env_values.get("OPEN_TRADER_PYTHON", sys.executable)),
        timezone=env_values.get("OPEN_TRADER_TIMEZONE", "Asia/Shanghai"),
        deadline=env_values.get("OPEN_TRADER_DEADLINE", "09:30"),
        futu_host=env_values.get("OPEN_TRADER_FUTU_HOST", "127.0.0.1"),
        futu_port=int(env_values.get("OPEN_TRADER_FUTU_PORT", "11111")),
        data_dir=data_root,
        reports_dir=reports_root,
        logs_dir=data_root / "logs",
        portfolio=data_root / "latest" / "portfolio.csv",
        trend_review_us_simulate_acc_id=int(
            env_values.get("OPEN_TRADER_TREND_REVIEW_US_SIMULATE_ACC_ID", "0") or "0"
        ),
        trend_executor_host=env_values.get("OPEN_TRADER_TREND_EXECUTOR_HOST", ""),
    )


def _us_cycle_calendar(data_root: Path) -> list[date]:
    """Trading-day evidence for the US ledger: batch execution dates (the
    tiger-era ledger that drove the durable cycles) plus daily close dates.
    Sorted, deduplicated; never touches the filesystem beyond reads.
    """
    days: set[date] = set()
    for root in (data_root / BATCH_LEDGER_REL, data_root / DAILY_CLOSE_REL):
        for path in root.glob("*.json"):
            match = _DAILY_LEDGER_DATE.match(path.name)
            if match:
                days.add(date.fromisoformat(match.group(1)))
    return sorted(days)


def _anchor_cycle_dates(data_root: Path) -> tuple[str, str]:
    """Return ``(as_of_date, execution_date)`` of the cycle immediately before
    the controller's current cycle.

    Semantics match production (as_of 2026-08-18 / execution 2026-08-19):
    current as_of = most recent completed trading session on record;
    anchor execution = that current as_of; anchor as_of = the trading day
    right before it.  With no trading-day evidence the step fails closed
    instead of guessing.
    """
    calendar = _us_cycle_calendar(data_root)
    if not calendar:
        raise ValueError(
            f"无法确定锚定周期：{data_root / BATCH_LEDGER_REL} 与 "
            f"{data_root / DAILY_CLOSE_REL} 均无交易日证据"
        )
    current_as_of = calendar[-1]
    prior = [item for item in calendar if item < current_as_of]
    if not prior:
        raise ValueError(
            f"无法确定锚定周期：{current_as_of} 之前没有已完成的交易日"
        )
    return prior[-1].isoformat(), current_as_of.isoformat()


def _check_anchor_authorization_window(
    anchor_as_of: str, anchor_execution: str, now: datetime
) -> None:
    """Fail closed before any file is moved on the authorization-window
    conditions of ``_record_legacy_cycle_cutover`` that are decidable
    locally: the write must happen after the buy window closes on the
    anchor execution date, and the anchor as_of must predate the
    authorization date."""
    market = ANCHOR_MARKET
    window_end = datetime.combine(
        date.fromisoformat(anchor_execution),
        BUY_WINDOWS[market][1],
        tzinfo=TIMEZONES[market],
    )
    authorized = now.astimezone(TIMEZONES[market])
    if not (
        authorized > window_end
        and date.fromisoformat(anchor_as_of) < authorized.date()
    ):
        raise ValueError(
            f"锚定授权窗口未开启：legacy cutover 写入要求授权时刻晚于 "
            f"{window_end.isoformat()}（{TIMEZONES[market]}）且 as_of "
            f"{anchor_as_of} 早于授权日；当前授权时刻 {authorized.isoformat()}"
        )


def _build_validations(
    *,
    tiger_data: Path,
    tiger_reports: Path,
    futu_data: Path,
    futu_reports: Path,
) -> list[dict[str, object]]:
    validations: list[dict[str, object]] = []
    if not tiger_data.is_dir():
        validations.append(
            {"name": "tiger_data_dir", "ok": False, "detail": f"缺少 {tiger_data}"}
        )
        return validations
    for name in MIGRATED_FILES:
        path = tiger_data / name
        if not path.is_file():
            validations.append({"name": name, "ok": False, "detail": "文件缺失"})
            continue
        try:
            if name.endswith(".jsonl"):
                rows = _read_jsonl_lines(path)
                detail = f"存在，{len(rows)} 行 JSONL 均可解析"
            else:
                payload = _json_object(path, name)
                detail = f"存在，JSON 可解析（键：{', '.join(sorted(payload)[:6])}）"
        except ValueError as exc:
            validations.append({"name": name, "ok": False, "detail": str(exc)})
            continue
        validations.append({"name": name, "ok": True, "detail": detail})
    count, first, last = _ledger_range(tiger_data / "daily_delivery")
    validations.append(
        {
            "name": "daily_delivery",
            "ok": count > 0,
            "detail": (
                f"{count} 份去重账本，{first} 至 {last}" if count else "目录缺失或为空"
            ),
        }
    )
    latest = _latest_tiger_report(tiger_reports)
    if latest is None:
        validations.append(
            {
                "name": "tiger_reports",
                "ok": False,
                "detail": f"{tiger_reports} 没有可解析的 JSON 报告",
            }
        )
    else:
        path, payload = latest
        try:
            _baseline_payload(payload)
        except ValueError as exc:
            validations.append(
                {"name": "tiger_reports", "ok": False, "detail": str(exc)}
            )
        else:
            snapshots = payload["signal_snapshots"]
            assert isinstance(snapshots, Mapping)
            validations.append(
                {
                    "name": "tiger_reports",
                    "ok": True,
                    "detail": (
                        f"基线来源 {path.name}，as_of_date={payload.get('as_of_date')}，"
                        f"candidates={len(snapshots.get('candidates') or [])}，"
                        f"holdings={len(snapshots.get('holdings') or {})}，"
                        f"real_holdings={len(snapshots.get('real_holdings') or {})}"
                    ),
                }
            )
    for label, path in (("legacy_futu_data", futu_data), ("legacy_futu_reports", futu_reports)):
        validations.append(
            {
                "name": label,
                "ok": True,
                "detail": f"存在，将归档" if path.exists() else "不存在，无需归档",
            }
        )
    return validations


def _run(
    *,
    data_root: Path,
    reports_root: Path,
    dry_run: bool,
    now: datetime,
    anchor_as_of: str,
    anchor_execution: str,
    config: DailyPremarketConfig | None = None,
    actor: str | None = None,
) -> dict[str, object]:
    tiger_data = data_root / TIGER_DATA_DIR
    futu_data = data_root / FUTU_DATA_DIR
    tiger_reports = reports_root / TIGER_REPORTS_DIR
    futu_reports = reports_root / FUTU_REPORTS_DIR
    data_archive_root = data_root / "archive"
    reports_archive_root = reports_root / "archive"
    actor_name = (actor or "").strip() or getpass.getuser()

    if not tiger_data.is_dir():
        raise ValueError(f"缺少老虎数据目录 {tiger_data}，无法迁移")

    validations = _build_validations(
        tiger_data=tiger_data,
        tiger_reports=tiger_reports,
        futu_data=futu_data,
        futu_reports=futu_reports,
    )
    failed = [item for item in validations if item.get("ok") is not True]
    if failed:
        raise ValueError(
            "迁移前校验失败："
            + "；".join(str(item.get("name")) for item in failed)
        )

    actions: list[str] = []
    archives: dict[str, object] = {"data": None, "reports": None, "batches": None}
    if futu_data.exists():
        destination = _unique_archive_path(data_archive_root, FUTU_DATA_DIR, now)
        archives["data"] = str(destination.relative_to(data_root))
        actions.append(f"归档 {futu_data} -> {destination}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(futu_data), str(destination))
    if futu_reports.exists():
        destination = _unique_archive_path(reports_archive_root, FUTU_REPORTS_DIR, now)
        archives["reports"] = str(destination.relative_to(reports_root))
        actions.append(f"归档 {futu_reports} -> {destination}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(futu_reports), str(destination))
    if not dry_run:
        futu_data.mkdir(parents=True, exist_ok=True)

    # Compute the ledger range before any move: in real mode the
    # daily_delivery/ directory is moved below, so reading it afterwards
    # would always report an empty ledger in the manifest.
    ledger_count, ledger_first, ledger_last = _ledger_range(
        tiger_data / "daily_delivery"
    )

    migrated: dict[str, str] = {}
    for name in MIGRATED_FILES:
        source = tiger_data / name
        target = futu_data / name
        if not source.is_file():
            actions.append(f"跳过 {name}：老虎侧缺失")
            continue
        outcome = "dry-run" if dry_run else _move(source, target)
        migrated[name] = outcome
        actions.append(f"迁移 {source.name} -> {target.name}（{outcome}）")
    for name in MIGRATED_DIRECTORIES:
        source = tiger_data / name
        target = futu_data / name
        if not source.is_dir():
            actions.append(f"跳过 {name}/：老虎侧缺失")
            continue
        outcome = "dry-run" if dry_run else _move(source, target)
        migrated[name + "/"] = outcome
        actions.append(f"迁移 {name}/ -> {target.name}（{outcome}）")

    latest = _latest_tiger_report(tiger_reports)
    if latest is None:
        raise ValueError("没有可用老虎报告重建 attention 基线")
    source_path, report = latest
    baseline = _baseline_payload(report)
    snapshots = baseline["signal_snapshots"]
    assert isinstance(snapshots, Mapping)
    baseline["rebuilt_from"] = str(source_path.relative_to(reports_root))
    baseline_path = futu_data / BASELINE_NAME
    actions.append(
        f"重建 {BASELINE_NAME}（来源 {source_path.name}，"
        f"as_of_date={baseline.get('as_of_date')}，"
        f"candidates={len(snapshots.get('candidates') or [])}，"
        f"holdings={len(snapshots.get('holdings') or {})}，"
        f"real_holdings={len(snapshots.get('real_holdings') or {})}）"
    )
    if not dry_run:
        _write_json(baseline_path, baseline)
        loaded = _json_object(baseline_path, BASELINE_NAME)
        if loaded.get("as_of_date") != baseline.get("as_of_date"):
            raise ValueError(f"{baseline_path} 落盘后校验不一致")

    # anchor_as_of/anchor_execution are derived in main() before the first
    # file mutation, so the fail-closed checks (no trading-day evidence / no
    # prior completed day) fire before anything is archived or migrated.  In
    # production this anchors as_of 2026-08-18 / execution 2026-08-19, the
    # cycle immediately before the first futu cycle.

    # Step 5: archive the tiger-era US batch ledger (move, never delete).
    batch_root = data_root / BATCH_LEDGER_REL
    batch_files = sorted(
        (
            path
            for path in batch_root.glob("*.json")
            if _DAILY_LEDGER_DATE.match(path.name)
        ),
        key=lambda path: path.name,
    )
    batch_first = (
        min(
            _DAILY_LEDGER_DATE.match(path.name).group(1)  # type: ignore[union-attr]
            for path in batch_files
        )
        if batch_files
        else ""
    )
    batch_last = (
        max(
            _DAILY_LEDGER_DATE.match(path.name).group(1)  # type: ignore[union-attr]
            for path in batch_files
        )
        if batch_files
        else ""
    )
    batch_archive: dict[str, object] = {
        "files": len(batch_files),
        "from": batch_first,
        "to": batch_last,
        "destination": None,
        "note": "老虎时代批次账本归档（移动不删除），防 durable 周期回溯到 7 月",
    }
    if batch_files:
        destination = _unique_archive_path(
            data_archive_root, BATCH_ARCHIVE_STEM, now
        )
        batch_archive["destination"] = str(destination.relative_to(data_root))
        archives["batches"] = batch_archive["destination"]
        actions.append(
            f"归档老虎时代批次账本 {len(batch_files)} 份 -> {destination}"
            f"（{batch_first} 至 {batch_last}）"
        )
        if not dry_run:
            destination.mkdir(parents=True, exist_ok=True)
            for batch_path in batch_files:
                shutil.move(str(batch_path), str(destination / batch_path.name))
    else:
        actions.append(f"跳过批次账本归档：{batch_root} 已空")

    # Step 6: anchor the last pre-cutover cycle as completed (revision
    # request with empty baseline + report_missing legacy cutover).  The
    # anchor batch was just archived, otherwise the controller helpers
    # reject the write as "execution has begun".
    anchor: dict[str, object] = {
        "market": ANCHOR_MARKET,
        "as_of_date": anchor_as_of,
        "execution_date": anchor_execution,
        "skipped": False,
        "legacy_cutover_path": None,
        "revision_request_path": None,
        "note": "锚定切割前最后一个老虎周期为已完成，防控制器回溯补产旧报告",
    }
    legacy_cutover_path = (
        data_root
        / "trend_controller"
        / ANCHOR_MARKET
        / "legacy_cutovers"
        / f"{anchor_as_of}.json"
    )
    if legacy_cutover_path.exists():
        anchor["skipped"] = True
        anchor["legacy_cutover_path"] = str(
            legacy_cutover_path.relative_to(data_root)
        )
        actions.append(f"跳过锚定：legacy cutover 已存在 {legacy_cutover_path}")
    else:
        actions.append(
            f"锚定切割前周期 as_of={anchor_as_of} / execution={anchor_execution}"
            "（revision request + legacy cutover，report_missing）"
        )
        if not dry_run:
            if config is None:
                raise ValueError("缺少控制器配置，无法执行锚定切割前周期步骤")
            cycle = ControllerCycle(
                market=ANCHOR_MARKET,
                as_of_date=anchor_as_of,
                execution_date=anchor_execution,
                report_run_date=(
                    date.fromisoformat(anchor_as_of) + timedelta(days=1)
                ).isoformat(),
                session="catchup",
                market_open=False,
                next_check_at=now + timedelta(seconds=5),
            )
            try:
                request_path = _request_revision(config, cycle, now)
                _record_legacy_cycle_cutover(
                    config,
                    cycle,
                    actor=actor_name,
                    reason=ANCHOR_REASON,
                    authorized_at=now,
                    report_missing=True,
                )
            except ValueError as exc:
                raise ValueError(
                    f"锚定切割前周期失败（as_of={anchor_as_of} "
                    f"execution={anchor_execution}）：{exc}"
                ) from exc
            anchor["revision_request_path"] = str(
                request_path.relative_to(data_root)
            )
            anchor["legacy_cutover_path"] = str(
                legacy_cutover_path.relative_to(data_root)
            )

    manifest: dict[str, object] = {
        "schema_version": 1,
        "cutover": CUTOVER_ID,
        "executed_at": now.isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "data_root": str(data_root),
        "reports_root": str(reports_root),
        "archives": archives,
        "migrated": migrated,
        "baseline": {
            "source": baseline.get("rebuilt_from"),
            "as_of_date": baseline.get("as_of_date"),
            "candidates": len(snapshots.get("candidates") or []),
            "holdings": len(snapshots.get("holdings") or {}),
            "real_holdings": len(snapshots.get("real_holdings") or {}),
            "note": "从最后一份老虎报告重建；不使用 7 月旧基线",
        },
        "ledger": {
            "files": ledger_count,
            "from": ledger_first,
            "to": ledger_last,
            "note": "投递去重账本，防切换日重发飞书",
        },
        "batch_archive": batch_archive,
        "anchor_cycle": anchor,
        "actions": actions,
        "validations": validations,
        "notes": [
            f"{reports_root / TIGER_REPORTS_DIR}/ 原地保留为历史，未迁移未删除",
            f"{tiger_data}/ 其余文件（旧基线/投递收据/日志）保留，代码已不再读取",
            "老虎时代批次账本已归档，durable 周期不再回溯到 7 月；"
            f"锚定周期 as_of={anchor_as_of} / execution={anchor_execution}",
        ],
    }

    if not dry_run:
        marker = futu_data / MARKER_NAME
        _write_json(marker, manifest)
        archive_manifest = None
        for key in ("data", "reports", "batches"):
            relative = archives.get(key)
            if relative is None:
                continue
            directory = (
                data_root if key in ("data", "batches") else reports_root
            ) / str(relative)
            _write_json(directory / MANIFEST_NAME, manifest)
            archive_manifest = directory
        manifest["marker"] = str(marker)
        if archive_manifest is not None:
            manifest["archive_manifest"] = str(archive_manifest)
    return manifest


def _print_manifest(manifest: Mapping[str, object], *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}迁移清单（{CUTOVER_ID}）")
    for action in manifest.get("actions") or []:
        print(f"{prefix}  {action}")
    migrated = manifest.get("migrated")
    if isinstance(migrated, Mapping):
        print(f"{prefix}迁移结果：")
        for name, outcome in migrated.items():
            print(f"{prefix}  - {name}: {outcome}")
    baseline = manifest.get("baseline")
    if isinstance(baseline, Mapping):
        print(
            f"{prefix}attention 基线：来源 {baseline.get('source')}，"
            f"as_of_date={baseline.get('as_of_date')}"
        )
    batch = manifest.get("batch_archive")
    if isinstance(batch, Mapping):
        destination = batch.get("destination")
        print(
            f"{prefix}批次账本归档：{batch.get('files')} 份"
            f"（{batch.get('from')} 至 {batch.get('to')}）"
            + (
                f" -> {destination}"
                if destination
                else "（已空，跳过）"
            )
        )
    anchor = manifest.get("anchor_cycle")
    if isinstance(anchor, Mapping):
        skipped = "（跳过：已有 legacy cutover）" if anchor.get("skipped") else ""
        print(
            f"{prefix}锚定切割前周期：as_of={anchor.get('as_of_date')} / "
            f"execution={anchor.get('execution_date')}{skipped}"
        )
    print(f"{prefix}校验：")
    for item in manifest.get("validations") or []:
        name = str(item.get("name"))
        ok = "PASS" if item.get("ok") is True else "FAIL"
        print(f"{prefix}  [{ok}] {name}: {item.get('detail')}")
    for note in manifest.get("notes") or []:
        print(f"{prefix}备注：{note}")


def _now() -> datetime:
    return datetime.now().astimezone()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印迁移清单与校验，不落盘",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="数据根目录（默认 ./data）",
    )
    parser.add_argument(
        "--reports-root",
        type=Path,
        default=Path("reports"),
        help="报告根目录（默认 ./reports）",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
        help=(
            "daily_premarket 环境文件；真实模式锚定步骤需要 "
            "OPEN_TRADER_TREND_EXECUTOR_HOST（默认 config/daily_premarket.env）"
        ),
    )
    parser.add_argument(
        "--actor",
        default=getpass.getuser(),
        help="锚定记录署名（默认当前用户名）",
    )
    args = parser.parse_args(argv)
    now = _now()
    try:
        data_root = args.data_root.resolve()
        reports_root = args.reports_root.resolve()
        marker = data_root / FUTU_DATA_DIR / MARKER_NAME
        if marker.is_file():
            try:
                existing = _json_object(marker, MARKER_NAME)
            except ValueError as exc:
                print(f"已完成标记损坏：{exc}", file=sys.stderr)
                return 1
            print(
                f"已迁移（{existing.get('executed_at')}），"
                f"标记：{marker}"
            )
            return 0
        config: DailyPremarketConfig | None = None
        if not args.dry_run:
            try:
                env_values = _read_env_values(args.config)
            except OSError as exc:
                raise ValueError(
                    f"缺少配置文件 {args.config}（真实模式锚定步骤需要）：{exc}"
                ) from exc
            config = _controller_config(data_root, reports_root, env_values)
            # Fail fast before any file is moved: the anchor step requires
            # the trend executor identity.
            require_trend_executor(config)
        # The anchor dates are pure reads with fail-closed checks; derive
        # them before the first file mutation so a fail-closed exit (no
        # trading-day evidence / no prior completed day) never leaves
        # tiger-side files moved or archived.
        tiger_data = data_root / TIGER_DATA_DIR
        if not tiger_data.is_dir():
            raise ValueError(f"缺少老虎数据目录 {tiger_data}，无法迁移")
        anchor_as_of, anchor_execution = _anchor_cycle_dates(data_root)
        if not args.dry_run:
            # Mirror the authorization-window conditions of
            # _record_legacy_cycle_cutover that are decidable locally
            # before any file is moved; what cannot be judged here keeps
            # its existing fail-closed checks in the anchor step.
            _check_anchor_authorization_window(anchor_as_of, anchor_execution, now)
        manifest = _run(
            data_root=data_root,
            reports_root=reports_root,
            dry_run=args.dry_run,
            now=now,
            config=config,
            actor=args.actor,
            anchor_as_of=anchor_as_of,
            anchor_execution=anchor_execution,
        )
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    _print_manifest(manifest, dry_run=args.dry_run)
    if not args.dry_run:
        print(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
