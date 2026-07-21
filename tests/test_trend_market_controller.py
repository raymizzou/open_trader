from __future__ import annotations

import json
import hashlib
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader import a_share_trend as a_share_trend
from open_trader import trend_market_controller as controller
from open_trader.daily_premarket import DailyPremarketConfig, RunLock
from open_trader.futu_symbols import to_futu_symbol
from open_trader.trend_market_controller import (
    ControllerCycle,
    load_trend_market_status,
    run_trend_market_controller,
)
from open_trader.trend_review import (
    _report_hash,
    lock_trend_execution_batch,
    trend_action_key,
    trend_attempt_remark,
)


NOW = datetime.fromisoformat("2026-07-20T09:31:00+08:00")


def protection_success() -> SimpleNamespace:
    return SimpleNamespace(
        status="completed",
        exception_count=0,
        unknown_quote_count=0,
    )


def controller_config(tmp_path: Path) -> DailyPremarketConfig:
    return DailyPremarketConfig(
        repo=tmp_path,
        python=Path(sys.executable),
        timezone="Asia/Shanghai",
        deadline="09:00",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        trend_executor_host="executor",
    )


def active_cn_cycle() -> ControllerCycle:
    return ControllerCycle(
        market="CN",
        as_of_date="2026-07-17",
        execution_date="2026-07-20",
        report_run_date="2026-07-17",
        session="morning",
        market_open=True,
        next_check_at=datetime.fromisoformat("2026-07-20T09:31:05+08:00"),
    )


def valid_cn_report(
    *, as_of_date: str, execution_date: str, buy: bool = False
) -> dict[str, object]:
    formal_actions: list[dict[str, object]] = []
    if buy:
        formal_actions.append(
            {
                "action": "BUY",
                "symbol": "600001",
                "target_weight": "0.04",
                "lot_size": 100,
                "estimated_shares": 400,
                "target_amount": "4000",
                "atr": "0.5",
            }
        )
    return {
        "schema_version": 1,
        "generated_at": f"{as_of_date}T18:00:00+08:00",
        "as_of_date": as_of_date,
        "execution_date": execution_date,
        "account": {
            "source_date": as_of_date,
            "fresh": True,
            "net_value": "100000",
            "available_cash": "100000",
            "positions": [],
            "exceptions": [],
            "position_count": 0,
        },
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "protection_state": {"schema_version": 1, "positions": {}},
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/CN/v1",
            "strategy_version": "v1",
            "process_version": "test-sha",
            "parameters": {"buy_window": "09:30-10:00"},
            "parameter_rows": [
                {
                    "group": "execution",
                    "name": "buy_window",
                    "value": "09:30-10:00",
                }
            ],
        },
        "strategy_judgments": {
            "formal_actions": formal_actions,
            "holding_decisions": [],
            "top10_candidates": [],
        },
    }


def write_report(
    config: DailyPremarketConfig,
    *,
    revision: int = 0,
    buy: bool = False,
) -> tuple[Path, dict[str, object]]:
    suffix = f"-r{revision}" if revision else ""
    path = config.reports_dir / "trend_a_share" / f"2026-07-17{suffix}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = valid_cn_report(
        as_of_date="2026-07-17", execution_date="2026-07-20", buy=buy
    )
    if revision:
        report["generated_at"] = "2026-07-17T18:01:00+08:00"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, report


def write_report_delivery_receipt(
    config: DailyPremarketConfig,
    report_path: Path,
    report: dict[str, object],
    *,
    status: str,
    markdown: str = "# frozen",
    receipt_report: dict[str, object] | None = None,
    receipt_markdown: str | None = None,
    receipt_protection_state: dict[str, object] | None = None,
) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_json = json.dumps(report)
    report_path.write_text(report_json, encoding="utf-8")
    report_path.with_suffix(".md").write_text(markdown, encoding="utf-8")
    receipt_path = (
        config.data_dir
        / "trend_a_share"
        / "delivery"
        / f"{report_path.stem}.json"
    )
    a_share_trend._write_delivery_receipt(
        receipt_path,
        status=status,
        generated_at=str(report["generated_at"]),
        artifact_stem=report_path.stem,
        markdown=receipt_markdown if receipt_markdown is not None else markdown,
        report_json=json.dumps(receipt_report or report),
        protection_state=(
            receipt_protection_state
            if receipt_protection_state is not None
            else report["protection_state"]
        ),
    )
    return receipt_path


def patch_cycle(monkeypatch: pytest.MonkeyPatch, cycle: ControllerCycle) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(
        controller, "_derive_cycle", lambda _config, _market, _now: cycle
    )
    monkeypatch.setattr(
        controller, "_run_protection_pass", lambda *_args: protection_success()
    )

    def capture(
        config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        path = (
            config.data_dir
            / "trend_review"
            / "daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)


def test_start_after_original_trigger_generates_report_and_executes_inside_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    calls: list[tuple[str, str, object]] = []
    reports: list[tuple[Path, dict[str, object]]] = []

    def generate(
        _config: DailyPremarketConfig, market: str, run_date: str, revision: bool
    ) -> None:
        calls.append(("generate", market, (run_date, revision)))
        reports.append(write_report(config))

    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, _now: active_cn_cycle(),
    )
    monkeypatch.setattr(
        controller,
        "_load_latest_valid_report",
        lambda _config, _market, _date: reports[-1] if reports else None,
    )
    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda _config, market, day: calls.append(("protect", market, day))
        or protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, market, day, path, report: calls.append(
            ("execute", market, (day, path.name))
        )
        or {"status": "submitted", "submitted_count": 1},
    )
    def capture_close(
        _config: DailyPremarketConfig, market: str, day: str
    ) -> None:
        calls.append(("close", market, day))
        path = config.data_dir / "trend_review/daily" / market / f"{day}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture_close)
    monkeypatch.setattr(
        controller,
        "_notify_once",
        lambda title, message, key: calls.append(
            ("notify", title, (message, key))
        )
        or True,
    )

    result = run_trend_market_controller(
        config,
        "CN",
        once=True,
        now_fn=lambda: NOW,
    )

    assert ("generate", "CN", ("2026-07-17", False)) in calls
    assert ("protect", "CN", "2026-07-20") in calls
    assert ("execute", "CN", ("2026-07-20", "2026-07-17.json")) in calls
    assert result["phase"] == "monitoring"


def test_report_failure_before_freeze_retries_same_logical_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[tuple[str, str, bool]] = []
    reports: list[tuple[Path, dict[str, object]]] = []

    def generate(
        _config: DailyPremarketConfig, market: str, run_date: str, revision: bool
    ) -> None:
        calls.append((market, run_date, revision))
        if len(calls) == 1:
            raise RuntimeError("upstream unavailable")
        reports.append(write_report(config))

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_load_latest_valid_report",
        lambda *_args: reports[-1] if reports else None,
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: {"status": "unchanged", "submitted_count": 0},
    )

    first = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)
    second = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == [
        ("CN", "2026-07-17", False),
        ("CN", "2026-07-17", False),
    ]
    assert first["phase"] == "recovering_report"
    assert second["phase"] == "monitoring"
    assert not list(config.reports_dir.rglob("*-r*.json"))


def test_failed_report_keeps_same_logical_dates_after_cycle_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    active = active_cn_cycle()
    closed = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-20T15:01:05+08:00"),
    )
    before_close = datetime.fromisoformat("2026-07-20T14:59:00+08:00")
    after_close = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    times = iter((before_close, before_close, after_close, after_close))
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, now: active if now < after_close else closed,
    )
    calls: list[str] = []
    failed = threading.Event()
    retried = threading.Event()

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        run_date: str,
        _revision: bool,
    ) -> None:
        calls.append(run_date)
        if len(calls) == 1:
            failed.set()
            raise RuntimeError("upstream unavailable")
        retried.set()

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(controller, "_load_latest_valid_report", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args: failed.wait(timeout=1),
    )
    def capture_close(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        path = (
            config.data_dir
            / "trend_review"
            / "daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture_close)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            return
        assert retried.wait(timeout=1)
        raise RuntimeError("stop controller test")

    with pytest.raises(RuntimeError, match="stop controller test"):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: next(times),
            sleep_fn=advance,
        )

    assert calls == ["2026-07-17", "2026-07-17"]


def test_frozen_delivery_failure_retries_delivery_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report = valid_cn_report(
        as_of_date="2026-07-17", execution_date="2026-07-20"
    )
    receipt_path = config.data_dir / "trend_a_share/delivery/2026-07-17.json"
    a_share_trend._write_delivery_receipt(
        receipt_path,
        status="pending",
        generated_at="2026-07-17T18:00:00+08:00",
        artifact_stem="2026-07-17",
        markdown="# frozen",
        report_json=json.dumps(report),
        protection_state={"schema_version": 1, "positions": {}},
    )
    expensive_calls = 0

    def rebuild(*_args: object, **_kwargs: object) -> None:
        nonlocal expensive_calls
        expensive_calls += 1
        pytest.fail("frozen delivery recovery rebuilt the report")

    monkeypatch.setattr(a_share_trend, "_attempt_report", rebuild)

    controller._generate_report(config, "CN", "2026-07-17", False)

    assert expensive_calls == 0
    assert (config.reports_dir / "trend_a_share/2026-07-17.json").exists()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == (
        "delivery_failed"
    )


def test_restart_after_report_freeze_does_not_regenerate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    generated: list[object] = []
    monkeypatch.setattr(controller, "_load_latest_valid_report", lambda *_args: report)
    monkeypatch.setattr(controller, "_generate_report", lambda *_args: generated.append(1))
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)
    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert generated == []


def test_report_is_not_locked_or_executed_before_market_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    cycle = ControllerCycle(
        **{
            **active_cn_cycle().__dict__,
            "session": "before",
            "market_open": False,
        }
    )
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(controller, "_load_latest_valid_report", lambda *_args: report)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: pytest.fail("report executed before the market opened"),
    )

    before_open = datetime.fromisoformat("2026-07-20T09:00:00+08:00")
    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: before_open
    )

    assert result["phase"] == "before"
    assert not list(config.data_dir.glob("trend_review/ledgers/CN/batches/*.json"))


def test_report_recovery_during_session_keeps_protection_ticks_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    release = threading.Event()
    protected = threading.Event()
    reports: list[tuple[Path, dict[str, object]]] = []

    def generate(*_args: object) -> None:
        assert release.wait(timeout=1)
        reports.append(write_report(config))

    def protect(*_args: object) -> object:
        protected.set()
        release.set()

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller,
        "_load_latest_valid_report",
        lambda *_args: reports[-1] if reports else None,
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert protected.is_set()
    assert reports


def test_heartbeat_is_written_before_slow_reconciliation_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    release = threading.Event()

    def generate(*_args: object) -> None:
        assert release.wait(timeout=1)
        write_report(config)

    def protect(*_args: object) -> None:
        status_path = config.data_dir / "trend_controller/CN/status.json"
        assert json.loads(status_path.read_text(encoding="utf-8"))["phase"] == (
            "reconciling"
        )
        release.set()
        return protection_success()

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller,
        "_load_latest_valid_report",
        lambda *_args: (
            write_report(config)
            if (config.reports_dir / "trend_a_share/2026-07-17.json").exists()
            else None
        ),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: {"status": "unchanged", "submitted_count": 0},
    )

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert result["phase"] == "monitoring"


def test_heartbeat_refreshes_before_each_calendar_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report = write_report(config)
    calendar_blocked = threading.Event()
    release = threading.Event()
    controller_stopped = threading.Event()
    second_tick = datetime.fromisoformat("2026-07-20T09:31:05+08:00")
    times = iter((NOW, NOW, second_tick, second_tick))
    derive_calls = 0
    sleep_calls = 0

    def derive(*_args: object) -> ControllerCycle:
        nonlocal derive_calls
        derive_calls += 1
        if derive_calls == 2:
            calendar_blocked.set()
            assert release.wait(timeout=2)
        return active_cn_cycle()

    class StopController(Exception):
        pass

    def sleep_fn(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise StopController

    monkeypatch.setattr(controller, "_derive_cycle", derive)
    monkeypatch.setattr(
        controller, "_cycle_to_reconcile", lambda _config, cycle, _now: cycle
    )
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )
    monkeypatch.setattr(controller, "_run_protection_pass", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: {"status": "unchanged", "submitted_count": 0},
    )

    def capture_close(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        path = (
            config.data_dir
            / "trend_review/daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture_close)

    def run() -> None:
        try:
            run_trend_market_controller(
                config,
                "CN",
                now_fn=lambda: next(times),
                sleep_fn=sleep_fn,
            )
        except StopController:
            controller_stopped.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert calendar_blocked.wait(timeout=2)
    status = load_trend_market_status(config, "CN", now=second_tick)
    release.set()
    thread.join(timeout=2)

    assert status["heartbeat_at"] == second_tick.isoformat(timespec="seconds")
    assert controller_stopped.is_set()


def test_report_finished_after_window_is_preserved_and_actions_become_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report_path, report = write_report(config, buy=True)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[Path] = []
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: (report_path, report)
    )
    monkeypatch.setattr(controller, "_generate_report", pytest.fail)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, _day, path, _report: calls.append(path)
        or {"status": "missed_window", "submitted_count": 0},
    )

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == [report_path]
    assert result["phase"] == "missed"
    assert report_path.exists()
    assert result["last_success"]["submitted_count"] == 0


def test_report_future_keeps_its_execution_date_when_cycle_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    active = ControllerCycle(
        **{
            **active_cn_cycle().__dict__,
            "session": "afternoon",
        }
    )
    closed = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-20T15:01:05+08:00"),
    )
    before_close = datetime.fromisoformat("2026-07-20T14:59:00+08:00")
    after_close = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    times = iter((before_close, before_close, after_close, after_close))
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, now: active if now < after_close else closed,
    )
    release = threading.Event()
    generated = threading.Event()
    reports: dict[str, tuple[Path, dict[str, object]]] = {}

    def generate(*_args: object) -> None:
        assert release.wait(timeout=1)
        reports["2026-07-20"] = write_report(config, buy=True)
        generated.set()

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_load_latest_valid_report",
        lambda _config, _market, execution_date: reports.get(execution_date),
    )
    monkeypatch.setattr(controller, "_run_protection_pass", lambda *_args: None)
    executed: list[str] = []
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, execution_date, _path, _report: executed.append(
            execution_date
        )
        or {"status": "missed_window", "submitted_count": 0},
    )
    monkeypatch.setattr(controller, "_capture_close", lambda *_args: None)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            release.set()
            assert generated.wait(timeout=1)
            return
        raise RuntimeError("stop controller test")

    with pytest.raises(RuntimeError, match="stop controller test"):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: next(times),
            sleep_fn=advance,
        )

    assert executed == ["2026-07-20"]


def test_later_revision_does_not_change_locked_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    base_path, base_report = write_report(config)
    revision_path, revision_report = write_report(config, revision=1)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date="2026-07-20",
        report_path=base_path,
        report=base_report,
        locked_at=NOW.isoformat(),
    )
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller,
        "_load_latest_valid_report",
        lambda *_args: (revision_path, revision_report),
    )
    executed: list[Path] = []
    notifications: list[object] = []
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, _date, path, _report: executed.append(path)
        or {"status": "unchanged", "submitted_count": 0},
    )
    monkeypatch.setattr(
        controller,
        "_notify_once",
        lambda _title, _message, key: notifications.append(key) or True,
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert executed == [base_path]
    assert len(notifications) == 1


def test_readonly_controller_returns_without_report_broker_or_notification_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "readonly-copy")
    calls: list[str] = []
    for name in (
        "_derive_cycle",
        "_load_latest_valid_report",
        "_generate_report",
        "_run_protection_pass",
        "_execute_locked_report",
        "_capture_close",
        "_notify_once",
    ):
        monkeypatch.setattr(
            controller,
            name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name),
        )

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert result["phase"] == "readonly"
    assert result["blocker"] == "local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST"
    assert calls == []
    assert not config.data_dir.exists()


def test_calendar_failure_writes_blocker_instead_of_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("calendar offline")),
    )
    for name in (
        "_load_latest_valid_report",
        "_generate_report",
        "_execute_locked_report",
        "_capture_close",
    ):
        monkeypatch.setattr(
            controller,
            name,
            lambda *_args, _name=name: pytest.fail(f"unexpected call: {_name}"),
        )
    protected: list[str] = []
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda _config, _market, day: protected.append(day)
        or protection_success(),
    )
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert result["phase"] == "blocked"
    assert result["blocker"] == "calendar offline"
    assert protected == ["2026-07-20"]
    assert load_trend_market_status(config, "CN") == result


def test_controller_restart_reconciles_existing_futu_order_without_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report_path, report = write_report(config, buy=True)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: (report_path, report)
    )
    calls = 0

    def execute(*_args: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "unchanged", "submitted_count": 0, "repaired_count": 1}

    monkeypatch.setattr(controller, "_execute_locked_report", execute)

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == 1
    assert result["last_success"]["repaired_count"] == 1
    assert result["last_success"]["submitted_count"] == 0


def test_quote_failure_still_records_missed_without_broker_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(controller_config(tmp_path), trend_review_cn_simulate_acc_id=101)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report_path, report = write_report(config, buy=True)
    report["metadata"]["simulate_acc_id"] = 101  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class BrokenQuote:
        def get_snapshots(self, _symbols: object) -> object:
            raise RuntimeError("quote offline")

        def close(self) -> None:
            pass

    class Orders:
        def __init__(self, **_kwargs: object) -> None:
            self.requests: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(request)
            return {"futu_order_id": "unexpected"}

        def close(self) -> None:
            pass

    orders = Orders()
    monkeypatch.setattr(controller, "FutuQuoteClient", lambda **_kwargs: BrokenQuote())
    monkeypatch.setattr(
        controller,
        "FutuSimulateOrderExecutionClient",
        lambda **_kwargs: orders,
    )

    result = controller._execute_locked_report(
        config,
        "CN",
        "2026-07-20",
        report_path,
        report,
    )

    assert result["submitted_count"] == 0
    assert orders.requests == []
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.data_dir.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
    ]
    assert any(event.get("status") == "missed" for event in events)


def test_broker_failure_uses_bounded_backoff_without_stopping_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(controller, "_load_latest_valid_report", lambda *_args: report)
    protected = 0

    def protect(*_args: object) -> object:
        nonlocal protected
        protected += 1
        return protection_success()

    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    current = NOW
    times = iter((NOW, NOW, NOW.replace(second=5), NOW.replace(second=10)))

    def now_fn() -> datetime:
        nonlocal current
        current = next(times)
        return current

    attempts: list[datetime] = []

    def execute(*_args: object) -> dict[str, object]:
        attempts.append(current)
        if len(attempts) == 1:
            raise RuntimeError("broker offline")
        return {"status": "unchanged", "submitted_count": 0}

    monkeypatch.setattr(controller, "_execute_locked_report", execute)
    sleeps = 0

    def stop_after_three_ticks(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise RuntimeError("stop controller test")

    with pytest.raises(RuntimeError, match="stop controller test"):
        run_trend_market_controller(
            config, "CN", now_fn=now_fn, sleep_fn=stop_after_three_ticks
        )

    assert attempts == [NOW, NOW.replace(second=10)]
    assert protected == 3


def test_close_capture_is_recovered_once_after_session_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report_path, report = write_report(config)
    closed = ControllerCycle(
        **{
            **active_cn_cycle().__dict__,
            "session": "closed",
            "market_open": False,
        }
    )
    patch_cycle(monkeypatch, closed)
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: (report_path, report)
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: {"status": "missed_window", "submitted_count": 0},
    )
    calls = 0

    def capture(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        nonlocal calls
        calls += 1
        path = (
            config.data_dir
            / "trend_review"
            / "daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)
    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == 1


def test_restart_after_close_recovers_unlocked_prior_execution_before_next_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    prior = active_cn_cycle()
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-20T15:01:05+08:00"),
    )
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, now: prior
        if now.astimezone().hour < 15
        else current,
    )
    prior_path, prior_report = write_report(config, buy=True)
    current_path = config.reports_dir / "trend_a_share/2026-07-20.json"
    current_report = valid_cn_report(
        as_of_date="2026-07-20", execution_date="2026-07-21"
    )
    current_path.write_text(json.dumps(current_report), encoding="utf-8")
    loads: list[str] = []

    def load(
        _config: DailyPremarketConfig, _market: str, execution_date: str
    ) -> tuple[Path, dict[str, object]]:
        loads.append(execution_date)
        return (
            (prior_path, prior_report)
            if execution_date == "2026-07-20"
            else (current_path, current_report)
        )

    monkeypatch.setattr(controller, "_load_latest_valid_report", load)
    monkeypatch.setattr(controller, "_run_protection_pass", lambda *_args: None)
    monkeypatch.setattr(controller, "_capture_close", lambda *_args: None)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)

    def execute(
        _config: DailyPremarketConfig,
        market: str,
        execution_date: str,
        path: Path,
        report: dict[str, object],
    ) -> dict[str, object]:
        lock_trend_execution_batch(
            config.data_dir,
            market=market,
            execution_date=execution_date,
            report_path=path,
            report=report,
            locked_at=NOW.isoformat(),
        )
        action_key = trend_action_key(
            market,
            execution_date,
            to_futu_symbol(market, "600001"),
            "buy",
        )
        event = (
            config.data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "actions"
            / execution_date
            / action_key
            / "missed.json"
        )
        event.parent.mkdir(parents=True, exist_ok=True)
        event.write_text(
                json.dumps({
                    "market": market,
                    "date": execution_date,
                    "strategy_version": report["strategy_snapshot"][
                        "strategy_version"
                    ],
                    "report_sha256": _report_hash(report),
                    "action_index": 0,
                    "symbol": "600001",
                "futu_code": to_futu_symbol(market, "600001"),
                "side": "buy",
                "status": "missed",
                "reason": "buy_window_closed",
                "recorded_at": NOW.isoformat(),
            }),
            encoding="utf-8",
        )
        return {"status": "missed_window", "submitted_count": 0}

    monkeypatch.setattr(controller, "_execute_locked_report", execute)
    after_close = datetime.fromisoformat("2026-07-20T15:01:00+08:00")

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: after_close)
    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: after_close)

    assert loads == ["2026-07-20", "2026-07-21"]


def test_restart_after_batch_lock_reconciles_prior_until_missed_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    prior = active_cn_cycle()
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-20T15:01:05+08:00"),
    )
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, now: prior
        if now.date().isoformat() == prior.execution_date
        and now.hour < 15
        else current,
    )
    prior_path, prior_report = write_report(config, buy=True)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=prior.execution_date,
        report_path=prior_path,
        report=prior_report,
        locked_at=NOW.isoformat(),
    )
    current_path = config.reports_dir / "trend_a_share/2026-07-20.json"
    current_report = valid_cn_report(
        as_of_date="2026-07-20", execution_date="2026-07-21"
    )
    current_path.write_text(json.dumps(current_report), encoding="utf-8")
    loads: list[str] = []

    def load(
        _config: DailyPremarketConfig, _market: str, execution_date: str
    ) -> tuple[Path, dict[str, object]]:
        loads.append(execution_date)
        return (
            (prior_path, prior_report)
            if execution_date == prior.execution_date
            else (current_path, current_report)
        )

    def execute(
        _config: DailyPremarketConfig,
        _market: str,
        execution_date: str,
        _path: Path,
        _report: dict[str, object],
    ) -> dict[str, object]:
        assert execution_date == prior.execution_date
        action_key = trend_action_key(
            "CN",
            prior.execution_date,
            to_futu_symbol("CN", "600001"),
            "buy",
        )
        event = (
            config.data_dir
            / "trend_review/ledgers/CN/actions"
            / prior.execution_date
            / action_key
            / "missed.json"
        )
        event.parent.mkdir(parents=True, exist_ok=True)
        event.write_text(
                json.dumps({
                    "market": "CN",
                    "date": prior.execution_date,
                    "strategy_version": prior_report["strategy_snapshot"][
                        "strategy_version"
                    ],
                    "report_sha256": _report_hash(prior_report),
                    "action_index": 0,
                    "symbol": "600001",
                "futu_code": to_futu_symbol("CN", "600001"),
                "side": "buy",
                "status": "missed",
                "reason": "buy_window_closed",
                "recorded_at": NOW.isoformat(),
            }),
            encoding="utf-8",
        )
        return {"status": "missed_window", "submitted_count": 0}

    monkeypatch.setattr(controller, "_load_latest_valid_report", load)
    monkeypatch.setattr(controller, "_execute_locked_report", execute)
    monkeypatch.setattr(controller, "_run_protection_pass", lambda *_args: None)
    monkeypatch.setattr(controller, "_capture_close", lambda *_args: None)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    after_close = datetime.fromisoformat("2026-07-20T15:01:00+08:00")

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: after_close)
    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: after_close)

    assert loads == [prior.execution_date, current.execution_date]


def test_next_morning_reconciles_unfinished_prior_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    prior = active_cn_cycle()
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="premarket",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-21T09:00:05+08:00"),
    )
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, now: prior
        if now.date().isoformat() == prior.execution_date
        else current,
    )
    report_path, report = write_report(config, buy=True)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=prior.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    executed: list[str] = []
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, execution_date, _path, _report: executed.append(
            execution_date
        )
        or {"status": "missed_window", "submitted_count": 0},
    )
    monkeypatch.setattr(controller, "_run_protection_pass", lambda *_args: None)
    captured: list[str] = []

    def capture(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        captured.append(trading_date)
        path = (
            config.data_dir
            / "trend_review"
            / "daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    next_morning = datetime.fromisoformat("2026-07-21T09:00:00+08:00")

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: next_morning)

    assert executed == [prior.execution_date]
    assert captured == [current.as_of_date]


def test_weekend_reconciles_unfinished_prior_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    prior = ControllerCycle(
        market="CN",
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        report_run_date="2026-07-16",
        session="morning",
        market_open=True,
        next_check_at=datetime.fromisoformat("2026-07-17T09:31:05+08:00"),
    )
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-17",
        execution_date="2026-07-20",
        report_run_date="2026-07-17",
        session="holiday",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-18T09:00:05+08:00"),
    )
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, now: prior
        if now.date().isoformat() == prior.execution_date
        else current,
    )
    report_path = config.reports_dir / "trend_a_share/2026-07-16.json"
    report_path.parent.mkdir(parents=True)
    report = valid_cn_report(
        as_of_date=prior.as_of_date,
        execution_date=prior.execution_date,
        buy=True,
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=prior.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    executed: list[str] = []
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, execution_date, _path, _report: executed.append(
            execution_date
        )
        or {"status": "missed_window", "submitted_count": 0},
    )
    captured: list[str] = []

    def capture(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        captured.append(trading_date)
        path = (
            config.data_dir
            / "trend_review"
            / "daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    weekend = datetime.fromisoformat("2026-07-18T09:00:00+08:00")

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: weekend)

    assert executed == [prior.execution_date]
    assert captured == [current.as_of_date]


def test_buy_without_terminal_event_remains_incomplete(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    report_path, report = write_report(config, buy=True)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    assert controller._execution_completed(config, cycle) is False


def test_active_session_restart_recovers_prior_close_after_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[tuple[str, str]] = []

    def protect(
        _config: DailyPremarketConfig, _market: str, execution_date: str
    ) -> object:
        calls.append(("protect", execution_date))
        return protection_success()

    def capture(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        calls.append(("close", trading_date))
        path = (
            config.data_dir
            / "trend_review/daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(controller, "_capture_close", capture)
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == [
        ("protect", "2026-07-20"),
        ("close", "2026-07-17"),
    ]


@pytest.mark.parametrize("artifact", ["event", "resolution"])
def test_execution_completion_rejects_unvalidated_terminal_artifact(
    tmp_path: Path,
    artifact: str,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    report_path, report = write_report(config, buy=True)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    action_key = trend_action_key(
        "CN",
        cycle.execution_date,
        to_futu_symbol("CN", "600001"),
        "buy",
    )
    root = (
        config.data_dir
        / "trend_review/ledgers/CN/actions"
        / cycle.execution_date
        / action_key
    )
    if artifact == "event":
        path = root / "missed.json"
        payload = {
            "market": "US",
            "date": cycle.execution_date,
            "symbol": "600001",
            "futu_code": to_futu_symbol("CN", "600001"),
            "side": "buy",
            "status": "missed",
            "recorded_at": NOW.isoformat(),
        }
    else:
        path = root / "resolutions/bare.json"
        payload = {"resolution": "abandon"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        controller._execution_completed(config, cycle)


def test_multi_session_outage_reconciles_oldest_unfinished_cycle_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    completed = active_cn_cycle()
    completed_path, completed_report = write_report(config)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=completed.execution_date,
        report_path=completed_path,
        report=completed_report,
        locked_at=NOW.isoformat(),
    )
    first_missing = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-21T15:01:05+08:00"),
    )
    second_missing = ControllerCycle(
        market="CN",
        as_of_date="2026-07-21",
        execution_date="2026-07-22",
        report_run_date="2026-07-21",
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-22T15:01:05+08:00"),
    )
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-22",
        execution_date="2026-07-23",
        report_run_date="2026-07-22",
        session="before",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-23T09:00:05+08:00"),
    )

    def derive(
        _config: DailyPremarketConfig, _market: str, now: datetime
    ) -> ControllerCycle:
        return {
            "2026-07-20": first_missing,
            "2026-07-21": second_missing,
            "2026-07-22": current,
            "2026-07-23": current,
        }[now.date().isoformat()]

    monkeypatch.setattr(controller, "_derive_cycle", derive)
    now = datetime.fromisoformat("2026-07-23T09:00:00+08:00")

    assert controller._cycle_to_reconcile(config, current, now) == first_missing

    first_path = config.reports_dir / "trend_a_share/2026-07-20.json"
    first_report = valid_cn_report(
        as_of_date=first_missing.as_of_date,
        execution_date=first_missing.execution_date,
    )
    first_path.write_text(json.dumps(first_report), encoding="utf-8")
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=first_missing.execution_date,
        report_path=first_path,
        report=first_report,
        locked_at=NOW.isoformat(),
    )

    assert controller._cycle_to_reconcile(config, current, now) == second_missing


def test_invalid_historical_batch_remains_selected_but_cannot_be_revised(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    historical = active_cn_cycle()
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="morning",
        market_open=True,
        next_check_at=NOW + timedelta(seconds=5),
    )
    report_path, report = write_report(config, revision=2)
    lock_trend_execution_batch(
        config.data_dir,
        market=historical.market,
        execution_date=historical.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    report["schema_version"] = 999
    report_path.write_text(json.dumps(report), encoding="utf-8")

    selected = controller._cycle_to_reconcile(config, current, NOW)

    assert selected.as_of_date == historical.as_of_date
    assert selected.execution_date == historical.execution_date
    with pytest.raises(ValueError, match="execution has begun"):
        controller._request_revision(config, selected, NOW)
    request_path, _ = controller._revision_paths(
        config, selected.market, selected.as_of_date
    )
    assert not request_path.exists()


def prepare_legacy_cutover(
    config: DailyPremarketConfig,
) -> tuple[ControllerCycle, Path, Path, datetime]:
    cycle = active_cn_cycle()
    report_path, report = write_report(config, revision=2)
    report["schema_version"] = 999
    report_path.write_text(json.dumps(report), encoding="utf-8")
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    request_path = controller._request_revision(config, cycle, authorized_at)
    return cycle, report_path, request_path, authorized_at


def test_legacy_cutover_skips_only_exact_expired_unreplayable_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    historical = active_cn_cycle()
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="morning",
        market_open=True,
        next_check_at=NOW + timedelta(seconds=5),
    )
    path, report = write_report(config, revision=2)
    report["schema_version"] = 999
    path.write_text(json.dumps(report), encoding="utf-8")
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    controller._request_revision(config, historical, authorized_at)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    cutover = controller._record_legacy_cycle_cutover(
        config,
        historical,
        actor="ray",
        reason="historical replay evidence and dated account snapshot unavailable",
        authorized_at=authorized_at,
    )

    assert cutover.exists()
    assert controller._execution_completed(config, historical) is True
    assert controller._cycle_to_reconcile(config, current, authorized_at) == current
    assert not controller._batch_path(
        config, historical.market, historical.execution_date
    ).exists()
    assert not (config.data_dir / "trend_review/ledgers/CN/actions").exists()


@pytest.mark.parametrize("blocker", ["open_window", "batch"])
def test_legacy_cutover_rejects_open_window_or_existing_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    config = controller_config(tmp_path)
    cycle, _, _, authorized_at = prepare_legacy_cutover(config)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    if blocker == "open_window":
        authorized_at = NOW
    else:
        batch = controller._batch_path(config, cycle.market, cycle.execution_date)
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        controller._record_legacy_cycle_cutover(
            config,
            cycle,
            actor="ray",
            reason="historical evidence unavailable",
            authorized_at=authorized_at,
        )


@pytest.mark.parametrize("bound_artifact", ["report", "request"])
def test_legacy_cutover_fails_closed_after_report_or_request_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_artifact: str,
) -> None:
    config = controller_config(tmp_path)
    cycle, report_path, request_path, authorized_at = prepare_legacy_cutover(config)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    controller._record_legacy_cycle_cutover(
        config,
        cycle,
        actor="ray",
        reason="historical evidence unavailable",
        authorized_at=authorized_at,
    )
    target = report_path if bound_artifact == "report" else request_path
    target.write_bytes(target.read_bytes() + b" ")

    with pytest.raises(ValueError, match="invalid legacy trend cutover"):
        controller._execution_completed(config, cycle)


def test_legacy_cutover_is_immutable_and_validates_operator_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle, _, _, authorized_at = prepare_legacy_cutover(config)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    values = {
        "config": config,
        "cycle": cycle,
        "actor": "ray",
        "reason": "historical evidence unavailable",
        "authorized_at": authorized_at,
    }
    first = controller._record_legacy_cycle_cutover(**values)
    assert controller._record_legacy_cycle_cutover(**values) == first
    with pytest.raises(FileExistsError, match="immutable artifact collision"):
        controller._record_legacy_cycle_cutover(
            **{**values, "reason": "different reason"}
        )
    for index, changed in enumerate((
        {"actor": ""},
        {"reason": ""},
        {"authorized_at": datetime(2026, 7, 21, 18)},
    )):
        other = controller_config(tmp_path / str(index))
        other_cycle, _, _, other_at = prepare_legacy_cutover(other)
        with pytest.raises(ValueError):
            controller._record_legacy_cycle_cutover(
                other,
                other_cycle,
                actor=str(changed.get("actor", "ray")),
                reason=str(changed.get("reason", "historical evidence unavailable")),
                authorized_at=changed.get("authorized_at", other_at),
            )


def test_revision_targets_invalid_historical_cycle_then_recovers_next_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    historical = active_cn_cycle()
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="morning",
        market_open=True,
        next_check_at=NOW + timedelta(seconds=5),
    )
    invalid_path, invalid = write_report(config, revision=2)
    invalid["schema_version"] = 999
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_derive_cycle", lambda *_args: current)
    monkeypatch.setattr(
        controller, "_run_protection_pass", lambda *_args: protection_success()
    )

    blocked = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )
    assert blocked["phase"] == "blocked"
    assert "run --revision" in str(blocked["blocker"])

    controller_lock = config.data_dir / "runs/.trend_market_controller.CN.lock"
    with RunLock(controller_lock):
        requested = run_trend_market_controller(
            config, "CN", revision=True, once=True, now_fn=lambda: NOW
        )

    historical_request, historical_completion = controller._revision_paths(
        config, historical.market, historical.as_of_date
    )
    current_request, _ = controller._revision_paths(
        config, current.market, current.as_of_date
    )
    request = json.loads(historical_request.read_text(encoding="utf-8"))
    assert requested["phase"] == "revision_requested"
    assert request["execution_date"] == historical.execution_date
    assert request["baseline_report_path"] == str(invalid_path)
    assert request["baseline_revision"] == 2
    assert not current_request.exists()

    generated: list[tuple[str, bool]] = []
    generated_ready = threading.Event()

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        run_date: str,
        revision: bool,
    ) -> None:
        generated.append((run_date, revision))
        r3_path, r3 = write_report(config, revision=3)
        write_report_delivery_receipt(config, r3_path, r3, status="sent")
        generated_ready.set()

    def capture_close(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        fact = controller._close_path(config, market, trading_date)
        fact.parent.mkdir(parents=True, exist_ok=True)
        fact.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(controller, "_capture_close", capture_close)

    def stop_after_reconcile(_seconds: float) -> None:
        if controller._batch_path(
            config, historical.market, historical.execution_date
        ).exists():
            raise RuntimeError("historical cycle reconciled")
        assert generated_ready.wait(timeout=1)

    with pytest.raises(RuntimeError, match="historical cycle reconciled"):
        run_trend_market_controller(
            config,
            "CN",
            once=False,
            now_fn=lambda: NOW,
            sleep_fn=stop_after_reconcile,
        )

    completion = json.loads(historical_completion.read_text(encoding="utf-8"))
    completed_report = Path(str(completion["report_path"]))
    assert load_trend_market_status(config, "CN", now=NOW)["blocker"] is None
    assert generated == [(historical.report_run_date, True)]
    assert completed_report.name == "2026-07-17-r3.json"
    assert completion["request_sha256"] == hashlib.sha256(
        historical_request.read_bytes()
    ).hexdigest()
    assert completion["report_sha256"] == _report_hash(
        json.loads(completed_report.read_text(encoding="utf-8"))
    )
    assert controller._execution_completed(config, historical) is True
    assert controller._cycle_to_reconcile(config, current, NOW) == current


def test_explicit_revision_request_is_durable_while_controller_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    lock_path = config.data_dir / "runs/.trend_market_controller.CN.lock"

    with RunLock(lock_path):
        result = run_trend_market_controller(
            config, "CN", revision=True, once=True, now_fn=lambda: NOW
        )

    request_path = (
        config.data_dir
        / "trend_controller/CN/revision_requests/2026-07-17.json"
    )
    assert result["phase"] == "revision_requested"
    assert json.loads(request_path.read_text(encoding="utf-8")) | {
        "schema_version": "open_trader.trend_controller.revision_request.v1",
        "market": "CN",
        "as_of_date": "2026-07-17",
        "execution_date": "2026-07-20",
    } == json.loads(request_path.read_text(encoding="utf-8"))

    next_config = controller_config(tmp_path / "next")
    report_path = next_config.reports_dir / "trend_a_share/2026-07-20.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = valid_cn_report(
        as_of_date="2026-07-20", execution_date="2026-07-21"
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")
    lock_trend_execution_batch(
        next_config.data_dir,
        market="CN",
        execution_date="2026-07-21",
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    next_cycle = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="morning",
        market_open=True,
        next_check_at=NOW,
    )
    monkeypatch.setattr(controller, "_derive_cycle", lambda *_args: next_cycle)
    with pytest.raises(ValueError, match="execution has begun"):
        run_trend_market_controller(
            next_config, "CN", revision=True, once=True, now_fn=lambda: NOW
        )


def test_pending_revision_does_not_lock_or_execute_the_base_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    base = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(controller, "_load_latest_valid_report", lambda *_args: base)
    release = threading.Event()
    started = threading.Event()

    def generate(*_args: object) -> None:
        started.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: pytest.fail("base report executed while revision was pending"),
    )

    result = run_trend_market_controller(
        config, "CN", revision=True, once=True, now_fn=lambda: NOW
    )
    release.set()

    assert started.is_set()
    assert result["phase"] == "recovering_report"
    assert not list(config.data_dir.glob("trend_review/ledgers/CN/batches/*.json"))


def test_revision_request_is_rejected_during_batch_lock_critical_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    cycle = active_cn_cycle()
    gate = config.data_dir / "runs/.trend_market_revision.CN.2026-07-20.lock"

    with RunLock(gate), pytest.raises(ValueError, match="execution has begun"):
        controller._request_revision(config, cycle, NOW)

    request, _ = controller._revision_paths(config, "CN", cycle.as_of_date)
    assert not request.exists()


def test_pending_revision_is_checked_again_at_batch_lock_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    cycle = active_cn_cycle()
    report_path, report = write_report(config)
    controller._request_revision(config, cycle, NOW)

    with pytest.raises(RuntimeError, match="revision request is pending"):
        controller._execute_locked_report(
            config,
            "CN",
            cycle.execution_date,
            report_path,
            report,
        )

    assert not list(config.data_dir.glob("trend_review/ledgers/CN/batches/*.json"))


def test_revision_replaces_invalid_frozen_report_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    invalid_path, invalid = write_report(config)
    invalid["schema_version"] = 999
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        _run_date: str,
        revision: bool,
    ) -> None:
        assert revision is True
        report_path, report = write_report(config, revision=1)
        write_report_delivery_receipt(
            config, report_path, report, status="sent"
        )

    monkeypatch.setattr(controller, "_generate_report", generate)
    executed: list[Path] = []
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, _date, path, _report: executed.append(path)
        or {"status": "unchanged", "submitted_count": 0},
    )

    result = run_trend_market_controller(
        config, "CN", revision=True, once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "monitoring"
    assert [path.name for path in executed] == ["2026-07-17-r1.json"]


def test_malformed_expected_frozen_report_blocks_without_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    path = config.reports_dir / "trend_a_share/2026-07-17.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    generated: list[object] = []
    protected: list[str] = []
    monkeypatch.setattr(controller, "_generate_report", lambda *_args: generated.append(1))
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda _config, _market, execution_date: protected.append(execution_date),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args: pytest.fail("malformed frozen report was executed"),
    )

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert result["phase"] == "blocked"
    assert "invalid frozen trend report" in str(result["blocker"])
    assert generated == []
    assert protected == ["2026-07-20"]


def test_cycle_report_does_not_fall_back_to_wrong_as_of_date(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-16.json"
    path.parent.mkdir(parents=True)
    report = valid_cn_report(
        as_of_date="2026-07-16", execution_date="2026-07-20"
    )
    path.write_text(json.dumps(report), encoding="utf-8")

    assert controller._load_cycle_report(config, active_cn_cycle()) is None


def test_load_status_rejects_malformed_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    status_path = config.data_dir / "trend_controller/CN/status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text('{"phase":"monitoring"}', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trend controller status"):
        load_trend_market_status(config, "CN", now=NOW)


def test_controller_recovers_failed_delivery_for_valid_frozen_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report = valid_cn_report(
        as_of_date="2026-07-17", execution_date="2026-07-20"
    )
    report_path = config.reports_dir / "trend_a_share/2026-07-17.json"
    receipt_path = write_report_delivery_receipt(
        config,
        report_path,
        report,
        status="delivery_failed",
        markdown="# frozen",
    )
    monkeypatch.setattr(controller, "_derive_cycle", lambda *_args: active_cn_cycle())
    monkeypatch.setattr(
        controller, "_run_protection_pass", lambda *_args: protection_success()
    )
    def capture_delivery_close(
        _config: DailyPremarketConfig, market: str, trading_date: str
    ) -> None:
        path = (
            config.data_dir
            / "trend_review"
            / "daily"
            / market
            / f"{trading_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "_capture_close", capture_delivery_close)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
    )
    monkeypatch.setattr(
        a_share_trend,
        "_attempt_report",
        lambda *_args, **_kwargs: pytest.fail("delivery recovery rebuilt content"),
    )
    monkeypatch.setattr(
        a_share_trend,
        "_deliver_a_share_daily_text",
        lambda **_kwargs: "sent",
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "sent"
    assert json.loads(report_path.read_text(encoding="utf-8"))["delivery_status"] == "sent"
    assert result["blocker"] is None


@pytest.mark.parametrize("mismatch", ["json", "markdown"])
def test_controller_rejects_receipt_bound_to_different_frozen_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    write_report(config)
    controller._request_revision(config, cycle, NOW)
    report_path, report = write_report(config, revision=1)
    different = json.loads(json.dumps(report))
    different["strategy_snapshot"]["process_version"] = "other-sha"
    write_report_delivery_receipt(
        config,
        report_path,
        report,
        status="delivery_failed",
        receipt_report=different if mismatch == "json" else None,
        receipt_markdown="# different report" if mismatch == "markdown" else None,
    )
    generated: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda _config, _market, run_date, revision: generated.append(
            (run_date, revision)
        ),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: pytest.fail("mismatched receipt was executed"),
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "blocked"
    assert "delivery receipt" in str(result["blocker"])
    assert generated == []


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_prepared_recovery_rejects_receipt_protection_state_not_in_report(
    tmp_path: Path, market: str
) -> None:
    config = controller_config(tmp_path)
    report = valid_cn_report(
        as_of_date="2026-07-17", execution_date="2026-07-20"
    )
    report["metadata"] = {
        "market": market,
        "broker": {"CN": "eastmoney", "HK": "phillips", "US": "tiger"}[market],
    }
    report_path = controller._report_dir(config, market) / "2026-07-17-r1.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_json = json.dumps(report)
    report_path.write_text(report_json, encoding="utf-8")
    report_path.with_suffix(".md").write_text("# frozen", encoding="utf-8")
    receipt_path = controller._delivery_receipt_path(config, market, report_path)
    a_share_trend._write_delivery_receipt(
        receipt_path,
        status="prepared",
        generated_at=str(report["generated_at"]),
        artifact_stem=report_path.stem,
        markdown="# frozen",
        report_json=report_json,
        protection_state={
            "schema_version": 1,
            "positions": {"different": {"active_line": "1"}},
        },
    )

    with pytest.raises(ValueError, match="protection state"):
        controller._recovery_revision_for_report(
            config, market, (report_path, report)
        )


def test_failed_r1_delivery_without_request_recovers_in_revision_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    report_path, report = write_report(config, revision=1)
    write_report_delivery_receipt(
        config,
        report_path,
        report,
        status="delivery_failed",
    )
    generated: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda _config, _market, run_date, revision: generated.append(
            (run_date, revision)
        ),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert generated == [(cycle.report_run_date, True)]
    assert not (report_path.parent / "2026-07-17.json").exists()


def test_controller_rejects_frozen_report_with_mismatched_replay_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    report_path, report = write_report(config)
    evidence_path = config.data_dir / "trend_review/evidence/CN/fake.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text("{}", encoding="utf-8")
    report["replay_evidence"] = {
        "path": str(evidence_path.relative_to(config.data_dir)),
        "sha256": "0" * 64,
    }
    write_report_delivery_receipt(
        config,
        report_path,
        report,
        status="delivery_failed",
    )
    generated: list[object] = []
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: generated.append(1),
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "blocked"
    assert "replay evidence" in str(result["blocker"])
    assert generated == []


def test_empty_action_report_executes_without_futu_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report_path, report = write_report(config)
    monkeypatch.setattr(
        controller,
        "FutuQuoteClient",
        lambda **_kwargs: pytest.fail("empty report opened quote client"),
    )
    monkeypatch.setattr(
        controller,
        "_new_order_client",
        lambda *_args: pytest.fail("empty report opened order client"),
    )

    result = controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report
    )

    assert result["status"] == "unchanged"
    assert result["submitted_count"] == 0


def test_overdue_untouched_buy_is_missed_without_futu_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report_path, report = write_report(config, buy=True)
    monkeypatch.setattr(
        controller,
        "FutuQuoteClient",
        lambda **_kwargs: pytest.fail("overdue untouched buy opened quote client"),
    )
    monkeypatch.setattr(
        controller,
        "_new_order_client",
        lambda *_args: pytest.fail("overdue untouched buy opened order client"),
    )

    result = controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report
    )

    assert result["status"] == "missed_window"
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.data_dir.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
    ]
    assert [event["status"] for event in events] == ["missed"]


def test_overdue_buy_with_pending_intent_requires_futu_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report_path, report = write_report(config, buy=True)
    action_key = trend_action_key("CN", "2026-07-20", "SH.600001", "buy")
    intent = (
        config.data_dir
        / "trend_review/ledgers/CN/open/2026-07-20"
        / f"{action_key}-intent.json"
    )
    intent.parent.mkdir(parents=True)
    intent.write_text(
        json.dumps({
            "market": "CN",
            "date": "2026-07-20",
            "report_sha256": _report_hash(report),
            "action_index": 0,
            "attempt": 1,
            "request": {
                "market": "CN",
                "futu_code": "SH.600001",
                "side": "buy",
                "order_type": "MARKET",
                "price": "0",
                "qty": "400",
                "remark": trend_attempt_remark(
                    "CN", "2026-07-20", action_key, 1
                ),
            },
            "created_at": "2026-07-20T09:31:00+08:00",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        controller, "FutuQuoteClient", lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("quote offline")
        )
    )
    monkeypatch.setattr(
        controller, "_new_order_client", lambda *_args: (_ for _ in ()).throw(
            RuntimeError("broker offline")
        )
    )

    with pytest.raises(RuntimeError, match="broker offline"):
        controller._execute_locked_report(
            config, "CN", "2026-07-20", report_path, report
        )

    assert not any(
        json.loads(path.read_text(encoding="utf-8")).get("status") == "missed"
        for path in config.data_dir.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
    )


def test_protection_runs_before_calendar_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda _config, market, day: calls.append((market, day))
        or protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("calendar offline")),
    )
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert calls == [("CN", "2026-07-20")]
    assert result["phase"] == "blocked"
    assert result["blocker"] == "calendar offline"


def test_run_protection_pass_returns_watcher_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    expected = protection_success()
    monkeypatch.setattr(
        controller, "watch_a_share_protection", lambda **_kwargs: expected
    )

    assert controller._run_protection_pass(config, "CN", "2026-07-20") is expected


@pytest.mark.parametrize("market_open", [True, False])
def test_abnormal_protection_result_disables_new_buys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, market_open: bool
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config, buy=True)
    patch_cycle(
        monkeypatch,
        replace(active_cn_cycle(), market_open=market_open),
    )
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args: SimpleNamespace(
            status="abnormal", exception_count=1, unknown_quote_count=0
        ),
    )
    allow_flags: list[bool] = []

    def execute(
        *_args: object, allow_new_buys: bool = True
    ) -> dict[str, object]:
        allow_flags.append(allow_new_buys)
        return {"status": "unchanged", "submitted_count": 0}

    monkeypatch.setattr(controller, "_execute_locked_report", execute)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert allow_flags == [False]
    assert "protection" in str(result["blocker"])


def test_protection_failure_still_executes_sell_without_new_buy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    report_path, report = write_report(config, buy=True)
    report["strategy_judgments"]["formal_actions"].insert(  # type: ignore[index]
        0, {"action": "SELL_ALL", "symbol": "600002", "reason": "trend_exit"}
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class Orders:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": [{"code": "SH.600002", "qty": "100"}],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(request)
            return {"futu_order_id": "SELL-1"}

        def close(self) -> None:
            pass

    orders = Orders()
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args: orders)
    monkeypatch.setattr(
        controller,
        "FutuQuoteClient",
        lambda **_kwargs: pytest.fail("protection failure fetched buy quotes"),
    )

    result = controller._execute_locked_report(
        config,
        "CN",
        "2026-07-20",
        report_path,
        report,
        allow_new_buys=False,
    )

    assert result["submitted_count"] == 1
    assert [request["side"] for request in orders.requests] == ["sell"]


def test_capture_close_closes_quote_if_order_client_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)

    class Quote:
        closed = False

        def close(self) -> None:
            self.closed = True

    quote = Quote()
    monkeypatch.setattr(controller, "FutuQuoteClient", lambda **_kwargs: quote)
    monkeypatch.setattr(
        controller,
        "_new_order_client",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("broker offline")),
    )

    with pytest.raises(RuntimeError, match="broker offline"):
        controller._capture_close(config, "CN", "2026-07-17")

    assert quote.closed is True


def test_fresh_zero_position_sell_writes_terminal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    cycle = active_cn_cycle()
    report_path, report = write_report(config)
    report["strategy_judgments"]["formal_actions"] = [  # type: ignore[index]
        {"action": "SELL_ALL", "symbol": "600001", "reason": "trend_exit"}
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class Orders:
        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            pytest.fail("zero-position sell submitted an order")

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "_new_order_client", lambda *_args: Orders())

    controller._execute_locked_report(
        config, "CN", cycle.execution_date, report_path, report
    )

    assert controller._execution_completed(config, cycle) is True
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.data_dir.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
    ]
    assert any(
        event.get("reason") == "position_zero_confirmed" for event in events
    )


def test_global_execution_noop_protocol_is_removed() -> None:
    assert not hasattr(controller, "_record_execution_noop")
    assert not hasattr(controller, "_execution_noop_path")


def test_revision_request_freezes_latest_report_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    baseline_path, _ = write_report(config, revision=1)

    request_path = controller._request_revision(config, cycle, NOW)

    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["baseline_report_path"] == str(baseline_path)
    assert request["baseline_report_sha256"] == hashlib.sha256(
        baseline_path.read_bytes()
    ).hexdigest()
    assert request["baseline_revision"] == 1


@pytest.mark.parametrize(
    ("market", "relative_lock"),
    [
        ("CN", "runs/.trend_a_share_report.lock"),
        ("HK", "runs/.trend_hk_phillips_report.lock"),
        ("US", "runs/.trend_us_tiger_report.lock"),
    ],
)
def test_revision_request_waits_for_report_freeze_before_capturing_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    market: str,
    relative_lock: str,
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(active_cn_cycle(), market=market)
    report_path = controller._report_dir(config, market) / "2026-07-17.json"
    report_lock = config.data_dir / relative_lock
    lock_held = threading.Event()
    release_report = threading.Event()
    baseline_checked = threading.Event()
    lock_visible_at_baseline: list[bool] = []
    original_baseline = controller._revision_baseline

    def observe_baseline(
        observed_config: DailyPremarketConfig, observed_cycle: ControllerCycle
    ) -> tuple[Path | None, str | None, int]:
        lock_visible_at_baseline.append(report_lock.exists())
        baseline_checked.set()
        return original_baseline(observed_config, observed_cycle)

    def freeze_base_report() -> None:
        with RunLock(report_lock):
            lock_held.set()
            assert release_report.wait(timeout=2)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            payload = (
                valid_cn_report(
                    as_of_date=cycle.as_of_date,
                    execution_date=cycle.execution_date,
                )
                if market == "CN"
                else {}
            )
            report_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(controller, "_revision_baseline", observe_baseline)
    with ThreadPoolExecutor(max_workers=2) as pool:
        freeze_future = pool.submit(freeze_base_report)
        assert lock_held.wait(timeout=1)
        request_future = pool.submit(controller._request_revision, config, cycle, NOW)
        try:
            assert not baseline_checked.wait(timeout=0.1)
        finally:
            release_report.set()
        freeze_future.result(timeout=1)
        request_path = request_future.result(timeout=1)

    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["baseline_report_path"] == str(report_path)
    assert request["baseline_report_sha256"] == hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    assert request["baseline_revision"] == 0
    assert lock_visible_at_baseline == [True]
    if market == "CN":
        patch_cycle(monkeypatch, cycle)
        generated: list[tuple[str, bool]] = []

        def generate(
            _config: DailyPremarketConfig,
            _market: str,
            run_date: str,
            revision: bool,
        ) -> None:
            generated.append((run_date, revision))
            r1_path, r1 = write_report(config, revision=1)
            write_report_delivery_receipt(config, r1_path, r1, status="sent")

        executed: list[Path] = []
        monkeypatch.setattr(controller, "_generate_report", generate)
        monkeypatch.setattr(
            controller,
            "_execute_locked_report",
            lambda _config, _market, _date, path, _report, **_kwargs: executed.append(
                path
            )
            or {"status": "unchanged", "submitted_count": 0},
        )

        run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

        assert generated == [(cycle.report_run_date, True)]
        assert [path.name for path in executed] == ["2026-07-17-r1.json"]


def test_revision_requested_without_baseline_requires_r1_not_r0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    request_path = controller._request_revision(config, cycle, NOW)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["baseline_revision"] == -1
    r0_path, r0 = write_report(config)
    write_report_delivery_receipt(config, r0_path, r0, status="sent")
    generated: list[tuple[str, bool]] = []

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        run_date: str,
        revision: bool,
    ) -> None:
        generated.append((run_date, revision))
        r1_path, r1 = write_report(config, revision=1)
        write_report_delivery_receipt(config, r1_path, r1, status="sent")

    executed: list[Path] = []
    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, _date, path, _report, **_kwargs: executed.append(path)
        or {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    _, completion_path = controller._revision_paths(
        config, cycle.market, cycle.as_of_date
    )
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert generated == [(cycle.report_run_date, True)]
    assert [path.name for path in executed] == ["2026-07-17-r1.json"]
    assert completion["report_path"].endswith("2026-07-17-r1.json")


def test_revision_completion_rejects_r0_when_baseline_is_missing(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    request_path = controller._request_revision(config, cycle, NOW)
    report_path, report = write_report(config)
    write_report_delivery_receipt(config, report_path, report, status="sent")
    _, completion_path = controller._revision_paths(
        config, cycle.market, cycle.as_of_date
    )
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.revision_completion.v1",
            "market": cycle.market,
            "as_of_date": cycle.as_of_date,
            "execution_date": cycle.execution_date,
            "request_path": str(request_path),
            "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
            "report_path": str(report_path),
            "report_sha256": _report_hash(report),
            "completed_at": NOW.isoformat(),
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend report revision completion"):
        controller._revision_state(
            config,
            cycle.market,
            cycle.as_of_date,
            cycle.execution_date,
        )


def test_legacy_revision_request_without_baseline_fails_closed(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    request_path, _ = controller._revision_paths(
        config, cycle.market, cycle.as_of_date
    )
    request_path.parent.mkdir(parents=True)
    request_path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.revision_request.v1",
            "market": cycle.market,
            "as_of_date": cycle.as_of_date,
            "execution_date": cycle.execution_date,
            "requested_at": NOW.isoformat(),
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid trend report revision request"):
        controller._revision_state(
            config,
            cycle.market,
            cycle.as_of_date,
            cycle.execution_date,
        )


def test_revision_frozen_before_request_requires_next_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    r1_path, _ = write_report(config, revision=1)
    controller._request_revision(config, cycle, NOW)
    generated: list[tuple[str, bool]] = []

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        run_date: str,
        revision: bool,
    ) -> None:
        generated.append((run_date, revision))
        report_path, report = write_report(config, revision=2)
        write_report_delivery_receipt(
            config, report_path, report, status="sent"
        )

    executed: list[Path] = []
    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, _date, path, _report, **_kwargs: executed.append(path)
        or {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    _, completion_path = controller._revision_paths(
        config, cycle.market, cycle.as_of_date
    )
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert generated == [(cycle.report_run_date, True)]
    assert [path.name for path in executed] == ["2026-07-17-r2.json"]
    assert completion["report_path"].endswith("2026-07-17-r2.json")
    assert r1_path.exists()


def test_pending_revision_does_not_accept_newer_report_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    write_report(config)
    controller._request_revision(config, cycle, NOW)
    r1_path, _ = write_report(config, revision=1)
    generated: list[tuple[str, bool]] = []

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        run_date: str,
        revision: bool,
    ) -> None:
        generated.append((run_date, revision))
        r2_path, r2 = write_report(config, revision=2)
        write_report_delivery_receipt(
            config, r2_path, r2, status="sent"
        )

    executed: list[Path] = []
    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, _date, path, _report, **_kwargs: executed.append(path)
        or {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert generated == [(cycle.report_run_date, True)]
    assert [path.name for path in executed] == ["2026-07-17-r2.json"]
    assert r1_path.exists()


def test_pending_revision_completes_existing_delivered_r1_without_r2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    write_report(config)
    request = controller._request_revision(config, cycle, NOW)
    r1_path, r1 = write_report(config, revision=1)
    write_report_delivery_receipt(config, r1_path, r1, status="sent")
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: pytest.fail("existing delivered r1 generated r2"),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    _, completion_path = controller._revision_paths(config, "CN", cycle.as_of_date)
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert completion["request_path"] == str(request)
    assert completion["request_sha256"] == hashlib.sha256(request.read_bytes()).hexdigest()
    assert completion["report_path"] == str(r1_path)
    assert completion["report_sha256"] == _report_hash(r1)
    assert not (r1_path.parent / "2026-07-17-r2.json").exists()


def test_pending_revision_recovers_existing_failed_r1_and_binds_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    write_report(config)
    request = controller._request_revision(config, cycle, NOW)
    r1_path, r1 = write_report(config, revision=1)
    receipt_path = write_report_delivery_receipt(
        config,
        r1_path,
        r1,
        status="delivery_failed",
    )
    generated: list[tuple[str, bool]] = []

    def recover(
        _config: DailyPremarketConfig,
        _market: str,
        run_date: str,
        revision: bool,
    ) -> None:
        generated.append((run_date, revision))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["status"] = "sent"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    monkeypatch.setattr(controller, "_generate_report", recover)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    _, completion_path = controller._revision_paths(config, "CN", cycle.as_of_date)
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert generated == [(cycle.report_run_date, True)]
    assert completion["request_sha256"] == hashlib.sha256(request.read_bytes()).hexdigest()
    assert completion["report_path"] == str(r1_path)
    assert completion["report_sha256"] == _report_hash(r1)
    assert not (r1_path.parent / "2026-07-17-r2.json").exists()


def test_controller_cycle_has_no_unused_buy_window_field() -> None:
    assert "buy_window_open" not in {field.name for field in fields(ControllerCycle)}
