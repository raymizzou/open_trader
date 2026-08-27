from __future__ import annotations

import hashlib
import json
import multiprocessing
import socket
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader import a_share_trend as a_share_trend
from open_trader import trend_market_controller as controller
from open_trader import trend_review
from open_trader.account_http import AccountHttpError
from open_trader.daily_premarket import DailyPremarketConfig, RunLock
from open_trader.futu_symbols import to_futu_symbol
from open_trader.kelly_order_execution import FutuOrderExecutionError
from open_trader.notifications import (
    CompositeNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
)
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
from open_trader.trend_allocation import build_allocation_snapshot


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


@pytest.fixture(autouse=True)
def legacy_scheduler_execution_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep pre-request-boundary execution fixtures focused on order behavior."""
    original_require = controller.require_trend_review_config
    original_execute = controller.execute_simulated_trend_report

    def execute(
        config: DailyPremarketConfig,
        market: str,
        execution_date: str,
        report_sha: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if (
            controller.require_trend_review_config is original_require
        ):
            try:
                original_require(config, market)
            except ValueError:
                for path in controller._report_dir(config, market).glob("*.json"):
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict) and controller._report_hash(payload) == report_sha:
                        return controller._execute_locked_report(
                            config,
                            market,
                            execution_date,
                            path,
                            payload,
                            allow_new_buys=kwargs.get("allow_new_buys", True),
                            quote_client=kwargs.get("quote_client"),
                            order_client=kwargs.get("order_client"),
                        )
        return original_execute(
            config,
            market,
            execution_date,
            report_sha,
            actor=str(kwargs.get("actor") or "trend-market-controller"),
            reason=str(kwargs.get("reason") or "scheduled execution"),
            now=kwargs.get("now") if isinstance(kwargs.get("now"), datetime) else None,
            quote_client=kwargs.get("quote_client"),
            order_client=kwargs.get("order_client"),
            allow_new_buys=bool(kwargs.get("allow_new_buys", True)),
            scheduled=bool(kwargs.get("scheduled", False)),
        )

    monkeypatch.setattr(controller, "execute_simulated_trend_report", execute)


def write_controller_action(
    config: DailyPremarketConfig,
    key: str,
    payload: dict[str, object],
) -> None:
    path = (
        config.data_dir
        / "trend_review/ledgers/CN/actions/2026-07-20"
        / key
        / "2026-07-20T09-31-00+08-00.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**payload, "recorded_at": NOW.isoformat()}),
        encoding="utf-8",
    )


class FlakyFeishu(FeishuWebhookNotifier):
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempt_count = 0

    def notify(self, title: str, message: str) -> None:
        self.attempt_count += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Feishu unavailable")


class RecordingMacOS(MacOSNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


def _retry_pending_feishu_notifications_in_process(
    config: DailyPremarketConfig,
    start: object,
    finished: object,
    attempts: object,
    release: object,
    entered: object | None = None,
) -> None:
    controller.build_notifier = lambda _config: CompositeNotifier([
        BlockingProcessFeishu(attempts, release, entered)
    ])
    start.wait()
    try:
        controller._retry_pending_feishu_notifications(config)
    finally:
        with finished.get_lock():
            finished.value += 1


class BlockingProcessFeishu(FeishuWebhookNotifier):
    def __init__(
        self,
        attempts: object,
        release: object,
        entered: object | None = None,
    ) -> None:
        self.attempts = attempts
        self.release = release
        self.entered = entered

    def notify(self, title: str, message: str) -> None:
        with self.attempts.get_lock():
            self.attempts.value += 1
        if self.entered is not None:
            self.entered.set()
        self.release.wait(timeout=5)


def test_controller_notification_retries_only_feishu_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(controller_config(tmp_path), notifiers=("feishu", "macos"))
    feishu = FlakyFeishu(failures=1)
    macos = RecordingMacOS()
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([feishu, macos]),
    )
    key = (
        config,
        "US",
        "2026-07-22",
        "controller",
        "snapshot_failed",
        "2026-07-22T10:00:00+08:00",
    )

    assert controller._notify_once(
        "US 趋势控制器阻塞", "snapshot unavailable", key
    ) is False
    identity = "|".join(("US", "2026-07-22", "controller", "snapshot_failed"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{digest}.json"
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": "open_trader.trend_controller.notification.v2",
        "market": "US",
        "execution_date": "2026-07-22",
        "action": "controller",
        "reason": "snapshot_failed",
        "occurred_at": "2026-07-22T10:00:00+08:00",
        "non_feishu_attempted": True,
        "feishu_attempts": 1,
        "feishu_title": "【需处理｜富途｜美股趋势控制器阻塞｜2026-07-22】",
        "feishu_message": (
            "发生：趋势控制器已进入阻塞状态\n"
            "影响：美股自动趋势流程暂停\n"
            "现在做：检查 Dashboard 控制器状态与最近日志\n"
            "原因：详见控制器日志"
        ),
        "channels": ["macos"],
    }

    controller._retry_pending_feishu_notifications(config)
    assert controller._notify_once(
        "US 趋势控制器阻塞", "snapshot unavailable", key
    ) is True

    assert feishu.attempt_count == 2
    assert len(macos.messages) == 1


def test_terminal_rejection_notification_is_stable_across_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(controller_config(tmp_path), notifiers=("macos",))
    macos = RecordingMacOS()
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([macos]),
    )
    failed = {
        "market": "CN",
        "date": "2026-07-20",
        "report_sha256": "a" * 64,
        "action_index": 0,
        "symbol": "600001",
        "futu_code": "SH.600001",
        "side": "buy",
        "status": "failed",
        "reason": "broker_order_no_progress",
        "filled_qty": "0",
        "target_qty": "100",
    }
    events = [failed]
    monkeypatch.setattr(
        controller,
        "_latest_action_events",
        lambda *_args: list(events),
    )

    controller._notify_terminal_rejections(
        config, "CN", "2026-07-20", "2026-07-20T09:31:00+08:00"
    )
    controller._notify_terminal_rejections(
        config, "CN", "2026-07-20", "2026-07-20T09:32:00+08:00"
    )

    assert len(macos.messages) == 1
    events[:] = [{**failed, "status": "submitted"}]
    controller._notify_terminal_rejections(
        config, "CN", "2026-07-20", "2026-07-20T09:33:00+08:00"
    )
    assert len(macos.messages) == 1


def test_controller_terminal_rejection_sets_blocker_and_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        write_controller_action(config, "600001-buy", {
            "market": "CN",
            "date": "2026-07-20",
            "report_sha256": _report_hash(report[1]),
            "action_index": 0,
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "status": "failed",
            "reason": "broker_order_no_progress",
            "filled_qty": "0",
            "target_qty": "100",
        })
        return {
            "status": "terminal_rejected",
            "submitted_count": 0,
            "terminal_rejected": True,
        }

    monkeypatch.setattr(controller, "_execute_locked_report", execute)
    notifications: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        controller,
        "_notify_once",
        lambda title, message, key: notifications.append((title, message, key))
        or True,
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "terminal_rejected"
    assert result["blocker"] == "terminal_rejected"
    assert len(notifications) == 1


@pytest.mark.parametrize("status", ["submitted", "filled", "complete", "unchanged"])
def test_terminal_rejection_notification_ignores_normal_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    config = replace(controller_config(tmp_path), notifiers=("macos",))
    macos = RecordingMacOS()
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([macos]),
    )
    monkeypatch.setattr(
        controller,
        "_latest_action_events",
        lambda *_args: [{"status": status}],
    )

    assert controller._notify_terminal_rejections(
        config, "CN", "2026-07-20", "2026-07-20T09:31:00+08:00"
    ) == 0
    assert macos.messages == []


def test_controller_notification_stops_after_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    feishu = FlakyFeishu(failures=2)
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([feishu]),
    )
    key = (
        config,
        "US",
        "2026-07-22",
        "controller",
        "snapshot_failed",
        "2026-07-22T10:00:00+08:00",
    )

    assert controller._notify_once(
        "US 趋势控制器阻塞", "snapshot unavailable", key
    ) is False
    controller._retry_pending_feishu_notifications(config)
    controller._retry_pending_feishu_notifications(config)

    assert feishu.attempt_count == 2


def test_concurrent_controller_retries_send_feishu_once(tmp_path: Path) -> None:
    config = controller_config(tmp_path)
    context = multiprocessing.get_context("spawn")
    attempts = context.Value("i", 0)
    finished = context.Value("i", 0)
    release = context.Event()
    entered = context.Event()
    identity = "|".join(("US", "2026-07-22", "controller", "snapshot_failed"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{digest}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.notification.v2",
            "market": "US",
            "execution_date": "2026-07-22",
            "action": "controller",
            "reason": "snapshot_failed",
            "occurred_at": "2026-07-22T10:00:00+08:00",
            "non_feishu_attempted": True,
            "feishu_attempts": 1,
            "feishu_title": "retry title",
            "feishu_message": "retry message",
            "channels": ["macos"],
        }),
        encoding="utf-8",
    )
    start = context.Event()
    processes = [
        context.Process(
            target=_retry_pending_feishu_notifications_in_process,
            args=(config, start, finished, attempts, release, entered),
        )
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    start.set()
    assert entered.wait(timeout=5)
    deadline = time.monotonic() + 5
    try:
        while finished.value < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert attempts.value == 1
        assert finished.value == 2
    finally:
        release.set()
        for process in processes:
            process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert attempts.value == 1


def test_direct_notification_retry_and_scanner_send_feishu_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(controller_config(tmp_path), notifiers=("feishu",))
    context = multiprocessing.get_context("spawn")
    attempts = context.Value("i", 0)
    finished = context.Value("i", 0)
    release = context.Event()
    entered = context.Event()
    identity = "|".join(("US", "2026-07-22", "controller", "snapshot_failed"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{digest}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.notification.v2",
            "market": "US",
            "execution_date": "2026-07-22",
            "action": "controller",
            "reason": "snapshot_failed",
            "occurred_at": "2026-07-22T10:00:00+08:00",
            "non_feishu_attempted": True,
            "feishu_attempts": 1,
            "feishu_title": "retry title",
            "feishu_message": "retry message",
            "channels": ["macos"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([
            BlockingProcessFeishu(attempts, release)
        ]),
    )
    start = context.Event()
    scanner = context.Process(
        target=_retry_pending_feishu_notifications_in_process,
        args=(config, start, finished, attempts, release, entered),
    )
    scanner.start()
    start.set()
    assert entered.wait(timeout=5)
    direct_finished = threading.Event()
    direct_results: list[bool] = []
    key = (
        config,
        "US",
        "2026-07-22",
        "controller",
        "snapshot_failed",
        "2026-07-22T10:00:01+08:00",
    )

    def retry_directly() -> None:
        direct_results.append(
            controller._notify_once(
                "US 趋势控制器阻塞", "retry unavailable", key
            )
        )
        direct_finished.set()

    direct = threading.Thread(target=retry_directly)
    direct.start()
    deadline = time.monotonic() + 5
    try:
        while (
            attempts.value < 2
            and not direct_finished.is_set()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert attempts.value == 1
        assert direct_finished.wait(timeout=5)
        assert direct_results == [False]
    finally:
        release.set()
        direct.join(timeout=5)
        scanner.join(timeout=5)

    assert scanner.exitcode == 0
    assert attempts.value == 1


def test_concurrent_first_controller_notifications_send_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent: list[set[str]] = []
    first_non_feishu = threading.Event()
    second_non_feishu = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    local_attempts = 0
    mutex = threading.Lock()

    def send(
        _notifier: object,
        _title: str,
        _message: str,
        *,
        channels: set[str],
    ) -> list[SimpleNamespace]:
        nonlocal local_attempts
        if "feishu" not in channels:
            with mutex:
                local_attempts += 1
                attempt = local_attempts
            if attempt == 1:
                first_non_feishu.set()
                assert release_first.wait(timeout=2)
            else:
                second_non_feishu.set()
                assert release_second.wait(timeout=2)
        sent.append(channels)
        channel = "feishu_app" if "feishu" in channels else "macos"
        return [SimpleNamespace(channel=channel, success=True)]

    monkeypatch.setattr(controller, "send_notification_with_results", send)
    key = (
        config,
        "US",
        "2026-07-22",
        "controller",
        "snapshot_failed",
        "2026-07-22T10:00:00+08:00",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            controller._notify_once,
            "US 趋势控制器阻塞",
            "snapshot unavailable",
            key,
        )
        assert first_non_feishu.wait(timeout=2)
        second = pool.submit(
            controller._notify_once,
            "US 趋势控制器阻塞",
            "snapshot unavailable",
            key,
        )
        if second_non_feishu.wait(timeout=1):
            release_first.set()
            assert first.result(timeout=2) is True
            release_second.set()
        else:
            assert second.done()
            release_first.set()
            assert first.result(timeout=2) is True
        second.result(timeout=2)

    assert sent.count({"macos", "xiaoai"}) == 1
    assert sent.count({"feishu", "feishu_app"}) == 1


def test_legacy_controller_notification_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    feishu = FlakyFeishu(failures=0)
    macos = RecordingMacOS()
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([feishu, macos]),
    )
    identity = "|".join(("US", "2026-07-22", "controller", "snapshot_failed"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{digest}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.notification.v1",
            "market": "US",
            "execution_date": "2026-07-22",
            "action": "controller",
            "reason": "snapshot_failed",
            "notified_at": "2026-07-22T09:00:00+08:00",
            "channels": ["macos"],
        }),
        encoding="utf-8",
    )

    key = (
        config,
        "US",
        "2026-07-22",
        "controller",
        "snapshot_failed",
        "2026-07-22T10:00:00+08:00",
    )
    assert controller._notify_once(
        "US 趋势控制器阻塞", "snapshot unavailable", key
    ) is True
    assert feishu.attempt_count == 0
    assert macos.messages == []


def test_ambiguous_legacy_v1_controller_notification_does_not_suppress_current_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent = _record_controller_notification_attempts(monkeypatch)
    legacy_identity = "|".join(
        ("US", "2026-07-22", "controller", "RuntimeError")
    )
    legacy_digest = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()
    legacy_path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{legacy_digest}.json"
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.notification.v1",
            "market": "US",
            "execution_date": "2026-07-22",
            "action": "controller",
            "reason": "RuntimeError",
            "notified_at": "2026-07-22T09:00:00+08:00",
            "channels": ["macos"],
        }),
        encoding="utf-8",
    )
    review_identity = "|".join(
        ("US", "2026-07-22", "review", "RuntimeError")
    )
    review_digest = hashlib.sha256(review_identity.encode("utf-8")).hexdigest()
    review_path = legacy_path.with_name(f"{review_digest}.json")

    assert controller._notify_controller_failure(
        config,
        "US",
        "2026-07-22",
        "review",
        RuntimeError("same exception"),
        datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
    ) is True

    assert [channels for _, _, channels in sent] == [
        {"macos", "xiaoai"},
        {"feishu", "feishu_app"},
    ]
    assert json.loads(review_path.read_text(encoding="utf-8")) == {
        "schema_version": "open_trader.trend_controller.notification.v2",
        "market": "US",
        "execution_date": "2026-07-22",
        "action": "review",
        "reason": "RuntimeError",
        "occurred_at": "2026-07-22T10:00:00+08:00",
        "non_feishu_attempted": True,
        "feishu_attempts": 1,
        "feishu_title": "【需处理｜富途｜美股趋势复盘待恢复｜2026-07-22】",
        "feishu_message": (
            "发生：趋势复盘未完成\n"
            "影响：复盘数据暂未更新\n"
            "现在做：检查 OpenD 与复盘账本后等待自动恢复\n"
            "原因：详见控制器日志"
        ),
        "channels": ["macos", "feishu_app"],
    }


def test_delivered_legacy_v2_review_is_not_replayed_as_current_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent = _record_controller_notification_attempts(monkeypatch)
    legacy_identity = "|".join(
        ("US", "2026-07-22", "controller", "RuntimeError")
    )
    legacy_digest = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()
    legacy_path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{legacy_digest}.json"
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.notification.v2",
            "market": "US",
            "execution_date": "2026-07-22",
            "action": "controller",
            "reason": "RuntimeError",
            "occurred_at": "2026-07-22T09:00:00+08:00",
            "non_feishu_attempted": True,
            "feishu_attempts": 1,
            "feishu_title": "【需处理｜富途｜美股趋势复盘待恢复｜2026-07-22】",
            "feishu_message": "review unavailable",
            "channels": ["feishu_app"],
        }),
        encoding="utf-8",
    )
    review_identity = "|".join(
        ("US", "2026-07-22", "review", "RuntimeError")
    )
    review_digest = hashlib.sha256(review_identity.encode("utf-8")).hexdigest()
    review_path = legacy_path.with_name(f"{review_digest}.json")

    assert controller._notify_controller_failure(
        config,
        "US",
        "2026-07-22",
        "review",
        RuntimeError("same exception"),
        datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
    ) is True

    assert sent == []
    assert not review_path.exists()


def test_legacy_v2_controller_alert_does_not_suppress_current_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent = _record_controller_notification_attempts(monkeypatch)
    legacy_identity = "|".join(
        ("US", "2026-07-22", "controller", "RuntimeError")
    )
    legacy_digest = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()
    legacy_path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{legacy_digest}.json"
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.notification.v2",
            "market": "US",
            "execution_date": "2026-07-22",
            "action": "controller",
            "reason": "RuntimeError",
            "occurred_at": "2026-07-22T09:00:00+08:00",
            "non_feishu_attempted": True,
            "feishu_attempts": 1,
            "feishu_title": "【需处理｜富途｜美股趋势控制器阻塞｜2026-07-22】",
            "feishu_message": "controller unavailable",
            "channels": ["feishu_app"],
        }),
        encoding="utf-8",
    )
    review_identity = "|".join(
        ("US", "2026-07-22", "review", "RuntimeError")
    )
    review_digest = hashlib.sha256(review_identity.encode("utf-8")).hexdigest()
    review_path = legacy_path.with_name(f"{review_digest}.json")

    assert controller._notify_controller_failure(
        config,
        "US",
        "2026-07-22",
        "review",
        RuntimeError("same exception"),
        datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
    ) is True

    assert [channels for _, _, channels in sent] == [
        {"macos", "xiaoai"},
        {"feishu", "feishu_app"},
    ]
    assert review_path.exists()


def test_pending_legacy_v2_review_retries_only_its_feishu_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent = _record_controller_notification_attempts(monkeypatch)
    legacy_identity = "|".join(
        ("US", "2026-07-22", "controller", "RuntimeError")
    )
    legacy_digest = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()
    legacy_path = (
        config.data_dir
        / "trend_controller/US/notifications/2026-07-22"
        / f"{legacy_digest}.json"
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_controller.notification.v2",
            "market": "US",
            "execution_date": "2026-07-22",
            "action": "controller",
            "reason": "RuntimeError",
            "occurred_at": "2026-07-22T09:00:00+08:00",
            "non_feishu_attempted": True,
            "feishu_attempts": 1,
            "feishu_title": "【需处理｜富途｜美股趋势复盘待恢复｜2026-07-22】",
            "feishu_message": "review unavailable",
            "channels": ["macos"],
        }),
        encoding="utf-8",
    )
    review_identity = "|".join(
        ("US", "2026-07-22", "review", "RuntimeError")
    )
    review_digest = hashlib.sha256(review_identity.encode("utf-8")).hexdigest()
    review_path = legacy_path.with_name(f"{review_digest}.json")

    assert controller._notify_controller_failure(
        config,
        "US",
        "2026-07-22",
        "review",
        RuntimeError("same exception"),
        datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
    ) is True

    assert [channels for _, _, channels in sent] == [{"feishu", "feishu_app"}]
    assert not review_path.exists()
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == {
        "schema_version": "open_trader.trend_controller.notification.v2",
        "market": "US",
        "execution_date": "2026-07-22",
        "action": "controller",
        "reason": "RuntimeError",
        "occurred_at": "2026-07-22T09:00:00+08:00",
        "non_feishu_attempted": True,
        "feishu_attempts": 2,
        "feishu_title": "【需处理｜富途｜美股趋势复盘待恢复｜2026-07-22】",
        "feishu_message": "review unavailable",
        "channels": ["macos", "feishu_app"],
    }


def test_protection_blocker_notifies_feishu_once_per_market_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    feishu = FlakyFeishu(failures=0)
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([feishu]),
    )

    for occurred_at in (
        "2026-07-22T09:31:00+08:00",
        "2026-07-22T09:31:05+08:00",
    ):
        controller._notify_protection_blocker(
            config,
            "CN",
            "2026-07-22",
            "protection pass abnormal: unknown_quotes=2",
            occurred_at,
        )

    assert feishu.attempt_count == 1


def test_protection_blocker_accepts_only_clean_holiday() -> None:
    clean = SimpleNamespace(
        status="holiday", exception_count=0, unknown_quote_count=0
    )
    unknown = SimpleNamespace(
        status="holiday", exception_count=0, unknown_quote_count=1
    )

    assert controller._protection_blocker(clean) is None
    assert controller._protection_blocker(unknown) == (
        "protection pass abnormal: status=holiday, exceptions=0, "
        "unknown_quotes=1"
    )


def _record_controller_notification_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, set[str]]]:
    sent: list[tuple[str, str, set[str]]] = []

    def send(
        _notifier: object,
        title: str,
        message: str,
        *,
        channels: set[str],
    ) -> list[SimpleNamespace]:
        sent.append((title, message, channels))
        channel = "feishu_app" if "feishu" in channels else "macos"
        return [SimpleNamespace(channel=channel, success=True)]

    monkeypatch.setattr(controller, "send_notification_with_results", send)
    return sent


def test_different_connectivity_errors_share_one_controller_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent = _record_controller_notification_attempts(monkeypatch)

    controller._notify_controller_failure(
        config,
        "CN",
        "2026-07-22",
        "calendar",
        RuntimeError("Connect timeout"),
        datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
    )
    controller._notify_controller_failure(
        config,
        "HK",
        "2026-07-22",
        "controller",
        RuntimeError("protocol disconnected"),
        datetime.fromisoformat("2026-07-22T10:00:01+08:00"),
    )

    feishu = [item for item in sent if "feishu" in item[2]]
    assert len(feishu) == 1
    assert feishu[0][0] == "【需处理｜系统｜OpenD 连接故障｜2026-07-22】"
    assert "影响：CN、HK、US 行情与订单监控可能中断" in feishu[0][1]
    state = json.loads(
        (
            config.data_dir
            / "trend_controller/shared/incidents/opend-connectivity.json"
        ).read_text(encoding="utf-8")
    )
    assert state["affected_markets"] == ["CN", "HK"]


def test_connectivity_and_rate_limit_are_separate_controller_incidents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent = _record_controller_notification_attempts(monkeypatch)

    for error in (RuntimeError("network down"), RuntimeError("请求频率太高")):
        controller._notify_controller_failure(
            config,
            "US",
            "2026-07-22",
            "controller",
            error,
            datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
        )

    feishu_titles = [title for title, _, channels in sent if "feishu" in channels]
    assert feishu_titles == [
        "【需处理｜系统｜OpenD 连接故障｜2026-07-22】",
        "【需处理｜系统｜OpenD 请求限频｜2026-07-22】",
    ]
    assert (
        config.data_dir
        / "trend_controller/shared/incidents/opend-connectivity.json"
    ).exists()
    assert (
        config.data_dir
        / "trend_controller/shared/incidents/opend-rate-limit.json"
    ).exists()


def test_shared_incident_state_failure_falls_back_to_per_market_feishu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    incident = (
        config.data_dir
        / "trend_controller/shared/incidents/opend-connectivity.json"
    )
    incident.parent.mkdir(parents=True, exist_ok=True)
    incident.write_text("{}", encoding="utf-8")
    sent = _record_controller_notification_attempts(monkeypatch)

    controller._notify_controller_failure(
        config,
        "CN",
        "2026-07-22",
        "controller",
        RuntimeError("Connect timeout"),
        datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
    )

    feishu = [item for item in sent if "feishu" in item[2]]
    assert len(feishu) == 1
    assert feishu[0][0] == "【需处理｜东方财富｜A股趋势控制器阻塞｜2026-07-22】"
    assert len([item for item in sent if "feishu" not in item[2]]) == 1


def test_unknown_controller_errors_stay_per_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    sent = _record_controller_notification_attempts(monkeypatch)

    controller._notify_controller_failure(
        config,
        "US",
        "2026-07-22",
        "controller",
        RuntimeError("unknown broker response"),
        datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
    )

    feishu = [item for item in sent if "feishu" in item[2]]
    assert len(feishu) == 1
    assert feishu[0][0] == "【需处理｜富途｜美股趋势控制器阻塞｜2026-07-22】"
    local = [item for item in sent if "feishu" not in item[2]]
    assert local == [
        (
            "US 趋势控制器阻塞",
            "unknown broker response",
            {"macos", "xiaoai"},
        )
    ]
    assert not (
        config.data_dir / "trend_controller/shared/incidents"
    ).exists()
    states = list(
        config.data_dir.glob(
            "trend_controller/US/notifications/2026-07-22/*.json"
        )
    )
    assert len(states) == 1
    assert json.loads(states[0].read_text(encoding="utf-8"))["reason"] == (
        "RuntimeError"
    )


def test_review_and_controller_failures_keep_distinct_notification_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    _record_controller_notification_attempts(monkeypatch)
    occurred_at = datetime.fromisoformat("2026-07-22T10:00:00+08:00")

    for action in ("review", "controller"):
        controller._notify_controller_failure(
            config,
            "US",
            "2026-07-22",
            action,
            RuntimeError("same exception"),
            occurred_at,
        )

    states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.data_dir.glob(
            "trend_controller/US/notifications/2026-07-22/*.json"
        )
    ]
    assert {state["action"] for state in states} == {"review", "controller"}


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


def test_successful_controller_cycle_records_opend_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    write_report(config)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    incident = (
        config.data_dir
        / "trend_controller/shared/incidents/opend-connectivity.json"
    )
    incident.parent.mkdir(parents=True, exist_ok=True)
    incident.write_text(
        json.dumps({
            "schema_version": "open_trader.opend_incident.v1",
            "category": "connectivity",
            "active": True,
            "first_detected_at": "2026-07-20T09:30:00+08:00",
            "updated_at": "2026-07-20T09:30:00+08:00",
            "affected_markets": ["CN"],
            "reasons": {"CN": "连接超时"},
            "healthy_markets": [],
            "feishu_attempts": 1,
            "feishu_delivered_at": "2026-07-20T09:30:00+08:00",
            "channels": ["feishu_app"],
        }),
        encoding="utf-8",
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "monitoring"
    assert json.loads(incident.read_text(encoding="utf-8"))["active"] is False


def test_repeated_controller_and_watcher_calendar_queries_stay_below_futu_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_trader.futu_quote import FutuQuoteClient
    from open_trader.market_trend_watch import next_market_open

    requests: list[tuple[object, str, str]] = []
    contexts: list[object] = []

    class CalendarContext:
        def __init__(self, *, host: str, port: int) -> None:
            self.closed = False
            contexts.append(self)

        def request_trading_days(
            self, *, market: object, start: str, end: str
        ) -> tuple[int, list[dict[str, str]]]:
            requests.append((market, start, end))
            current = date.fromisoformat(start)
            last = date.fromisoformat(end)
            rows: list[dict[str, str]] = []
            while current <= last:
                if current.weekday() < 5:
                    rows.append({"time": current.isoformat()})
                current += timedelta(days=1)
            return 0, rows

        def close(self) -> None:
            self.closed = True

    def context_factory(*, host: str, port: int) -> CalendarContext:
        return CalendarContext(host=host, port=port)

    def quote_factory(**kwargs: object) -> FutuQuoteClient:
        return FutuQuoteClient(
            **kwargs,
            context_factory=context_factory,
            connectivity_checker=lambda _host, _port: True,
        )

    monkeypatch.setattr(controller, "FutuQuoteClient", quote_factory)
    monkeypatch.setattr(controller, "_durable_report_cycles", lambda *_args: [])
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args: True)
    config = controller_config(tmp_path)
    now = datetime.fromisoformat("2026-07-20T09:31:00+08:00")

    for _ in range(6):
        for market in ("CN", "HK", "US"):
            cycle = controller._derive_cycle(config, market, now)
            assert controller._cycle_to_reconcile(config, cycle, now) == cycle
        for market in ("HK", "US"):
            quote = quote_factory(host=config.futu_host, port=config.futu_port)
            try:
                local_day = now.astimezone(controller.TIMEZONES[market]).date()
                quote.get_trading_days(
                    market=market,
                    start=local_day.isoformat(),
                    end=local_day.isoformat(),
                )
                next_market_open(quote, market=market, now=now)
            finally:
                quote.close()

    assert len(contexts) > 30
    assert all(context.closed for context in contexts)
    assert len(requests) == 10


def test_cycle_reconciliation_reuses_completed_audits_without_new_quote_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(
        controller,
        "FutuQuoteClient",
        lambda **_kwargs: pytest.fail("opened an unowned Futu quote client"),
    )
    current = active_cn_cycle()
    historical = replace(
        current,
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        report_run_date="2026-07-16",
    )
    calls: list[str] = []
    progress_calls: list[None] = []
    completed_execution_dates: set[str] = set()
    quote = SimpleNamespace(
        get_trading_days=lambda **_kwargs: [
            "2026-07-16",
            "2026-07-17",
            "2026-07-20",
            "2026-07-21",
        ]
    )
    monkeypatch.setattr(
        controller, "_durable_report_cycles", lambda *_args: [historical]
    )
    def execution_completed(
        _config: DailyPremarketConfig,
        cycle: ControllerCycle,
        *,
        progress: Callable[[], None] | None = None,
    ) -> bool:
        assert progress is not None
        progress()
        calls.append(cycle.execution_date)
        return True

    monkeypatch.setattr(controller, "_execution_completed", execution_completed)

    for _ in range(2):
        assert controller._cycle_to_reconcile(
            config,
            current,
            NOW,
            quote_client=quote,
            completed_execution_dates=completed_execution_dates,
            progress=lambda: progress_calls.append(None),
        ) == current

    assert calls == [historical.execution_date]
    assert progress_calls == [None]


def test_controller_reuses_quote_and_account_clients_across_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    quote_clients: list[object] = []
    account_clients: list[object] = []

    class Quote:
        closed = False

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-17", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            self.closed = True

    class Account:
        closed = False

        def close(self) -> None:
            self.closed = True

    def quote_factory(**_kwargs: object) -> object:
        quote = Quote()
        quote_clients.append(quote)
        return quote

    def account_factory(**_kwargs: object) -> object:
        account = Account()
        account_clients.append(account)
        return account

    def protect(
        _config: DailyPremarketConfig,
        _market: str,
        day: str,
        *,
        quote_client: object,
        account_loader: Callable[..., object],
    ) -> object:
        assert quote_client is quote_clients[0]
        account_loader(
            config.portfolio,
            expected_date=day,
            timezone=controller.TIMEZONES["CN"],
        )
        return protection_success()

    monkeypatch.setattr(controller, "FutuQuoteClient", quote_factory)
    monkeypatch.setattr(
        controller, "FutuSimulateOrderExecutionClient", account_factory
    )
    monkeypatch.setattr(
        controller,
        "load_futu_simulate_trend_account",
        lambda **kwargs: SimpleNamespace(positions=())
        if kwargs["account_client"] is account_clients[0]
        else pytest.fail("controller did not borrow its account client"),
    )
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_record_status", lambda *_args, **kwargs: kwargs
    )
    monkeypatch.setattr(
        controller,
        "_new_order_client",
        lambda *_args: pytest.fail("idle loop opened order client"),
    )

    class StopLoop(RuntimeError):
        pass

    sleeps = 0

    def stop_after_two_loops(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise StopLoop

    with pytest.raises(StopLoop):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=stop_after_two_loops,
        )

    assert len(quote_clients) == 1
    assert len(account_clients) == 1
    assert quote_clients[0].closed is True
    assert account_clients[0].closed is True


def test_new_order_client_does_not_construct_trade_context_when_gate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    calls: list[dict[str, object]] = []

    class DeadQuote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            calls.append(kwargs)
            raise controller.FutuQuoteError("quote protocol offline")

    monkeypatch.setattr(
        controller,
        "FutuSimulateOrderExecutionClient",
        lambda **_kwargs: pytest.fail("dead gate constructed trade context"),
    )

    with pytest.raises(controller.FutuQuoteError, match="quote protocol offline"):
        controller._new_order_client(config, "CN", quote_client=DeadQuote())

    assert calls == [{
        "market": "CN",
        "start": calls[0]["end"],
        "end": calls[0]["end"],
        "use_cache": False,
    }]


def test_controller_lazy_account_does_not_construct_trade_context_when_gate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    gate_calls: list[dict[str, object]] = []

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            gate_calls.append(kwargs)
            raise controller.FutuQuoteError("quote protocol offline")

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "FutuQuoteClient", lambda **_kwargs: Quote())
    monkeypatch.setattr(
        controller,
        "FutuSimulateOrderExecutionClient",
        lambda **_kwargs: pytest.fail("dead gate constructed account context"),
    )

    def protect(
        _config: DailyPremarketConfig,
        _market: str,
        day: str,
        *,
        account_loader: Callable[..., object],
        **_kwargs: object,
    ) -> object:
        return account_loader(
            config.portfolio,
            expected_date=day,
            timezone=controller.TIMEZONES["CN"],
        )

    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller, "_derive_cycle", lambda *_args, **_kwargs: active_cn_cycle()
    )
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_record_status", lambda *_args, **kwargs: kwargs
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "blocked"
    assert gate_calls[0]["use_cache"] is False


def test_controller_rebuilds_shared_clients_after_reader_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    quote_clients: list[object] = []
    account_clients: list[object] = []

    class Quote:
        closed = False

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-17", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            self.closed = True

    class Account:
        closed = False

        def close(self) -> None:
            self.closed = True

    def quote_factory(**_kwargs: object) -> object:
        quote = Quote()
        quote_clients.append(quote)
        return quote

    def account_factory(**_kwargs: object) -> object:
        account = Account()
        account_clients.append(account)
        return account

    def protect(
        *_args: object,
        quote_client: object,
        account_loader: Callable[..., object],
        **_kwargs: object,
    ) -> object:
        if quote_client is quote_clients[0]:
            raise controller.FutuQuoteError("quote failed")
        account_loader(
            config.portfolio,
            expected_date=NOW.date().isoformat(),
            timezone=controller.TIMEZONES["CN"],
        )
        return protection_success()

    monkeypatch.setattr(controller, "FutuQuoteClient", quote_factory)
    monkeypatch.setattr(
        controller, "FutuSimulateOrderExecutionClient", account_factory
    )
    monkeypatch.setattr(
        controller,
        "load_futu_simulate_trend_account",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("account failed"))
        if kwargs["account_client"] is account_clients[0]
        else SimpleNamespace(positions=()),
    )
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_record_status", lambda *_args, **kwargs: kwargs
    )

    class StopLoop(RuntimeError):
        pass

    sleeps = 0

    def stop_after_three_loops(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise StopLoop

    with pytest.raises(StopLoop):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=stop_after_three_loops,
        )

    assert len(quote_clients) == 2
    assert all(quote.closed for quote in quote_clients)
    assert len(account_clients) == 2
    assert all(account.closed for account in account_clients)


def test_controller_rebuilds_quote_when_failed_quote_close_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    quote_clients: list[object] = []

    class Quote:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self is quote_clients[0]:
                raise RuntimeError("quote close failed")

    def quote_factory(**_kwargs: object) -> object:
        quote = Quote()
        quote_clients.append(quote)
        return quote

    def protect(
        *_args: object, quote_client: object, **_kwargs: object
    ) -> object:
        if quote_client is quote_clients[0]:
            raise controller.FutuQuoteError("quote operation failed")
        return protection_success()

    monkeypatch.setattr(controller, "FutuQuoteClient", quote_factory)
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_record_status", lambda *_args, **kwargs: kwargs
    )

    class StopLoop(RuntimeError):
        pass

    with pytest.raises(StopLoop):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=lambda _seconds: (_ for _ in ()).throw(StopLoop()),
        )

    assert len(quote_clients) == 2
    assert [quote.close_calls for quote in quote_clients] == [1, 1]


def test_controller_rebuilds_account_when_failed_account_close_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    account_clients: list[object] = []
    operation_errors: list[str] = []

    class Account:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self is account_clients[0]:
                raise RuntimeError("account close failed")

    def account_factory(**_kwargs: object) -> object:
        account = Account()
        account_clients.append(account)
        return account

    def protect(
        _config: DailyPremarketConfig,
        _market: str,
        day: str,
        *,
        account_loader: Callable[..., object],
        **_kwargs: object,
    ) -> object:
        try:
            account_loader(
                config.portfolio,
                expected_date=day,
                timezone=controller.TIMEZONES["CN"],
            )
        except Exception as exc:
            operation_errors.append(str(exc))
            raise
        return protection_success()

    monkeypatch.setattr(
        controller,
        "FutuQuoteClient",
        lambda **_kwargs: SimpleNamespace(
            get_trading_days=lambda **_query: [], close=lambda: None
        ),
    )
    monkeypatch.setattr(
        controller, "FutuSimulateOrderExecutionClient", account_factory
    )
    monkeypatch.setattr(
        controller,
        "load_futu_simulate_trend_account",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("account operation failed")
        )
        if kwargs["account_client"] is account_clients[0]
        else SimpleNamespace(positions=()),
    )
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_record_status", lambda *_args, **kwargs: kwargs
    )

    class StopLoop(RuntimeError):
        pass

    sleeps = 0

    def stop_after_two_loops(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise StopLoop

    with pytest.raises(StopLoop):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=stop_after_two_loops,
        )

    assert len(account_clients) == 2
    assert [account.close_calls for account in account_clients] == [1, 1]
    assert operation_errors == ["account operation failed"]


def test_controller_shutdown_attempts_every_cleanup_after_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    cleanup: list[str] = []

    class Quote:
        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return []

        def close(self) -> None:
            cleanup.append("quote")

    class Account:
        def close(self) -> None:
            cleanup.append("account")
            raise RuntimeError("account close failed")

    class Pool:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def shutdown(self, **_kwargs: object) -> None:
            cleanup.append("pool")

    def protect(
        _config: DailyPremarketConfig,
        _market: str,
        day: str,
        *,
        account_loader: Callable[..., object],
        **_kwargs: object,
    ) -> object:
        account_loader(
            config.portfolio,
            expected_date=day,
            timezone=controller.TIMEZONES["CN"],
        )
        return protection_success()

    monkeypatch.setattr(controller, "FutuQuoteClient", lambda **_kwargs: Quote())
    monkeypatch.setattr(
        controller,
        "FutuSimulateOrderExecutionClient",
        lambda **_kwargs: Account(),
    )
    monkeypatch.setattr(
        controller,
        "load_futu_simulate_trend_account",
        lambda **_kwargs: SimpleNamespace(positions=()),
    )
    monkeypatch.setattr(controller, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_record_status", lambda *_args, **kwargs: kwargs
    )

    with pytest.raises(RuntimeError, match="account close failed"):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=lambda _seconds: (_ for _ in ()).throw(
                RuntimeError("stop loop")
            ),
        )

    assert cleanup == ["account", "quote", "pool"]
    lock_path = config.data_dir / "runs/.trend_market_controller.CN.lock"
    with RunLock(lock_path):
        pass


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


def write_v2_controller_report(
    config: DailyPremarketConfig,
    *,
    positions: list[dict[str, object]] | None = None,
    actions: list[dict[str, object]] | None = None,
    real_rotation: bool = False,
    simulated_available: bool = True,
) -> tuple[Path, dict[str, object]]:
    report = valid_cn_report(as_of_date="2026-07-19", execution_date="2026-07-20")

    def physical_symbol(value: object) -> str:
        try:
            return to_futu_symbol("CN", str(value)).strip().upper()
        except ValueError:
            return str(value).strip().upper()

    held_symbols = {
        physical_symbol(
            position.get("futu_symbol")
            or position.get("code")
            or position.get("symbol")
            or ""
        )
        for position in positions or []
        if Decimal(
            str(position.get("quantity", position.get("qty", "0")))
        ) > 0
    }
    report["account"] = {
        **report["account"],
        "fresh": simulated_available,
        "status": "available" if simulated_available else "unavailable",
        "positions": positions or [],
        "position_count": len(held_symbols),
    }
    report["metadata"] = {**report["metadata"], "simulate_acc_id": 101}
    judgments = report["strategy_judgments"]
    assert isinstance(judgments, dict)
    formal_actions: list[dict[str, object]] = []
    for action in actions or []:
        normalized = dict(action)
        if action.get("action") == "BUY":
            normalized.update({"close": "10", "atr": "0.5"})
            if "executable" not in normalized:
                try:
                    shares = Decimal(str(normalized["estimated_shares"]))
                    lot = Decimal(str(normalized["lot_size"]))
                    atr = Decimal(str(normalized["atr"]))
                    normalized["executable"] = (
                        all(
                            value.is_finite() and value > 0
                            for value in (shares, lot, atr)
                        )
                        and shares % lot == 0
                    )
                except (ArithmeticError, KeyError, TypeError, ValueError):
                    normalized["executable"] = False
        formal_actions.append(normalized)
    judgments.update({
        "formal_actions": formal_actions,
        "holding_decisions": (
            [{"symbol": "600001"}] if positions else []
        ),
        "top10_candidates": [
            {
                "symbol": "600002",
                **({"close": "10"} if real_rotation else {}),
            }
        ],
        "simulate_rotation_pairs": [],
        "simulate_rotation_comparisons": [],
        "real_rotation_pairs": [],
        "real_rotation_comparisons": [],
    })
    candidate_symbols = {
        str(item.get("symbol") or "")
        for item in [*judgments["top10_candidates"], *formal_actions]
        if isinstance(item, dict) and item.get("symbol")
    }
    report["signal_snapshots"] = {
        "candidates": [
            {"symbol": symbol, "close": "10", "atr": "0.5"}
            for symbol in sorted(candidate_symbols)
        ]
    }
    frozen_fifo = [
        {
            "source": "formal",
            "symbol": str(action.get("symbol") or ""),
            "futu_symbol": str(
                action.get("futu_symbol")
                or f"SH.{action.get('symbol')}"
            ),
            "owners": [{
                "source": "formal",
                "action_index": action_index,
                "symbol": action.get("symbol"),
                "futu_symbol": str(
                    action.get("futu_symbol")
                    or f"SH.{action.get('symbol')}"
                ),
            }],
        }
        for action_index, action in enumerate(formal_actions)
        if isinstance(action, dict) and action.get("action") == "BUY"
        and action.get("executable") is True
    ]
    judgments["simulated_buy_fifo"] = frozen_fifo
    judgments["planned_new_seats"] = 0
    if real_rotation:
        judgments["real_holding_decisions"] = [{"symbol": "600001"}]
        judgments["real_holding_decisions_status"] = "available"
        judgments["real_holding_decisions_source"] = {}
        judgments["real_rotation_pairs"] = [{
            "pair_index": 0,
            "sell_symbol": "600001",
            "sell_name": "Weak",
            "sell_futu_symbol": "SH.600001",
            "sell_global_strength": "10",
            "buy_symbol": "600002",
            "buy_name": "Strong",
            "buy_futu_symbol": "SH.600002",
            "buy_global_strength": "90",
            "strength_gap": "80",
            "sell_asset": "A股",
            "buy_asset": "A股",
            "sell_local_strength": "10",
            "buy_local_strength": "90",
            "strength_basis": "local",
            "sell_compared_strength": "10",
            "buy_compared_strength": "90",
            "threshold": "20",
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 400,
            "lot_size": 100,
            "atr": "0.5",
            "reason": "relative_rotation",
            "execution_date": "2026-07-20",
            "execution_mode": "manual",
        }]
        judgments["real_rotation_comparisons"] = [{
            **judgments["real_rotation_pairs"][0],
            "outcome": "planned",
        }]

    roots = {
        market: {
            "stock": {
                "asset": stock,
                "tm_id": index * 10,
                "as_of_date": "2026-08-03",
                "global_strength": stock_strength,
            },
            "etf": {
                "asset": etf,
                "tm_id": index * 10 + 1,
                "as_of_date": "2026-08-03",
                "global_strength": etf_strength,
            },
        }
        for index, (market, stock, etf, stock_strength, etf_strength) in enumerate(
            (
                ("CN", "A股", "ETF基金", "90", "80"),
                ("HK", "港股", "香港ETF", "70", "60"),
                ("US", "美股", "美国ETF", "50", "40"),
            ),
            1,
        )
    }
    allocation_snapshot = build_allocation_snapshot(
        allocation_date="2026-08-03",
        generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40,
        roots=roots,
        previous=None,
        version=2,
    )
    daily_path = config.data_dir / "trend_allocation/daily/2026-08-03.json"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    allocation_body = (
        json.dumps(
            allocation_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    daily_path.write_text(allocation_body, encoding="utf-8")
    report["allocation"] = {
        "version": allocation_snapshot["version"],
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": hashlib.sha256(allocation_body.encode()).hexdigest(),
        "allocation_date": "2026-08-03",
        "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
        "roots": allocation_snapshot["roots"],
        "markets": allocation_snapshot["markets"],
    }
    del report["allocation"]["version"]
    if frozen_fifo:
        full_exit_symbols = {
            physical_symbol(
                action.get("futu_symbol")
                or f"SH.{action.get('symbol')}"
            )
            for action in actions or []
            if isinstance(action, dict) and action.get("action") == "SELL_ALL"
        }
        position_limit = allocation_snapshot["markets"]["CN"]["position_limit"]
        judgments["planned_new_seats"] = max(
            0,
            position_limit - len(held_symbols - full_exit_symbols),
        )
    report["strategy_snapshot"] = a_share_trend.live_trend_strategy_snapshot(
        "CN",
        "test-sha",
        (622466, 697199),
        allocation={
            "daily_path": report["allocation"]["daily_path"],
            "sha256": report["allocation"]["sha256"],
            "snapshot": allocation_snapshot,
        },
    )
    report["plan_availability"] = {
        "simulated_account": {
            "status": "available" if simulated_available else "unavailable",
            "reason": "" if simulated_available else "simulation account offline",
            "executable": simulated_available,
        },
        "real_account": {
            "status": "available" if real_rotation else "unavailable",
            "reason": "" if real_rotation else "real account is informational",
            "executable": False,
            **(
                {"net_value": "100000", "available_cash": "100000"}
                if real_rotation
                else {}
            ),
        },
    }
    path = config.reports_dir / "trend_a_share/2026-07-19.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, report


def test_execution_completed_rejects_unbound_request_completion(
    tmp_path: Path,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    _report_path, report = write_v2_controller_report(config)
    _report_path.write_bytes(controller._canonical_json_bytes(report))
    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        report["execution_date"],
        _report_hash(report),
        actor="test",
        reason="completion binding",
        now=NOW,
    )
    execution_id = str(result["execution_id"])
    cycle = active_cn_cycle()
    completion_path = controller._request_completion_path(
        config, cycle.market, cycle.execution_date, execution_id
    )
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    wrong_path = str(tmp_path / "unbound-request.json")
    wrong_sha = "0" * 64
    completion["request_path"] = wrong_path
    completion["request"]["report_sha256"] = wrong_sha
    completion["result"]["request_path"] = wrong_path
    completion["result"]["report_sha256"] = wrong_sha
    completion_path.write_text(json.dumps(completion), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid simulation request completion"):
        controller._execution_completed(
            config,
            cycle,
            execution_id=execution_id,
        )


def test_current_cycle_ignores_unfinished_legacy_execution_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    class Quote:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-19", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "FutuQuoteClient", Quote)
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args, **_kwargs: protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args, **_kwargs: {"status": "completed"},
    )

    def capture_close(
        config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
        projection = config.data_dir / f"latest/trend_review_{market.lower()}.json"
        projection.parent.mkdir(parents=True, exist_ok=True)
        projection.write_text(
            json.dumps({"schema_version": "open_trader.trend_review.projection.v5"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(controller, "_capture_close", capture_close)
    current_path, current_report = write_v2_controller_report(config)

    legacy_path = config.reports_dir / "trend_a_share/2026-07-18.json"
    legacy_report = valid_cn_report(
        as_of_date="2026-07-18", execution_date="2026-07-19", buy=True
    )
    legacy_report["metadata"]["simulate_acc_id"] = 101  # type: ignore[index]
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps(legacy_report), encoding="utf-8")
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date="2026-07-19",
        report_path=legacy_path,
        report=legacy_report,
        locked_at=NOW.isoformat(),
    )
    legacy_batch_path = (
        config.data_dir
        / "trend_review/ledgers/CN/batches/2026-07-19.json"
    )
    legacy_batch_bytes = legacy_batch_path.read_bytes()
    current_report_bytes = (
        json.dumps(
            current_report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    expected_sha = hashlib.sha256(current_report_bytes).hexdigest()

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    last_success = result["last_success"]
    assert isinstance(last_success, dict)
    assert (
        last_success["date"],
        last_success["report_sha256"],
    ) == ("2026-07-20", expected_sha)
    current_batch_path = (
        config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    )
    request_batches = list(
        (
            config.data_dir
            / "trend_review/ledgers/CN/batches/requests/2026-07-20"
        ).glob("*.json")
    )
    assert not current_batch_path.exists()
    assert len(request_batches) == 1
    request_batch = json.loads(request_batches[0].read_text(encoding="utf-8"))
    assert (
        request_batch["report_sha256"],
        request_batch["report_path"],
    ) == (expected_sha, str(current_path))
    assert legacy_batch_path.read_bytes() == legacy_batch_bytes


def test_current_cycle_failure_never_falls_back_to_legacy_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    buy = {
        "action": "BUY",
        "symbol": "600001",
        "futu_symbol": "SH.600001",
        "target_weight": "0.04",
        "lot_size": 100,
        "estimated_shares": 100,
        "target_amount": "1000",
        "atr": "0.5",
    }
    _current_path, current_report = write_v2_controller_report(
        config, actions=[buy]
    )

    legacy_path = config.reports_dir / "trend_a_share/2026-07-18.json"
    legacy_report = valid_cn_report(
        as_of_date="2026-07-18", execution_date="2026-07-19", buy=True
    )
    legacy_report["metadata"]["simulate_acc_id"] = 101  # type: ignore[index]
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps(legacy_report), encoding="utf-8")
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date="2026-07-19",
        report_path=legacy_path,
        report=legacy_report,
        locked_at=NOW.isoformat(),
    )
    legacy_batch_path = (
        config.data_dir
        / "trend_review/ledgers/CN/batches/2026-07-19.json"
    )
    legacy_batch_bytes = legacy_batch_path.read_bytes()
    current_report_bytes = (
        json.dumps(
            current_report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    expected_sha = hashlib.sha256(current_report_bytes).hexdigest()

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Quote:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-19", "2026-07-20", "2026-07-21"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Account:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "available_cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            return {
                "futu_order_id": "REJECTED",
                "status": "REJECTED",
                "order_status": "REJECTED",
                "dealt_qty": "0",
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "FutuQuoteClient", Quote)
    monkeypatch.setattr(controller, "FutuSimulateOrderExecutionClient", Account)
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args, **_kwargs: protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args, **_kwargs: {"status": "completed"},
    )

    def capture_close(
        config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
        projection = (
            config.data_dir
            / f"latest/trend_review_{market.lower()}.json"
        )
        projection.parent.mkdir(parents=True, exist_ok=True)
        projection.write_text(
            json.dumps(
                {"schema_version": "open_trader.trend_review.projection.v5"}
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(controller, "_capture_close", capture_close)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "terminal_rejected"
    assert result["blocker"] == "terminal_rejected"
    last_success = result["last_success"]
    assert isinstance(last_success, dict)
    assert (
        last_success["date"],
        last_success["report_sha256"],
    ) == ("2026-07-20", expected_sha)
    current_action_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (
            config.data_dir / "trend_review/ledgers/CN/actions/2026-07-20"
        ).glob("*/*.json")
    ]
    assert any(
        event.get("reason") == "broker_order_no_progress"
        for event in current_action_events
    )
    assert not (
        config.data_dir / "trend_review/ledgers/CN/actions/2026-07-19"
    ).exists()
    assert legacy_batch_path.read_bytes() == legacy_batch_bytes


def partial_sell_action(symbol: str = "600001") -> dict[str, object]:
    return {
        "action": "SELL_PARTIAL",
        "symbol": symbol,
        "reason": "overheat_take_profit",
        "target_fraction": "0.30",
        "lot_size": 100,
        "estimated_shares": 300,
        "position_started_for": "2026-07-01",
        "overheat_signals": ["boiling"],
    }


def test_v2_allow_new_buys_false_suppresses_formal_buy_but_keeps_sell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    sell = {"action": "SELL_ALL", "symbol": "600001"}
    buy = {
        "action": "BUY",
        "symbol": "600002",
        "futu_symbol": "SH.600002",
        "target_weight": "0.04",
        "lot_size": 100,
        "estimated_shares": 100,
        "target_amount": "1000",
        "atr": "0.5",
    }
    report_path, report = write_v2_controller_report(
        config,
        positions=[{
            "symbol": "600001",
            "name": "Weak",
            "asset_class": "stock",
            "quantity": "100",
            "market_value": "1000",
            "avg_cost_price": "10",
        }],
        actions=[sell, buy],
    )
    report["execution_date"] = NOW.date().isoformat()
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class OrderClient:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "positions": [
                    {"code": "SH.600001", "qty": "100", "can_sell_qty": "100"}
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            order = {
                **request,
                "order_id": f"SIM-{len(self.requests)}",
                "code": request["futu_code"],
                "trd_side": request["side"],
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }
            self.orders.append(order)
            return {
                "futu_order_id": order["order_id"],
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }

    client = OrderClient()
    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        NOW.date().isoformat(),
        _report_hash(report),
        actor="test",
        reason="protection failure",
        now=NOW,
        order_client=client,
        allow_new_buys=False,
    )
    assert (
        result["submitted_count"],
        [(request["side"], request["futu_code"], request["qty"])
         for request in client.requests],
    ) == (1, [("sell", "SH.600001", "100")])
    assert report_path.exists()


def test_controller_v2_requires_plan_availability(tmp_path: Path) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    report_path, report = write_v2_controller_report(config)
    report.pop("plan_availability")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class NeverOrderClient:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("invalid v2 report reached the order client")

    with pytest.raises(ValueError, match="invalid frozen trend report"):
        controller.execute_simulated_trend_report(
            config,
            "CN",
            report["execution_date"],
            _report_hash(report),
            actor="test",
            reason="missing plan availability",
            now=NOW,
            order_client=NeverOrderClient(),
        )


def test_controller_simulated_unavailable_ignores_real_rotation_plan(
    tmp_path: Path,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    report_path, report = write_v2_controller_report(
        config,
        real_rotation=True,
        simulated_available=False,
    )
    report["account_input"] = {
        "snapshot_generation": "sha256:" + "0" * 64,
        "account_generation": "sha256:" + "1" * 64,
        "status": "healthy",
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class NeverOrderClient:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("unavailable simulated plan reached the order client")

    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        report["execution_date"],
        _report_hash(report),
        actor="test",
        reason="unavailable simulated account",
        now=NOW,
        order_client=NeverOrderClient(),
    )

    assert (
        result["status"],
        len(report["strategy_judgments"]["real_rotation_pairs"]),  # type: ignore[index]
    ) == ("unchanged", 1)


def test_controller_v2_executable_report_requires_frozen_buy_plan(
    tmp_path: Path,
) -> None:
    invalid_reports = [
        lambda judgments: (judgments.pop("simulated_buy_fifo"), judgments.pop("planned_new_seats")),
        lambda judgments: judgments.pop("simulated_buy_fifo"),
        lambda judgments: judgments.pop("planned_new_seats"),
        lambda judgments: judgments.update({"simulated_buy_fifo": ["SH.600001"]}),
        lambda judgments: judgments.update({"planned_new_seats": True}),
    ]

    class NeverOrderClient:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("invalid v2 report reached the order client")

    outcomes: list[str] = []
    for index, mutate in enumerate(invalid_reports):
        config = replace(
            controller_config(tmp_path / f"invalid-{index}"),
            trend_review_cn_simulate_acc_id=101,
            trend_executor_host=socket.gethostname(),
        )
        report_path, report = write_v2_controller_report(config)
        judgments = report["strategy_judgments"]
        assert isinstance(judgments, dict)
        mutate(judgments)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        try:
            controller.execute_simulated_trend_report(
                config,
                "CN",
                report["execution_date"],
                _report_hash(report),
                actor="test",
                reason="invalid frozen buy plan",
                now=NOW,
                order_client=NeverOrderClient(),
            )
        except ValueError:
            outcomes.append("ValueError")
        else:
            outcomes.append("accepted")

    valid_config = replace(
        controller_config(tmp_path / "valid"),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    _, valid_report = write_v2_controller_report(valid_config)
    valid = controller.execute_simulated_trend_report(
        valid_config,
        "CN",
        valid_report["execution_date"],
        _report_hash(valid_report),
        actor="test",
        reason="empty frozen buy plan",
        now=NOW,
        order_client=NeverOrderClient(),
    )

    assert (outcomes, valid["status"], valid["submitted_count"]) == (
        ["ValueError"] * 5,
        "unchanged",
        0,
    )


def test_controller_v2_rejects_frozen_fifo_entry_without_owner_identity(
    tmp_path: Path,
) -> None:
    buy = {
        "action": "BUY",
        "symbol": "600001",
        "futu_symbol": "SH.600001",
        "target_weight": "0.04",
        "lot_size": 100,
        "estimated_shares": 100,
        "target_amount": "1000",
        "atr": "0.5",
    }
    valid_owner = {
        "source": "formal",
        "action_index": 0,
        "symbol": "600001",
        "futu_symbol": "SH.600001",
    }
    invalid_fifos = [
        [{}],
        [{"source": "formal", "symbol": "600001", "futu_symbol": "SH.600001"}],
        [{"source": "formal", "symbol": "600999", "futu_symbol": "SH.600999"}],
        [{
            "source": "formal",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "owners": [{**valid_owner, "symbol": "600999"}],
        }],
        [{
            "source": "formal",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "owners": [{key: value for key, value in valid_owner.items() if key != "futu_symbol"}],
        }],
        [{
            "source": "formal",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "owners": [{**valid_owner, "futu_symbol": "SH.600999"}],
        }],
        [],
    ]

    class Boundary:
        def __init__(self) -> None:
            self.snapshot_calls = 0
            self.quote_calls = 0
            self.order_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            return {
                "acc_id": 101,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            self.order_calls += 1
            return {"futu_order_id": "UNEXPECTED", "status": "SUBMITTED"}

    outcomes: list[str] = []
    for index, fifo in enumerate(invalid_fifos):
        config = replace(
            controller_config(tmp_path / f"invalid-{index}"),
            trend_review_cn_simulate_acc_id=101,
            trend_executor_host=socket.gethostname(),
        )
        report_path, report = write_v2_controller_report(config, actions=[buy])
        judgments = report["strategy_judgments"]
        assert isinstance(judgments, dict)
        judgments["simulated_buy_fifo"] = fifo
        report_path.write_text(json.dumps(report), encoding="utf-8")
        boundary = Boundary()
        quote = SimpleNamespace(
            get_snapshots=lambda _symbols: setattr(boundary, "quote_calls", boundary.quote_calls + 1)
            or {},
        )
        try:
            controller.execute_simulated_trend_report(
                config,
                "CN",
                report["execution_date"],
                _report_hash(report),
                actor="test",
                reason="invalid frozen FIFO owner",
                now=NOW,
                quote_client=quote,
                order_client=boundary,
            )
        except ValueError:
            outcomes.append("rejected")
        else:
            outcomes.append("accepted")
        assert boundary.quote_calls == 0
        assert boundary.order_calls == 0

    valid_config = replace(
        controller_config(tmp_path / "valid-empty"),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    _, valid_report = write_v2_controller_report(valid_config)
    valid = controller.execute_simulated_trend_report(
        valid_config,
        "CN",
        valid_report["execution_date"],
        _report_hash(valid_report),
        actor="test",
        reason="truthful empty frozen FIFO",
        now=NOW,
        order_client=Boundary(),
    )

    assert (outcomes, valid["status"], valid["submitted_count"]) == (
        ["rejected"] * len(invalid_fifos),
        "unchanged",
        0,
    )


def test_controller_v2_rejects_reordered_frozen_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [
        {
            "symbol": f"600{index:03d}",
            "name": f"Holding {index}",
            "asset_class": "stock",
            "quantity": "100",
            "market_value": "1000",
            "avg_cost_price": "10",
        }
        for index in range(100, 119)
    ]
    actions = [
        {
            "action": "BUY",
            "symbol": symbol,
            "futu_symbol": f"SH.{symbol}",
            "target_weight": "0.04",
            "global_strength": strength,
            "lot_size": 100,
            "estimated_shares": 100,
            "target_amount": "1000",
            "atr": "0.5",
        }
        for symbol, strength in (("600001", "95"), ("600002", "90"))
    ]
    valid_config = replace(
        controller_config(tmp_path / "valid"),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    _, valid_report = write_v2_controller_report(
        valid_config, positions=positions, actions=actions
    )

    class Boundary:
        def __init__(self) -> None:
            self.snapshot_calls = 0
            self.order_calls = 0
            self.requests: list[dict[str, object]] = []
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            return {
                "acc_id": 101,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": [
                    {"code": f"SH.600{index:03d}", "qty": "100"}
                    for index in range(100, 119)
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.order_calls += 1
            self.requests.append(dict(request))
            self.orders.append({
                **request,
                "order_id": f"SIM-{self.order_calls}",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            })
            return {
                "futu_order_id": f"SIM-{self.order_calls}",
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }

    class Quote:
        def __init__(self) -> None:
            self.calls = 0

        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            self.calls += 1
            return {
                symbol: SimpleNamespace(last_price=10)
                for symbol in symbols
            }

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    valid_boundary = Boundary()
    valid_quote = Quote()
    valid = controller.execute_simulated_trend_report(
        valid_config,
        "CN",
        valid_report["execution_date"],  # type: ignore[arg-type]
        _report_hash(valid_report),
        actor="test",
        reason="valid frozen FIFO",
        now=NOW,
        quote_client=valid_quote,
        order_client=valid_boundary,
    )

    assert (
        valid_report["strategy_judgments"]["planned_new_seats"],  # type: ignore[index]
        valid["submitted_count"],
        [request["futu_code"] for request in valid_boundary.requests],
    ) == (1, 1, ["SH.600001"])

    reordered_config = replace(
        controller_config(tmp_path / "reordered"),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    report_path, report = write_v2_controller_report(
        reordered_config, positions=positions, actions=actions
    )
    judgments = report["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["simulated_buy_fifo"] = list(reversed(judgments["simulated_buy_fifo"]))
    report_path.write_text(json.dumps(report), encoding="utf-8")

    boundary = Boundary()
    quote = Quote()
    with pytest.raises(ValueError, match="invalid frozen trend report"):
        controller.execute_simulated_trend_report(
            reordered_config,
            "CN",
            report["execution_date"],  # type: ignore[arg-type]
            _report_hash(report),
            actor="test",
            reason="reordered frozen FIFO",
            now=NOW,
            quote_client=quote,
            order_client=boundary,
        )

    assert (quote.calls, boundary.order_calls) == (0, 0)


def test_controller_v2_skips_data_missing_buy_and_executes_later_fifo_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [
        {
            "symbol": f"600{index:03d}",
            "name": f"Holding {index}",
            "asset_class": "stock",
            "quantity": "100",
            "market_value": "1000",
            "avg_cost_price": "10",
        }
        for index in range(100, 119)
    ]
    actions = [
        {
            "action": "BUY",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "target_weight": "0.04",
            "global_strength": "95",
            "lot_size": 0,
            "estimated_shares": 0,
            "target_amount": "0",
            "atr": "0",
            "sizing_note": "每手股数未知，无法定量",
        },
        {
            "action": "BUY",
            "symbol": "600002",
            "futu_symbol": "SH.600002",
            "target_weight": "0.04",
            "global_strength": "90",
            "lot_size": 100,
            "estimated_shares": 100,
            "target_amount": "1000",
            "atr": "0.5",
        },
    ]
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    report_path, report = write_v2_controller_report(
        config, positions=positions, actions=actions
    )
    judgments = report["strategy_judgments"]
    assert isinstance(judgments, dict)
    assert judgments["planned_new_seats"] == 1
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Boundary:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": [
                    {"code": f"SH.600{index:03d}", "qty": "100"}
                    for index in range(100, 119)
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            order_id = f"SIM-{len(self.requests)}"
            self.orders.append({
                **request,
                "order_id": order_id,
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            })
            return {
                "futu_order_id": order_id,
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    boundary = Boundary()
    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        report["execution_date"],  # type: ignore[arg-type]
        _report_hash(report),
        actor="test",
        reason="skip data-missing FIFO candidate",
        now=NOW,
        quote_client=Quote(),
        order_client=boundary,
    )
    action_root = (
        config.data_dir / "trend_review" / "ledgers" / "CN" / "actions"
        / report["execution_date"]  # type: ignore[index]
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in action_root.rglob("*.json")
    ] if action_root.exists() else []

    assert (
        result["submitted_count"],
        [request["futu_code"] for request in boundary.requests],
        [event for event in events if event.get("futu_code") == "SH.600001"],
    ) == (1, ["SH.600002"], [])


def test_controller_v2_rejects_frozen_seat_count_above_report_derived_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def report_position(index: int) -> dict[str, object]:
        symbol = f"600{index:03d}"
        return {
            "symbol": symbol,
            "name": symbol,
            "asset_class": "stock",
            "quantity": "100",
            "market_value": "1000",
            "avg_cost_price": "10",
        }

    def buy_action(symbol: str) -> dict[str, object]:
        return {
            "action": "BUY",
            "symbol": symbol,
            "futu_symbol": f"SH.{symbol}",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 100,
            "target_amount": "1000",
            "atr": "0.5",
        }

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Boundary:
        def __init__(self, positions: list[dict[str, object]]) -> None:
            self.positions = positions
            self.quote_calls = 0
            self.order_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": self.positions,
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            self.order_calls += 1
            raise AssertionError("seat-budget validation reached order submission")

    class Quote:
        def __init__(self, boundary: Boundary) -> None:
            self.boundary = boundary

        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            self.boundary.quote_calls += 1
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    tampered_config = replace(
        controller_config(tmp_path / "tampered"),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    tampered_positions = [report_position(index) for index in range(100, 119)]
    tampered_path, tampered_report = write_v2_controller_report(
        tampered_config,
        positions=tampered_positions,
        actions=[buy_action("600001"), buy_action("600002")],
    )
    tampered_report["account"]["position_count"] = 19  # type: ignore[index]
    tampered_report["strategy_judgments"]["planned_new_seats"] = 2  # type: ignore[index]
    tampered_path.write_text(json.dumps(tampered_report), encoding="utf-8")
    tampered_boundary = Boundary([
        {
            "code": f"SH.{position['symbol']}",
            "qty": position["quantity"],
            "can_sell_qty": position["quantity"],
        }
        for position in tampered_positions
    ])
    tampered_quote = Quote(tampered_boundary)

    with pytest.raises(ValueError, match="invalid frozen trend report"):
        controller.execute_simulated_trend_report(
            tampered_config,
            "CN",
            tampered_report["execution_date"],  # type: ignore[arg-type]
            _report_hash(tampered_report),
            actor="test",
            reason="tampered frozen seat budget",
            now=NOW,
            quote_client=tampered_quote,
            order_client=tampered_boundary,
        )

    assert (tampered_boundary.quote_calls, tampered_boundary.order_calls) == (0, 0)

    truthful_config = replace(
        controller_config(tmp_path / "truthful"),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    truthful_positions = [report_position(index) for index in range(100, 120)]
    truthful_path, truthful_report = write_v2_controller_report(
        truthful_config,
        positions=truthful_positions,
        actions=[buy_action("600001")],
    )
    truthful_report["account"]["position_count"] = 20  # type: ignore[index]
    truthful_report["strategy_judgments"]["planned_new_seats"] = 0  # type: ignore[index]
    truthful_path.write_text(json.dumps(truthful_report), encoding="utf-8")
    truthful_boundary = Boundary([
        {
            "code": f"SH.{position['symbol']}",
            "qty": position["quantity"],
            "can_sell_qty": position["quantity"],
        }
        for position in truthful_positions
    ])
    truthful_quote = Quote(truthful_boundary)
    truthful = controller.execute_simulated_trend_report(
        truthful_config,
        "CN",
        truthful_report["execution_date"],  # type: ignore[arg-type]
        _report_hash(truthful_report),
        actor="test",
        reason="truthful frozen seat budget at cap",
        now=NOW,
        quote_client=truthful_quote,
        order_client=truthful_boundary,
    )

    assert (truthful["status"], truthful["submitted_count"], truthful_boundary.order_calls) == (
        "unchanged",
        0,
        0,
    )


def test_controller_v2_rejects_frozen_position_count_inconsistent_with_positive_holdings(
    tmp_path: Path,
) -> None:
    positions = [
        {
            "symbol": f"600{index:03d}",
            "name": f"Holding {index}",
            "asset_class": "stock",
            "quantity": "100",
            "market_value": "1000",
            "avg_cost_price": "10",
        }
        for index in range(100, 119)
    ]
    actions = [
        {
            "action": "BUY",
            "symbol": symbol,
            "futu_symbol": f"SH.{symbol}",
            "target_weight": "0.04",
            "global_strength": strength,
            "lot_size": 100,
            "estimated_shares": 100,
            "target_amount": "1000",
            "atr": "0.5",
        }
        for symbol, strength in (("600001", "90"), ("600002", "80"))
    ]

    class Boundary:
        def __init__(self) -> None:
            self.snapshot_calls = 0
            self.order_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            raise AssertionError("inconsistent report reached account snapshot")

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("inconsistent report reached order history")

        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            self.order_calls += 1
            raise AssertionError("inconsistent report reached order submission")

    class Quote:
        def __init__(self) -> None:
            self.calls = 0

        def get_snapshots(self, _symbols: list[str]) -> dict[str, object]:
            self.calls += 1
            raise AssertionError("inconsistent report reached quote lookup")

    outcomes: list[tuple[int, int, int, int]] = []
    for index, (position_count, planned_new_seats) in enumerate(((21, 2), (18, 1))):
        config = replace(
            controller_config(tmp_path / f"inconsistent-{index}"),
            trend_review_cn_simulate_acc_id=101,
            trend_executor_host=socket.gethostname(),
        )
        path, report = write_v2_controller_report(
            config,
            positions=positions,
            actions=actions,
        )
        report["account"]["position_count"] = position_count  # type: ignore[index]
        report["strategy_judgments"]["planned_new_seats"] = planned_new_seats  # type: ignore[index]
        path.write_text(json.dumps(report), encoding="utf-8")
        boundary = Boundary()
        quote = Quote()

        with pytest.raises(ValueError, match="invalid frozen trend report"):
            controller.execute_simulated_trend_report(
                config,
                "CN",
                report["execution_date"],  # type: ignore[arg-type]
                _report_hash(report),
                actor="test",
                reason="inconsistent frozen position count",
                now=NOW,
                quote_client=quote,
                order_client=boundary,
            )

        outcomes.append((boundary.snapshot_calls, quote.calls, boundary.order_calls, position_count))

    assert outcomes == [(0, 0, 0, 21), (0, 0, 0, 18)]


def test_controller_stamps_production_snapshot_observation_for_cross_execution_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    _report_path, report = write_v2_controller_report(
        config,
        actions=[{
            "action": "BUY",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "target_weight": "0.04",
            "global_strength": "90",
            "lot_size": 100,
            "estimated_shares": 100,
            "target_amount": "1000",
            "atr": "0.5",
        }],
    )

    class FixedDateTime(datetime):
        current = NOW

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return cls.current if tz is None else cls.current.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class ProductionOrderClient:
        def __init__(self) -> None:
            self.positions: list[dict[str, object]] = []
            self.orders: list[dict[str, object]] = []
            self.requests: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "positions": [dict(position) for position in self.positions],
                "updated_time": "2099-01-01T00:00:00+08:00",
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": [dict(order) for order in self.orders]}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            order = {
                **request,
                "order_id": f"SIM-{len(self.requests)}",
                "code": request["futu_code"],
                "trd_side": "BUY",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
                "updated_time": "2099-01-01T00:00:00+08:00",
            }
            self.orders.append(order)
            self.positions = [{"code": request["futu_code"], "qty": request["qty"]}]
            return {
                "futu_order_id": order["order_id"],
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    client = ProductionOrderClient()
    quote = Quote()
    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        report["execution_date"],  # type: ignore[arg-type]
        _report_hash(report),
        actor="first-owner",
        reason="initial explicit buy",
        now=NOW,
        quote_client=quote,
        order_client=client,
    )

    FixedDateTime.current = NOW + timedelta(minutes=1)
    second = controller.execute_simulated_trend_report(
        config,
        "CN",
        report["execution_date"],  # type: ignore[arg-type]
        _report_hash(report),
        actor="second-owner",
        reason="reconcile explicit buy",
        now=FixedDateTime.current,
        quote_client=quote,
        order_client=client,
    )

    action_root = config.data_dir / "trend_review/ledgers/CN/actions/2026-07-20"
    reasons = {
        json.loads(path.read_text(encoding="utf-8")).get("reason")
        for path in action_root.glob("*/*.json")
    }
    assert (
        first["submitted_count"],
        second["status"],
        second["submitted_count"],
        len(client.requests),
        [
            (request["side"], request["futu_code"], request["qty"])
            for request in client.requests
        ],
        "holdings_snapshot_not_newer" not in reasons,
        "terminal_fill_not_reconciled" not in reasons,
    ) == (1, "unchanged", 0, 1, [("buy", "SH.600001", "100")], True, True)


def test_valid_report_accepts_only_strict_partial_sell_actions(tmp_path: Path) -> None:
    config = controller_config(tmp_path)
    path, report = write_report(config)
    actions = report["strategy_judgments"]["formal_actions"]  # type: ignore[index]
    actions.append(partial_sell_action())

    assert controller._valid_report(config, "CN", "2026-07-20", path, report)

    for key, value in (
        ("target_fraction", "0.29"),
        ("lot_size", "100.0"),
        ("estimated_shares", 250),
        ("position_started_for", "2026-7-01"),
        ("overheat_signals", ["unknown"]),
    ):
        invalid = json.loads(json.dumps(report))
        invalid["strategy_judgments"]["formal_actions"][-1][key] = value
        assert not controller._valid_report(
            config, "CN", "2026-07-20", path, invalid
        )
    conflicting = json.loads(json.dumps(report))
    conflicting["strategy_judgments"]["formal_actions"].append({
        "action": "SELL_ALL", "symbol": "600001"
    })
    assert not controller._valid_report(
        config, "CN", "2026-07-20", path, conflicting
    )


def test_controller_loads_non_executable_unavailable_simulated_account_report(
    tmp_path: Path,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
    )
    report = valid_cn_report(
        as_of_date="2026-07-19",
        execution_date="2026-07-20",
    )
    report["account"] = {
        **report["account"],
        "fresh": False,
        "status": "unavailable",
        "reason": "simulation account offline",
    }
    report["metadata"] = {
        **report["metadata"],
        "simulate_acc_id": 123,
    }
    report["plan_availability"] = {
        "simulated_account": {
            "status": "unavailable",
            "reason": "simulation account offline",
            "executable": False,
        },
        "real_account": {
            "status": "unavailable",
            "reason": "real account is informational",
            "executable": False,
        },
    }
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class NeverOrderClient:
        def place_order(self, _request: dict[str, object]) -> dict[str, object]:
            raise AssertionError("unavailable simulated account must not order")

    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        _report_hash(report),
        actor="test",
        reason="unavailable report load",
        now=NOW,
        order_client=NeverOrderClient(),
    )

    assert result["status"] == "unchanged"


@pytest.mark.parametrize("variant", ("missing_executable", "executable"))
def test_controller_rejects_malformed_unavailable_simulated_account_report(
    tmp_path: Path,
    variant: str,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
    )
    report = valid_cn_report(
        as_of_date="2026-07-19",
        execution_date="2026-07-20",
    )
    report["account"] = {
        **report["account"],
        "fresh": False,
        "status": "unavailable",
        "reason": "simulation account offline",
    }
    report["metadata"] = {
        **report["metadata"],
        "simulate_acc_id": 123,
    }
    simulated_plan: dict[str, object] = {
        "status": "unavailable",
        "reason": "simulation account offline",
        "executable": False,
    }
    if variant == "missing_executable":
        del simulated_plan["executable"]
    else:
        simulated_plan["executable"] = True
    report["plan_availability"] = {
        "simulated_account": simulated_plan,
        "real_account": {
            "status": "unavailable",
            "reason": "real account is informational",
            "executable": False,
        },
    }
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid frozen trend report"):
        controller.execute_simulated_trend_report(
            config,
            "CN",
            "2026-07-20",
            _report_hash(report),
            actor="test",
            reason="malformed unavailable report",
            now=NOW,
            order_client=object(),
        )


def test_controller_rejects_fresh_account_with_unavailable_formal_plan(
    tmp_path: Path,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
    )
    report = valid_cn_report(
        as_of_date="2026-07-19",
        execution_date="2026-07-20",
        buy=True,
    )
    report["metadata"] = {**report["metadata"], "simulate_acc_id": 123}
    report["plan_availability"] = {
        "simulated_account": {
            "status": "unavailable",
            "reason": "simulation account offline",
            "executable": False,
        },
        "real_account": {
            "status": "unavailable",
            "reason": "real account is informational",
            "executable": False,
        },
    }
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class NeverOrderClient:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("contradictory report used the order client")

    with pytest.raises(ValueError, match="invalid frozen trend report"):
        controller.execute_simulated_trend_report(
            config,
            "CN",
            "2026-07-20",
            _report_hash(report),
            actor="test",
            reason="contradictory formal plan",
            now=NOW,
            order_client=NeverOrderClient(),
        )


def test_controller_rejects_fresh_account_with_unavailable_rotation_plan(
    tmp_path: Path,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
    )
    report = valid_cn_report(as_of_date="2026-07-19", execution_date="2026-07-20")
    report["metadata"] = {**report["metadata"], "simulate_acc_id": 123}
    roots = {
        market: {
            "stock": {
                "asset": stock,
                "tm_id": index * 10,
                "as_of_date": "2026-08-03",
                "global_strength": stock_strength,
            },
            "etf": {
                "asset": etf,
                "tm_id": index * 10 + 1,
                "as_of_date": "2026-08-03",
                "global_strength": etf_strength,
            },
        }
        for index, (market, stock, etf, stock_strength, etf_strength) in enumerate(
            (
                ("CN", "A股", "ETF基金", "90", "80"),
                ("HK", "港股", "香港ETF", "70", "60"),
                ("US", "美股", "美国ETF", "50", "40"),
            ),
            1,
        )
    }
    allocation_snapshot = build_allocation_snapshot(
        allocation_date="2026-08-03",
        generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40,
        roots=roots,
        previous=None,
        version=2,
    )
    daily_path = config.data_dir / "trend_allocation/daily/2026-08-03.json"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    allocation_body = (
        json.dumps(
            allocation_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    daily_path.write_text(allocation_body, encoding="utf-8")
    report["allocation"] = {
        "version": allocation_snapshot["version"],
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": hashlib.sha256(allocation_body.encode()).hexdigest(),
        "allocation_date": "2026-08-03",
        "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
        "roots": allocation_snapshot["roots"],
        "markets": allocation_snapshot["markets"],
    }
    report["strategy_snapshot"] = a_share_trend.live_trend_strategy_snapshot(
        "CN",
        "test-sha",
        (622466, 697199),
        allocation={
            "daily_path": report["allocation"]["daily_path"],  # type: ignore[index]
            "sha256": report["allocation"]["sha256"],  # type: ignore[index]
            "snapshot": allocation_snapshot,
        },
    )
    judgments = report["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["holding_decisions"] = [{"symbol": "WEAK"}]
    judgments["top10_candidates"] = [{"symbol": "STRONG"}]
    judgments["simulate_rotation_pairs"] = [{
        "pair_index": 0,
        "sell_symbol": "WEAK",
        "sell_name": "Weak",
        "sell_futu_symbol": "SH.WEAK",
        "sell_global_strength": "10",
        "buy_symbol": "STRONG",
        "buy_name": "Strong",
        "buy_futu_symbol": "SH.STRONG",
        "buy_global_strength": "90",
        "strength_gap": "80",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 400,
        "lot_size": 100,
        "atr": "0.5",
        "reason": "relative_rotation",
        "execution_date": "2026-07-20",
        "execution_mode": "automatic",
        "sell_asset": "A股",
        "buy_asset": "A股",
        "sell_local_strength": "10",
        "buy_local_strength": "90",
        "strength_basis": "local",
        "sell_compared_strength": "10",
        "buy_compared_strength": "90",
        "threshold": "20",
    }]
    judgments["simulate_rotation_comparisons"] = [{
        "pair_index": 0,
        "sell_symbol": "WEAK",
        "sell_name": "Weak",
        "sell_asset": "A股",
        "sell_local_strength": "10",
        "sell_global_strength": "10",
        "buy_symbol": "STRONG",
        "buy_name": "Strong",
        "buy_asset": "A股",
        "buy_local_strength": "90",
        "buy_global_strength": "90",
        "strength_basis": "local",
        "sell_compared_strength": "10",
        "buy_compared_strength": "90",
        "strength_gap": "80",
        "threshold": "20",
        "outcome": "planned",
        "reason": "relative_rotation",
    }]
    judgments["real_rotation_pairs"] = []
    judgments["real_rotation_comparisons"] = []
    report["plan_availability"] = {
        "simulated_account": {
            "status": "unavailable",
            "reason": "simulation account offline",
            "executable": False,
        },
        "real_account": {
            "status": "unavailable",
            "reason": "real account is informational",
            "executable": False,
        },
    }
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class NeverOrderClient:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("contradictory report used the order client")

    with pytest.raises(ValueError, match="invalid frozen trend report"):
        controller.execute_simulated_trend_report(
            config,
            "CN",
            "2026-07-20",
            _report_hash(report),
            actor="test",
            reason="contradictory rotation plan",
            now=NOW,
            order_client=NeverOrderClient(),
        )


def test_controller_requires_fresh_account_for_available_simulated_plan(
    tmp_path: Path,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
    )
    report = valid_cn_report(as_of_date="2026-07-19", execution_date="2026-07-20")
    report["account"] = {
        **report["account"],
        "fresh": False,
        "status": "available",
    }
    report["metadata"] = {**report["metadata"], "simulate_acc_id": 123}
    report["plan_availability"] = {
        "simulated_account": {
            "status": "available",
            "reason": "",
            "executable": True,
        },
        "real_account": {
            "status": "unavailable",
            "reason": "real account is informational",
            "executable": False,
        },
    }
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class NeverOrderClient:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("stale report used the order client")

    with pytest.raises(ValueError, match="invalid frozen trend report"):
        controller.execute_simulated_trend_report(
            config,
            "CN",
            "2026-07-20",
            _report_hash(report),
            actor="test",
            reason="stale available plan",
            now=NOW,
            order_client=NeverOrderClient(),
        )


def test_valid_report_rejects_invalid_frozen_allocation_and_rotation_pair(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    path, report = write_report(config)
    roots = {
        market: {
            "stock": {"asset": stock, "tm_id": index * 10, "as_of_date": "2026-08-03", "global_strength": stock_strength},
            "etf": {"asset": etf, "tm_id": index * 10 + 1, "as_of_date": "2026-08-03", "global_strength": etf_strength},
        }
        for index, (market, stock, etf, stock_strength, etf_strength) in enumerate(
            (("CN", "A股", "ETF基金", "90", "80"), ("HK", "港股", "香港ETF", "70", "60"), ("US", "美股", "美国ETF", "50", "40")), 1
        )
    }
    snapshot = build_allocation_snapshot(
        allocation_date="2026-08-03", generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40, roots=roots, previous=None,
    )
    daily = config.data_dir / "trend_allocation/daily/2026-08-03.json"
    daily.parent.mkdir(parents=True)
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    daily.write_text(body, encoding="utf-8")
    report["allocation"] = {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json", "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "allocation_date": "2026-08-03", "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False, "stale_a_trading_days": 0, "failure_reason": "",
        "roots": snapshot["roots"], "markets": snapshot["markets"],
    }
    report["strategy_snapshot"] = a_share_trend.live_trend_strategy_snapshot(
        "CN", "test-sha", (622466, 697199),
        allocation={
            "daily_path": report["allocation"]["daily_path"],
            "sha256": report["allocation"]["sha256"],
            "snapshot": snapshot,
        },
    )
    judgments = report["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["holding_decisions"] = [{"symbol": "WEAK"}]
    judgments["top10_candidates"] = [{"symbol": "STRONG"}]
    judgments["simulate_rotation_pairs"] = [{
        "pair_index": 0, "sell_symbol": "WEAK", "sell_name": "Weak",
        "sell_futu_symbol": "SH.WEAK", "sell_global_strength": "10",
        "buy_symbol": "STRONG", "buy_name": "Strong", "buy_futu_symbol": "SH.STRONG",
        "buy_global_strength": "90", "strength_gap": "80", "target_weight": "0.04",
        "target_amount": "4000", "estimated_shares": 400, "lot_size": 100,
        "atr": "0.5", "reason": "relative_rotation", "execution_date": "2026-07-20",
        "execution_mode": "automatic", "sell_asset": "A股", "buy_asset": "A股",
        "sell_local_strength": "10", "buy_local_strength": "90",
        "strength_basis": "local", "sell_compared_strength": "10",
        "buy_compared_strength": "90", "threshold": "20",
    }]
    judgments["simulate_rotation_comparisons"] = [{
        "pair_index": 0, "sell_symbol": "WEAK", "sell_name": "Weak", "sell_asset": "A股",
        "sell_local_strength": "10", "sell_global_strength": "10",
        "buy_symbol": "STRONG", "buy_name": "Strong", "buy_asset": "A股",
        "buy_local_strength": "90", "buy_global_strength": "90",
        "strength_basis": "local", "sell_compared_strength": "10",
        "buy_compared_strength": "90", "strength_gap": "80", "threshold": "20",
        "outcome": "planned", "reason": "relative_rotation",
    }]
    judgments["real_rotation_pairs"] = []
    judgments["real_rotation_comparisons"] = []

    assert controller._valid_report(config, "CN", "2026-07-20", path, report)
    malformed = json.loads(json.dumps(report))
    malformed["strategy_judgments"]["simulate_rotation_pairs"][0]["strength_gap"] = "19.9"
    assert not controller._valid_report(config, "CN", "2026-07-20", path, malformed)
    malformed_hash = json.loads(json.dumps(report))
    malformed_hash["allocation"]["sha256"] = "c" * 64
    assert not controller._valid_report(config, "CN", "2026-07-20", path, malformed_hash)
    allocationless_pairs = json.loads(json.dumps(report))
    del allocationless_pairs["allocation"]
    assert not controller._valid_report(
        config, "CN", "2026-07-20", path, allocationless_pairs
    )


def test_execution_completion_distinguishes_partial_and_full_sell_goals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = active_cn_cycle()
    config = controller_config(tmp_path)
    report_path, report = write_report(config)
    report["strategy_judgments"]["formal_actions"].append(partial_sell_action())  # type: ignore[index]
    report["strategy_judgments"]["formal_actions"].append({  # type: ignore[index]
        "action": "BUY",
        "symbol": "SH.600001",
        "target_weight": "0.04",
        "lot_size": 100,
        "estimated_shares": 200,
        "target_amount": "2000",
        "atr": "0.5",
    })
    report_path.write_text(json.dumps(report), encoding="utf-8")
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    events: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []
    monkeypatch.setattr(
        controller,
        "load_trend_action_audit",
        lambda *_args, **_kwargs: (events, resolutions),
    )
    progress = {
        "status": "complete",
        "filled_qty": "300",
        "lifecycle_target_qty": "300",
    }
    monkeypatch.setattr(
        controller,
        "overheat_trim_progress",
        lambda *_args, **_kwargs: progress,
        raising=False,
    )

    events[:] = [{"status": "below_lot", "sell_goal": "partial_30"}]
    assert controller._execution_completed(config, cycle)
    events[:] = [{
        "status": "filled",
        "sell_goal": "partial_30",
        "filled_qty": "200",
        "lifecycle_target_qty": "300",
    }]
    assert controller._execution_completed(config, cycle)
    for protection_status in ("submitted", "partially_filled"):
        events[:] = [
            {
                "status": "filled",
                "sell_goal": "partial_30",
                "filled_qty": "300",
                "lifecycle_target_qty": "300",
            },
            {"status": protection_status, "sell_goal": "position_zero"},
        ]
        assert not controller._execution_completed(config, cycle)
    events[-1] = {
        "status": "incomplete",
        "reason": "position_zero_confirmed",
        "sell_goal": "position_zero",
    }
    assert controller._execution_completed(config, cycle)
    events[:] = [{
        "status": "filled",
        "sell_goal": "partial_30",
        "filled_qty": "not-a-number",
        "lifecycle_target_qty": "300",
    }]
    assert not controller._execution_completed(config, cycle)
    events.clear()
    resolutions[:] = [{"resolution": "abandon"}]
    assert controller._execution_completed(config, cycle)

    full_config = controller_config(tmp_path / "full")
    full_path, full_report = write_report(full_config)
    full_report["strategy_judgments"]["formal_actions"].append({  # type: ignore[index]
        "action": "SELL_ALL", "symbol": "600001", "reason": "danger_signal"
    })
    full_path.write_text(json.dumps(full_report), encoding="utf-8")
    lock_trend_execution_batch(
        full_config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=full_path,
        report=full_report,
        locked_at=NOW.isoformat(),
    )
    resolutions.clear()
    events[:] = [{
        "status": "filled",
        "sell_goal": "partial_30",
        "filled_qty": "300",
        "lifecycle_target_qty": "300",
    }]
    assert not controller._execution_completed(full_config, cycle)
    events[:] = [{
        "status": "incomplete",
        "reason": "position_zero_confirmed",
        "sell_goal": "position_zero",
    }]
    assert controller._execution_completed(full_config, cycle)


def test_controller_executes_partial_and_full_sells_before_buys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report_path, report = write_report(config)
    report["metadata"]["symbol_mapping_schema"] = (  # type: ignore[index]
        "open_trader.trend_symbol_mapping.v1"
    )
    report["strategy_judgments"]["formal_actions"] = [  # type: ignore[index]
        {
            "action": "BUY",
            "symbol": "000001",
            "futu_symbol": "SH.600001",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 200,
            "target_amount": "2000",
            "atr": "0.5",
        },
        {
            "action": "BUY",
            "symbol": "600003",
            "futu_symbol": "SH.600003",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 200,
            "target_amount": "2000",
            "atr": "0.5",
        },
        {
            **partial_sell_action("000001"),
            "futu_symbol": "SH.600001",
        },
        {
            "action": "SELL_ALL",
            "symbol": "600002",
            "futu_symbol": "SH.600002",
            "reason": "danger_signal",
        },
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    class Orders:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "positions": [
                    {"code": "SH.600001", "qty": "1000"},
                    {"code": "SH.600002", "qty": "300"},
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(request)
            order_id = f"SIM-{len(self.requests)}"
            self.orders.append({
                **request,
                "order_id": order_id,
                "code": request["futu_code"],
                "trd_side": str(request["side"]).upper(),
                "dealt_qty": "0",
                "order_status": "SUBMITTED",
            })
            return {"futu_order_id": order_id}

        def close(self) -> None:
            pass

    quoted_symbols: list[list[str]] = []

    class Quote:
        def get_snapshots(self, symbols: object) -> dict[str, object]:
            quoted_symbols.append(list(symbols))  # type: ignore[arg-type]
            return {
                str(symbol): SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols  # type: ignore[union-attr]
            }

    orders = Orders()
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args: orders)

    result = controller._execute_locked_report(
        config,
        "CN",
        "2026-07-20",
        report_path,
        report,
        quote_client=Quote(),
    )

    assert result["submitted_count"] == 3
    assert [request["side"] for request in orders.requests] == ["sell", "sell", "buy"]
    assert {request["futu_code"] for request in orders.requests[:2]} == {
        "SH.600001", "SH.600002"
    }
    assert orders.requests[-1]["futu_code"] == "SH.600003"
    assert quoted_symbols == [["SH.600003"]]


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


def patch_controller_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        controller,
        "FutuQuoteClient",
        lambda **_kwargs: SimpleNamespace(
            get_trading_days=lambda **_query: [], close=lambda: None
        ),
    )


def patch_cycle(monkeypatch: pytest.MonkeyPatch, cycle: ControllerCycle) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    patch_controller_quote(monkeypatch)
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, _now, **_kwargs: cycle,
    )
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args, **_kwargs: protection_success(),
    )

    def capture(
        config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
        projection = config.data_dir / f"latest/trend_review_{market.lower()}.json"
        projection.parent.mkdir(parents=True, exist_ok=True)
        projection.write_text(
            json.dumps({"schema_version": "open_trader.trend_review.projection.v5"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(controller, "_capture_close", capture)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args: {"status": "completed"},
        raising=False,
    )


def test_controller_attempts_statistics_then_always_generates_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[str] = []
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: calls.append("statistics") or {"status": "completed"},
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: {"status": "waiting_for_promotion"},
    )
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: calls.append("report") or write_report(config),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls[:2] == ["statistics", "report"]


def test_statistics_failure_does_not_become_report_or_controller_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    generated: list[str] = []
    attempted: list[str] = []

    def fail_statistics(*_args: object) -> dict[str, object]:
        attempted.append("statistics")
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        fail_statistics,
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: {"status": "blocked"},
    )

    def generate(*_args: object) -> None:
        generated.append("report")
        write_report(config)

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert attempted == ["statistics"]
    assert generated == ["report"]
    assert result["blocker"] != "broker unavailable"
    assert result["phase"] != "blocked"
    state = json.loads(
        (
            config.data_dir
            / "trend_api_stats/daily/CN/2026-07-17.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"
    assert state["reason"] == "broker unavailable"


def test_controller_report_statistics_and_benchmark_fail_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[str] = []
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: calls.append("statistics") or {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("quote failed")),
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: calls.append("report") or write_report(config),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == ["statistics", "report"]
    state = json.loads(
        trend_review.long_term_benchmark_cycle_path(
            config.data_dir, "CN", "2026-07"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"
    assert state["reason"] == "quote failed"


def test_controller_benchmark_completes_when_statistics_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[str] = []
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: calls.append("statistics") or {"status": "failed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args: calls.append("benchmark") or {"status": "completed"},
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: calls.append("report") or write_report(config),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == ["statistics", "benchmark", "report"]


def test_controller_benchmark_completes_when_report_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[str] = []
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: calls.append("statistics") or {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args: calls.append("benchmark") or {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: calls.append("report")
        or (_ for _ in ()).throw(RuntimeError("report failed")),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == ["statistics", "benchmark", "report"]


def test_failed_benchmark_result_uses_controller_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    benchmark_cycles: list[ControllerCycle] = []
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda _config, selected, *_args: benchmark_cycles.append(selected)
        or {"status": "failed", "error": "quote failed"},
    )
    monkeypatch.setattr(
        controller, "_generate_report", lambda *_args: write_report(config)
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    current = NOW

    class StopController(Exception):
        pass

    def advance(_seconds: float) -> None:
        nonlocal current
        current += timedelta(seconds=5)
        if current >= NOW + timedelta(seconds=10):
            raise StopController

    with pytest.raises(StopController):
        run_trend_market_controller(
            config, "CN", now_fn=lambda: current, sleep_fn=advance
        )

    assert benchmark_cycles == [cycle]


def test_revision_request_skips_statistics_and_benchmark_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: pytest.fail("revision attempted natural statistics"),
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args: pytest.fail("revision attempted benchmark refresh"),
        raising=False,
    )
    monkeypatch.setattr(controller, "_request_revision", lambda *_args: None)
    monkeypatch.setattr(
        controller, "_generate_report", lambda *_args: write_report(config)
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    run_trend_market_controller(
        config, "CN", revision=True, once=True, now_fn=lambda: NOW
    )


def test_benchmark_failure_record_recovers_malformed_cycle_state(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    path = trend_review.long_term_benchmark_cycle_path(
        config.data_dir, cycle.market, "2026-07"
    )
    path.parent.mkdir(parents=True)
    path.write_text("{malformed", encoding="utf-8")

    state = controller._record_long_term_benchmark_exception(
        config, cycle, NOW, "test-sha", RuntimeError("quote failed")
    )

    assert state["status"] == "failed"
    assert json.loads(path.read_text(encoding="utf-8"))["reason"] == "quote failed"


def test_statement_consumer_exception_does_not_stop_report_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: {"status": "completed"},
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("consumer crashed")),
    )
    generated: list[str] = []
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: generated.append("report") or write_report(config),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert generated == ["report"]
    assert result["phase"] == "monitoring"
    statement_state = json.loads(
        (
            config.data_dir
            / "trend_controller/CN/statement_statistics/eastmoney.json"
        ).read_text(encoding="utf-8")
    )
    assert statement_state["status"] == "failed"
    assert statement_state["reason"] == "consumer crashed"


def test_statistics_wrapper_delegates_malformed_marker_to_cycle_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    state_path = (
        config.data_dir
        / "trend_api_stats/daily/CN"
        / f"{cycle.as_of_date}.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{malformed", encoding="utf-8")
    opened: list[str] = []

    class Client:
        def close(self) -> None:
            opened.append("closed")

    monkeypatch.setattr(
        controller,
        "FutuSimulateFillClient",
        lambda **_kwargs: Client(),
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "TigerActualFillClient",
        lambda **_kwargs: pytest.fail("CN cycle opened Tiger"),
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "require_trend_review_config",
        lambda _config, _market: "CN-account",
    )

    def recover_marker(**_kwargs: object) -> dict[str, object]:
        state_path.write_text(
            json.dumps({
                "schema_version": "open_trader.trend_api_stats.cycle.v1",
                "status": "completed",
                "market": "CN",
                "as_of_date": cycle.as_of_date,
            }),
            encoding="utf-8",
        )
        return {"status": "completed"}

    monkeypatch.setattr(
        controller,
        "run_trend_statistics_cycle",
        recover_marker,
        raising=False,
    )

    result = controller._run_cycle_statistics(config, cycle, NOW, "test-sha")

    assert result["status"] == "completed"
    assert opened == ["closed"]

    state_path.write_text("{still malformed", encoding="utf-8")
    failed = controller._record_statistics_exception(
        config,
        cycle,
        NOW,
        "test-sha",
        RuntimeError("cycle failed"),
    )
    assert failed["status"] == "failed"
    assert json.loads(state_path.read_text(encoding="utf-8"))["reason"] == (
        "cycle failed"
    )


def test_completed_statistics_cycle_opens_no_broker_clients_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    state_path = (
        config.data_dir
        / "trend_api_stats/daily/CN"
        / f"{cycle.as_of_date}.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "schema_version": "open_trader.trend_api_stats.cycle.v1",
            "status": "completed",
            "market": cycle.market,
            "as_of_date": cycle.as_of_date,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        controller,
        "FutuSimulateFillClient",
        lambda **_kwargs: pytest.fail("completed cycle reopened Futu"),
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "FutuActualFillClient",
        lambda **_kwargs: pytest.fail("completed cycle opened actual client"),
        raising=False,
    )

    result = controller._run_cycle_statistics(config, cycle, NOW, "test-sha")

    assert result["status"] == "already_completed"


def test_statistics_wrapper_selects_futu_for_market_and_futu_actual_only_for_us(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    opened: list[tuple[str, str]] = []
    closed: list[tuple[str, str]] = []

    class Client:
        def __init__(self, source: str, market: str) -> None:
            self.source = source
            self.market = market

        def close(self) -> None:
            closed.append((self.source, self.market))

    def futu_client(**kwargs: object) -> Client:
        market = str(kwargs["trd_market"])
        opened.append(("futu", market))
        return Client("futu", market)

    def actual_client(**kwargs: object) -> Client:
        market = str(kwargs["trd_market"])
        opened.append(("actual", market))
        return Client("actual", market)

    monkeypatch.setattr(
        controller, "FutuSimulateFillClient", futu_client, raising=False
    )
    monkeypatch.setattr(
        controller, "FutuActualFillClient", actual_client, raising=False
    )
    monkeypatch.setattr(
        controller,
        "require_trend_review_config",
        lambda _config, market: f"{market}-account",
    )
    received: list[dict[str, object]] = []
    monkeypatch.setattr(
        controller,
        "run_trend_statistics_cycle",
        lambda **kwargs: received.append(kwargs) or {"status": "completed"},
        raising=False,
    )
    us_cycle = replace(active_cn_cycle(), market="US")

    controller._run_cycle_statistics(config, active_cn_cycle(), NOW, "test-sha")
    controller._run_cycle_statistics(config, us_cycle, NOW, "test-sha")

    assert opened == [("futu", "CN"), ("futu", "US"), ("actual", "US")]
    assert closed == opened
    assert received[0]["actual_client"] is None
    assert received[1]["actual_client"].source == "actual"


def test_statistics_wrapper_closes_actual_when_futu_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    closed: list[str] = []

    class Futu:
        def close(self) -> None:
            closed.append("futu")
            raise RuntimeError("futu close failed")

    class Actual:
        def close(self) -> None:
            closed.append("actual")

    monkeypatch.setattr(
        controller,
        "FutuSimulateFillClient",
        lambda **_kwargs: Futu(),
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "FutuActualFillClient",
        lambda **_kwargs: Actual(),
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "require_trend_review_config",
        lambda _config, _market: "US-account",
    )
    monkeypatch.setattr(
        controller,
        "run_trend_statistics_cycle",
        lambda **_kwargs: {"status": "completed"},
        raising=False,
    )

    with pytest.raises(RuntimeError, match="futu close failed"):
        controller._run_cycle_statistics(
            config, replace(active_cn_cycle(), market="US"), NOW, "test-sha"
        )

    assert closed == ["futu", "actual"]


def test_statistics_failure_and_recovery_notify_once_per_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    notify_once = controller._notify_once
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(controller, "_notify_once", notify_once)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: {"status": "waiting_for_promotion"},
    )
    write_report(config)
    statuses = iter(("failed", "failed", "completed"))

    def run_statistics(
        _config: DailyPremarketConfig,
        cycle: ControllerCycle,
        now: datetime,
        _process_version: str,
    ) -> dict[str, object]:
        status = next(statuses)
        state = {
            "schema_version": "open_trader.trend_api_stats.cycle.v1",
            "status": status,
            "market": cycle.market,
            "as_of_date": cycle.as_of_date,
            "attempt_count": 1,
        }
        if status == "failed":
            state.update({
                "attempted_at": now.isoformat(timespec="seconds"),
                "reason": "broker unavailable",
            })
        path = (
            config.data_dir
            / "trend_api_stats/daily/CN"
            / f"{cycle.as_of_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        return state

    monkeypatch.setattr(controller, "_run_cycle_statistics", run_statistics)
    sent = _record_controller_notification_attempts(monkeypatch)

    for _ in range(3):
        result = run_trend_market_controller(
            config, "CN", once=True, now_fn=lambda: NOW
        )
        assert result["phase"] == "monitoring"
        assert result["blocker"] is None

    states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.data_dir.glob(
            "trend_controller/CN/notifications/2026-07-17/*.json"
        )
    ]
    assert {state["action"] for state in states} == {
        "statistics_failed",
        "statistics_recovered",
    }
    assert len(sent) == 4
    feishu_messages = [message for _, message, channels in sent if "feishu" in channels]
    assert len(feishu_messages) == 2
    assert all(
        "报告与执行继续使用最后一次已接受的统计快照" in message
        for message in feishu_messages
    )
    cycle_state = json.loads(
        (
            config.data_dir
            / "trend_api_stats/daily/CN/2026-07-17.json"
        ).read_text(encoding="utf-8")
    )
    assert cycle_state["failure_notified_at"] == NOW.isoformat(timespec="seconds")
    assert cycle_state["recovery_notified_at"] == NOW.isoformat(timespec="seconds")


def test_report_retry_keeps_statistics_bound_to_natural_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: {"status": "waiting_for_promotion"},
    )
    statistics_cycles: list[ControllerCycle] = []
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda _config, selected, *_args: statistics_cycles.append(selected)
        or {"status": "completed"},
        raising=False,
    )
    report_attempts = 0

    def generate(*_args: object) -> None:
        nonlocal report_attempts
        report_attempts += 1
        if report_attempts == 1:
            raise RuntimeError("report unavailable")
        write_report(config)

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    current = NOW

    class StopController(Exception):
        pass

    def now_fn() -> datetime:
        return current

    def advance(_seconds: float) -> None:
        nonlocal current
        current += timedelta(seconds=11)
        if report_attempts == 2:
            raise StopController

    with pytest.raises(StopController):
        run_trend_market_controller(
            config, "CN", now_fn=now_fn, sleep_fn=advance
        )

    assert report_attempts == 2
    assert statistics_cycles == [cycle]


def test_failed_statistics_due_does_not_run_during_report_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: {"status": "waiting_for_promotion"},
    )
    statistics_calls: list[ControllerCycle] = []

    def fail_statistics(
        _config: DailyPremarketConfig,
        selected: ControllerCycle,
        *_args: object,
    ) -> dict[str, object]:
        statistics_calls.append(selected)
        return {"status": "failed", "reason": "statistics unavailable"}

    monkeypatch.setattr(controller, "_run_cycle_statistics", fail_statistics)
    report_attempts = 0

    def generate(*_args: object) -> None:
        nonlocal report_attempts
        report_attempts += 1
        if report_attempts == 1:
            raise RuntimeError("report unavailable")
        write_report(config)

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    current = NOW

    class StopController(Exception):
        pass

    def now_fn() -> datetime:
        return current

    def advance(_seconds: float) -> None:
        nonlocal current
        current += timedelta(seconds=11)
        if report_attempts == 2:
            raise StopController

    with pytest.raises(StopController):
        run_trend_market_controller(
            config, "CN", now_fn=now_fn, sleep_fn=advance
        )

    assert report_attempts == 2
    assert statistics_calls == [cycle]


def test_statement_already_consumed_is_diagnostic_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: {
            "status": "already_consumed",
            "broker": "eastmoney",
            "statement_generation": "statement-1",
            "snapshot_generation": "snapshot-2",
            "account_generation": "account-1",
        },
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    write_report(config)
    checkpoint = (
        config.data_dir / "trend_statement_consumption/eastmoney.json"
    )
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "schema_version": "open_trader.trend.statement_consumption.v1",
        "status": "consumed",
        "broker": "eastmoney",
        "statement_generation": "statement-1",
        "account_generation": "account-1",
    }
    checkpoint.write_text(json.dumps(original), encoding="utf-8")

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert json.loads(checkpoint.read_text(encoding="utf-8")) == original
    diagnostic = json.loads(
        (
            config.data_dir
            / "trend_controller/CN/statement_statistics/eastmoney.json"
        ).read_text(encoding="utf-8")
    )
    assert diagnostic["status"] == "already_consumed"
    assert diagnostic["snapshot_generation"] == "snapshot-2"


def test_revision_request_does_not_attempt_natural_cycle_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args: pytest.fail("revision attempted natural statistics"),
        raising=False,
    )
    monkeypatch.setattr(
        controller,
        "_request_revision",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        controller,
        "consume_accepted_statement_facts",
        lambda **_kwargs: {"status": "waiting_for_promotion"},
    )
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: write_report(config),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    result = run_trend_market_controller(
        config, "CN", revision=True, once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "monitoring"


def test_start_after_original_trigger_generates_report_and_executes_inside_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    patch_controller_quote(monkeypatch)
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
        lambda _config, _market, _now, **_kwargs: active_cn_cycle(),
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
        lambda _config, market, day, **_kwargs: calls.append(
            ("protect", market, day)
        )
        or protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, market, day, path, report, **_kwargs: calls.append(
            ("execute", market, (day, path.name))
        )
        or {"status": "submitted", "submitted_count": 1},
    )
    def capture_close(
        _config: DailyPremarketConfig,
        market: str,
        day: str,
        **_kwargs: object,
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
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
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


def test_report_failure_remains_visible_while_close_waits_for_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(active_cn_cycle(), session="closed", market_open=False)
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    monkeypatch.setattr(controller, "_load_cycle_report", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("upstream unavailable")),
    )
    monkeypatch.setattr(controller, "_capture_close", pytest.fail)
    blockers: list[object] = []
    sleeps = 0

    class _StopController(Exception):
        pass

    def stop_after_backoff(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        assert sleeps <= 5, blockers
        blockers.append(load_trend_market_status(config, "CN", now=NOW)["blocker"])
        if blockers[-2:] == [
            "report generation failed: upstream unavailable",
            "report generation failed: upstream unavailable",
        ]:
            raise _StopController

    with pytest.raises(_StopController):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=stop_after_backoff,
        )


def test_trend_waiting_reason_holiday() -> None:
    assert controller._trend_waiting_reason(
        phase="holiday",
        execution_date="2026-07-20",
        latest=None,
        report_waiting=None,
        blocker="old blocker",
    ) == "下一执行日 2026-07-20 报告已产出，今日休市无需重跑"


def test_trend_waiting_reason_recovering_report_with_gap() -> None:
    gap = "香港ETF 2026-07-16 → 2026-07-17"
    assert controller._trend_waiting_reason(
        phase="recovering_report",
        execution_date="2026-07-20",
        latest=None,
        report_waiting=gap,
        blocker=None,
    ) == "下一执行日 2026-07-20 报告未产出，正在补产：" + gap
    assert controller._trend_waiting_reason(
        phase="recovering_report",
        execution_date="2026-07-20",
        latest=("report.md", {"report": "x"}),
        report_waiting=gap,
        blocker="report generation failed: x",
    ) == "report generation failed: x"


def test_controller_waiting_reports_missing_report_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(active_cn_cycle(), session="closed", market_open=False)
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    monkeypatch.setattr(controller, "_load_cycle_report", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: (_ for _ in ()).throw(
            controller.ReportGenerationError(
                "CN trend report generation returned waiting: "
                "港股 2026-07-16 → 2026-07-17",
                waiting_reason="港股 2026-07-16 → 2026-07-17",
            )
        ),
    )
    monkeypatch.setattr(controller, "_capture_close", pytest.fail)
    statuses: list[dict[str, object]] = []

    class _StopController(Exception):
        pass

    def stop_after_first_status(_seconds: float) -> None:
        statuses.append(load_trend_market_status(config, "CN", now=NOW))
        raise _StopController

    with pytest.raises(_StopController):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=stop_after_first_status,
        )

    assert statuses[-1]["waiting"] == (
        "下一执行日 2026-07-20 报告未产出，正在补产："
        "港股 2026-07-16 → 2026-07-17"
    )
    assert str(statuses[-1]["blocker"]).startswith("report generation failed:")


def test_report_blocker_remains_visible_when_close_capture_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(active_cn_cycle(), session="closed", market_open=False)
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    monkeypatch.setattr(controller, "_load_cycle_report", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "_generate_report",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("upstream unavailable")),
    )
    close_attempts: list[str] = []

    def capture_close(
        _config: DailyPremarketConfig,
        _market: str,
        trading_date: str,
        **_kwargs: object,
    ) -> None:
        close_attempts.append(trading_date)
        raise RuntimeError("close unavailable")

    monkeypatch.setattr(controller, "_capture_close", capture_close)
    observed: list[object] = []
    report_created = False

    class _StopController(Exception):
        pass

    def stop_after_close_failure(_seconds: float) -> None:
        nonlocal report_created
        observed.append(load_trend_market_status(config, "CN", now=NOW)["blocker"])
        assert len(observed) <= 5
        if not report_created and observed[-1] == (
            "report generation failed: upstream unavailable"
        ):
            write_report(config)
            report_created = True
        elif close_attempts:
            raise _StopController

    with pytest.raises(_StopController):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=stop_after_close_failure,
        )

    assert close_attempts == [cycle.as_of_date]
    assert observed[-1] == "report generation failed: upstream unavailable"


def test_close_review_failure_is_nonblocking_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(
        active_cn_cycle(),
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-20T15:01:05+08:00"),
    )
    now = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    write_report(config)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    reason = "无权限获取SH.000985的行情，请检查A股市场指数行情权限"
    notifications: list[tuple[str, str, object, object]] = []
    monkeypatch.setattr(
        controller,
        "_capture_close",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            controller.FutuQuoteError(reason)
        ),
    )
    monkeypatch.setattr(
        controller,
        "_notify_once",
        lambda title, message, key: notifications.append((title, message, key))
        or True,
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: now
    )

    assert result["phase"] == "recovering_review"
    assert result["blocker"] is None
    assert result["last_success"] == {
        "status": "reconciled",
        "market": "CN",
        "date": cycle.execution_date,
        "submitted_count": 0,
        "artifact_paths": [],
    }
    assert result["next_check_at"] == "2026-07-20T15:01:10+08:00"
    assert notifications[0][1] == reason
    assert notifications[0][2][3:5] == ("review", "snapshot_failed")


def test_review_backoff_does_not_delay_order_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(
        active_cn_cycle(),
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-20T15:01:05+08:00"),
    )
    current = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    write_report(config)
    due_checks = 0

    def execution_due(*_args: object) -> bool:
        nonlocal due_checks
        due_checks += 1
        return due_checks == 2

    executions: list[str] = []
    monkeypatch.setattr(controller, "_execution_due", execution_due)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda _config, _market, execution_date, *_args, **_kwargs: executions.append(
            execution_date
        )
        or {
            "status": "submitted",
            "market": "CN",
            "date": execution_date,
            "submitted_count": 1,
            "artifact_paths": ["intent.json"],
        },
    )
    close_attempts: list[str] = []

    def capture_close(
        _config: DailyPremarketConfig,
        _market: str,
        trading_date: str,
        **_kwargs: object,
    ) -> None:
        close_attempts.append(trading_date)
        raise controller.FutuQuoteError("CN index permission unavailable")

    monkeypatch.setattr(controller, "_capture_close", capture_close)
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)

    class StopController(RuntimeError):
        pass

    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal current, sleeps
        sleeps += 1
        current += timedelta(seconds=5)
        if sleeps == 2:
            raise StopController

    with pytest.raises(StopController):
        run_trend_market_controller(
            config, "CN", now_fn=lambda: current, sleep_fn=advance
        )

    result = load_trend_market_status(config, "CN", now=current)
    assert close_attempts == [cycle.as_of_date]
    assert executions == [cycle.execution_date]
    assert result["phase"] == "recovering_review"
    assert result["blocker"] is None
    assert result["last_success"] == {
        "status": "submitted",
        "market": "CN",
        "date": cycle.execution_date,
        "submitted_count": 1,
        "artifact_paths": ["intent.json"],
    }


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("trend review daily fact is invalid"),
        FileExistsError("immutable artifact collision"),
    ],
)
def test_close_review_integrity_failure_remains_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(active_cn_cycle(), session="closed", market_open=False)
    now = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    write_report(config)
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(
        controller,
        "_capture_close",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: now
    )

    assert result["phase"] == "blocked"
    assert result["blocker"] == str(failure)


def test_close_review_recovery_completes_once_after_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    cycle = replace(
        active_cn_cycle(),
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-20T15:01:05+08:00"),
    )
    current = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    capture_close = controller._capture_close
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(controller, "_capture_close", capture_close)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    report_path, report = write_report(config)
    report["metadata"]["simulate_acc_id"] = 101  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)

    class Account:
        def account_snapshot(self) -> dict[str, object]:
            return {"acc_id": 101, "net_value": "100000", "positions": []}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        controller,
        "FutuSimulateOrderExecutionClient",
        lambda **_kwargs: Account(),
    )
    benchmark_attempts = 0

    def benchmark(
        _quote: object, market: str, trading_date: str
    ) -> dict[str, str]:
        nonlocal benchmark_attempts
        benchmark_attempts += 1
        if benchmark_attempts == 1:
            raise controller.FutuQuoteError("CN index permission unavailable")
        return {
            "date": trading_date,
            "close": "5833.72",
            "source_id": "CSI_500_PRICE",
            "futu_symbol": "SH.000905",
        }

    projections: list[str] = []

    def build_projection(data_dir: Path, market: str) -> None:
        path = data_dir / "latest" / f"trend_review_{market.lower()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema_version": "open_trader.trend_review.projection.v5"}),
            encoding="utf-8",
        )
        projections.append(market)

    monkeypatch.setattr(controller, "benchmark_fact", benchmark)
    monkeypatch.setattr(
        controller,
        "build_trend_review_projection",
        build_projection,
    )
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)

    class StopController(RuntimeError):
        pass

    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal current, sleeps
        sleeps += 1
        current += timedelta(seconds=5)
        if sleeps == 4:
            raise StopController

    with pytest.raises(StopController):
        run_trend_market_controller(
            config, "CN", now_fn=lambda: current, sleep_fn=advance
        )

    fact = controller._close_path(config, "CN", cycle.as_of_date)
    completion = controller._close_completion_path(
        config, "CN", cycle.as_of_date
    )
    result = load_trend_market_status(config, "CN", now=current)
    assert benchmark_attempts == 2
    assert fact.exists()
    assert completion.exists()
    assert projections == ["CN"]
    assert result["phase"] == "closed"
    assert result["last_success"] == {
        "status": "close_captured",
        "date": cycle.as_of_date,
    }


def test_failed_report_retry_uses_current_cycle_after_cycle_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    class Quote:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return [
                "2026-07-17",
                "2026-07-20",
                "2026-07-21",
                "2026-07-22",
            ]

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "FutuQuoteClient", Quote)
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    before_close = datetime.fromisoformat("2026-07-20T14:59:00+08:00")
    after_close = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    times = iter((before_close, before_close, after_close, after_close))
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
        lambda *_args, **_kwargs: failed.wait(timeout=1),
    )
    def capture_close(
        _config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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

    assert calls == ["2026-07-17", "2026-07-20"]
    assert not (
        config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    ).exists()


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
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
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
        lambda *_args, **_kwargs: pytest.fail(
            "report executed before the market opened"
        ),
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

    def protect(*_args: object, **_kwargs: object) -> object:
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
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
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

    def protect(*_args: object, **_kwargs: object) -> None:
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
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
    )

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert result["phase"] == "monitoring"


def test_controller_process_version_is_fixed_across_status_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
    )
    version_calls = 0

    def process_version(_repo: Path) -> str:
        nonlocal version_calls
        version_calls += 1
        return "start-sha" if version_calls == 1 else "changed-sha"

    written: list[dict[str, object]] = []
    write_status = controller._write_status

    def capture_status(
        observed_config: DailyPremarketConfig,
        market: str,
        payload: dict[str, object],
    ) -> None:
        written.append(payload.copy())
        write_status(observed_config, market, payload)

    monkeypatch.setattr(controller, "_process_version", process_version)
    monkeypatch.setattr(controller, "_write_status", capture_status)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert version_calls == 1
    assert [payload["phase"] for payload in written] == [
        "starting",
        "reconciling",
        "monitoring",
    ]
    assert {payload["git_sha"] for payload in written} == {"start-sha"}
    assert result["git_sha"] == "start-sha"
    assert load_trend_market_status(config, "CN", now=NOW)["git_sha"] == (
        "start-sha"
    )


def test_heartbeat_refreshes_before_each_calendar_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    patch_controller_quote(monkeypatch)
    report = write_report(config)
    calendar_blocked = threading.Event()
    release = threading.Event()
    controller_stopped = threading.Event()
    second_tick = datetime.fromisoformat("2026-07-20T09:31:05+08:00")
    times = iter((NOW, NOW, second_tick, second_tick))
    derive_calls = 0
    sleep_calls = 0

    def derive(*_args: object, **_kwargs: object) -> ControllerCycle:
        nonlocal derive_calls
        derive_calls += 1
        if derive_calls == 2:
            calendar_blocked.set()
            assert release.wait(timeout=10)
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
        controller,
        "_cycle_to_reconcile",
        lambda _config, cycle, _now, **_kwargs: cycle,
    )
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )
    monkeypatch.setattr(
        controller, "_run_protection_pass", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
    )

    def capture_close(
        _config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
    assert calendar_blocked.wait(timeout=10)
    status = load_trend_market_status(config, "CN", now=second_tick)
    release.set()
    thread.join(timeout=10)

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
        lambda _config, _market, _day, path, _report, **_kwargs: calls.append(path)
        or {"status": "missed_window", "submitted_count": 0},
    )

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == [report_path]
    assert result["phase"] == "missed"
    assert report_path.exists()
    assert result["last_success"]["submitted_count"] == 0


def test_completed_action_batch_is_reconciled_without_duplicate_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    patch_cycle(monkeypatch, cycle)
    report_path, report = write_report(config, buy=True)
    write_report_delivery_receipt(config, report_path, report, status="sent")
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )
    write_controller_action(
        config,
        trend_action_key("CN", cycle.execution_date, "SH.600001", "buy"),
        {
            "market": "CN",
            "date": cycle.execution_date,
            "report_sha256": _report_hash(report),
            "strategy_version": report["strategy_snapshot"]["strategy_version"],
            "action_index": 0,
            "symbol": "600001",
            "futu_code": "SH.600001",
            "side": "buy",
            "status": "missed",
            "reason": "buy_window_closed",
        },
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: pytest.fail("completed batch was re-executed"),
    )

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["last_success"]["status"] == "reconciled"
    assert result["phase"] == "monitoring"


def test_controller_groups_uncertain_actions_by_side_for_feishu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        write_controller_action(config, "600001-buy", {
            "symbol": "600001", "side": "buy", "status": "uncertain",
        })
        write_controller_action(config, "600002-buy", {
            "symbol": "600002", "side": "buy", "status": "uncertain",
        })
        write_controller_action(config, "600003-sell", {
            "symbol": "600003", "side": "sell", "status": "uncertain",
        })
        return {"status": "uncertain", "submitted_count": 0}

    monkeypatch.setattr(controller, "_execute_locked_report", execute)
    non_feishu: list[tuple[str, str]] = []
    feishu: list[tuple[str, str]] = []
    monkeypatch.setattr(
        controller,
        "_notify_non_feishu_once",
        lambda title, message, _key: non_feishu.append((title, message)) or True,
    )
    monkeypatch.setattr(
        controller,
        "_notify_feishu_once",
        lambda title, message, _key: feishu.append((title, message)) or True,
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert [title for title, _ in non_feishu] == ["CN 趋势订单 uncertain"]
    assert [title for title, _ in feishu] == [
        "【需处理｜东方财富｜A股买入状态不确定｜2026-07-20】",
        "【需处理｜东方财富｜A股卖出状态不确定｜2026-07-20】",
    ]
    assert "- 600001" in feishu[0][1]
    assert "- 600002" in feishu[0][1]


def test_controller_groups_missed_buys_for_feishu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        for symbol in ("600001", "600002"):
            write_controller_action(config, f"{symbol}-buy", {
                "symbol": symbol,
                "side": "buy",
                "status": "missed",
                "reason": "buy_window_closed",
            })
        return {"status": "missed_window", "submitted_count": 0}

    monkeypatch.setattr(controller, "execute_simulated_trend_report", execute)
    non_feishu: list[str] = []
    feishu: list[tuple[str, str]] = []
    monkeypatch.setattr(
        controller,
        "_notify_non_feishu_once",
        lambda title, _message, _key: non_feishu.append(title) or True,
    )
    monkeypatch.setattr(
        controller,
        "_notify_feishu_once",
        lambda title, message, _key: feishu.append((title, message)) or True,
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert non_feishu == ["CN 趋势买入已错过窗口"]
    assert [title for title, _ in feishu] == [
        "【需处理｜东方财富｜A股买入错过窗口｜2026-07-20】"
    ]
    assert "- 600001" in feishu[0][1]
    assert "- 600002" in feishu[0][1]


def test_controller_submitted_actions_create_no_feishu_order_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        write_controller_action(config, "600001-buy", {
            "symbol": "600001", "side": "buy", "status": "submitted",
        })
        return {"status": "submitted", "submitted_count": 1}

    monkeypatch.setattr(controller, "_execute_locked_report", execute)
    feishu: list[str] = []
    monkeypatch.setattr(
        controller,
        "_notify_feishu_once",
        lambda title, _message, _key: feishu.append(title) or True,
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert feishu == []


def test_controller_directionless_abnormal_execution_uses_batch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report", lambda *_args: report
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {
            "status": "uncertain",
            "submitted_count": 0,
        },
    )
    feishu: list[str] = []
    monkeypatch.setattr(
        controller,
        "_notify_non_feishu_once",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        controller,
        "_notify_feishu_once",
        lambda title, _message, _key: feishu.append(title) or True,
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert feishu == ["【需处理｜东方财富｜A股批次执行失败｜2026-07-20】"]


def test_report_future_crossing_cycle_never_executes_old_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    class Quote:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return [
                "2026-07-17",
                "2026-07-20",
                "2026-07-21",
                "2026-07-22",
            ]

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "FutuQuoteClient", Quote)
    before_close = datetime.fromisoformat("2026-07-20T14:59:00+08:00")
    after_close = datetime.fromisoformat("2026-07-20T15:01:00+08:00")
    execution_open = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    times = iter(
        (before_close, before_close, after_close, execution_open, execution_open)
    )
    release = threading.Event()
    current_started = threading.Event()
    current_release = threading.Event()
    old_generated = threading.Event()
    current_generated = threading.Event()
    runs: list[str] = []

    def write_generated_report(report_run_date: str) -> None:
        execution_date = {
            "2026-07-17": "2026-07-20",
            "2026-07-20": "2026-07-21",
        }[report_run_date]
        report = valid_cn_report(
            as_of_date=report_run_date,
            execution_date=execution_date,
        )
        metadata = report["metadata"]
        assert isinstance(metadata, dict)
        metadata["simulate_acc_id"] = 101
        path = (
            config.reports_dir
            / "trend_a_share"
            / f"{report_run_date}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        report_run_date: str,
        _revision: object,
    ) -> None:
        runs.append(report_run_date)
        if report_run_date == "2026-07-17":
            assert release.wait(timeout=1)
            write_generated_report(report_run_date)
            old_generated.set()
            return
        current_started.set()
        assert current_release.wait(timeout=1)
        write_generated_report(report_run_date)
        current_generated.set()

    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args, **_kwargs: protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller, "_capture_close", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    current_batch_path = (
        config.data_dir / "trend_review/ledgers/CN/batches/2026-07-21.json"
    )
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            release.set()
            assert old_generated.wait(timeout=1)
            return
        if sleeps == 2:
            assert current_started.wait(timeout=1)
            return
        if sleeps == 3:
            assert not current_batch_path.exists()
            current_release.set()
            return
        assert current_generated.wait(timeout=1)
        assert current_batch_path.exists()
        raise RuntimeError("stop controller test")

    with pytest.raises(RuntimeError, match="stop controller test"):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: next(times),
            sleep_fn=advance,
        )

    assert runs == ["2026-07-17", "2026-07-20"]
    old_batch_path = (
        config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    )
    assert not old_batch_path.exists()
    assert not list(
        (
            config.data_dir
            / "trend_controller/CN/simulation_requests/2026-07-20"
        ).glob("*.json")
    )
    assert not list(
        (
            config.data_dir
            / "trend_controller/CN/simulation_requests/completions/2026-07-20"
        ).glob("*.json")
    )
    assert current_batch_path.exists()
    assert list(
        (
            config.data_dir
            / "trend_controller/CN/simulation_requests/2026-07-21"
        ).glob("*.json")
    )
    assert list(
        (
            config.data_dir
            / "trend_controller/CN/simulation_requests/completions/2026-07-21"
        ).glob("*.json")
    )


def test_same_logical_cycle_keeps_inflight_report_when_next_check_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    class Quote:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-17", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "FutuQuoteClient", Quote)
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args, **_kwargs: protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args, **_kwargs: {"status": "completed"},
    )

    def capture_close(
        config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
        projection = config.data_dir / f"latest/trend_review_{market.lower()}.json"
        projection.parent.mkdir(parents=True, exist_ok=True)
        projection.write_text(
            json.dumps({"schema_version": "open_trader.trend_review.projection.v5"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(controller, "_capture_close", capture_close)

    first_check = datetime.fromisoformat("2026-07-20T09:31:00+08:00")
    next_check = datetime.fromisoformat("2026-07-20T09:32:00+08:00")
    times = iter((first_check, first_check, next_check, next_check))
    release = threading.Event()
    started = threading.Event()
    generated = threading.Event()
    runs: list[str] = []

    def generate(
        _config: DailyPremarketConfig,
        _market: str,
        report_run_date: str,
        _revision: object,
    ) -> None:
        runs.append(report_run_date)
        started.set()
        assert release.wait(timeout=1)
        report = valid_cn_report(
            as_of_date="2026-07-17", execution_date="2026-07-20"
        )
        metadata = report["metadata"]
        assert isinstance(metadata, dict)
        metadata["simulate_acc_id"] = 101
        path = config.reports_dir / "trend_a_share/2026-07-17.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
        generated.set()

    monkeypatch.setattr(controller, "_generate_report", generate)
    current_batch_path = (
        config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    )
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            assert started.wait(timeout=1)
            return
        if sleeps == 2:
            release.set()
            return
        assert generated.wait(timeout=1)
        assert current_batch_path.exists()
        assert list(
            (
                config.data_dir
                / "trend_controller/CN/simulation_requests/completions/2026-07-20"
            ).glob("*.json")
        )
        raise RuntimeError("stop controller test")

    with pytest.raises(RuntimeError, match="stop controller test"):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: next(times),
            sleep_fn=advance,
        )

    assert runs == ["2026-07-17"]


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
        lambda _config, _market, _date, path, _report, **_kwargs: executed.append(path)
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


def test_current_controller_uses_latest_revision_each_round_and_ignores_old_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    cycle = ControllerCycle(
        market="CN",
        as_of_date="2026-07-19",
        execution_date="2026-07-20",
        report_run_date="2026-07-19",
        session="morning",
        market_open=True,
        next_check_at=datetime.fromisoformat("2026-07-20T09:31:05+08:00"),
    )
    patch_cycle(monkeypatch, cycle)
    buy = {
        "action": "BUY",
        "symbol": "600001",
        "futu_symbol": "SH.600001",
        "target_weight": "0.04",
        "lot_size": 100,
        "estimated_shares": 400,
        "target_amount": "4000",
        "atr": "0.5",
    }
    base_path, base_report = write_v2_controller_report(config, actions=[buy])
    revision_reports: dict[int, tuple[Path, dict[str, object]]] = {}
    for revision, generated_at in ((1, "2026-07-19T18:01:00+08:00"), (2, "2026-07-19T18:02:00+08:00")):
        revision_report = json.loads(json.dumps(base_report))
        revision_report["generated_at"] = generated_at
        revision_path = (
            config.reports_dir
            / "trend_a_share"
            / f"2026-07-19-r{revision}.json"
        )
        revision_path.write_text(json.dumps(revision_report), encoding="utf-8")
        revision_reports[revision] = (revision_path, revision_report)
        if revision == 2:
            revision_path.unlink()

    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=base_path,
        report=base_report,
        locked_at=NOW.isoformat(),
    )

    executed: list[str] = []

    def canonical_sha(report: dict[str, object]) -> str:
        body = (
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def execute(
        _config: DailyPremarketConfig,
        _market: str,
        _execution_date: str,
        report_sha: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        executed.append(report_sha)
        if len(executed) == 1:
            revision_path, revision_report = revision_reports[2]
            revision_path.write_text(
                json.dumps(revision_report), encoding="utf-8"
            )
        return {"status": "submitted", "submitted_count": 1}

    monkeypatch.setattr(controller, "execute_simulated_trend_report", execute)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)
    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert executed == [
        canonical_sha(revision_reports[1][1]),
        canonical_sha(revision_reports[2][1]),
    ]


def test_current_revision_executes_when_earlier_same_cycle_batch_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host="executor",
    )
    cycle = ControllerCycle(
        market="CN",
        as_of_date="2026-07-19",
        execution_date="2026-07-20",
        report_run_date="2026-07-19",
        session="morning",
        market_open=True,
        next_check_at=datetime.fromisoformat("2026-07-20T09:31:05+08:00"),
    )
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_run_cycle_statistics",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        controller,
        "_run_cycle_long_term_benchmark",
        lambda *_args, **_kwargs: {"status": "completed"},
    )

    base_buy = {
        "action": "BUY",
        "symbol": "600001",
        "futu_symbol": "SH.600001",
        "target_weight": "0.04",
        "lot_size": 100,
        "estimated_shares": 100,
        "target_amount": "1000",
        "atr": "0.5",
    }
    latest_buy = {
        **base_buy,
        "symbol": "600002",
        "futu_symbol": "SH.600002",
        "estimated_shares": 200,
        "target_amount": "2000",
    }
    base_path, base_report = write_v2_controller_report(
        config, actions=[base_buy]
    )
    base_bytes = base_path.read_bytes()
    _, latest_report = write_v2_controller_report(
        config, actions=[latest_buy]
    )
    latest_bytes = base_path.read_bytes()
    base_path.write_bytes(base_bytes)
    latest_path = base_path.with_name("2026-07-19-r1.json")
    latest_path.write_bytes(latest_bytes)
    latest_report = json.loads(latest_bytes)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=base_path,
        report=base_report,
        locked_at=NOW.isoformat(),
    )
    shared_batch_path = (
        config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    )
    shared_batch_bytes = shared_batch_path.read_bytes()

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Broker:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "available_cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            return {"futu_order_id": "SIM-1"}

        def close(self) -> None:
            pass

    quote = Quote()
    broker = Broker()
    monkeypatch.setattr(controller, "FutuQuoteClient", lambda **_kwargs: quote)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args: broker)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    report_body = (
        json.dumps(
            latest_report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    expected_latest_sha = hashlib.sha256(report_body).hexdigest()
    requests = list(
        (
            config.data_dir
            / "trend_controller/CN/simulation_requests/2026-07-20"
        ).glob("*.json")
    )
    request_batches = list(
        (
            config.data_dir
            / "trend_review/ledgers/CN/batches/requests/2026-07-20"
        ).glob("*.json")
    )
    request = json.loads(requests[0].read_text(encoding="utf-8"))
    request_batch = json.loads(request_batches[0].read_text(encoding="utf-8"))

    assert (
        [request["futu_code"] for request in broker.requests],
        shared_batch_path.read_bytes(),
        request["report_sha256"],
        request_batch["report_sha256"],
        request["report_path"],
    ) == (
        ["SH.600002"],
        shared_batch_bytes,
        expected_latest_sha,
        expected_latest_sha,
        str(latest_path),
    )


def test_current_controller_never_executes_legacy_strategy_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(
        active_cn_cycle(), as_of_date="2026-07-19", report_run_date="2026-07-19"
    )
    patch_cycle(monkeypatch, cycle)
    report_path, report = write_v2_controller_report(config)
    report["allocation"] = {**report["allocation"], "version": 2}
    report["strategy_snapshot"] = {
        **report["strategy_snapshot"],
        "strategy_id": "trend_animals_warm_to_hot/CN/v15",
        "strategy_version": "v15",
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    executed: list[str] = []

    def execute(
        _config: DailyPremarketConfig,
        _market: str,
        _execution_date: str,
        report_sha: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        executed.append(report_sha)
        return {"status": "submitted", "submitted_count": 1}

    monkeypatch.setattr(controller, "execute_simulated_trend_report", execute)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert (
        executed,
        result["phase"],
        result["blocker"],
        report_path.exists(),
        all(
            legacy != current
            for legacy, current in {
                "CN": ("v15", "v17"),
                "HK": ("v13", "v14"),
                "US": ("v13", "v14"),
            }.values()
        ),
    ) == (
        [],
        "blocked",
        "no current executable report",
        True,
        True,
    )


def test_locked_report_selects_latest_valid_report_before_batch_exists(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    base_path, _ = write_report(config)
    revision_path, revision_report = write_report(config, revision=1)

    selected_path, selected_report = controller._locked_report(
        config,
        active_cn_cycle(),
        (revision_path, revision_report),
        NOW,
    )

    assert selected_path == revision_path
    assert selected_report["generated_at"] == "2026-07-17T18:01:00+08:00"


def test_scheduled_execution_without_batch_uses_latest_valid_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    base_path, _ = write_report(config)
    revision_path, revision_report = write_report(config, revision=1)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller,
        "_load_latest_valid_report",
        lambda *_args: (revision_path, revision_report),
    )
    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    batch_path = (
        config.data_dir
        / "trend_review/ledgers/CN/batches/2026-07-20.json"
    )
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    assert batch["report_path"] == str(revision_path)
    expected_sha = hashlib.sha256(
        json.dumps(
            revision_report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    ).hexdigest()
    assert batch["report_sha256"] == expected_sha
    assert base_path != revision_path


def test_manual_revision_sha_executes_without_replacing_locked_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    locked_path, locked_report = write_report(config)
    requested_path, requested_report = write_report(config, revision=1)
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date="2026-07-20",
        report_path=locked_path,
        report=locked_report,
        locked_at=NOW.isoformat(),
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    result = controller._execute_locked_report(
        config,
        "CN",
        "2026-07-20",
        requested_path,
        requested_report,
        scheduled=False,
    )

    batch_path = config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    assert result["status"] == "unchanged"
    assert batch["report_path"] == str(locked_path)
    assert batch["report_sha256"] == _report_hash(locked_report)


@pytest.mark.parametrize("completed", [False, True], ids=["pending", "completed"])
def test_manual_exact_revision_ignores_revision_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed: bool,
) -> None:
    config = controller_config(tmp_path)
    base_path, base_report = write_report(config)
    cycle = active_cn_cycle()
    lock_trend_execution_batch(
        config.data_dir,
        market=cycle.market,
        execution_date=cycle.execution_date,
        report_path=base_path,
        report=base_report,
        locked_at=NOW.isoformat(),
    )
    controller._request_revision(config, cycle, NOW)
    revision_path, revision_report = write_report(config, revision=1)
    if completed:
        write_report_delivery_receipt(
            config, revision_path, revision_report, status="sent"
        )
        controller._complete_revision(
            config, cycle, (revision_path, revision_report), NOW
        )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(
        controller, "require_trend_review_config", lambda *_args: 123
    )

    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        cycle.execution_date,
        _report_hash(revision_report),
        actor="ray",
        reason="manual revision retry",
        now=NOW,
        scheduled=False,
    )

    batch_path = config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    assert result["status"] == "unchanged"
    assert result["report_sha256"] == _report_hash(revision_report)
    assert batch["report_sha256"] == _report_hash(base_report)


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


def test_readonly_status_reads_current_process_version_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    versions = iter(("first-sha", "second-sha"))
    monkeypatch.setattr(socket, "gethostname", lambda: "readonly-copy")
    monkeypatch.setattr(controller, "_process_version", lambda _repo: next(versions))

    first = load_trend_market_status(config, "CN", now=NOW)
    second = load_trend_market_status(config, "CN", now=NOW)

    assert first["git_sha"] == "first-sha"
    assert second["git_sha"] == "second-sha"
    assert not config.data_dir.exists()


def test_calendar_failure_writes_blocker_instead_of_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    patch_controller_quote(monkeypatch)
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("calendar offline")
        ),
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
        lambda _config, _market, day, **_kwargs: protected.append(day)
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

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
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


def test_in_window_quote_failure_keeps_controller_blocked_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(controller_config(tmp_path), trend_review_cn_simulate_acc_id=101)
    patch_cycle(monkeypatch, active_cn_cycle())
    report_path, report = write_report(config, buy=True)
    report["metadata"]["simulate_acc_id"] = 101  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    class BrokenQuote:
        def get_snapshots(self, _symbols: object) -> object:
            raise RuntimeError("quote offline")

        def close(self) -> None:
            pass

    class Orders:
        def __init__(self) -> None:
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
    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    monkeypatch.setattr(controller, "FutuQuoteClient", lambda **_kwargs: BrokenQuote())
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args: orders)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "blocked"
    assert result["last_success"] is None
    assert "current quote unavailable" in str(result["blocker"])
    assert orders.requests == []
    assert any(
        json.loads(event_path.read_text(encoding="utf-8")).get("reason")
        == "current_quote_unavailable"
        for event_path in config.data_dir.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
    )


def test_broker_failure_uses_bounded_backoff_without_stopping_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(controller, "_load_latest_valid_report", lambda *_args: report)
    protected = 0

    def protect(*_args: object, **_kwargs: object) -> object:
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

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
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
        lambda *_args, **_kwargs: {"status": "missed_window", "submitted_count": 0},
    )
    calls = 0

    def capture(
        _config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
        projection = config.data_dir / f"latest/trend_review_{market.lower()}.json"
        projection.parent.mkdir(parents=True, exist_ok=True)
        projection.write_text(
            json.dumps({"schema_version": "open_trader.trend_review.projection.v5"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(controller, "_capture_close", capture)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)
    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert calls == 1


def test_closed_restart_rebuilds_legacy_trend_review_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    closed = replace(active_cn_cycle(), session="closed", market_open=False)
    patch_cycle(monkeypatch, closed)
    write_report(config)
    fact = controller._close_path(config, "CN", closed.as_of_date)
    fact.parent.mkdir(parents=True)
    fact.write_text("{}", encoding="utf-8")
    controller._complete_close(config, "CN", closed.as_of_date, NOW)
    projection = config.data_dir / "latest/trend_review_cn.json"
    projection.parent.mkdir(parents=True)
    projection.write_text(
        json.dumps({"schema_version": "open_trader.trend_review.projection.v4"}),
        encoding="utf-8",
    )

    def rebuild(*_args: object, **_kwargs: object) -> None:
        projection.write_text(
            json.dumps({"schema_version": "open_trader.trend_review.projection.v5"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(controller, "_capture_close", rebuild)

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert json.loads(projection.read_text(encoding="utf-8"))["schema_version"] == (
        "open_trader.trend_review.projection.v5"
    )


def test_stable_closed_restart_records_successful_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="closed",
        market_open=False,
        next_check_at=NOW + timedelta(seconds=5),
    )
    patch_cycle(monkeypatch, cycle)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    report_path = config.reports_dir / "trend_a_share/2026-07-20.json"
    report_path.parent.mkdir(parents=True)
    report = valid_cn_report(
        as_of_date=cycle.as_of_date,
        execution_date=cycle.execution_date,
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        controller, "_load_cycle_report", lambda *_args: (report_path, report)
    )
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    projection = config.data_dir / "latest/trend_review_cn.json"
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text(
        json.dumps({"schema_version": "open_trader.trend_review.projection.v5"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(controller, "_execute_locked_report", pytest.fail)

    result = run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )

    assert result["phase"] == "closed"
    assert result["blocker"] is None
    assert result["last_success"] == {
        "status": "reconciled",
        "market": "CN",
        "date": "2026-07-21",
        "submitted_count": 0,
        "artifact_paths": [],
    }


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


def test_execution_completion_skips_pending_and_data_missing_buys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "strategy_judgments": {
            "formal_actions": [
                {
                    "action": "BUY",
                    "symbol": "600001",
                    "target_weight": "0.04",
                    "lot_size": 100,
                    "estimated_shares": 100,
                    "atr": "0.5",
                    "executable": False,
                    "sizing_note": "席位已满，待现金/席位释放",
                },
                {
                    "action": "BUY",
                    "symbol": "600002",
                    "target_weight": "0.04",
                    "lot_size": 0,
                    "estimated_shares": 0,
                    "atr": "0",
                    "sizing_note": "每手股数未知，无法定量",
                },
            ],
            "simulate_rotation_pairs": [{"buy_futu_symbol": "SH.STRONG"}],
            "real_rotation_pairs": [],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    batch = config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    batch.parent.mkdir(parents=True)
    batch.write_text(json.dumps({
        "schema_version": "open_trader.trend_review.batch.v1", "market": "CN",
        "execution_date": "2026-07-20", "report_path": str(report_path),
        "report_sha256": _report_hash(report),
    }), encoding="utf-8")
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(
        controller,
        "load_trend_action_audit",
        lambda *_args, **_kwargs: pytest.fail(
            "pending/data-missing buys must not load the action audit"
        ),
    )
    audited: list[object] = []
    monkeypatch.setattr(
        controller,
        "relative_rotations_completed",
        lambda *_args, **_kwargs: audited.append(_kwargs["report"]) or True,
    )

    # 待条件（executable=False）与数据缺失类（0 股/0 手/0 ATR）买入不产生终态
    # 事件：视为已完成，不阻塞轮换执行与当日批次完成。
    assert controller._execution_completed(config, cycle) is True
    assert audited == [report]

    # 回归对照：可执行买入（executable 缺失、数量齐全）无终态事件时仍视为未完成。
    monkeypatch.setattr(
        controller, "load_trend_action_audit", lambda *_args, **_kwargs: ([], [])
    )
    executable = json.loads(json.dumps(report))
    action = executable["strategy_judgments"]["formal_actions"][0]
    action.pop("executable")
    action["sizing_note"] = ""
    executable_path = tmp_path / "executable.json"
    executable_path.write_text(json.dumps(executable), encoding="utf-8")
    batch.write_text(json.dumps({
        "schema_version": "open_trader.trend_review.batch.v1", "market": "CN",
        "execution_date": "2026-07-20", "report_path": str(executable_path),
        "report_sha256": _report_hash(executable),
    }), encoding="utf-8")
    assert controller._execution_completed(config, cycle) is False


def test_active_session_restart_recovers_prior_close_after_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    report = write_report(config)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls: list[tuple[str, str]] = []

    def protect(
        _config: DailyPremarketConfig,
        _market: str,
        execution_date: str,
        **_kwargs: object,
    ) -> object:
        calls.append(("protect", execution_date))
        return protection_success()

    def capture(
        _config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
        lambda *_args, **_kwargs: {"status": "unchanged", "submitted_count": 0},
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
        _config: DailyPremarketConfig,
        _market: str,
        now: datetime,
        **_kwargs: object,
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


def test_invalid_historical_batch_remains_selected_and_can_be_revised(
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
    request_path = controller._request_revision(config, selected, NOW)
    assert request_path.exists()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["market"] == historical.market
    assert request["as_of_date"] == historical.as_of_date
    assert request["execution_date"] == historical.execution_date
    assert request["baseline_report_path"] == str(report_path)
    assert request["baseline_report_sha256"] == hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    assert request["baseline_revision"] == 2


def test_missing_report_cutover_skips_exact_expired_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    historical = ControllerCycle(
        market="HK",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="catchup",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-21T10:01:05+08:00"),
    )
    current = ControllerCycle(
        market="HK",
        as_of_date="2026-07-21",
        execution_date="2026-07-22",
        report_run_date="2026-07-21",
        session="closed",
        market_open=False,
        next_check_at=datetime.fromisoformat("2026-07-22T16:01:05+08:00"),
    )
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, now, **_kwargs: (
            historical if now.hour == 9 else current
        ),
    )

    assert controller._cycle_to_reconcile(config, current, authorized_at) == (
        historical
    )
    request_path = controller._request_revision(
        config, historical, authorized_at
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    cutover = controller._record_legacy_cycle_cutover(
        config,
        historical,
        actor="ray",
        reason="historical report was never generated",
        authorized_at=authorized_at,
        report_missing=True,
    )
    payload = json.loads(cutover.read_text(encoding="utf-8"))

    assert request["baseline_report_path"] is None
    assert request["baseline_report_sha256"] is None
    assert request["baseline_revision"] == -1
    assert payload["report_missing"] is True
    assert payload["report_path"] is None
    assert payload["report_sha256"] is None
    assert controller._execution_completed(config, historical) is True
    assert controller._cycle_to_reconcile(config, current, authorized_at) == current
    assert not controller._batch_path(
        config, historical.market, historical.execution_date
    ).exists()
    assert not (config.data_dir / "trend_review/ledgers/HK/actions").exists()


@pytest.mark.parametrize(
    ("report_timing", "suffix"),
    [("before", ".json"), ("before", ".md"), ("after", ".json")],
)
def test_missing_report_cutover_fails_closed_if_report_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_timing: str,
    suffix: str,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    controller._request_revision(config, cycle, authorized_at)
    report_path = config.reports_dir / f"trend_a_share/2026-07-17{suffix}"

    if report_timing == "before":
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid legacy trend cutover"):
            controller._record_legacy_cycle_cutover(
                config,
                cycle,
                actor="ray",
                reason="historical report was never generated",
                authorized_at=authorized_at,
                report_missing=True,
            )
        return

    controller._record_legacy_cycle_cutover(
        config,
        cycle,
        actor="ray",
        reason="historical report was never generated",
        authorized_at=authorized_at,
        report_missing=True,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid legacy trend cutover"):
        controller._execution_completed(config, cycle)


@pytest.mark.parametrize(
    "blocker",
    [
        "readonly",
        "open_window",
        "current_cycle",
        "batch",
        "actor",
        "reason",
        "naive_time",
    ],
)
def test_missing_report_cutover_preserves_authorization_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    config = controller_config(tmp_path)
    cycle = (
        ControllerCycle(
            market="CN",
            as_of_date="2026-07-21",
            execution_date="2026-07-22",
            report_run_date="2026-07-21",
            session="before",
            market_open=False,
            next_check_at=datetime.fromisoformat("2026-07-22T09:00:05+08:00"),
        )
        if blocker == "current_cycle"
        else active_cn_cycle()
    )
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    monkeypatch.setattr(
        socket,
        "gethostname",
        lambda: "readonly-copy" if blocker == "readonly" else "executor",
    )
    controller._request_revision(config, cycle, authorized_at)
    if blocker == "open_window":
        authorized_at = NOW
    elif blocker == "batch":
        batch = controller._batch_path(
            config, cycle.market, cycle.execution_date
        )
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_text("{}", encoding="utf-8")
    elif blocker == "naive_time":
        authorized_at = datetime(2026, 7, 21, 18)

    with pytest.raises(ValueError):
        controller._record_legacy_cycle_cutover(
            config,
            cycle,
            actor="" if blocker == "actor" else "ray",
            reason="" if blocker == "reason" else "historical report missing",
            authorized_at=authorized_at,
            report_missing=True,
        )


def test_missing_report_cutover_is_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    controller._request_revision(config, cycle, authorized_at)
    values = {
        "config": config,
        "cycle": cycle,
        "actor": "ray",
        "reason": "historical report missing",
        "authorized_at": authorized_at,
        "report_missing": True,
    }

    first = controller._record_legacy_cycle_cutover(**values)

    assert controller._record_legacy_cycle_cutover(**values) == first
    with pytest.raises(FileExistsError, match="immutable artifact collision"):
        controller._record_legacy_cycle_cutover(
            **{**values, "reason": "different reason"}
        )


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
    monkeypatch.setattr(
        controller, "_derive_cycle", lambda *_args, **_kwargs: current
    )

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
    monkeypatch.setattr(
        controller, "_derive_cycle", lambda *_args, **_kwargs: next_cycle
    )
    next_lock_path = (
        next_config.data_dir / "runs/.trend_market_controller.CN.lock"
    )
    with RunLock(next_lock_path):
        result = run_trend_market_controller(
            next_config, "CN", revision=True, once=True, now_fn=lambda: NOW
        )

    request_path, _ = controller._revision_paths(
        next_config, next_cycle.market, next_cycle.as_of_date
    )
    assert result["phase"] == "revision_requested"
    assert request_path.exists()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["market"] == next_cycle.market
    assert request["as_of_date"] == next_cycle.as_of_date
    assert request["execution_date"] == next_cycle.execution_date
    assert request["baseline_report_path"] == str(report_path)
    assert request["baseline_report_sha256"] == hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    assert request["baseline_revision"] == 0


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
        lambda *_args, **_kwargs: pytest.fail(
            "base report executed while revision was pending"
        ),
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


def test_revision_request_allows_existing_execution_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    cycle = active_cn_cycle()
    report_path, report = write_report(config)
    lock_trend_execution_batch(
        config.data_dir,
        market=cycle.market,
        execution_date=cycle.execution_date,
        report_path=report_path,
        report=report,
        locked_at=NOW.isoformat(),
    )

    request_path = controller._request_revision(config, cycle, NOW)

    assert request_path.exists()


def test_pending_revision_is_checked_again_at_batch_lock_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    cycle = active_cn_cycle()
    report_path, report = write_report(config)
    controller._request_revision(config, cycle, NOW)

    result = controller._execute_locked_report(
        config,
        "CN",
        cycle.execution_date,
        report_path,
        report,
    )

    assert result["status"] == "unchanged"
    assert list(config.data_dir.glob("trend_review/ledgers/CN/batches/*.json"))


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
        lambda _config, _market, _date, path, _report, **_kwargs: executed.append(path)
        or {"status": "unchanged", "submitted_count": 0},
    )

    result = run_trend_market_controller(
        config, "CN", revision=True, once=True, now_fn=lambda: NOW
    )

    request_path, completion_path = controller._revision_paths(
        config, "CN", "2026-07-17"
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    revision_path = config.reports_dir / "trend_a_share/2026-07-17-r1.json"
    revision_report = json.loads(revision_path.read_text(encoding="utf-8"))
    assert request["baseline_report_path"] == str(invalid_path)
    assert request["baseline_report_sha256"] == hashlib.sha256(
        invalid_path.read_bytes()
    ).hexdigest()
    assert request["baseline_revision"] == 0
    assert executed == [revision_path]
    assert completion["request_path"] == str(request_path)
    assert completion["request_sha256"] == hashlib.sha256(
        request_path.read_bytes()
    ).hexdigest()
    assert completion["report_path"] == str(revision_path)
    assert completion["report_sha256"] == _report_hash(revision_report)
    assert json.loads(
        controller._delivery_receipt_path(config, "CN", revision_path).read_text(
            encoding="utf-8"
        )
    )["status"] == "sent"
    assert not controller._batch_path(config, "CN", "2026-07-20").exists()


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
        lambda _config, _market, execution_date, **_kwargs: protected.append(
            execution_date
        ),
    )
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed frozen report was executed"
        ),
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
    patch_controller_quote(monkeypatch)
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
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args, **_kwargs: protection_success(),
    )
    def capture_delivery_close(
        _config: DailyPremarketConfig,
        market: str,
        trading_date: str,
        **_kwargs: object,
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
        "broker": {"CN": "eastmoney", "HK": "phillips", "US": "futu"}[market],
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
    patch_controller_quote(monkeypatch)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda _config, market, day, **_kwargs: calls.append((market, day))
        or protection_success(),
    )
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("calendar offline")
        ),
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


def test_run_stop_returns_uncertain_upgrade_and_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    expected = {"status": "uncertain", "submitted_count": 0}
    notifications: list[tuple[str, str, object]] = []

    class Orders:
        closed = False

        def close(self) -> None:
            self.closed = True

    orders = Orders()
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args: orders)
    monkeypatch.setattr(
        controller, "execute_trend_review_stop", lambda **_kwargs: expected
    )
    monkeypatch.setattr(
        controller,
        "_notify_feishu_once",
        lambda title, message, key: notifications.append((title, message, key))
        or True,
    )

    assert controller._run_stop(
        config,
        "CN",
        {
            "symbol": "600001",
            "trading_date": "2026-07-20",
            "event_id": "protection-1",
            "occurred_at": NOW.isoformat(),
        },
    ) is expected
    assert orders.closed is True
    assert len(notifications) == 1
    assert notifications[0][2][3:5] == ("protection_sell", "uncertain")


def test_default_protection_loader_gates_each_new_account_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    events: list[object] = []
    quotes: list[object] = []

    class Quote:
        closed = False

        def get_trading_days(self, **kwargs: object) -> list[str]:
            events.append(("gate", kwargs))
            if len(quotes) == 2:
                raise controller.FutuQuoteError("quote protocol offline")
            return ["2026-07-20"]

        def close(self) -> None:
            self.closed = True
            events.append("close")

    def quote_factory(**_kwargs: object) -> object:
        quote = Quote()
        quotes.append(quote)
        return quote

    def load_account(**_kwargs: object) -> object:
        events.append("load")
        return SimpleNamespace(positions=())

    def watch(*, account_loader: Callable[..., object], **_kwargs: object) -> object:
        account_loader(
            config.portfolio,
            expected_date="2026-07-20",
            timezone=controller.TIMEZONES["CN"],
        )
        return account_loader(
            config.portfolio,
            expected_date="2026-07-20",
            timezone=controller.TIMEZONES["CN"],
        )

    monkeypatch.setattr(controller, "FutuQuoteClient", quote_factory)
    monkeypatch.setattr(controller, "load_futu_simulate_trend_account", load_account)
    monkeypatch.setattr(controller, "watch_a_share_protection", watch)

    with pytest.raises(controller.FutuQuoteError, match="quote protocol offline"):
        controller._run_protection_pass(config, "CN", "2026-07-20")

    gate_calls = [event[1] for event in events if isinstance(event, tuple)]
    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "gate",
        "close",
        "load",
        "gate",
        "close",
    ]
    assert all(call["use_cache"] is False for call in gate_calls)
    assert len(quotes) == 2
    assert all(quote.closed for quote in quotes)


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
        lambda *_args, **_kwargs: SimpleNamespace(
            status="abnormal", exception_count=1, unknown_quote_count=0
        ),
    )
    allow_flags: list[bool] = []

    def execute(
        *_args: object,
        allow_new_buys: bool = True,
        **_kwargs: object,
    ) -> dict[str, object]:
        allow_flags.append(allow_new_buys)
        return {"status": "unchanged", "submitted_count": 0}

    monkeypatch.setattr(controller, "execute_simulated_trend_report", execute)

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


def test_controller_close_capture_borrows_readers_without_closing_or_recreating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    captured: list[dict[str, object]] = []

    class Quote:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Account:
        closed = False

        def account_snapshot(self) -> dict[str, object]:
            return {"acc_id": 101, "positions": []}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def close(self) -> None:
            self.closed = True

    quote = Quote()
    account = Account()
    monkeypatch.setattr(
        controller,
        "FutuQuoteClient",
        lambda **_kwargs: pytest.fail("borrowed close capture opened quote"),
    )
    monkeypatch.setattr(
        controller,
        "_new_order_client",
        lambda *_args, **_kwargs: pytest.fail(
            "borrowed close capture opened order wrapper"
        ),
    )
    monkeypatch.setattr(controller, "benchmark_fact", lambda client, *_args: client)
    monkeypatch.setattr(
        controller,
        "capture_trend_review_close",
        lambda **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(
        controller, "build_trend_review_projection", lambda *_args: None
    )

    controller._capture_close(
        config,
        "CN",
        "2026-07-17",
        quote_client=quote,
        account_client=account,
    )

    assert captured[0]["simulate_snapshot"] == {"acc_id": 101, "positions": []}
    assert captured[0]["orders"] == []
    assert captured[0]["benchmark"] is quote
    assert quote.closed is False
    assert account.closed is False


def test_controller_close_capture_rebuilds_failed_shared_account_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    report_path, report = write_report(config)
    report["metadata"]["simulate_acc_id"] = 101  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    cycle = replace(active_cn_cycle(), session="closed", market_open=False)
    current = NOW
    times = iter((NOW, NOW, NOW.replace(second=5), NOW.replace(second=10)))
    account_clients: list[object] = []
    gate_calls: list[dict[str, object]] = []

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            gate_calls.append(kwargs)
            return ["2026-07-20"]

        def close(self) -> None:
            pass

    class Account:
        close_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            if self is account_clients[0]:
                raise FutuOrderExecutionError(
                    "shared account read failed", error_type="query_failed"
                )
            return {"acc_id": 101, "positions": []}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def close(self) -> None:
            self.close_calls += 1
            if self is account_clients[0] and current == NOW:
                raise RuntimeError("stale account close failed")

    def account_factory(**_kwargs: object) -> object:
        account = Account()
        account_clients.append(account)
        return account

    def now_fn() -> datetime:
        nonlocal current
        current = next(times)
        return current

    def capture(**kwargs: object) -> None:
        fact = controller._close_path(config, "CN", cycle.as_of_date)
        fact.parent.mkdir(parents=True, exist_ok=True)
        fact.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(controller, "FutuQuoteClient", lambda **_kwargs: Quote())
    monkeypatch.setattr(
        controller, "FutuSimulateOrderExecutionClient", account_factory
    )
    monkeypatch.setattr(
        controller,
        "_derive_cycle",
        lambda _config, _market, _now, **_kwargs: cycle,
    )
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda _config, _cycle, _now, **_kwargs: cycle,
    )
    monkeypatch.setattr(
        controller,
        "_run_protection_pass",
        lambda *_args, **_kwargs: protection_success(),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(
        controller, "_run_cycle_statistics", lambda *_args: {"status": "completed"}
    )
    monkeypatch.setattr(controller, "_notify_once", lambda *_args: True)
    monkeypatch.setattr(controller, "benchmark_fact", lambda *_args: {})
    monkeypatch.setattr(controller, "capture_trend_review_close", capture)
    monkeypatch.setattr(
        controller, "build_trend_review_projection", lambda *_args: None
    )
    blockers: list[object] = []

    class StopController(RuntimeError):
        pass

    def stop_after_second_attempt(_seconds: float) -> None:
        status = json.loads(
            (config.data_dir / "trend_controller/CN/status.json").read_text(
                encoding="utf-8"
            )
        )
        blockers.append(status["blocker"])
        if len(blockers) == 3:
            raise StopController

    with pytest.raises(StopController):
        run_trend_market_controller(
            config, "CN", now_fn=now_fn, sleep_fn=stop_after_second_attempt
        )

    assert blockers == [
        "shared account read failed",
        "shared account read failed",
        None,
    ]
    assert len(account_clients) == 2
    assert [client.close_calls for client in account_clients] == [1, 1]
    assert len(gate_calls) == 2


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


def test_scheduled_sell_all_lock_time_zero_writes_terminal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host="executor",
    )
    report_path, report = write_v2_controller_report(
        config,
        positions=[{"code": "SH.600001", "qty": "100", "can_sell_qty": "100"}],
        actions=[{"action": "SELL_ALL", "symbol": "600001", "reason": "trend_exit"}],
    )
    report["account"]["positions"] = [{
        "symbol": "600001",
        "name": "600001",
        "asset_class": "stock",
        "quantity": "100",
        "market_value": "1000",
        "avg_cost_price": "10",
    }]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Client:
        def __init__(self) -> None:
            self.snapshot_calls = 0
            self.requests: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            positions = (
                [{"code": "SH.600001", "qty": "100", "can_sell_qty": "100"}]
                if self.snapshot_calls <= 2
                else []
            )
            return {
                "acc_id": 101,
                "net_value": "100000",
                "available_cash": "100000",
                "positions": positions,
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            pytest.fail("lock-time zero-position sell submitted an order")

    client = Client()
    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        _report_hash(report),
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW,
        order_client=client,
        scheduled=True,
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.data_dir.glob(
            "trend_review/ledgers/CN/actions/2026-07-20/*/*.json"
        )
    ]
    snapshots_after_first = client.snapshot_calls
    cycle = ControllerCycle(
        market="CN",
        as_of_date=str(report["as_of_date"]),
        execution_date="2026-07-20",
        report_run_date=str(report["generated_at"])[:10],
        session="execution",
        market_open=True,
        next_check_at=NOW,
    )
    replay = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        _report_hash(report),
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW + timedelta(minutes=1),
        order_client=client,
        scheduled=True,
    )

    assert (
        first["status"],
        first["submitted_count"],
        len(client.requests),
        any(
            event.get("status") == "incomplete"
            and event.get("reason") == "position_zero_confirmed"
            for event in events
        ),
        controller._request_completion_path(
            config, "CN", "2026-07-20", str(first["execution_id"])
        ).exists(),
        controller._execution_completed(config, cycle, execution_id=str(first["execution_id"])),
        replay["status"],
        client.snapshot_calls,
    ) == ("unchanged", 0, 0, True, True, True, "reconciled", snapshots_after_first)


def test_global_execution_noop_protocol_is_removed() -> None:
    assert not hasattr(controller, "_record_execution_noop")
    assert not hasattr(controller, "_execution_noop_path")


def test_relative_rotation_runs_after_ordinary_actions_and_merges_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "strategy_judgments": {
            "formal_actions": [],
            "simulate_rotation_pairs": [{"buy_futu_symbol": "SH.STRONG"}],
            "real_rotation_pairs": [{"buy_futu_symbol": "SH.REAL"}],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls: list[str] = []

    class Client:
        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            assert symbols == ["SH.STRONG"]
            return {"SH.STRONG": SimpleNamespace(last_price=Decimal("10"))}

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **_kwargs: calls.append("ordinary") or {
            "status": "unchanged", "submitted_count": 0, "artifact_paths": ["ordinary"],
        },
    )
    monkeypatch.setattr(
        controller,
        "execute_relative_rotations",
        lambda **_kwargs: calls.append("rotation") or {
            "status": "submitted", "submitted_count": 2, "artifact_paths": ["rotation"],
        },
    )

    result = controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=Quote()
    )

    assert calls == ["ordinary", "rotation"]
    assert result["submitted_count"] == 2
    assert result["artifact_paths"] == ["ordinary", "rotation"]


def test_v2_unfinished_ordinary_phase_stays_unfinished_without_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = valid_cn_report(
        as_of_date="2026-07-17", execution_date="2026-07-20", buy=True,
    )
    report["allocation"] = {"version": 2, "markets": {"CN": {"position_limit": 10}}}
    report["strategy_snapshot"] = {
        "strategy_id": "trend_animals_warm_to_hot/CN/v15",
        "strategy_version": "v15",
    }
    report["strategy_judgments"]["simulate_rotation_pairs"] = []
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class Client:
        def account_snapshot(self) -> dict[str, object]:
            return {"acc_id": 123, "positions": []}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(
        controller, "record_trend_review_missed_buys", lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        controller, "_new_order_client", lambda *_args, **_kwargs: Client(),
    )
    monkeypatch.setattr(
        controller, "freeze_simulated_buy_fifo", lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **_kwargs: {
            "status": "submitted", "submitted_count": 1, "artifact_paths": [],
        },
    )

    result = controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=Quote(),
        scheduled=False, execution_id="manual-execution-1", account_id=123,
    )

    assert result["status"] == "submitted"
    assert controller._request_result_is_terminal(result) is False


def test_manual_simulation_request_binds_exact_report_sha_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report = {"schema_version": 1, "execution_date": "2026-07-20"}
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)
    monkeypatch.setattr(controller, "require_trend_review_config", lambda *_args: 123)
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {"status": "complete", "submitted_count": 1},
    )

    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="ray",
        reason="manual retry",
        now=NOW,
    )

    request_path = Path(str(result["request_path"]))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request == {
        "schema_version": "open_trader.trend_controller.simulation_request.v1",
        "execution_id": result["execution_id"],
        "account_type": "futu_simulate",
        "account_id": 123,
        "market": "CN",
        "execution_date": "2026-07-20",
        "report_path": str(report_path),
        "report_sha256": report_sha,
        "actor": "ray",
        "reason": "manual retry",
        "requested_at": "2026-07-20T09:31:00+08:00",
    }
    assert result["report_sha256"] == report_sha


def test_manual_simulation_request_replays_one_identity_without_reexecuting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report = {"schema_version": 1, "execution_date": "2026-07-20"}
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)
    calls: list[str] = []
    monkeypatch.setattr(controller, "require_trend_review_config", lambda *_args: 123)
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: calls.append("execute") or {
            "status": "complete", "submitted_count": 1,
        },
    )

    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="ray",
        reason="manual retry",
        now=NOW,
    )
    second = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="ray",
        reason="manual retry",
        now=NOW + timedelta(minutes=5),
    )

    request_path = Path(str(first["request_path"]))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert first["request_reused"] is False
    assert second["request_reused"] is True
    assert second["status"] == "reconciled"
    assert second["execution_id"] == first["execution_id"]
    assert second["request_path"] == first["request_path"]
    assert request["requested_at"] == "2026-07-20T09:31:00+08:00"
    assert calls == ["execute"]


def test_simulation_completion_rejects_mismatched_stored_result_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host="executor",
    )
    report = valid_cn_report(as_of_date="2026-07-19", execution_date="2026-07-20")
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report["metadata"]["simulate_acc_id"] = 123  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")

    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="ray",
        reason="manual identity check",
        now=NOW,
    )
    completion_path = controller._request_completion_path(
        config, "CN", "2026-07-20", str(first["execution_id"]),
    )
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    completion["result"].update({
        "market": "HK",
        "account_id": 999,
        "request_path": str(tmp_path / "other-request.json"),
        "execution_id": "other-execution",
        "report_sha256": "0" * 64,
    })
    completion_path.write_text(json.dumps(completion), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid simulation request completion"):
        controller.execute_simulated_trend_report(
            config,
            "CN",
            "2026-07-20",
            report_sha,
            actor="ray",
            reason="manual identity check",
            now=NOW + timedelta(minutes=5),
        )


def test_manual_simulation_request_reenters_until_phase_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report = {"schema_version": 1, "execution_date": "2026-07-20"}
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)
    calls: list[str] = []
    monkeypatch.setattr(controller, "require_trend_review_config", lambda *_args: 123)
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("execute")
        return (
            {"status": "submitted", "submitted_count": 1}
            if len(calls) == 1
            else {"status": "complete", "submitted_count": 0}
        )

    monkeypatch.setattr(controller, "_execute_locked_report", execute)

    first = controller.execute_simulated_trend_report(
        config, "CN", "2026-07-20", report_sha,
        actor="ray", reason="manual retry", now=NOW,
    )
    completion_path = controller._request_completion_path(
        config, "CN", "2026-07-20", str(first["execution_id"]),
    )
    assert first["status"] == "submitted"
    assert not completion_path.exists()

    second = controller.execute_simulated_trend_report(
        config, "CN", "2026-07-20", report_sha,
        actor="ray", reason="manual retry", now=NOW + timedelta(minutes=5),
    )
    replay = controller.execute_simulated_trend_report(
        config, "CN", "2026-07-20", report_sha,
        actor="ray", reason="manual retry", now=NOW + timedelta(minutes=10),
    )

    assert second["status"] == "complete"
    assert second["request_reused"] is True
    assert replay["status"] == "reconciled"
    assert calls == ["execute", "execute"]
    assert completion_path.exists()


def test_manual_simulation_request_replays_rejection_and_distinct_reason_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report = {"schema_version": 1, "execution_date": "2026-07-20"}
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)
    calls: list[str] = []
    monkeypatch.setattr(controller, "require_trend_review_config", lambda *_args: 123)
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("execute")
        return {
            "status": "terminal_rejected",
            "submitted_count": 1,
            "terminal_rejected": True,
        }

    monkeypatch.setattr(controller, "_execute_locked_report", execute)

    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="ray",
        reason="manual retry",
        now=NOW,
    )
    replay = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="ray",
        reason="manual retry",
        now=NOW + timedelta(minutes=5),
    )
    distinct = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="ray",
        reason="manual retry after reconciliation",
        now=NOW + timedelta(minutes=10),
    )

    assert first["terminal_rejected"] is True
    assert replay["status"] == "reconciled"
    assert replay["execution_id"] == first["execution_id"]
    assert distinct["execution_id"] != first["execution_id"]
    assert calls == ["execute", "execute"]


def test_scheduled_zero_fill_replay_passes_stable_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report = {"schema_version": 1, "execution_date": "2026-07-20"}
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "require_trend_review_config", lambda *_args: 123)
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(controller, "_request_result_is_terminal", lambda *_args: False)

    def execute(*_args: object, **kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {
            "status": "terminal_rejected",
            "submitted_count": 0,
            "terminal_rejected": True,
        }

    monkeypatch.setattr(controller, "_execute_locked_report", execute)

    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW,
        scheduled=True,
    )
    second = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW + timedelta(minutes=5),
        scheduled=True,
    )

    assert first["execution_id"] == second["execution_id"]
    assert len(calls) == 2
    assert all(call["execution_id"] == first["execution_id"] for call in calls)
    assert all(call["request_path"] == Path(str(first["request_path"])) for call in calls)
    assert all(call["account_id"] == 123 for call in calls)


def test_scheduled_terminal_rejection_replay_recovers_notification_via_public_order_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
        notifiers=("macos",),
    )
    report = valid_cn_report(
        as_of_date="2026-07-19", execution_date="2026-07-20", buy=True,
    )
    report["metadata"]["simulate_acc_id"] = 123  # type: ignore[index]
    roots = {
        market: {
            "stock": {
                "asset": stock,
                "tm_id": index * 10,
                "as_of_date": "2026-08-03",
                "global_strength": stock_strength,
            },
            "etf": {
                "asset": etf,
                "tm_id": index * 10 + 1,
                "as_of_date": "2026-08-03",
                "global_strength": etf_strength,
            },
        }
        for index, (market, stock, etf, stock_strength, etf_strength) in enumerate(
            (
                ("CN", "A股", "ETF基金", "90", "80"),
                ("HK", "港股", "香港ETF", "70", "60"),
                ("US", "美股", "美国ETF", "50", "40"),
            ),
            1,
        )
    }
    allocation_snapshot = build_allocation_snapshot(
        allocation_date="2026-08-03",
        generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40,
        roots=roots,
        previous=None,
        version=2,
    )
    daily_path = config.data_dir / "trend_allocation/daily/2026-08-03.json"
    daily_path.parent.mkdir(parents=True)
    allocation_body = (
        json.dumps(
            allocation_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    daily_path.write_text(allocation_body, encoding="utf-8")
    report["allocation"] = {
        "version": allocation_snapshot["version"],
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": hashlib.sha256(allocation_body.encode()).hexdigest(),
        "allocation_date": "2026-08-03",
        "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
        "roots": allocation_snapshot["roots"],
        "markets": allocation_snapshot["markets"],
    }
    report["strategy_snapshot"] = a_share_trend.live_trend_strategy_snapshot(
        "CN",
        "test-sha",
        (622466, 697199),
        allocation={
            "daily_path": report["allocation"]["daily_path"],  # type: ignore[index]
            "sha256": report["allocation"]["sha256"],  # type: ignore[index]
            "snapshot": allocation_snapshot,
        },
    )
    report["risk_summary"] = {"normal_cost_rate": "0.001"}
    report["strategy_judgments"]["formal_actions"][0].update({  # type: ignore[index]
        "estimated_initial_line": "9",
        "normal_cost": "4",
        "planned_stop_risk": "404",
        "planned_stop_risk_pct": "0.00404",
    })
    report["strategy_judgments"]["formal_actions"][0].update({  # type: ignore[index]
        "close": "10",
        "atr": "0.5",
        "executable": True,
    })
    report["signal_snapshots"] = {
        "candidates": [{"symbol": "600001", "close": "10", "atr": "0.5"}],
    }
    report["strategy_judgments"].update({  # type: ignore[union-attr]
        "simulate_rotation_pairs": [],
        "real_rotation_pairs": [],
        "simulate_rotation_comparisons": [],
        "real_rotation_comparisons": [],
        "simulated_buy_fifo": [
            {
                "source": "formal",
                "symbol": "600001",
                "futu_symbol": "SH.600001",
                "owners": [{
                    "source": "formal",
                    "action_index": 0,
                    "symbol": "600001",
                    "futu_symbol": "SH.600001",
                }],
            }
        ],
        "planned_new_seats": 20,
    })
    report["plan_availability"] = {
        "simulated_account": {
            "status": "available",
            "reason": "",
            "executable": True,
        },
        "real_account": {
            "status": "unavailable",
            "reason": "real account is informational",
            "executable": False,
        },
    }
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            value = datetime.fromisoformat("2026-07-20T09:31:00+08:00")
            return value if tz is None else value.astimezone(tz)  # type: ignore[arg-type]

    class TerminalRejectingClient:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []
            self.place_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 123,
                "net_value": "100000",
                "cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.place_calls += 1
            order = {
                **request,
                "order_id": "BROKER-1",
                "code": request["futu_code"],
                "trd_side": str(request["side"]).upper(),
                "dealt_qty": "0",
                "order_status": "REJECTED",
            }
            self.orders.append(order)
            return {
                "futu_order_id": "BROKER-1",
                "status": "REJECTED",
                "order_status": "REJECTED",
                "dealt_qty": "0",
            }

        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    client = TerminalRejectingClient()
    monkeypatch.setattr(controller, "datetime", FrozenDateTime)

    class CrashNotifier(MacOSNotifier):
        def notify(self, _title: str, _message: str) -> None:
            raise KeyboardInterrupt("simulated crash after completion")

    monkeypatch.setattr(controller, "build_notifier", lambda _config: CrashNotifier())
    with pytest.raises(KeyboardInterrupt, match="simulated crash"):
        controller.execute_simulated_trend_report(
            config,
            "CN",
            "2026-07-20",
            report_sha,
            actor="trend-market-controller",
            reason="scheduled execution",
            now=NOW,
            quote_client=Quote(),
            order_client=client,
            scheduled=True,
        )
    request_path = next(
        (config.data_dir / "trend_controller/CN/simulation_requests/2026-07-20").glob(
            "*.json"
        )
    )
    completion_path = controller._request_completion_path(
        config, "CN", "2026-07-20", request_path.stem,
    )
    completion_bytes = completion_path.read_bytes()
    completion_paths = list(completion_path.parent.glob("*.json"))
    notification_paths = list(
        (config.data_dir / "trend_controller/CN/notifications/2026-07-20").glob(
            "*.json"
        )
    )
    assert (client.place_calls, len(completion_paths), notification_paths) == (
        1, 1, [],
    )

    recovered = RecordingMacOS()
    monkeypatch.setattr(controller, "build_notifier", lambda _config: recovered)
    replay = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW + timedelta(minutes=5),
        quote_client=Quote(),
        order_client=client,
        scheduled=True,
    )
    further_replay = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW + timedelta(minutes=10),
        quote_client=Quote(),
        order_client=client,
        scheduled=True,
    )

    assert (
        replay["status"],
        further_replay["status"],
        client.place_calls,
        len(recovered.messages),
        completion_path.read_bytes(),
    ) == (
        "reconciled", "reconciled", 1, 1, completion_bytes,
    )


def test_manual_exact_sha_execution_keeps_scheduled_batch_and_passes_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "manual-report.json"
    report = valid_cn_report(as_of_date="2026-07-19", execution_date="2026-07-20", buy=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    scheduled_batch_path = controller._batch_path(config, "CN", "2026-07-20")
    scheduled_batch_path.parent.mkdir(parents=True, exist_ok=True)
    scheduled_batch = {"schema_version": "scheduled-sentinel"}
    scheduled_batch_path.write_text(json.dumps(scheduled_batch), encoding="utf-8")
    request_path = config.data_dir / "trend_controller/CN/simulation_requests/2026-07-20/manual.json"
    execution_id = "manual-execution-1"
    calls: list[dict[str, object]] = []

    class Quote:
        def close(self) -> None:
            pass

    class Client:
        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: True)

    def execute_open(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "complete", "submitted_count": 1, "artifact_paths": []}

    monkeypatch.setattr(controller, "execute_trend_review_open", execute_open)
    result = controller._execute_locked_report(
        config,
        "CN",
        "2026-07-20",
        report_path,
        report,
        quote_client=Quote(),
        scheduled=False,
        execution_id=execution_id,
        request_path=request_path,
        account_id=123,
    )

    manual_batch_path = controller._request_batch_path(
        config, "CN", "2026-07-20", execution_id
    )
    manual_batch = json.loads(manual_batch_path.read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert json.loads(scheduled_batch_path.read_text(encoding="utf-8")) == scheduled_batch
    assert manual_batch["execution_id"] == execution_id
    assert manual_batch["request_path"] == str(request_path)
    assert manual_batch["account_id"] == 123
    assert calls and calls[0]["execution_id"] == execution_id
    assert calls[0]["request_path"] == str(request_path)
    assert calls[0]["account_id"] == 123


def test_v2_execution_keeps_report_fifo_and_seats_when_live_positions_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "frozen-report.json"
    report = valid_cn_report(as_of_date="2026-07-19", execution_date="2026-07-20", buy=True)
    report["strategy_snapshot"] = {
        "strategy_id": "trend_animals_warm_to_hot/CN/v15",
        "strategy_version": "v15",
    }
    report["allocation"] = {
        "version": 2,
        "markets": {"CN": {"position_limit": 10}},
    }
    report["strategy_judgments"]["formal_actions"] = [
        {
            "action": "BUY",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 100,
            "atr": "0.5",
        },
        {
            "action": "BUY",
            "symbol": "600002",
            "futu_symbol": "SH.600002",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 100,
            "atr": "0.5",
        },
    ]
    report["strategy_judgments"]["simulated_buy_fifo"] = [
        {"source": "formal", "futu_symbol": "SH.600001", "symbol": "600001"},
        {"source": "formal", "futu_symbol": "SH.600002", "symbol": "600002"},
    ]
    report["strategy_judgments"]["planned_new_seats"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    buy_calls: list[str] = []

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Client:
        snapshot_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            positions = []
            if self.snapshot_calls > 1:
                positions = [
                    {"code": f"SH.LIVE{index}", "qty": "100"}
                    for index in range(99)
                ]
            return {"acc_id": 123, "positions": positions}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": []}

        def close(self) -> None:
            pass

    client = Client()
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: client)

    def execute_open(**kwargs: object) -> dict[str, object]:
        if not kwargs.get("include_buys"):
            return {"status": "complete", "submitted_count": 0, "artifact_paths": []}
        symbols = kwargs.get("buy_symbols")
        assert isinstance(symbols, tuple) and len(symbols) == 1
        code = symbols[0]
        buy_calls.append(code)
        rejected = code == "SH.600001"
        return {
            "status": "terminal_rejected" if rejected else "submitted",
            "submitted_count": 1,
            "artifact_paths": [],
            "terminal_rejected": rejected,
            "seat_consumed": not rejected,
            "seat_release_proven": rejected,
        }

    monkeypatch.setattr(controller, "execute_trend_review_open", execute_open)
    controller._execute_locked_report(
        config,
        "CN",
        "2026-07-20",
        report_path,
        report,
        quote_client=Quote(),
        scheduled=False,
        execution_id="manual-execution-1",
        request_path=tmp_path / "request.json",
        account_id=123,
    )

    assert buy_calls == ["SH.600001", "SH.600002"]


@pytest.mark.parametrize("head_state", ["pending", "held"])
def test_v2_frozen_fifo_head_consumes_seat_across_execution_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head_state: str,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "frozen-report.json"
    report = valid_cn_report(as_of_date="2026-07-19", execution_date="2026-07-20", buy=True)
    report["strategy_snapshot"] = {
        "strategy_id": "trend_animals_warm_to_hot/CN/v15",
        "strategy_version": "v15",
    }
    report["allocation"] = {
        "version": 2,
        "markets": {"CN": {"position_limit": 10}},
    }
    report["strategy_judgments"]["formal_actions"] = [
        {
            "action": "BUY", "symbol": "600001", "futu_symbol": "SH.600001",
            "global_strength": "95", "estimated_shares": 100,
            "lot_size": 100, "atr": "0.5",
        },
        {
            "action": "BUY", "symbol": "600002", "futu_symbol": "SH.600002",
            "global_strength": "90", "estimated_shares": 100,
            "lot_size": 100, "atr": "0.5",
        },
    ]
    report["strategy_judgments"]["simulated_buy_fifo"] = [
        {"source": "formal", "futu_symbol": "SH.600001", "symbol": "600001"},
        {"source": "formal", "futu_symbol": "SH.600002", "symbol": "600002"},
    ]
    report["strategy_judgments"]["planned_new_seats"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {symbol: SimpleNamespace(last_price=Decimal("10")) for symbol in symbols}

        def close(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []
            self.positions: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {"acc_id": 123, "positions": self.positions}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def close(self) -> None:
            pass

    client = Client()
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: client)
    place_calls: list[str] = []
    reconcile_calls: list[str] = []

    def execute_open(**kwargs: object) -> dict[str, object]:
        if kwargs.get("include_buys") is False:
            return {"status": "complete", "submitted_count": 0, "artifact_paths": []}
        code = str(tuple(kwargs["buy_symbols"])[0])
        if (
            any(
                str(order.get("code") or order.get("futu_code") or "").upper()
                == code
                for order in client.orders
            )
            or any(
                str(position.get("code") or position.get("futu_code") or "").upper()
                == code
                for position in client.positions
            )
        ):
            reconcile_calls.append(code)
            return {"status": "reconciled", "submitted_count": 0, "artifact_paths": []}
        place_calls.append(code)
        client.orders = [{
            "code": code,
            "futu_code": code,
            "trd_side": "BUY",
            "order_status": "SUBMITTED",
            "qty": "100",
            "dealt_qty": "0",
        }]
        if head_state == "held" and code == "SH.600001":
            client.positions = [{"code": code, "qty": "100"}]
        return {"status": "submitted", "submitted_count": 1, "artifact_paths": []}

    monkeypatch.setattr(controller, "execute_trend_review_open", execute_open)
    monkeypatch.setattr(
        controller,
        "execute_relative_rotations",
        lambda **_kwargs: {"status": "complete", "submitted_count": 0, "artifact_paths": []},
    )

    for _ in range(2):
        controller._execute_locked_report(
            config,
            "CN",
            "2026-07-20",
            report_path,
            report,
            quote_client=Quote(),
            scheduled=False,
            execution_id="manual-execution-1",
            request_path=tmp_path / "request.json",
            account_id=123,
        )

    assert place_calls == ["SH.600001"]
    assert reconcile_calls == ["SH.600001"]


def test_scheduled_simulation_replays_one_request_without_reexecuting_completed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report = {
        "schema_version": 1,
        "as_of_date": "2026-07-19",
        "execution_date": "2026-07-20",
    }
    report_path = config.reports_dir / "trend_a_share" / "2026-07-19.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)
    calls: list[str] = []
    monkeypatch.setattr(controller, "require_trend_review_config", lambda *_args: 123)
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: calls.append("execute") or {
            "status": "complete", "submitted_count": 1,
        },
    )
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: True)

    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW,
        scheduled=True,
    )
    second = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW + timedelta(minutes=5),
        scheduled=True,
    )

    request_path = Path(str(first["request_path"]))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert first["request_reused"] is False
    assert second["request_reused"] is True
    assert second["status"] == "reconciled"
    assert second["execution_id"] == first["execution_id"]
    assert second["request_path"] == first["request_path"]
    assert request["report_sha256"] == report_sha
    assert request["requested_at"] == "2026-07-20T09:31:00+08:00"
    assert calls == ["execute"]
    assert list(request_path.parent.glob("*.json")) == [request_path]


def test_mixed_v2_execution_stages_sells_before_formal_and_rotation_buys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "allocation": {"version": 2},
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY",
                "symbol": "600001",
                "futu_symbol": "SH.600001",
                "global_strength": "80",
                "estimated_shares": 100,
                "lot_size": 100,
                "atr": "0.5",
            }],
            "simulate_rotation_pairs": [{
                "pair_index": 0,
                "buy_futu_symbol": "SH.ROTATION",
                "buy_global_strength": "90",
            }],
        },
    }
    report["strategy_snapshot"] = {"strategy_version": "v15"}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls: list[tuple[str, object]] = []

    class Client:
        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **kwargs: calls.append(("ordinary", {
            "include_buys": kwargs.get("include_buys"),
            "include_sells": kwargs.get("include_sells"),
        })) or {
            "status": (
                "uncertain" if kwargs.get("include_sells") else "unchanged"
            ),
            "submitted_count": 0, "artifact_paths": [],
        },
    )
    monkeypatch.setattr(
        controller,
        "execute_relative_rotations",
        lambda **kwargs: calls.append(("rotation", kwargs.get("_phase"))) or {
            "status": (
                "pending" if kwargs.get("_phase") == "sell" else "complete"
            ),
            "submitted_count": 0, "artifact_paths": [],
        },
    )
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: False)

    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=Quote()
    )

    assert calls == [
        ("ordinary", {"include_buys": False, "include_sells": True}),
        ("rotation", "sell"),
        ("rotation", "buy"),
        ("ordinary", {"include_buys": True, "include_sells": False}),
    ]


def test_current_nominal_report_without_top_level_allocation_version_uses_staged_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "allocation": {"markets": {"CN": {"position_limit": 20}}},
        "strategy_snapshot": {"strategy_version": "v17"},
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY",
                "symbol": "600001",
                "futu_symbol": "SH.600001",
                "global_strength": "80",
                "estimated_shares": 100,
                "lot_size": 100,
                "atr": "0.5",
            }],
            "simulate_rotation_pairs": [{
                "pair_index": 0,
                "buy_futu_symbol": "SH.ROTATION",
                "buy_global_strength": "90",
            }],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls: list[tuple[str, object]] = []

    class Client:
        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **kwargs: calls.append(("ordinary", {
            "include_buys": kwargs.get("include_buys"),
            "include_sells": kwargs.get("include_sells"),
        })) or {
            "status": (
                "uncertain" if kwargs.get("include_sells") else "unchanged"
            ),
            "submitted_count": 0, "artifact_paths": [],
        },
    )
    monkeypatch.setattr(
        controller,
        "execute_relative_rotations",
        lambda **kwargs: calls.append(("rotation", kwargs.get("_phase"))) or {
            "status": (
                "pending" if kwargs.get("_phase") == "sell" else "complete"
            ),
            "submitted_count": 0, "artifact_paths": [],
        },
    )
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: False)

    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=Quote()
    )

    assert calls == [
        ("ordinary", {"include_buys": False, "include_sells": True}),
        ("rotation", "sell"),
    ]


@pytest.mark.parametrize(
    ("market", "version", "broker"),
    [("CN", "v17", "eastmoney"), ("HK", "v14", "phillips"), ("US", "v14", "futu")],
)
def test_current_nominal_report_without_top_level_version_requires_v2_plan_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    market: str,
    version: str,
    broker: str,
) -> None:
    config = controller_config(tmp_path)
    report = valid_cn_report(as_of_date="2026-07-17", execution_date="2026-07-20")
    report["metadata"] = {"market": market, "broker": broker}
    report["strategy_snapshot"] = {
        **report["strategy_snapshot"],
        "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
        "strategy_version": version,
    }
    report_path = controller._report_dir(config, market) / "2026-07-17.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(controller, "valid_frozen_report_contract", lambda _payload: True)

    assert controller._valid_report(
        config, market, "2026-07-20", report_path, report
    ) is False


def test_v2_formal_only_execution_uses_fifo_and_dynamic_position_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "allocation": {"version": 2, "markets": {"CN": {"position_limit": 20}}},
        "strategy_snapshot": {"strategy_version": "v15"},
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY",
                "symbol": "600001",
                "futu_symbol": "SH.600001",
                "global_strength": "90",
                "estimated_shares": 100,
                "lot_size": 100,
                "atr": "0.5",
            }],
            "simulate_rotation_pairs": [],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls: list[dict[str, object]] = []
    fifo_calls: list[dict[str, object]] = []
    quote_calls: list[tuple[str, ...]] = []

    class Client:
        def account_snapshot(self) -> dict[str, object]:
            return {
                "positions": [
                    {"code": f"SH.HOLD{index}", "qty": "100"}
                    for index in range(20)
                ],
            }

        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            quote_calls.append(tuple(symbols))
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(
        controller,
        "freeze_simulated_buy_fifo",
        lambda **kwargs: fifo_calls.append(kwargs) or [
            {"source": "formal", "futu_symbol": "SH.600001"},
        ],
    )
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **kwargs: calls.append(kwargs) or {
            "status": "unchanged", "submitted_count": 0, "artifact_paths": [],
        },
    )
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: True)

    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=Quote()
    )

    assert fifo_calls
    assert [bool(item.get("include_buys")) for item in calls] == [False]
    assert quote_calls[1] == ("SH.600001",)


def test_v2_mixed_execution_refreshes_formal_and_rotation_fifo_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "allocation": {"version": 2, "markets": {"CN": {"position_limit": 10}}},
        "strategy_snapshot": {"strategy_version": "v15"},
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY", "symbol": "600001", "futu_symbol": "SH.600001",
                "global_strength": "80", "estimated_shares": 100,
                "lot_size": 100, "atr": "0.5",
            }],
            "simulate_rotation_pairs": [{
                "pair_index": 0, "buy_futu_symbol": "SH.ROTATION",
                "buy_global_strength": "90",
            }],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    quote_calls: list[tuple[str, ...]] = []

    class Client:
        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            quote_calls.append(tuple(sorted(symbols)))
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(
        controller,
        "freeze_simulated_buy_fifo",
        lambda **_kwargs: [
            {"source": "formal", "futu_symbol": "SH.600001"},
            {"source": "rotation", "futu_symbol": "SH.ROTATION"},
        ],
    )
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **_kwargs: {
            "status": "unchanged", "submitted_count": 0, "artifact_paths": [],
        },
    )
    monkeypatch.setattr(
        controller,
        "execute_relative_rotations",
        lambda **kwargs: {
            "status": "complete", "submitted_count": 0, "artifact_paths": [],
        },
    )

    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=Quote()
    )

    assert quote_calls == [
        ("SH.600001", "SH.ROTATION"),
        ("SH.600001", "SH.ROTATION"),
    ]


def test_v2_empty_buy_batch_is_order_free_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "allocation": {"version": 2, "markets": {"CN": {"position_limit": 10}}},
        "strategy_snapshot": {"strategy_version": "v15"},
        "strategy_judgments": {
            "formal_actions": [], "simulate_rotation_pairs": [],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    submitted: list[object] = []

    class Client:
        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **kwargs: submitted.append(kwargs) or {
            "status": "complete", "submitted_count": 0, "artifact_paths": [],
        },
    )

    first = controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=None
    )
    second = controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=None
    )

    assert first["submitted_count"] == second["submitted_count"] == 0
    assert submitted == []


@pytest.mark.parametrize("head_status", ["SUBMITTED", "BROKER_UNKNOWN"])
def test_v2_fifo_head_consumes_one_seat_before_later_buy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    head_status: str,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "execution_date": "2026-07-20",
        "allocation": {"version": 2, "markets": {"CN": {"position_limit": 10}}},
        "strategy_snapshot": {"strategy_version": "v15"},
        "metadata": {"market": "CN"},
        "strategy_judgments": {
            "formal_actions": [
                {
                    "action": "BUY", "symbol": "600001", "futu_symbol": "SH.600001",
                    "global_strength": "95", "estimated_shares": 100,
                    "lot_size": 100, "target_amount": "1000", "atr": "0.5",
                },
                {
                    "action": "BUY", "symbol": "600002", "futu_symbol": "SH.600002",
                    "global_strength": "90", "estimated_shares": 100,
                    "lot_size": 100, "target_amount": "1000", "atr": "0.5",
                },
            ],
            "simulate_rotation_pairs": [{"pair_index": 0}],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_execution_completed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        controller,
        "freeze_simulated_buy_fifo",
        lambda **_kwargs: [
            {"source": "formal", "futu_symbol": "SH.600001"},
            {"source": "formal", "futu_symbol": "SH.600002"},
        ],
    )

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {"positions": []}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def close(self) -> None:
            pass

    client = Client()
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: client)
    buy_calls: list[str] = []
    head_calls = 0

    def execute_open(**kwargs: object) -> dict[str, object]:
        nonlocal head_calls
        if kwargs.get("include_buys") is False:
            return {"status": "unchanged", "submitted_count": 0, "artifact_paths": []}
        code = str(tuple(kwargs["buy_symbols"])[0])
        buy_calls.append(code)
        if code == "SH.600001":
            head_calls += 1
            client.orders[:] = [{
                "code": code, "trd_side": "BUY", "order_status": head_status,
            }]
            return {
                "status": "submitted", "submitted_count": 1,
                "artifact_paths": [], "buy_fifo_blocked": True,
            }
        assert head_calls == 1
        client.orders.append({
            "code": code, "trd_side": "BUY", "order_status": "SUBMITTED",
        })
        return {"status": "complete", "submitted_count": 1, "artifact_paths": []}

    monkeypatch.setattr(controller, "execute_trend_review_open", execute_open)
    monkeypatch.setattr(
        controller,
        "execute_relative_rotations",
        lambda **kwargs: (
            {"status": "complete", "submitted_count": 0, "artifact_paths": []}
            if kwargs.get("_phase") == "sell"
            else {"status": "unchanged", "submitted_count": 0, "artifact_paths": []}
        ),
    )
    quote = SimpleNamespace(
        get_snapshots=lambda symbols: {
            symbol: SimpleNamespace(last_price=Decimal("10")) for symbol in symbols
        }
    )

    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=quote
    )

    assert buy_calls == ["SH.600001", "SH.600002"]


@pytest.mark.parametrize(
    "first_order",
    [
        None,
        {
            "code": "SH.600001", "trd_side": "BUY",
            "order_status": "REJECTED", "dealt_qty": "0",
        },
    ],
    ids=["no_order", "zero_fill_reject"],
)
def test_v2_fifo_zero_fill_releases_seat_and_respects_frozen_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_order: dict[str, object] | None,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = {
        "as_of_date": "2026-07-17",
        "allocation": {"version": 2, "markets": {"CN": {"position_limit": 1}}},
        "strategy_snapshot": {"strategy_version": "v15"},
        "strategy_judgments": {
            "formal_actions": [
                {
                    "action": "BUY", "symbol": f"60000{index}",
                    "futu_symbol": f"SH.60000{index}",
                    "global_strength": str(100 - index),
                    "estimated_shares": 100, "lot_size": 100, "atr": "0.5",
                }
                for index in range(1, 4)
            ],
            "simulate_rotation_pairs": [],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(
        controller,
        "lock_trend_execution_batch",
        lambda *_args, **_kwargs: {
            "report_path": str(report_path), "report_sha256": _report_hash(report),
        },
    )
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {"positions": []}

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def close(self) -> None:
            pass

    client = Client()
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        controller,
        "freeze_simulated_buy_fifo",
        lambda **_kwargs: [
            {"source": "formal", "futu_symbol": "SH.600001"},
            {"source": "formal", "futu_symbol": "SH.600002"},
            {"source": "formal", "futu_symbol": "SH.600003"},
        ],
    )
    calls: list[str] = []

    def execute_open(**kwargs: object) -> dict[str, object]:
        if kwargs.get("include_buys") is False:
            return {"status": "uncertain", "submitted_count": 0, "artifact_paths": []}
        code = str(tuple(kwargs["buy_symbols"])[0])
        calls.append(code)
        if code == "SH.600001":
            client.orders[:] = [] if first_order is None else [dict(first_order)]
            return {
                "status": "uncertain" if first_order is None else "terminal_rejected",
                "submitted_count": 0,
                "artifact_paths": [],
                "terminal_rejected": first_order is not None,
                "seat_consumed": False,
            }
        client.orders[:] = [{
            "code": code, "trd_side": "BUY", "order_status": "SUBMITTED",
        }]
        return {"status": "submitted", "submitted_count": 1, "artifact_paths": []}

    monkeypatch.setattr(controller, "execute_trend_review_open", execute_open)
    quote = SimpleNamespace(
        get_snapshots=lambda symbols: {
            symbol: SimpleNamespace(last_price=Decimal("10")) for symbol in symbols
        }
    )

    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=quote
    )

    assert calls == (
        ["SH.600001"]
        if first_order is None
        else ["SH.600001", "SH.600002"]
    )


def test_public_v2_fifo_rejection_completes_request_with_broker_order_idempotency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
    )
    actions = [
        {
            "action": "BUY",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "global_strength": "95",
            "target_weight": "0.04",
            "target_amount": "4000",
            "planned_stop_risk": "400",
            "estimated_shares": 100,
            "lot_size": 100,
            "atr": "0.5",
        },
        {
            "action": "BUY",
            "symbol": "600002",
            "futu_symbol": "SH.600002",
            "global_strength": "90",
            "target_weight": "0.04",
            "target_amount": "4000",
            "planned_stop_risk": "400",
            "estimated_shares": 100,
            "lot_size": 100,
            "atr": "0.5",
        },
    ]
    report_path, report = write_v2_controller_report(config, actions=actions)
    report["metadata"]["simulate_acc_id"] = 123  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class OrderClient:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 123,
                "net_value": "100000",
                "cash": "100000",
                "available_cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            request = dict(request)
            code = str(request["futu_code"])
            rejected = code == "SH.600001"
            status = "REJECTED" if rejected else "SUBMITTED"
            self.requests.append(request)
            order = {
                **request,
                "order_id": f"BROKER-{len(self.requests)}",
                "code": code,
                "trd_side": "BUY",
                "dealt_qty": "0",
                "order_status": status,
            }
            self.orders.append(order)
            return {
                "futu_order_id": order["order_id"],
                "status": status,
                "order_status": status,
                "dealt_qty": "0",
            }

        def close(self) -> None:
            pass

    client = OrderClient()
    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="manual FIFO execution",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )
    completion_path = controller._request_completion_path(
        config, "CN", "2026-07-20", str(first["execution_id"])
    )
    replay = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="manual FIFO execution",
        now=NOW + timedelta(minutes=1),
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )

    assert (
        first.get("status"),
        first["terminal_rejected"],
        first["submitted_count"],
        [request["futu_code"] for request in client.requests],
        [order["order_status"] for order in client.orders],
        completion_path.exists(),
        replay["status"],
        replay["terminal_rejected"],
        replay["request_reused"],
        len(client.requests),
    ) == (
        "submitted",
        True,
        1,
        ["SH.600001", "SH.600002"],
        ["REJECTED", "SUBMITTED"],
        True,
        "terminal_rejected",
        True,
        True,
        2,
    )


def test_v2_fifo_overlap_uses_one_buy_and_closes_both_logical_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    pair = {
        "pair_index": 0,
        "sell_symbol": "WEAK",
        "sell_futu_symbol": "SH.WEAK",
        "buy_symbol": "600006",
        "buy_futu_symbol": "SH.600006",
        "buy_global_strength": "90",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 400,
        "lot_size": 100,
        "atr": "0.5",
        "reason": "relative_rotation",
        "execution_date": "2026-07-20",
        "execution_mode": "automatic",
    }
    report = {
        "as_of_date": "2026-07-17",
        "generated_at": "2026-07-17T18:00:00+08:00",
        "execution_date": "2026-07-20",
        "allocation": {"version": 2, "markets": {"CN": {"position_limit": 10}}},
        "metadata": {
            "market": "CN", "broker": "eastmoney",
            "price_fx_to_account_currency": "1", "simulate_acc_id": 101,
        },
        "risk_summary": {
            "normal_cost_rate": "0.001", "portfolio_remaining_risk": "4000",
        },
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/CN/v15",
            "strategy_version": "v15",
        },
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY", "symbol": "600006", "futu_symbol": "SH.600006",
                "global_strength": "90", "target_weight": "0.04",
                "lot_size": 100, "estimated_shares": 400,
                "target_amount": "4000", "planned_stop_risk": "1000", "atr": "0.5",
            }],
            "simulate_rotation_pairs": [pair],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(controller, "record_trend_review_missed_buys", lambda **_kwargs: 0)
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))

    class FilledClient:
        def __init__(self) -> None:
            self.cash = Decimal("0")
            self.positions = [
                {"code": "SH.WEAK", "qty": "1000", "can_sell_qty": "1000", "market_val": "7000"},
                *[
                    {"code": f"SH.HOLD{index}", "qty": "100", "can_sell_qty": "100", "market_val": "1000"}
                    for index in range(1, 10)
                ],
            ]
            self.requests: list[dict[str, object]] = []
            self.orders: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101, "net_value": "100000", "cash": str(self.cash),
                "available_cash": str(self.cash), "positions": self.positions,
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(request)
            order_id = f"SIM-{len(self.requests)}"
            qty = Decimal(str(request["qty"]))
            code = str(request["futu_code"])
            side = str(request["side"]).upper()
            order = {
                **request,
                "order_id": order_id,
                "code": code,
                "trd_side": side,
                "qty": str(request["qty"]),
                "dealt_qty": str(request["qty"]),
                "dealt_avg_price": "10",
                "order_status": "FILLED_ALL",
            }
            self.orders.append(order)
            if side == "SELL":
                self.positions[:] = [item for item in self.positions if item["code"] != code]
                self.cash += Decimal("7000")
            else:
                self.positions.append({"code": code, "qty": str(request["qty"])})
                self.cash -= qty * Decimal("10")
            return {"futu_order_id": order_id, "status": "submitted"}

        def close(self) -> None:
            pass

    client = FilledClient()
    monkeypatch.setattr(controller, "_new_order_client", lambda *_args, **_kwargs: client)
    quote = SimpleNamespace(
        get_snapshots=lambda symbols: {
            symbol: SimpleNamespace(last_price=Decimal("10")) for symbol in symbols
        }
    )

    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=quote
    )
    controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=quote
    )

    buy_orders = [order for order in client.orders if order["trd_side"] == "BUY"]
    assert len(buy_orders) == 1
    action_root = (
        config.data_dir / "trend_review/ledgers/CN/actions/2026-07-20"
        / trend_action_key("CN", "2026-07-20", "SH.600006", "buy")
    )
    formal_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in action_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("status") == "filled"
        and not json.loads(path.read_text(encoding="utf-8")).get("pair_key")
    ]
    rotation_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in action_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("status") == "filled"
        and json.loads(path.read_text(encoding="utf-8")).get("pair_key")
    ]
    assert len(formal_events) == 1
    assert len(rotation_events) == 1
    assert formal_events[0]["order_ids"] == rotation_events[0]["order_ids"]
    assert len(list(config.data_dir.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/buy-filled.json"
    ))) == 1
    assert len(list(config.data_dir.glob(
        "trend_review/ledgers/CN/rotations/2026-07-20/*/terminal.json"
    ))) == 1
    assert controller._execution_completed(
        config,
        ControllerCycle(
            market="CN", as_of_date="2026-07-17", execution_date="2026-07-20",
            report_run_date="2026-07-17", session="execution", market_open=True,
            next_check_at=NOW,
        ),
    ) is True


def test_current_execution_submits_audit_only_missing_atr_buy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    action = {
        "action": "BUY",
        "symbol": "600001",
        "futu_symbol": "SH.600001",
        "global_strength": "100",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 400,
        "lot_size": 100,
        "atr": "0",
        "estimated_initial_line": "0",
        "normal_cost": "4",
        "planned_stop_risk": "0",
        "planned_stop_risk_pct": "0",
        "sizing_note": "计划止损风险仅审计，不参与买入数量",
        "executable": True,
    }
    report_path, report = write_v2_controller_report(config, actions=[action])
    formal_action = report["strategy_judgments"]["formal_actions"][0]
    formal_action.update(
        {
            "atr": "0",
            "estimated_initial_line": "0",
            "normal_cost": "4",
            "planned_stop_risk": "0",
            "planned_stop_risk_pct": "0",
            "sizing_note": "计划止损风险仅审计，不参与买入数量",
            "executable": True,
        }
    )
    candidate = next(
        item
        for item in report["signal_snapshots"]["candidates"]
        if item["symbol"] == "600001"
    )
    candidate.pop("atr", None)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []
            self.requests: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "available_cash": "100000",
                "positions": [],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            order = {
                **request,
                "order_id": "SIM-1",
                "futu_order_id": "SIM-1",
                "code": request["futu_code"],
                "trd_side": request["side"],
                "order_status": "FILLED_ALL",
                "status": "FILLED_ALL",
                "dealt_qty": request["qty"],
                "dealt_avg_price": "10",
                "account_id": 101,
            }
            self.orders.append(order)
            return {
                "futu_order_id": "SIM-1",
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }

        def close(self) -> None:
            pass

    client = Client()
    controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="current audit-only missing ATR buy",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )

    assert (
        [
            (str(item["side"]).upper(), item["futu_code"], item["qty"])
            for item in client.requests
        ],
        (config.data_dir / "trend_a_share/protection_state.json").exists(),
    ) == ([
        ("BUY", "SH.600001", "400"),
    ], False)


def test_current_execution_sells_then_replacement_then_normal_with_live_rank_seats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
        notifiers=("macos",),
    )
    positions = [
        {
            "symbol": "WEAK" if index == 0 else f"HOLD{index}",
            "futu_symbol": "SH.WEAK" if index == 0 else f"SH.HOLD{index}",
            "name": "Weak" if index == 0 else f"Hold {index}",
            "asset_class": "stock",
            "quantity": "1000" if index == 0 else "100",
            "market_value": "7000" if index == 0 else "1000",
            "avg_cost_price": "10",
        }
        for index in range(13)
    ]
    actions = [
        {
            "action": "BUY",
            "symbol": "000001",
            "futu_symbol": "SH.NORMAL1",
            "global_strength": "90",
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 400,
            "lot_size": 100,
            "atr": "0.5",
        },
        {
            "action": "BUY",
            "symbol": "000002",
            "futu_symbol": "SH.NORMAL2",
            "global_strength": "80",
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 400,
            "lot_size": 100,
            "atr": "0.5",
        },
    ]
    report_path, report = write_v2_controller_report(
        config, positions=positions, actions=actions,
    )
    report["metadata"] = {
        **report["metadata"],  # type: ignore[dict-item]
        "simulate_acc_id": 123,
    }
    roots = {
        market: {
            "stock": {
                "asset": stock,
                "tm_id": index * 10,
                "as_of_date": "2026-08-03",
                "global_strength": stock_strength,
            },
            "etf": {
                "asset": etf,
                "tm_id": index * 10 + 1,
                "as_of_date": "2026-08-03",
                "global_strength": etf_strength,
            },
        }
        for index, (market, stock, etf, stock_strength, etf_strength) in enumerate(
            (
                ("CN", "A股", "ETF基金", "70", "60"),
                ("HK", "港股", "香港ETF", "90", "80"),
                ("US", "美股", "美国ETF", "50", "40"),
            ),
            1,
        )
    }
    allocation_snapshot = build_allocation_snapshot(
        allocation_date="2026-08-03",
        generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40,
        roots=roots,
        previous=None,
        version=2,
    )
    daily_path = config.data_dir / "trend_allocation/daily/2026-08-03.json"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    allocation_body = (
        json.dumps(
            allocation_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    daily_path.write_text(allocation_body, encoding="utf-8")
    report["allocation"] = {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": hashlib.sha256(allocation_body.encode()).hexdigest(),
        "allocation_date": "2026-08-03",
        "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
        "roots": allocation_snapshot["roots"],
        "markets": allocation_snapshot["markets"],
    }
    report["strategy_snapshot"] = a_share_trend.live_trend_strategy_snapshot(
        "CN",
        "test-sha",
        (622466, 697199),
        allocation={
            "daily_path": report["allocation"]["daily_path"],  # type: ignore[index]
            "sha256": report["allocation"]["sha256"],  # type: ignore[index]
            "snapshot": allocation_snapshot,
        },
    )
    report["strategy_judgments"]["holding_decisions"] = [{  # type: ignore[index]
        "symbol": "WEAK", "action": "SELL_ALL",
    }]
    report["signal_snapshots"] = {
        "candidates": [
            {"symbol": "000001", "close": "10", "atr": "0.5"},
            {"symbol": "000002", "close": "10", "atr": "0.5"},
            {"symbol": "REPLACE", "close": "10", "atr": "0.5"},
        ],
    }
    pair = {
        "pair_index": 0,
        "sell_symbol": "WEAK",
        "sell_name": "Weak",
        "sell_futu_symbol": "SH.WEAK",
        "sell_global_strength": "10",
        "buy_symbol": "REPLACE",
        "buy_name": "Replace",
        "buy_futu_symbol": "SH.REPLACE",
        "buy_global_strength": "90",
        "sell_asset": "A股",
        "buy_asset": "A股",
        "sell_compared_strength": "10",
        "buy_compared_strength": "90",
        "strength_gap": "80",
        "strength_basis": "global",
        "threshold": "20",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 400,
        "lot_size": 100,
        "atr": "0.5",
        "close": "10",
        "execution_date": "2026-07-20",
        "execution_mode": "automatic",
        "reason": "relative_rotation",
    }
    comparison = {
        **{
            key: pair[key]
            for key in (
                "pair_index", "sell_symbol", "sell_name", "sell_asset",
                "sell_global_strength", "sell_compared_strength",
                "buy_symbol", "buy_name", "buy_asset", "buy_global_strength",
                "buy_compared_strength", "strength_gap", "strength_basis",
                "threshold", "reason",
            )
        },
        "outcome": "planned",
    }
    judgments = report["strategy_judgments"]
    judgments.update({  # type: ignore[union-attr]
        "simulate_rotation_pairs": [pair],
        "simulate_rotation_comparisons": [comparison],
        "real_rotation_pairs": [],
        "real_rotation_comparisons": [],
    })
    judgments.pop("simulated_buy_fifo", None)
    judgments.pop("planned_new_seats", None)
    fifo = controller.freeze_simulated_buy_fifo(
        data_dir=config.data_dir,
        report=report,
        market="CN",
        execution_date="2026-07-20",
        pre_sell_position_count=13,
        held_symbols=[item["futu_symbol"] for item in positions],  # type: ignore[index]
        persist=False,
    )
    judgments["simulated_buy_fifo"] = fifo  # type: ignore[index]
    judgments["planned_new_seats"] = 3  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    macos = RecordingMacOS()
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([macos]),
    )

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []
            self.requests: list[tuple[str, str]] = []
            self._aliases = {
                "SZ.000001": "SH.NORMAL1",
                "SZ.000002": "SH.NORMAL2",
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 123,
                "net_value": "100000",
                "cash": "0",
                "available_cash": "0",
                "positions": [
                    {
                        "code": "SH.WEAK" if index == 0 else f"SH.HOLD{index}",
                        "qty": "1000" if index == 0 else "100",
                        "can_sell_qty": "1000" if index == 0 else "100",
                        "market_val": "7000" if index == 0 else "1000",
                    }
                    for index in range(13)
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            side = str(request["side"]).upper()
            code = str(request["futu_code"])
            self.requests.append((side, self._aliases.get(code, code)))
            order_id = f"SIM-{len(self.orders) + 1}"
            status = "REJECTED" if side == "SELL" else "SUBMITTED"
            order = {
                **request,
                "order_id": order_id,
                "code": code,
                "trd_side": side,
                "dealt_qty": "0",
                "order_status": status,
                "reason": "broker rejected sell" if side == "SELL" else "",
                "account_id": 123,
            }
            self.orders.append(order)
            return {
                "futu_order_id": order_id,
                "status": status,
                "order_status": status,
                "dealt_qty": "0",
                "reason": "broker rejected sell" if side == "SELL" else "",
            }

        def close(self) -> None:
            pass

    client = Client()
    controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="current buy engine",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.data_dir.glob(
            "trend_review/ledgers/CN/rotations/2026-07-20/**/*.json"
        )
    ]
    record_text = json.dumps(records, ensure_ascii=False)
    request_codes = [code for _, code in client.requests]
    assert (
        client.requests,
        "SH.NORMAL2" in request_codes,
        13 + 1 + 1,
        any(
            value in record_text
            for value in ("CN", "123", "SH.WEAK", "SELL", "1000", "broker rejected sell")
        ),
        client.requests.index(("BUY", "SH.REPLACE")) > client.requests.index(("SELL", "SH.WEAK")),
        [code for side, code in client.requests if side == "BUY"] == [
            "SH.REPLACE", "SH.NORMAL1",
        ],
    ) == (
        [
            ("SELL", "SH.WEAK"),
            ("BUY", "SH.REPLACE"),
            ("BUY", "SH.NORMAL1"),
        ],
        False,
        15,
        True,
        True,
        True,
    )


def test_current_execution_uses_live_symbol_idempotency_across_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=123,
        trend_executor_host=socket.gethostname(),
        notifiers=("macos",),
    )
    positions = [
        {
            "symbol": symbol,
            "futu_symbol": futu_symbol,
            "name": symbol,
            "asset_class": "stock",
            "quantity": quantity,
            "market_value": "1000",
            "avg_cost_price": "10",
        }
        for symbol, futu_symbol, quantity in (
            ("HELD", "SH.600001", "100"),
            ("PARTIAL", "SH.600005", "50"),
        )
    ]
    actions = [
        {
            "action": "BUY",
            "symbol": symbol,
            "futu_symbol": futu_symbol,
            "global_strength": strength,
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 400,
            "lot_size": 100,
            "atr": "0.5",
        }
        for symbol, futu_symbol, strength in (
            ("600001", "SH.600001", "100"),
            ("600002", "SH.600002", "95"),
            ("600003", "SH.600003", "90"),
            ("600004", "SH.600004", "85"),
            ("600005", "SH.600005", "80"),
        )
    ]
    report_path, report = write_v2_controller_report(
        config, positions=[], actions=actions,
    )
    report["metadata"] = {
        **report["metadata"],  # type: ignore[dict-item]
        "simulate_acc_id": 123,
    }
    report["account"]["positions"].append({  # type: ignore[index]
        "symbol": "ABSENT",
        "futu_symbol": "SH.ABSENT",
        "name": "Absent",
        "asset_class": "stock",
        "quantity": "0",
        "market_value": "1000",
        "avg_cost_price": "10",
    })
    report["strategy_judgments"]["holding_decisions"] = [{  # type: ignore[index]
        "symbol": "ABSENT", "action": "SELL_ALL",
    }]
    pair = {
        "pair_index": 0,
        "sell_symbol": "ABSENT",
        "sell_name": "Absent",
        "sell_futu_symbol": "SH.ABSENT",
        "sell_global_strength": "10",
        "buy_symbol": "600003",
        "buy_name": "Overlap",
        "buy_futu_symbol": "SH.600003",
        "buy_global_strength": "90",
        "sell_asset": "A股",
        "buy_asset": "A股",
        "sell_compared_strength": "10",
        "buy_compared_strength": "90",
        "strength_gap": "80",
        "strength_basis": "global",
        "threshold": "20",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 400,
        "lot_size": 100,
        "atr": "0.5",
        "close": "10",
        "execution_date": "2026-07-20",
        "execution_mode": "automatic",
        "reason": "relative_rotation",
    }
    comparison = {
        **{
            key: pair[key]
            for key in (
                    "pair_index", "sell_symbol", "sell_name", "sell_asset",
                    "sell_global_strength", "sell_compared_strength",
                    "buy_symbol", "buy_name", "buy_asset", "buy_global_strength",
                "buy_compared_strength", "strength_gap", "strength_basis",
                "threshold", "reason",
            )
        },
        "outcome": "planned",
    }
    judgments = report["strategy_judgments"]
    judgments.update({  # type: ignore[union-attr]
        "simulate_rotation_pairs": [pair],
        "simulate_rotation_comparisons": [comparison],
        "real_rotation_pairs": [],
        "real_rotation_comparisons": [],
    })
    judgments.pop("simulated_buy_fifo", None)
    judgments.pop("planned_new_seats", None)
    fifo = controller.freeze_simulated_buy_fifo(
        data_dir=config.data_dir,
        report=report,
        market="CN",
        execution_date="2026-07-20",
        pre_sell_position_count=0,
        persist=False,
    )
    judgments["simulated_buy_fifo"] = fifo  # type: ignore[index]
    judgments["planned_new_seats"] = 20  # type: ignore[index]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    macos = RecordingMacOS()
    monkeypatch.setattr(
        controller,
        "build_notifier",
        lambda _config: CompositeNotifier([macos]),
    )

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = [{
                "order_id": "SIM-PENDING",
                "futu_code": "SH.600002",
                "code": "SH.600002",
                "side": "BUY",
                "trd_side": "BUY",
                "qty": "100",
                "dealt_qty": "0",
                "order_status": "SUBMITTED",
                "status": "SUBMITTED",
                "account_id": 123,
            }]
            self.requests: list[tuple[str, str]] = []
            self.aliases = {
                "SH.600001": "SH.HELD",
                "SH.600002": "SH.PENDING",
                "SH.600003": "SH.OVERLAP",
                "SH.600004": "SH.RETRY",
                "SH.600005": "SH.PARTIAL",
            }
            self.boundary_counts: list[tuple[int, int]] = []
            self.snapshot_calls = 0
            self.list_order_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            return {
                "acc_id": 123,
                "net_value": "100000",
                "cash": "0",
                "available_cash": "0",
                "positions": [
                    {
                        "code": futu_symbol,
                        "qty": quantity,
                        "can_sell_qty": quantity,
                        "market_val": "1000",
                    }
                    for futu_symbol, quantity in (
                        ("SH.600001", "100"),
                        ("SH.600005", "50"),
                    )
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            self.list_order_calls += 1
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            side = str(request["side"]).upper()
            code = str(request["futu_code"]).upper()
            self.boundary_counts.append(
                (self.snapshot_calls, self.list_order_calls)
            )
            self.requests.append((side, self.aliases.get(code, code)))
            attempt = sum(
                request_side == side
                and request_code == self.aliases.get(code, code)
                for request_side, request_code in self.requests
            )
            status = (
                "REJECTED"
                if code == "SH.600004" and attempt == 1
                else "SUBMITTED"
            )
            order_id = f"SIM-{len(self.orders) + 1}"
            order = {
                **request,
                "order_id": order_id,
                "futu_code": code,
                "code": code,
                "side": side,
                "trd_side": side,
                "qty": request.get("qty", "100"),
                "dealt_qty": "0",
                "order_status": status,
                "status": status,
                "account_id": 123,
            }
            self.orders.append(order)
            return {
                "futu_order_id": order_id,
                "status": status,
                "order_status": status,
                "dealt_qty": "0",
                "reason": "" if status == "SUBMITTED" else "retry rejected",
            }

        def close(self) -> None:
            pass

    client = Client()
    controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="current symbol idempotency round 1",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )
    first_requests = list(client.requests)
    first_boundaries = list(client.boundary_counts)
    controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="current symbol idempotency round 2",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )
    second_requests = client.requests[len(first_requests):]
    second_boundaries = client.boundary_counts[len(first_boundaries):]
    assert (
        first_requests,
        second_requests,
        client.requests.count(("BUY", "SH.OVERLAP")),
        all(
            snapshot_calls > 0 and order_calls > 0
            for snapshot_calls, order_calls in client.boundary_counts
        ),
        all(
            later[0] > earlier[0] and later[1] > earlier[1]
            for earlier, later in zip(
                client.boundary_counts, client.boundary_counts[1:]
            )
        ),
    ) == (
        [
            ("BUY", "SH.OVERLAP"),
            ("BUY", "SH.RETRY"),
        ],
        [("BUY", "SH.RETRY")],
        1,
        True,
        True,
    )


def test_current_execution_uses_live_capacity_when_frozen_planned_seats_are_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    positions = [
        {
            "symbol": f"HOLD{index}",
            "futu_symbol": f"SH.HOLD{index}",
            "name": f"Hold {index}",
            "asset_class": "stock",
            "quantity": "100",
            "market_value": "1000",
            "avg_cost_price": "10",
        }
        for index in range(20)
    ]
    live_positions = positions[:14]
    action = {
        "action": "BUY",
        "symbol": "600001",
        "futu_symbol": "SH.600001",
        "global_strength": "100",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 400,
        "lot_size": 100,
        "atr": "0.5",
    }
    report_path, report = write_v2_controller_report(
        config, positions=positions, actions=[action],
    )
    report["strategy_judgments"]["planned_new_seats"] = 0
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []
            self.requests: list[dict[str, object]] = []

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "available_cash": "100000",
                "positions": [
                    {
                        "code": position["futu_symbol"],
                        "qty": position["quantity"],
                        "can_sell_qty": position["quantity"],
                        "market_val": position["market_value"],
                    }
                    for position in live_positions
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            order = {
                **request,
                "order_id": "SIM-1",
                "futu_order_id": "SIM-1",
                "code": request["futu_code"],
                "trd_side": request["side"],
                "order_status": "FILLED_ALL",
                "status": "FILLED_ALL",
                "dealt_qty": request["qty"],
                "dealt_avg_price": "10",
                "account_id": 101,
            }
            self.orders.append(order)
            return {
                "futu_order_id": "SIM-1",
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }

        def close(self) -> None:
            pass

    client = Client()
    controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="current live capacity with frozen zero seats",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )

    assert (
        report["strategy_judgments"]["planned_new_seats"],
        [
            (str(item["side"]).upper(), item["futu_code"], item["qty"])
            for item in client.requests
        ],
    ) == (0, [("BUY", "SH.600001", "400")])


@pytest.mark.parametrize("failure_source", ["holdings", "orders"])
def test_current_execution_fails_closed_and_notifies_when_live_occupancy_refresh_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_source: str,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    positions = [
        {
            "symbol": f"HOLD{index}",
            "futu_symbol": f"SH.HOLD{index}",
            "name": f"Hold {index}",
            "asset_class": "stock",
            "quantity": "100",
            "market_value": "1000",
            "avg_cost_price": "10",
        }
        for index in range(20)
    ]
    live_positions = positions[:14]
    actions = [
        {
            "action": "BUY",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
            "global_strength": "100",
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 400,
            "lot_size": 100,
            "atr": "0.5",
        },
        {
            "action": "BUY",
            "symbol": "600002",
            "futu_symbol": "SH.600002",
            "global_strength": "99",
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 400,
            "lot_size": 100,
            "atr": "0.5",
        },
    ]
    report_path, report = write_v2_controller_report(
        config, positions=positions, actions=actions,
    )
    report["strategy_judgments"]["planned_new_seats"] = 0
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha = _report_hash(report)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    notifications: list[tuple[str, str, object]] = []

    def notify(title: str, message: str, key: object) -> bool:
        notifications.append((title, message, key))
        return True

    monkeypatch.setattr(controller, "_notify_once", notify)

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []
            self.requests: list[dict[str, object]] = []
            self.snapshot_calls = 0
            self.list_order_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            if failure_source == "holdings" and self.snapshot_calls == 4:
                raise RuntimeError("live holdings refresh unavailable")
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "available_cash": "100000",
                "positions": [
                    {
                        "code": position["futu_symbol"],
                        "qty": position["quantity"],
                        "can_sell_qty": position["quantity"],
                        "market_val": position["market_value"],
                    }
                    for position in live_positions
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            self.list_order_calls += 1
            if failure_source == "orders" and self.list_order_calls == 2:
                raise RuntimeError("live orders refresh unavailable")
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(dict(request))
            order = {
                **request,
                "order_id": "SIM-1",
                "futu_order_id": "SIM-1",
                "code": request["futu_code"],
                "trd_side": request["side"],
                "order_status": "FILLED_ALL",
                "status": "FILLED_ALL",
                "dealt_qty": request["qty"],
                "dealt_avg_price": "10",
                "account_id": 101,
            }
            self.orders.append(order)
            return {
                "futu_order_id": "SIM-1",
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": request["qty"],
            }

        def close(self) -> None:
            pass

    client = Client()
    result = controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="current live occupancy refresh unavailable",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=False,
    )

    assert (
        client.requests,
        result["submitted_count"],
        result["status"],
        [
            (title, message, key[3:5])
            for title, message, key in notifications
        ],
    ) == (
        [],
        0,
        "live_occupancy_unavailable",
        [
            (
                "CN 趋势买入暂缓",
                "无法刷新持仓或未终态买单，已跳过本轮买入",
                ("buy_occupancy", "unavailable"),
            ),
        ],
    )


def test_current_execution_caps_sells_to_live_sellable_quantity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    positions = [
        {
            "symbol": symbol,
            "futu_symbol": futu_symbol,
            "name": symbol,
            "asset_class": "stock",
            "quantity": quantity,
            "market_value": market_value,
            "avg_cost_price": "10",
        }
        for symbol, futu_symbol, quantity, market_value in (
            ("600001", "SH.600001", "100", "1000"),
            ("600002", "SH.600002", "200", "2000"),
            ("600003", "SH.600003", "100", "1000"),
        )
    ]
    actions = [
        {
            "action": "SELL_ALL",
            "symbol": "600001",
            "futu_symbol": "SH.600001",
        },
        {
            "action": "SELL_PARTIAL",
            "symbol": "600002",
            "futu_symbol": "SH.600002",
            "reason": "overheat_take_profit",
            "target_fraction": "0.30",
            "lot_size": 10,
            "estimated_shares": 100,
            "position_started_for": "2026-07-01",
            "overheat_signals": ["boiling"],
        },
        {
            "action": "SELL_ALL",
            "symbol": "600003",
            "futu_symbol": "SH.600003",
        },
    ]
    report_path, report = write_v2_controller_report(
        config, positions=positions, actions=actions,
    )
    report_sha = _report_hash(report)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(controller, "datetime", FixedDateTime)

    class Client:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = [{
                "order_id": "CROSS-DAY-600003",
                "futu_code": "SH.600003",
                "code": "SH.600003",
                "side": "SELL",
                "trd_side": "SELL",
                "qty": "100",
                "dealt_qty": "0",
                "order_status": "SUBMITTED",
                "status": "SUBMITTED",
                "account_id": 101,
            }]
            self.requests: list[tuple[str, str, str]] = []
            self.boundary_counts: list[tuple[int, int]] = []
            self.snapshot_calls = 0
            self.list_order_calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.snapshot_calls += 1
            return {
                "acc_id": 101,
                "net_value": "100000",
                "cash": "100000",
                "available_cash": "100000",
                "positions": [
                    {
                        "code": futu_code,
                        "qty": quantity,
                        "can_sell_qty": can_sell_qty,
                        "market_val": market_value,
                    }
                    for futu_code, quantity, can_sell_qty, market_value in (
                        ("SH.600001", "100", "70", "1000"),
                        ("SH.600002", "200", "60", "2000"),
                        ("SH.600003", "100", "100", "1000"),
                    )
                ],
            }

        def list_orders(self, **_kwargs: object) -> dict[str, object]:
            self.list_order_calls += 1
            return {"orders": self.orders}

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            side = str(request["side"]).upper()
            code = str(request["futu_code"])
            qty = str(request["qty"])
            self.boundary_counts.append(
                (self.snapshot_calls, self.list_order_calls)
            )
            self.requests.append((side, code, qty))
            order_id = f"SIM-{len(self.orders) + 1}"
            self.orders.append({
                **request,
                "order_id": order_id,
                "code": code,
                "trd_side": side,
                "order_status": "FILLED_ALL",
                "status": "FILLED_ALL",
                "dealt_qty": qty,
                "dealt_avg_price": "10",
                "account_id": 101,
            })
            return {
                "futu_order_id": order_id,
                "status": "FILLED_ALL",
                "order_status": "FILLED_ALL",
                "dealt_qty": qty,
            }

        def close(self) -> None:
            pass

    client = Client()
    controller.execute_simulated_trend_report(
        config,
        "CN",
        "2026-07-20",
        report_sha,
        actor="trend-market-controller",
        reason="current live sellable quantities",
        now=NOW,
        order_client=client,
        scheduled=False,
    )

    assert (
        client.requests,
        all(
            snapshot_calls > 0 and order_calls > 0
            for snapshot_calls, order_calls in client.boundary_counts
        ),
        any(code == "SH.600003" for _, code, _ in client.requests),
        report_path.exists(),
    ) == (
        [
            ("SELL", "SH.600001", "70"),
            ("SELL", "SH.600002", "60"),
        ],
        True,
        False,
        True,
    )


def test_execute_locked_report_runs_rotations_when_buys_are_pending_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    report_path = tmp_path / "locked.json"
    report = valid_cn_report(
        as_of_date="2026-07-17", execution_date="2026-07-20",
    )
    report["strategy_judgments"]["formal_actions"] = [
        {
            "action": "BUY",
            "symbol": "600001",
            "target_weight": "0.04",
            "lot_size": 100,
            "estimated_shares": 100,
            "atr": "0.5",
            "executable": False,
            "sizing_note": "席位已满，待现金/席位释放",
        },
    ]
    report["strategy_judgments"]["simulate_rotation_pairs"] = [
        {"buy_futu_symbol": "SH.STRONG"},
    ]
    report["strategy_judgments"]["real_rotation_pairs"] = []
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls: list[str] = []

    class Client:
        def close(self) -> None:
            pass

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_revision_state", lambda *_args: (None, None))
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_new_order_client", lambda *_args, **_kwargs: Client()
    )
    monkeypatch.setattr(
        controller,
        "execute_trend_review_open",
        lambda **_kwargs: calls.append("ordinary") or {
            "status": "unchanged", "submitted_count": 0, "artifact_paths": ["ordinary"],
        },
    )
    monkeypatch.setattr(
        controller,
        "execute_relative_rotations",
        lambda **_kwargs: calls.append("rotation") or {
            "status": "submitted", "submitted_count": 1, "artifact_paths": ["rotation"],
        },
    )

    result = controller._execute_locked_report(
        config, "CN", "2026-07-20", report_path, report, quote_client=Quote()
    )

    # 席位满 + 仅待条件买入：ordinary_complete=True，轮换照常执行，待条件不阻塞。
    assert calls == ["ordinary", "rotation"]
    assert result["submitted_count"] == 1
    assert result["artifact_paths"] == ["ordinary", "rotation"]


def test_execution_completion_audits_relative_rotation_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    report_path = tmp_path / "locked.json"
    report = {
        "strategy_judgments": {
            "formal_actions": [], "simulate_rotation_pairs": [{}],
            "real_rotation_pairs": [{}],
        },
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    batch = config.data_dir / "trend_review/ledgers/CN/batches/2026-07-20.json"
    batch.parent.mkdir(parents=True)
    batch.write_text(json.dumps({
        "schema_version": "open_trader.trend_review.batch.v1", "market": "CN",
        "execution_date": "2026-07-20", "report_path": str(report_path),
        "report_sha256": _report_hash(report),
    }), encoding="utf-8")
    monkeypatch.setattr(controller, "_valid_report", lambda *_args: True)
    audited: list[object] = []
    monkeypatch.setattr(
        controller, "relative_rotations_completed",
        lambda *_args, **_kwargs: audited.append(_kwargs["report"]) or False,
    )

    assert controller._execution_completed(config, cycle) is False
    assert audited == [report]


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
        ("US", "runs/.trend_us_futu_report.lock"),
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
        revision_path = config.reports_dir / "trend_a_share/2026-07-17-r1.json"
        assert executed == [revision_path]
        revision_report = json.loads(revision_path.read_text(encoding="utf-8"))
        _, completion_path = controller._revision_paths(
            config, cycle.market, cycle.as_of_date
        )
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        assert completion["request_path"] == str(request_path)
        assert completion["request_sha256"] == hashlib.sha256(
            request_path.read_bytes()
        ).hexdigest()
        assert completion["report_path"] == str(revision_path)
        assert completion["report_sha256"] == _report_hash(revision_report)
        assert json.loads(
            controller._delivery_receipt_path(
                config, "CN", revision_path
            ).read_text(encoding="utf-8")
        )["status"] == "sent"


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
    r1_path = config.reports_dir / "trend_a_share/2026-07-17-r1.json"
    assert executed == [r1_path]
    r1 = json.loads(r1_path.read_text(encoding="utf-8"))
    assert completion["request_path"] == str(request_path)
    assert completion["request_sha256"] == hashlib.sha256(
        request_path.read_bytes()
    ).hexdigest()
    assert completion["report_path"] == str(r1_path)
    assert completion["report_sha256"] == _report_hash(r1)
    assert json.loads(
        controller._delivery_receipt_path(config, "CN", r1_path).read_text(
            encoding="utf-8"
        )
    )["status"] == "sent"


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
    base_path, _ = write_report(config)
    request_path = controller._request_revision(config, cycle, NOW)
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
    r2_path = config.reports_dir / "trend_a_share/2026-07-17-r2.json"
    assert executed == [r2_path]
    r2 = json.loads(r2_path.read_text(encoding="utf-8"))
    _, completion_path = controller._revision_paths(
        config, cycle.market, cycle.as_of_date
    )
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert completion["request_path"] == str(request_path)
    assert completion["request_sha256"] == hashlib.sha256(
        request_path.read_bytes()
    ).hexdigest()
    assert completion["report_path"] == str(r2_path)
    assert completion["report_sha256"] == _report_hash(r2)
    assert not controller._delivery_receipt_path(
        config, "CN", r1_path
    ).exists()
    assert json.loads(
        controller._delivery_receipt_path(config, "CN", r2_path).read_text(
            encoding="utf-8"
        )
    )["status"] == "sent"
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


def test_revision_migration_selects_existing_report_without_rewriting_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    request_path = controller._request_revision(config, cycle, NOW)
    r1_path, r1 = write_report(config, revision=1)
    write_report_delivery_receipt(config, r1_path, r1, status="sent")
    controller._complete_revision(config, cycle, (r1_path, r1), NOW)
    r6_path, r6 = write_report(config, revision=6)
    write_report_delivery_receipt(config, r6_path, r6, status="sent")

    migration_path = controller._record_revision_migration(
        config,
        cycle,
        (r6_path, r6),
        actor="acceptance",
        reason="选择已存在且已交付的 r6，不重跑报告",
        authorized_at=datetime.fromisoformat("2026-07-20T10:01:00+08:00"),
        accepted_git_sha="a" * 40,
    )

    migration = json.loads(migration_path.read_text(encoding="utf-8"))
    assert migration["revision_request_path"] == str(request_path)
    assert migration["from_report_path"] == str(r1_path)
    assert migration["to_report_path"] == str(r6_path)
    assert migration["to_report_sha256"] == _report_hash(r6)
    _, effective_completion = controller._revision_state(
        config, cycle.market, cycle.as_of_date, cycle.execution_date
    )
    assert effective_completion is not None
    assert effective_completion["report_path"] == str(r6_path)
    assert effective_completion["report_sha256"] == _report_hash(r6)
    original_completion = json.loads(
        controller._revision_paths(config, cycle.market, cycle.as_of_date)[1]
        .read_text(encoding="utf-8")
    )
    assert original_completion["report_path"] == str(r1_path)
    assert (
        controller._record_revision_migration(
            config,
            cycle,
            (r6_path, r6),
            actor="acceptance",
            reason="选择已存在且已交付的 r6，不重跑报告",
            authorized_at=datetime.fromisoformat("2026-07-20T10:01:00+08:00"),
            accepted_git_sha="a" * 40,
        )
        == migration_path
    )
    with pytest.raises(ValueError, match="immutable trend report revision migration collision"):
        controller._record_revision_migration(
            config,
            cycle,
            (r6_path, r6),
            actor="acceptance",
            reason="选择已存在且已交付的 r6，不重跑报告",
            authorized_at=datetime.fromisoformat("2026-07-20T10:02:00+08:00"),
            accepted_git_sha="a" * 40,
        )
    migration["to_revision"] = 5
    migration_path.write_text(json.dumps(migration), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid trend report revision migration"):
        controller._revision_state(
            config, cycle.market, cycle.as_of_date, cycle.execution_date
        )
    migration["to_revision"] = 6
    migration["unexpected"] = True
    migration_path.write_text(json.dumps(migration), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid trend report revision migration"):
        controller._revision_state(
            config, cycle.market, cycle.as_of_date, cycle.execution_date
        )


def test_revision_migration_rejects_report_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    controller._request_revision(config, cycle, NOW)
    r1_path, r1 = write_report(config, revision=1)
    write_report_delivery_receipt(config, r1_path, r1, status="sent")
    controller._complete_revision(config, cycle, (r1_path, r1), NOW)

    with pytest.raises(ValueError, match="invalid trend report revision migration"):
        controller._record_revision_migration(
            config,
            cycle,
            (r1_path, r1),
            actor="acceptance",
            reason="不能回滚",
            authorized_at=datetime.fromisoformat("2026-07-20T10:01:00+08:00"),
            accepted_git_sha="a" * 40,
        )


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


def test_controller_never_generates_report_before_allocation_terminal_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(controller_config(tmp_path), trend_animals_api_key="test-key")
    patch_cycle(monkeypatch, active_cn_cycle())
    generated: list[object] = []
    monkeypatch.setattr(
        controller, "_allocation_reference_for_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("allocation has not made a terminal attempt")
        ),
    )
    monkeypatch.setattr(
        controller, "_generate_report", lambda *_args: generated.append(object()),
    )

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert generated == []
    assert result["phase"] == "blocked"
    assert "terminal attempt" in str(result["blocker"])


def test_controller_exposes_account_snapshot_failure_as_report_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(controller_config(tmp_path), trend_animals_api_key="test-key")
    patch_cycle(monkeypatch, active_cn_cycle())
    monkeypatch.setattr(
        controller, "_allocation_reference_for_cycle", lambda *_args, **_kwargs: None
    )

    def fail(*_args: object) -> None:
        raise AccountHttpError("account_unavailable")

    monkeypatch.setattr(controller, "_generate_report", fail)

    result = run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert result["phase"] == "recovering_report"
    assert result["blocker"] == "report generation failed: account_unavailable"
    assert not list(config.reports_dir.rglob("*.json"))


@pytest.mark.parametrize("reference", [
    {"daily_path": "data/trend_allocation/daily/2026-08-03.json", "sha256": "a" * 64},
    None,
])
def test_controller_passes_the_terminal_allocation_reference_to_report_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: dict[str, str] | None,
) -> None:
    config = replace(controller_config(tmp_path), trend_animals_api_key="test-key")
    patch_cycle(monkeypatch, active_cn_cycle())
    generated: list[object] = []
    monkeypatch.setattr(
        controller, "_allocation_reference_for_cycle", lambda *_args, **_kwargs: reference
    )
    monkeypatch.setattr(
        controller, "_generate_report", lambda *_args: generated.append(_args[-1])
    )

    run_trend_market_controller(config, "CN", once=True, now_fn=lambda: NOW)

    assert generated == [reference]


def test_allocation_gate_uses_shared_shanghai_date_for_us_report_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(controller_config(tmp_path), trend_animals_api_key="test-key")
    cycle = replace(active_cn_cycle(), market="US", as_of_date="2026-07-31")
    captured: dict[str, object] = {}

    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            captured["calendar"] = kwargs
            return ["2026-08-03"]

    monkeypatch.setattr(
        controller,
        "allocation_reference_for_report",
        lambda _config, **kwargs: captured.update(kwargs) or {"daily_path": "data/x", "sha256": "a" * 64},
    )

    result = controller._allocation_reference_for_cycle(
        config,
        now=datetime.fromisoformat("2026-08-03T09:31:00-04:00"),
        quote_client=Quote(),
    )

    assert result == {"daily_path": "data/x", "sha256": "a" * 64}
    assert captured["allocation_date"] == "2026-08-03"


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_generate_report_passes_allocation_reference_to_market_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    market: str,
) -> None:
    config = replace(controller_config(tmp_path), trend_animals_api_key="test-key")
    reference = {"daily_path": "data/x", "sha256": "a" * 64}
    captured: dict[str, object] = {}
    monkeypatch.setattr(controller, "require_trend_executor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "build_notifier", lambda _config: object())
    monkeypatch.setattr(
        controller,
        "run_a_share_trend_report",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(status="generated"),
    )
    monkeypatch.setattr(
        controller,
        "run_market_trend_report",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(status="generated"),
    )

    controller._generate_report(config, market, "2026-08-03", False, reference)

    assert captured["allocation_reference"] is reference


def test_scheduled_confirm_submitted_does_not_complete_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        controller_config(tmp_path),
        trend_review_cn_simulate_acc_id=101,
        trend_executor_host=socket.gethostname(),
    )
    buy = {
        "action": "BUY",
        "symbol": "600001",
        "futu_symbol": "SH.600001",
        "target_weight": "0.04",
        "lot_size": 100,
        "estimated_shares": 400,
        "target_amount": "4000",
        "atr": "0.5",
    }
    report_path, report = write_v2_controller_report(config, actions=[buy])
    report["execution_date"] = NOW.date().isoformat()
    report_path.write_text(json.dumps(report), encoding="utf-8")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is None else NOW.astimezone(tz)  # type: ignore[arg-type]

    class OrderClient:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
            self.fail_orders = 1

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
            self.requests.append(dict(request))
            if self.fail_orders:
                self.fail_orders -= 1
                raise RuntimeError("submission boundary lost response")
            raise AssertionError("confirmed submission must not be retried")

    class Quote:
        def get_snapshots(self, symbols: list[str]) -> dict[str, object]:
            return {
                symbol: SimpleNamespace(last_price=Decimal("10"))
                for symbol in symbols
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller, "datetime", FixedDateTime)
    client = OrderClient()
    first = controller.execute_simulated_trend_report(
        config,
        "CN",
        NOW.date().isoformat(),
        _report_hash(report),
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW,
        quote_client=Quote(),
        order_client=client,
        scheduled=True,
    )
    second = controller.execute_simulated_trend_report(
        config,
        "CN",
        NOW.date().isoformat(),
        _report_hash(report),
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW + timedelta(minutes=1),
        quote_client=Quote(),
        order_client=client,
        scheduled=True,
    )
    action_resolution = trend_review.resolve_trend_action(
        config.data_dir,
        market="CN",
        execution_date=NOW.date().isoformat(),
        symbol="600001",
        side="buy",
        resolution="confirm-submitted",
        actor="ray",
        reason="broker order accepted outside the client response",
        resolved_at="2026-07-20T09:33:00+08:00",
        futu_order_id="BROKER-42",
        execution_id=str(first["execution_id"]),
        request_path=str(first["request_path"]),
    )
    replay = controller.execute_simulated_trend_report(
        config,
        "CN",
        NOW.date().isoformat(),
        _report_hash(report),
        actor="trend-market-controller",
        reason="scheduled execution",
        now=NOW + timedelta(minutes=2),
        quote_client=Quote(),
        order_client=client,
        scheduled=True,
    )

    assert (
        first["status"],
        second["status"],
        json.loads(action_resolution.read_text(encoding="utf-8"))["futu_order_id"],
        replay["status"],
        len(client.requests),
    ) == (
        "uncertain",
        "uncertain",
        "BROKER-42",
        "uncertain",
        1,
    )
