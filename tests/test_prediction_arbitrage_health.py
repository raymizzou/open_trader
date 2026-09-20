from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_health import (
    format_report,
    report_to_dict,
    run_health_check,
    run_service,
    send_report,
    validate_frontend_gateway_health,
)


def base_state(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "healthy",
        "stale": False,
        "health": {
            "status": "healthy",
            "heartbeat_age_seconds": 0.5,
            "universe_age_seconds": 5.0,
            "universe_retry_exhausted": False,
        },
        "breaker": {"open": False},
        "cross_venue": {
            "status": "ready",
            "funnel": {
                "matched_pairs": 13,
                "monitored_pairs": 13,
                "codex_approved_pairs": 5,
            },
        },
        "relation_discovery": {
            "status": "healthy",
            "catalog": {"status": "healthy"},
        },
        "readiness": {"ready": True},
        "llm_usage_24h": {"calls": 10, "successes": 10},
        "thread": {"status": "running"},
    }
    payload.update(overrides)
    return payload


def run_check(
    *,
    payload: dict[str, object] | None = None,
    healthz: bool = True,
    llm: tuple[int, int] = (10, 10),
    process: dict[str, object] | None = {
        "schema_version": "open_trader.prediction_service.health.v1",
        "module": "prediction_service",
        "status": "running",
        "mode": "production",
        "production_owner": True,
        "mutations": "enabled",
        "source_state": "clean",
        "pid": 42,
        "cwd": "/srv/open_trader",
        "git_sha": "abc",
    },
    notify_configured: bool = True,
    sleep_fn=None,
):
    state = payload if payload is not None else base_state(
        llm_usage_24h={"calls": llm[0], "successes": llm[1]}
    )
    return run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *args: state,
        fetch_healthz=lambda *args: process if healthz else (_ for _ in ()).throw(ConnectionError("down")),
        notify_configured=notify_configured,
        sleep_fn=sleep_fn if sleep_fn is not None else (lambda _seconds: None),
    )


def test_healthy_passes() -> None:
    report = run_check()
    assert report.status == "PASS"
    assert report.summary["pid"] == "42"
    assert report.summary["llm_success"] == 10


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"stale": True}, "FAIL"),
        ({"breaker": {"open": True}}, "FAIL"),
        ({"status": "unavailable"}, "FAIL"),
        ({"status": "error"}, "FAIL"),
        ({"cross_venue": {"status": "unavailable", "funnel": {}}}, "FAIL"),
        ({"cross_venue": {"status": "degraded", "funnel": {}}}, "WARN"),
        (
            {"health": {"heartbeat_age_seconds": 61.0, "universe_age_seconds": 5.0}},
            "FAIL",
        ),
        (
            {"health": {"heartbeat_age_seconds": 0.5, "universe_age_seconds": 301.0}},
            "FAIL",
        ),
        (
            {"health": {"heartbeat_age_seconds": 0.5, "universe_age_seconds": 5.0, "universe_retry_exhausted": True}},
            "FAIL",
        ),
        (
            {
                "relation_discovery": {
                    "status": "degraded",
                    "catalog": {"status": "degraded"},
                }
            },
            "WARN",
        ),
        ({"readiness": {"ready": False, "reason": "wallet_unavailable"}}, "FAIL"),
    ],
)
def test_state_check_severity(payload: dict[str, object], expected: str) -> None:
    report = run_check(payload=base_state(**payload))
    assert report.status == expected


def test_endpoint_exception_fails() -> None:
    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *args: (_ for _ in ()).throw(ConnectionError("down")),
        fetch_healthz=lambda *args: {"status": "running", "pid": 42, "git_sha": "abc"},
        sleep_fn=lambda _seconds: None,
    )
    assert report.status == "FAIL"
    assert any(check.name == "endpoint" and check.status == "FAIL" for check in report.checks)


def test_service_down_fails() -> None:
    report = run_check(healthz=False)
    assert report.status == "FAIL"
    checks = {check.name: check for check in report.checks}
    assert checks["service"].status == "FAIL"


def test_llm_no_success_fails() -> None:
    assert run_check(llm=(10, 0)).status == "FAIL"


def test_llm_no_calls_passes() -> None:
    assert run_check(llm=(0, 0)).status == "PASS"


def test_process_missing_fails() -> None:
    assert run_check(process=None).status == "FAIL"


def test_service_health_identity_replaces_legacy_process_probe() -> None:
    report = run_check()
    checks = {check.name: check for check in report.checks}
    assert checks["process"].status == "PASS"
    assert report.summary["pid"] == "42"
    assert report.summary["sha"] == "abc"


def test_gateway_health_requires_service_route_and_prediction_upstream() -> None:
    health = {
        "schema_version": "open_trader.frontend_gateway.health.v1",
        "module": "frontend_gateway",
        "upstream_status": "ok",
        "prediction_route_mode": "service",
        "prediction_upstream_status": "ok",
    }
    assert validate_frontend_gateway_health(health) == (True, "")


@pytest.mark.parametrize(
    "override",
    [
        {"prediction_route_mode": "legacy"},
        {"prediction_route_mode": "maintenance"},
        {"prediction_route_mode": None},
        {"prediction_upstream_status": "unavailable"},
        {"prediction_upstream_status": None},
    ],
)
def test_gateway_health_fails_closed_without_service_prediction_route(
    override: dict[str, object],
) -> None:
    health = {
        "schema_version": "open_trader.frontend_gateway.health.v1",
        "module": "frontend_gateway",
        "upstream_status": "ok",
        "prediction_route_mode": "service",
        "prediction_upstream_status": "ok",
    }
    health.update(override)
    valid, reason = validate_frontend_gateway_health(health)
    assert valid is False
    assert reason


@pytest.mark.parametrize(
    "field_override",
    [
        {"mode": "shadow", "production_owner": False, "mutations": "prohibited"},
        {"production_owner": False},
        {"mutations": "prohibited"},
        {"schema_version": "open_trader.legacy_dashboard.health.v1", "module": "legacy_dashboard"},
        {"source_state": "dirty"},
        {"cwd": ""},
        {"git_sha": ""},
        {"pid": "42"},
    ],
)
def test_service_health_identity_fails_closed(field_override: dict[str, object]) -> None:
    process = {
        "schema_version": "open_trader.prediction_service.health.v1",
        "module": "prediction_service",
        "status": "running",
        "mode": "production",
        "production_owner": True,
        "mutations": "enabled",
        "source_state": "clean",
        "pid": 42,
        "cwd": "/srv/open_trader",
        "git_sha": "abc",
    }
    process.update(field_override)
    report = run_check(process=process)
    assert report.status == "FAIL"
    assert {check.name: check for check in report.checks}["service"].status == "FAIL"


def test_notify_unconfigured_warns() -> None:
    assert run_check(notify_configured=False).status == "WARN"


def test_format_pass_uses_three_line_readable_template() -> None:
    text = format_report(run_check())
    lines = text.splitlines()
    assert lines[0].startswith("心跳 ")
    assert "行情刷新" in lines[0]
    assert "LLM 校验 24h：" in lines[1]
    assert lines[2].startswith("PID 42 · 版本 ")


def test_format_fail_lists_checks() -> None:
    report = run_check(llm=(10, 0))
    text = format_report(report)
    assert text.startswith("· LLM 校验：24h 0/10 成功")
    assert "窗口内无成功的 LLM 校验" in text
    assert "其余 " in text
    assert "- FAIL llm:" not in text


def test_report_to_dict_is_jsonable() -> None:
    import json

    data = report_to_dict(run_check())
    assert data["status"] == "PASS"
    assert "url" not in data["summary"]
    assert json.dumps(data)


def test_send_report_calls_notifier() -> None:
    calls: list[tuple[str, str]] = []

    class FakeNotifier:
        def notify(self, title: str, message: str) -> None:
            calls.append((title, message))

    assert send_report(FakeNotifier(), run_check()) is True
    assert calls[0][0].startswith("✅ 预测套利正常（")
    assert calls[0][1].startswith("心跳 ")


def test_send_report_returns_false_on_failure() -> None:
    class BrokenNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("boom")

    assert send_report(BrokenNotifier(), run_check()) is False


def test_health_check_reports_auto_eat_stats() -> None:
    report = run_check(payload=base_state(auto_eat_stats={
        "mode": "auto",
        "today_attempts": 3,
        "today_submitted": 0,
        "today_cost": 0.0,
        "realized_pnl": 0.0,
        "rejected_by_reason": {"cooldown": 3},
    }))

    checks = {check.name: check for check in report.checks}
    assert checks["auto_eat"].status == "WARN"
    assert "submitted=0" in checks["auto_eat"].value
    assert report.summary["validation_mode"] == "auto"


def test_health_reads_llm_usage_from_service_state() -> None:
    report = run_check(payload=base_state(llm_usage_24h={"calls": 7, "successes": 4}))
    checks = {check.name: check for check in report.checks}
    assert checks["llm"].value == "4/7"
    assert report.summary["llm_total"] == 7
    assert report.summary["llm_success"] == 4


def test_production_consumers_do_not_open_prediction_sqlite_directly() -> None:
    forbidden = {
        "src/open_trader/dashboard.py",
        "src/open_trader/dashboard_web.py",
        "src/open_trader/cli.py",
        "src/open_trader/prediction_arbitrage_health.py",
    }
    root = Path(__file__).parents[1]
    for path in forbidden:
        text = (root / path).read_text(encoding="utf-8")
        assert "PredictionArbitrageStore(" not in text
        assert "prediction_arbitrage.sqlite3" not in text


_LONG_SHA = "abc1234def5678"


def run_check_with_sha(sha: str):
    process = {
        "schema_version": "open_trader.prediction_service.health.v1",
        "module": "prediction_service",
        "status": "running",
        "mode": "production",
        "production_owner": True,
        "mutations": "enabled",
        "source_state": "clean",
        "pid": 42,
        "cwd": "/srv/open_trader",
        "git_sha": sha,
    }
    return run_check(process=process)


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


def test_format_pass_uses_readable_template() -> None:
    report = run_check_with_sha(_LONG_SHA)
    notifier = RecordingNotifier()

    assert send_report(notifier, report) is True
    title, body = notifier.messages[0]
    assert title.startswith("✅ 预测套利正常（")
    assert title.endswith("）")
    assert re.fullmatch(r"\d{2}:\d{2}", title[len("✅ 预测套利正常（"):-1])
    assert "心跳" in body
    assert "行情刷新" in body
    assert "LLM 校验 24h" in body
    assert not re.search(r"\d+\.\d{3,}s", body)
    assert _LONG_SHA[:7] in body
    assert _LONG_SHA not in body


def test_format_fail_lists_chinese_checks_with_thresholds() -> None:
    report = run_check(
        payload=base_state(
            health={
                "status": "healthy",
                "heartbeat_age_seconds": 0.5,
                "universe_age_seconds": 301.0,
                "universe_retry_exhausted": False,
            },
            llm_usage_24h={"calls": 10, "successes": 0},
        )
    )
    assert report.status == "FAIL"
    notifier = RecordingNotifier()

    assert send_report(notifier, report) is True
    title, body = notifier.messages[0]
    assert "2 项失败需处理" in title
    assert "行情刷新" in body
    assert "300 秒" in body
    assert "LLM 校验" in body
    assert "FAIL llm:" not in body
    assert "PASS ·" not in body


def test_send_report_delivers_new_title_and_survives_broken_notifier() -> None:
    passing = RecordingNotifier()
    assert send_report(passing, run_check()) is True
    pass_title, pass_body = passing.messages[0]
    assert pass_title.startswith("✅ 预测套利正常（")
    assert "心跳" in pass_body

    failing = RecordingNotifier()
    assert send_report(failing, run_check(llm=(10, 0))) is True
    fail_title, fail_body = failing.messages[0]
    assert fail_title.startswith("❌ 预测套利异常：1 项失败需处理（")
    assert fail_body.startswith("· LLM 校验：")

    class BrokenNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("boom")

    assert send_report(BrokenNotifier(), run_check()) is False


def test_health_check_thread_item_three_states() -> None:
    running = run_check()
    checks = {check.name: check for check in running.checks}
    assert checks["thread"].status == "PASS"
    assert running.status == "PASS"

    gave_up = run_check(payload=base_state(thread={"status": "gave_up"}))
    checks = {check.name: check for check in gave_up.checks}
    assert checks["thread"].status == "FAIL"
    assert "监控线程已停止重启" in checks["thread"].reason
    assert gave_up.status == "FAIL"

    missing_payload = {
        key: value for key, value in base_state().items() if key != "thread"
    }
    missing = run_check(payload=missing_payload)
    checks = {check.name: check for check in missing.checks}
    assert checks["thread"].status == "FAIL"
    assert "监控线程状态缺失" in checks["thread"].reason
    assert missing.status == "FAIL"


def _healthz_payload(pid: int = 42, sha: str = "abc") -> dict[str, object]:
    return {
        "schema_version": "open_trader.prediction_service.health.v1",
        "module": "prediction_service",
        "status": "running",
        "mode": "production",
        "production_owner": True,
        "mutations": "enabled",
        "source_state": "clean",
        "pid": pid,
        "cwd": "/srv/open_trader",
        "git_sha": sha,
    }


class _StopRun(Exception):
    pass


class RecordingServiceNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class FlakyRecoveryNotifier:
    """Recording notifier whose recovery notice fails the first `fail_times` deliveries."""

    def __init__(self, fail_times: int) -> None:
        self.messages: list[tuple[str, str]] = []
        self.recovery_failures = 0
        self._fail_times = fail_times

    def notify(self, title: str, message: str) -> None:
        if title.startswith("✅ 预测套利已恢复（") and self._fail_times > 0:
            self._fail_times -= 1
            self.recovery_failures += 1
            raise RuntimeError("feishu transient outage")
        self.messages.append((title, message))


def run_service_rounds(
    notifier,
    *,
    start,
    rounds,
    fetch_state,
    fetch_healthz,
    interval: float = 7200.0,
    **kwargs,
):
    """Drive run_service for exactly `rounds` check cycles on a fake clock."""

    clock = {"now": start}
    sleeps: list[float] = []
    end = start + timedelta(seconds=interval * (rounds + 1))

    def now_fn():
        return clock["now"]

    def sleep_fn(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=seconds)
        if clock["now"] >= end:
            raise _StopRun

    with pytest.raises(_StopRun):
        run_service(
            notifier,
            url="http://127.0.0.1:8766",
            interval_seconds=interval,
            fetch_state=fetch_state,
            fetch_healthz=fetch_healthz,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
            **kwargs,
        )
    return sleeps


def test_endpoint_outage_sends_folded_once_then_recovers() -> None:
    notifier = RecordingServiceNotifier()
    state_calls = {"count": 0}

    def fetch_state(_url: str, _timeout: float):
        state_calls["count"] += 1
        if state_calls["count"] <= 6:  # two outage rounds x 3 attempts each
            raise TimeoutError("timed out")
        return base_state()

    def fetch_healthz(_url: str, _timeout: float):
        return _healthz_payload(pid=4242, sha=_LONG_SHA)

    # Production cadence: state timed out at 02:24 and 04:47 Beijing, the same
    # PID answered normally by 06:24; interval 2h keeps >=30min for recovery.
    sleeps = run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 18, 24, tzinfo=UTC),
        rounds=3,
        fetch_state=fetch_state,
        fetch_healthz=fetch_healthz,
    )

    assert sleeps == [7200.0, 30.0, 30.0, 7200.0, 30.0, 30.0, 7200.0, 7200.0]
    assert state_calls["count"] == 7  # 3+3 failed attempts, then one success
    assert len(notifier.messages) == 2

    fail_title, fail_body = notifier.messages[0]
    assert fail_title.startswith("❌ 预测套利：服务不可达（")
    assert fail_title.endswith("）")
    assert "TimeoutError: timed out" in fail_body
    assert "PID 4242" in fail_body
    assert "8 项失败" not in fail_body
    assert "下单就绪" not in fail_body

    recovery_title, recovery_body = notifier.messages[1]
    assert recovery_title.startswith("✅ 预测套利已恢复（")
    assert "PID 4242" in recovery_body


def test_transient_state_timeout_retries_once_and_stays_silent() -> None:
    state_calls = {"count": 0}

    def fetch_state(_url: str, _timeout: float):
        state_calls["count"] += 1
        if state_calls["count"] == 1:
            raise TimeoutError("timed out")
        return base_state()

    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=fetch_state,
        fetch_healthz=lambda *_args: _healthz_payload(),
        sleep_fn=lambda _seconds: None,
    )

    assert report.status == "PASS"
    assert state_calls["count"] == 2
    assert not any(
        check.name == "endpoint" and check.status == "FAIL"
        for check in report.checks
    )

    notifier = RecordingServiceNotifier()
    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 19, 0, tzinfo=UTC),  # 北京 03:00，非摘要时刻
        rounds=1,
        fetch_state=lambda *_args: base_state(),
        fetch_healthz=lambda *_args: _healthz_payload(),
    )

    assert notifier.messages == []


def _warn_relation_state() -> dict[str, object]:
    return base_state(
        relation_discovery={
            "status": "degraded",
            "catalog": {"status": "degraded"},
        }
    )


def test_same_fingerprint_reminds_once_after_24h() -> None:
    notifier = RecordingServiceNotifier()
    warn_state = _warn_relation_state()

    # 北京 09:00 起每 2h 一查；第 13 轮落在 24h 后的同指纹（09:00，<09:30 无摘要）。
    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 8, 23, 0, tzinfo=UTC),
        rounds=13,
        fetch_state=lambda *_args: dict(warn_state),
        fetch_healthz=lambda *_args: _healthz_payload(),
    )

    warns = [
        message
        for message in notifier.messages
        if message[0].startswith("⚠️ 预测套利有警告：关系目录")
    ]
    assert len(warns) == 2
    assert notifier.messages[0][0].startswith("⚠️ 预测套利有警告：关系目录")
    assert notifier.messages[-1] == warns[1]


def test_recovery_requires_30_minutes_of_persistent_failure() -> None:
    def scenario(fail_minutes: int, *, expect_recovery: bool) -> None:
        notifier = RecordingServiceNotifier()
        fail_state = base_state(llm_usage_24h={"calls": 10, "successes": 0})
        behaviors = [dict(fail_state), base_state()]

        run_service_rounds(
            notifier,
            start=datetime(2026, 9, 9, 0, 0, tzinfo=UTC),  # 北京 08:00
            rounds=2,
            interval=float(fail_minutes * 60),
            fetch_state=lambda *_args: behaviors.pop(0),
            fetch_healthz=lambda *_args: _healthz_payload(),
        )

        recoveries = [
            message
            for message in notifier.messages
            if message[0].startswith("✅ 预测套利已恢复（")
        ]
        assert len(recoveries) == (1 if expect_recovery else 0)
        assert notifier.messages[0][0].startswith("❌ 预测套利异常：")

    scenario(20, expect_recovery=False)
    scenario(45, expect_recovery=True)


def test_recovery_delivery_failure_retries_next_pass_cycle(capsys) -> None:
    notifier = FlakyRecoveryNotifier(fail_times=1)
    fail_state = base_state(llm_usage_24h={"calls": 10, "successes": 0})
    behaviors = [dict(fail_state), base_state(), base_state(), base_state()]

    # 北京 06:00 起每 45 分钟一查（全程 <09:30 无日报）：第 1 轮 FAIL，第 2 轮
    # 恢复投递失败，第 3 轮 PASS 重试成功，第 4 轮不再重复发。
    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 8, 22, 0, tzinfo=UTC),  # 北京 06:00
        rounds=4,
        interval=2700.0,  # 45 分钟，满足 30 分钟持续判定
        fetch_state=lambda *_args: behaviors.pop(0),
        fetch_healthz=lambda *_args: _healthz_payload(),
    )

    assert notifier.messages[0][0].startswith("❌ 预测套利异常：")
    recoveries = [
        message
        for message in notifier.messages
        if message[0].startswith("✅ 预测套利已恢复（")
    ]
    assert len(recoveries) == 1
    assert len(notifier.messages) == 2
    assert notifier.recovery_failures == 1
    assert capsys.readouterr().out.count("feishu delivery failed status=recovery") == 1


def test_new_non_pass_segment_after_failed_recovery_keeps_30min_threshold() -> None:
    notifier = FlakyRecoveryNotifier(fail_times=1)
    fail_state = base_state(llm_usage_24h={"calls": 10, "successes": 0})
    behaviors = [dict(fail_state), base_state(), dict(fail_state), base_state(), dict(fail_state), base_state()]

    # 北京 05:15 起，非均匀节奏（启动 sleep 先落到第 1 轮）：段 A 06:00 FAIL
    # （45 分钟）→ 06:45 恢复投递失败；段 B 06:50 FAIL 仅 5 分钟 → 06:55 PASS
    # 不得发恢复；段 C 07:40 FAIL 满 45 分钟 → 08:25 才按 30 分钟阈值发恢复
    # （不得沿用旧时间戳提前发）。
    start = datetime(2026, 9, 8, 21, 15, tzinfo=UTC)  # 北京 05:15，第 1 轮 06:00
    steps = iter(
        [
            timedelta(minutes=45),  # 启动 sleep → 第 1 轮 06:00
            timedelta(minutes=45),  # 第 2 轮 06:45：恢复投递失败
            timedelta(minutes=5),  # 第 3 轮 06:50：新一轮 FAIL（段 B）
            timedelta(minutes=5),  # 第 4 轮 06:55：段 B 仅 5 分钟
            timedelta(minutes=45),  # 第 5 轮 07:40：新一轮 FAIL（段 C）
            timedelta(minutes=45),  # 第 6 轮 08:25：段 C 满 45 分钟
        ]
    )
    clock = {"now": start}

    def now_fn():
        return clock["now"]

    def sleep_fn(_seconds: float) -> None:
        try:
            clock["now"] += next(steps)
        except StopIteration:
            raise _StopRun from None

    with pytest.raises(_StopRun):
        run_service(
            notifier,
            url="http://127.0.0.1:8766",
            interval_seconds=2700.0,
            fetch_state=lambda *_args: behaviors.pop(0),
            fetch_healthz=lambda *_args: _healthz_payload(),
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )

    # 三段同指纹：仅第 1 轮变更通知 + 第 6 轮恢复，无任何提前恢复。
    assert notifier.messages[0][0].startswith("❌ 预测套利异常：")
    recoveries = [
        message
        for message in notifier.messages
        if message[0].startswith("✅ 预测套利已恢复（")
    ]
    assert recoveries == [("✅ 预测套利已恢复（08:25）", recoveries[0][1])]
    assert len(notifier.messages) == 2
    assert notifier.recovery_failures == 1


def test_fingerprint_flapping_is_suppressed_within_60_minutes() -> None:
    notifier = RecordingServiceNotifier()
    warn_state = _warn_relation_state()
    fail_state = base_state(llm_usage_24h={"calls": 10, "successes": 0})
    behaviors = [dict(warn_state), dict(fail_state), dict(warn_state), dict(fail_state)]

    # 60 分钟窗口内 A→B→A→B，每 15 分钟一查。
    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 0, 0, tzinfo=UTC),  # 北京 08:00
        rounds=4,
        interval=900.0,
        fetch_state=lambda *_args: behaviors.pop(0),
        fetch_healthz=lambda *_args: _healthz_payload(),
    )

    assert len(notifier.messages) == 2
    assert notifier.messages[0][0].startswith("⚠️ 预测套利有警告：关系目录")
    assert notifier.messages[1][0].startswith("❌ 预测套利异常：")


def test_daily_summary_sends_once_after_0930_beijing() -> None:
    notifier = RecordingServiceNotifier()

    # 健康日：北京 01:00 起每 2h 一查，全天 12 次检查全部 PASS。
    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 15, 0, tzinfo=UTC),  # 北京 2026-09-09 23:00
        rounds=12,
        fetch_state=lambda *_args: base_state(),
        fetch_healthz=lambda *_args: _healthz_payload(pid=4242, sha=_LONG_SHA),
    )

    assert len(notifier.messages) == 1
    title, body = notifier.messages[0]
    assert title.startswith("📋 预测套利日报（2026-09-10）")
    assert "近24h 检查 6 次" in title
    assert "异常 0 次" in title
    assert "当前 正常" in title
    assert "PID 4242" in body


def test_once_mode_always_sends_exactly_one_message() -> None:
    scenarios = [
        (base_state(), 0),
        (base_state(llm_usage_24h={"calls": 10, "successes": 0}), 2),
        (base_state(relation_discovery={"status": "degraded", "catalog": {"status": "degraded"}}), 1),
    ]
    for state, expected_code in scenarios:
        notifier = RecordingServiceNotifier()
        code = run_service(
            notifier,
            url="http://127.0.0.1:8766",
            interval_seconds=7200.0,
            once=True,
            fetch_state=lambda _url, _timeout, _state=state: dict(_state),
            fetch_healthz=lambda _url, _timeout: _healthz_payload(),
            now_fn=lambda: datetime(2026, 9, 10, 1, 0, tzinfo=UTC),
            sleep_fn=lambda _seconds: None,
        )
        assert code == expected_code
        assert len(notifier.messages) == 1


def test_dashboard_url_line_only_when_configured() -> None:
    fail_report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *_args: base_state(
            llm_usage_24h={"calls": 10, "successes": 0}
        ),
        fetch_healthz=lambda *_args: _healthz_payload(pid=4242, sha=_LONG_SHA),
        sleep_fn=lambda _seconds: None,
    )
    assert fail_report.status == "FAIL"

    unconfigured = format_report(fail_report)
    assert "Dashboard：" not in unconfigured

    configured_report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *_args: base_state(
            llm_usage_24h={"calls": 10, "successes": 0}
        ),
        fetch_healthz=lambda *_args: _healthz_payload(pid=4242, sha=_LONG_SHA),
        sleep_fn=lambda _seconds: None,
        dashboard_url="https://example.test/d",
    )
    configured = format_report(configured_report)
    assert configured.count("https://example.test/d") == 1
    assert "Dashboard：https://example.test/d" in configured

    notifier = RecordingServiceNotifier()
    assert send_report(notifier, configured_report) is True
    assert notifier.messages[0][1].count("https://example.test/d") == 1


# --- Issue 150: 健康监控区分 N_LEG 暂停与服务不可达 -----------------------------

import email.message
import io
import json
from urllib.error import HTTPError

from open_trader.prediction_arbitrage_health import (
    NLegPausedError,
    _classify_state_http_error,
)

_STATE_URL = "http://127.0.0.1:8766/api/prediction-arbitrage/state"


def _http_error(code: int, body: bytes) -> HTTPError:
    return HTTPError(
        _STATE_URL,
        code,
        "Conflict" if code == 409 else "Server Error",
        email.message.Message(),
        io.BytesIO(body),
    )


def test_classify_state_http_error_maps_only_known_pause_contract() -> None:
    # 生产实测暂停契约（issue #150 票面 2026-09-20）。
    paused = _http_error(409, b'{"error":"N_LEG_PAUSED","error_code":"N_LEG_PAUSED"}')
    classified = _classify_state_http_error(paused)
    assert isinstance(classified, NLegPausedError)
    assert classified.error_code == "N_LEG_PAUSED"

    other = _http_error(409, b'{"error_code":"OTHER"}')
    assert _classify_state_http_error(other) is other

    server = _http_error(500, b'{"error_code":"N_LEG_PAUSED"}')
    assert _classify_state_http_error(server) is server

    bad_body = _http_error(409, b"not-json")
    assert _classify_state_http_error(bad_body) is bad_body


def _paused_healthz(**overrides: object) -> dict[str, object]:
    payload = _healthz_payload()
    payload.update(overrides)
    payload["n_leg"] = {"status": "paused", "code": "N_LEG_PAUSED"}
    return payload


def test_paused_healthz_reports_paused_without_state_fetch() -> None:
    healthz = _paused_healthz()
    state_calls = {"count": 0}
    sleeps: list[float] = []

    def fetch_state(_url: str, _timeout: float):
        state_calls["count"] += 1
        raise AssertionError("fetch_state must not be called while paused")

    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=fetch_state,
        fetch_healthz=lambda *_args: dict(healthz),
        sleep_fn=sleeps.append,
    )

    assert report.status == "PAUSED"
    assert state_calls["count"] == 0
    assert 30.0 not in sleeps
    assert all(check.status != "FAIL" for check in report.checks)
    checks = {check.name: check for check in report.checks}
    assert checks["service"].status == "PASS"
    assert checks["process"].status == "PASS"
    assert checks["n_leg"].status == "PASS"
    assert report.summary["pid"] == "42"
    assert report.summary["sha"] == "abc"


def test_paused_report_has_no_synthetic_state_checks() -> None:
    healthz = _paused_healthz()
    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *_args: base_state(),
        fetch_healthz=lambda *_args: dict(healthz),
        sleep_fn=lambda _seconds: None,
    )

    assert report.status == "PAUSED"
    forbidden = {
        "state_status",
        "websocket",
        "thread",
        "heartbeat",
        "universe",
        "breaker",
        "cross_venue",
        "universe_retry",
        "relation_catalog",
        "readiness",
        "auto_eat",
        "llm",
        "endpoint",
    }
    names = {check.name for check in report.checks}
    assert not (names & forbidden)


def test_pause_signal_during_check_rechecks_healthz() -> None:
    running = _healthz_payload()
    paused = _paused_healthz()
    healthz_payloads = [dict(running), dict(paused)]
    healthz_calls = {"count": 0}
    state_calls = {"count": 0}
    sleeps: list[float] = []

    def fetch_healthz(_url: str, _timeout: float):
        healthz_calls["count"] += 1
        return healthz_payloads.pop(0)

    def fetch_state(_url: str, _timeout: float):
        state_calls["count"] += 1
        raise NLegPausedError("HTTP Error 409: Conflict: N_LEG_PAUSED", error_code="N_LEG_PAUSED")

    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=fetch_state,
        fetch_healthz=fetch_healthz,
        sleep_fn=sleeps.append,
    )

    assert report.status == "PAUSED"
    assert state_calls["count"] == 1
    assert healthz_calls["count"] == 2
    assert 30.0 not in sleeps


def test_contradictory_pause_signal_fails_with_both_evidences() -> None:
    healthz = _healthz_payload()
    healthz["n_leg"] = {"status": "running", "code": "N_LEG_RUNNING"}

    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *_args: (
            _ for _ in ()
        ).throw(NLegPausedError("HTTP Error 409: Conflict: N_LEG_PAUSED", error_code="N_LEG_PAUSED")),
        fetch_healthz=lambda *_args: dict(healthz),
        sleep_fn=lambda _seconds: None,
    )

    assert report.status == "FAIL"
    checks = {check.name: check for check in report.checks}
    assert checks["endpoint"].status == "PASS"
    failing = [check for check in report.checks if check.status == "FAIL"]
    assert failing
    assert any(
        "N_LEG_PAUSED" in check.reason and "n_leg" in check.reason for check in failing
    )

    notifier = RecordingNotifier()
    assert send_report(notifier, report) is True
    title, _body = notifier.messages[0]
    assert title.startswith("❌ 预测套利异常")
    assert "服务不可达" not in title


def test_unknown_409_keeps_retry_and_fails_endpoint() -> None:
    body = json.dumps({"error_code": "OTHER"}).encode("utf-8")
    state_calls = {"count": 0}
    sleeps: list[float] = []

    def fetch_state(_url: str, _timeout: float):
        state_calls["count"] += 1
        raise _http_error(409, body)

    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=fetch_state,
        fetch_healthz=lambda *_args: _healthz_payload(),
        sleep_fn=sleeps.append,
    )

    assert report.status == "FAIL"
    checks = {check.name: check for check in report.checks}
    assert checks["endpoint"].status == "FAIL"
    assert "409" in checks["endpoint"].reason
    assert state_calls["count"] == 3
    assert sleeps == [30.0, 30.0]


def _run_paused_check() -> object:
    healthz = _paused_healthz()
    return run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *_args: base_state(),
        fetch_healthz=lambda *_args: dict(healthz),
        sleep_fn=lambda _seconds: None,
    )


def test_send_report_paused_uses_quiet_title_and_body() -> None:
    report = _run_paused_check()
    notifier = RecordingNotifier()

    assert send_report(notifier, report) is True
    assert len(notifier.messages) == 1
    title, body = notifier.messages[0]
    assert title.startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")
    assert title.endswith("）")
    assert "PID 42" in body
    for text in (title, body):
        assert "不可达" not in text
        assert "已恢复" not in text
        assert "下单就绪" not in text


def test_paused_report_formats_and_serializes() -> None:
    report = _run_paused_check()

    text = format_report(report)
    assert "多腿套利已暂停" in text
    assert "N_LEG_PAUSED" in text

    data = report_to_dict(report)
    assert data["status"] == "PAUSED"
    assert json.dumps(data, ensure_ascii=False)


def test_stable_pause_sends_once_then_stays_silent() -> None:
    notifier = RecordingServiceNotifier()
    paused_healthz = _paused_healthz()
    state_calls = {"count": 0}

    def fetch_state(_url: str, _timeout: float):
        state_calls["count"] += 1
        raise AssertionError("paused rounds must not fetch state")

    # run_service_rounds 先休眠一个 interval 再开始第 1 轮，每轮结束后各休眠一次：
    # 3 轮全暂停共 4 次 7200 秒休眠（与既有 outage 用例 rounds=3 的四个 7200 一致）。
    sleeps = run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 18, 24, tzinfo=UTC),  # 北京 02:24，全程无日报
        rounds=3,
        fetch_state=fetch_state,
        fetch_healthz=lambda *_args: dict(paused_healthz),
    )

    assert len(notifier.messages) == 1
    title, _body = notifier.messages[0]
    assert title.startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")
    assert state_calls["count"] == 0
    assert sleeps == [7200.0, 7200.0, 7200.0, 7200.0]
    assert not any(
        title.startswith(("❌", "✅")) for title, _body in notifier.messages
    )


def test_running_pause_running_sends_single_pause_notice() -> None:
    notifier = RecordingServiceNotifier()
    running = _healthz_payload()
    paused = _paused_healthz()
    healthz_payloads = [dict(running), dict(paused), dict(running)]

    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 18, 24, tzinfo=UTC),  # 北京 02:24，全程无日报
        rounds=3,
        fetch_state=lambda *_args: base_state(),
        fetch_healthz=lambda *_args: healthz_payloads.pop(0),
    )

    assert len(notifier.messages) == 1
    assert notifier.messages[0][0].startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")
    assert not any("已恢复" in title for title, _body in notifier.messages)


def test_outage_then_stable_pause_sends_folded_then_pause() -> None:
    notifier = RecordingServiceNotifier()
    running = _healthz_payload()
    paused = _paused_healthz()
    healthz_payloads = [dict(running), dict(paused), dict(paused)]

    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 18, 24, tzinfo=UTC),  # 北京 02:24，全程无日报
        rounds=3,
        fetch_state=lambda *_args: (_ for _ in ()).throw(TimeoutError("timed out")),
        fetch_healthz=lambda *_args: healthz_payloads.pop(0),
    )

    assert len(notifier.messages) == 2
    assert notifier.messages[0][0].startswith("❌ 预测套利：服务不可达（")
    assert notifier.messages[1][0].startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")
    assert not any("已恢复" in title for title, _body in notifier.messages)


def test_outage_pause_running_absorbs_recovery() -> None:
    notifier = RecordingServiceNotifier()
    running = _healthz_payload()
    paused = _paused_healthz()
    healthz_payloads = [dict(running), dict(paused), dict(running)]
    state_calls = {"count": 0}

    def fetch_state(_url: str, _timeout: float):
        state_calls["count"] += 1
        if state_calls["count"] <= 3:  # first round: 3 timeout attempts
            raise TimeoutError("timed out")
        return base_state()

    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 18, 24, tzinfo=UTC),  # 北京 02:24，全程无日报
        rounds=3,
        fetch_state=fetch_state,
        fetch_healthz=lambda *_args: healthz_payloads.pop(0),
    )

    assert len(notifier.messages) == 2
    assert notifier.messages[0][0].startswith("❌ 预测套利：服务不可达（")
    assert notifier.messages[1][0].startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")
    assert not any("已恢复" in title for title, _body in notifier.messages)


def test_paused_daily_summary_counts_zero_abnormal() -> None:
    notifier = RecordingServiceNotifier()
    paused_healthz = _paused_healthz(pid=4242, sha=_LONG_SHA)

    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 15, 0, tzinfo=UTC),  # 北京 2026-09-09 23:00
        rounds=6,
        fetch_state=lambda *_args: base_state(),
        fetch_healthz=lambda *_args: dict(paused_healthz),
    )

    daily = [
        message
        for message in notifier.messages
        if message[0].startswith("📋 预测套利日报")
    ]
    assert len(daily) == 1
    assert "异常 0 次" in daily[0][0]
    assert "正常（多腿暂停）" in daily[0][0]


def test_once_mode_paused_returns_zero_with_single_notice() -> None:
    notifier = RecordingServiceNotifier()
    paused_healthz = _paused_healthz()

    code = run_service(
        notifier,
        url="http://127.0.0.1:8766",
        interval_seconds=7200.0,
        once=True,
        fetch_state=lambda *_args: base_state(),
        fetch_healthz=lambda *_args: dict(paused_healthz),
        now_fn=lambda: datetime(2026, 9, 10, 1, 0, tzinfo=UTC),
        sleep_fn=lambda _seconds: None,
    )

    assert code == 0
    assert len(notifier.messages) == 1
    assert notifier.messages[0][0].startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")


class FlakyPauseNotifier:
    """Recording notifier whose pause notice fails the first `fail_times` deliveries."""

    def __init__(self, fail_times: int) -> None:
        self.messages: list[tuple[str, str]] = []
        self.pause_failures = 0
        self._fail_times = fail_times

    def notify(self, title: str, message: str) -> None:
        if (
            title.startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")
            and self._fail_times > 0
        ):
            self._fail_times -= 1
            self.pause_failures += 1
            raise RuntimeError("feishu transient outage")
        self.messages.append((title, message))


def test_pause_notice_delivery_failure_retries_next_round() -> None:
    # 验收标准 5：暂停通知投递失败时 last_signature 不更新，下一轮重试；
    # 成功后持续暂停静默。
    notifier = FlakyPauseNotifier(fail_times=1)
    paused_healthz = _paused_healthz()

    def fetch_state(_url: str, _timeout: float):
        raise AssertionError("paused rounds must not fetch state")

    run_service_rounds(
        notifier,
        start=datetime(2026, 9, 9, 18, 24, tzinfo=UTC),  # 北京 02:24，全程无日报
        rounds=3,
        fetch_state=fetch_state,
        fetch_healthz=lambda *_args: dict(paused_healthz),
    )

    # 第 1 轮投递失败被记录，第 2 轮重试成功恰好补发 1 条，第 3 轮保持静默。
    assert notifier.pause_failures == 1
    assert len(notifier.messages) == 1
    title, _body = notifier.messages[0]
    assert title.startswith("⏸ 预测套利：服务正常，多腿套利已暂停（")
    assert not any(
        title.startswith(("❌", "✅")) for title, _body in notifier.messages
    )


def test_pause_signal_recheck_transport_failure_fails_with_evidence() -> None:
    # 验收标准 3 第三子句：state 抛 N_LEG_PAUSED 后 healthz 复核传输失败
    # → FAIL 且证据双侧可见（state 收到 N_LEG_PAUSED + 复核失败原因），
    # endpoint 不折叠为「服务不可达」。
    running = _healthz_payload()
    healthz_calls = {"count": 0}
    sleeps: list[float] = []

    def fetch_healthz(_url: str, _timeout: float):
        healthz_calls["count"] += 1
        if healthz_calls["count"] == 1:
            return dict(running)
        raise TimeoutError("healthz recheck timed out")

    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *_args: (
            _ for _ in ()
        ).throw(NLegPausedError("N_LEG_PAUSED", error_code="N_LEG_PAUSED")),
        fetch_healthz=fetch_healthz,
        sleep_fn=sleeps.append,
    )

    assert report.status == "FAIL"
    checks = {check.name: check for check in report.checks}
    assert checks["endpoint"].status == "PASS"
    assert checks["service"].status == "FAIL"
    assert checks["state_status"].status == "FAIL"
    assert "N_LEG_PAUSED" in checks["state_status"].reason
    assert "TimeoutError" in checks["state_status"].reason
    assert "healthz recheck timed out" in checks["state_status"].reason
    # 复核走完整传输重试：3 次尝试、间隔 30 秒。
    assert healthz_calls["count"] == 4
    assert sleeps == [30.0, 30.0]

    notifier = RecordingNotifier()
    assert send_report(notifier, report) is True
    title, _body = notifier.messages[0]
    assert title.startswith("❌ 预测套利异常")
    assert "服务不可达" not in title


def test_pause_signal_recheck_identity_mismatch_fails_with_evidence() -> None:
    # 验收标准 3 第三子句：state 抛 N_LEG_PAUSED 后 healthz 复核身份无效
    # （mode=shadow）→ FAIL，service 失败原因为身份校验，state_status
    # 仍携带 N_LEG_PAUSED 证据。
    running = _healthz_payload()
    shadow = _healthz_payload()
    shadow["mode"] = "shadow"
    healthz_payloads = [dict(running), dict(shadow)]
    sleeps: list[float] = []

    def fetch_healthz(_url: str, _timeout: float):
        return healthz_payloads.pop(0)

    report = run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *_args: (
            _ for _ in ()
        ).throw(NLegPausedError("N_LEG_PAUSED", error_code="N_LEG_PAUSED")),
        fetch_healthz=fetch_healthz,
        sleep_fn=sleeps.append,
    )

    assert report.status == "FAIL"
    checks = {check.name: check for check in report.checks}
    assert checks["service"].status == "FAIL"
    assert checks["service"].reason == "health mode mismatch"
    assert checks["state_status"].status == "FAIL"
    assert "N_LEG_PAUSED" in checks["state_status"].reason
    assert "health mode mismatch" in checks["state_status"].reason
    # 复核载荷返回成功（无传输错误），不触发重试休眠。
    assert sleeps == []

    notifier = RecordingNotifier()
    assert send_report(notifier, report) is True
    title, _body = notifier.messages[0]
    assert title.startswith("❌ 预测套利异常")
    assert "服务不可达" not in title
