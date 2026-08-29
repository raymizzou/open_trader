from __future__ import annotations

import re
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_health import (
    format_report,
    report_to_dict,
    run_health_check,
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
):
    state = payload if payload is not None else base_state(
        llm_usage_24h={"calls": llm[0], "successes": llm[1]}
    )
    return run_health_check(
        url="http://127.0.0.1:8766",
        fetch_state=lambda *args: state,
        fetch_healthz=lambda *args: process if healthz else (_ for _ in ()).throw(ConnectionError("down")),
        notify_configured=notify_configured,
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
