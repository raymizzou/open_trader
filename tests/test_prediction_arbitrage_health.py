from __future__ import annotations

from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_health import (
    _dashboard_git_sha,
    format_report,
    report_to_dict,
    run_health_check,
    send_report,
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
    }
    payload.update(overrides)
    return payload


def run_check(
    *,
    payload: dict[str, object] | None = None,
    healthz: bool = True,
    llm: tuple[int, int] = (10, 10),
    process: dict[str, object] | None = {
        "pid": "42",
        "sha": "abc",
        "expected_sha": "abc",
    },
    notify_configured: bool = True,
):
    return run_health_check(
        url="http://127.0.0.1:8766",
        data_dir=Path("data"),
        repo_root=Path("."),
        fetch_state=lambda *args: payload if payload is not None else base_state(),
        fetch_healthz=lambda *args: healthz,
        llm_stats=lambda *args: llm,
        process_info=lambda *args: process,
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
        data_dir=Path("data"),
        repo_root=Path("."),
        fetch_state=lambda *args: (_ for _ in ()).throw(ConnectionError("down")),
        fetch_healthz=lambda *args: True,
        llm_stats=lambda *args: (10, 10),
        process_info=lambda *args: {"pid": "42", "sha": "abc", "expected_sha": "abc"},
    )
    assert report.status == "FAIL"
    assert any(check.name == "endpoint" and check.status == "FAIL" for check in report.checks)


def test_gateway_down_fails() -> None:
    assert run_check(healthz=False).status == "FAIL"


def test_llm_no_success_fails() -> None:
    assert run_check(llm=(10, 0)).status == "FAIL"


def test_llm_no_calls_passes() -> None:
    assert run_check(llm=(0, 0)).status == "PASS"


def test_process_missing_fails() -> None:
    assert run_check(process=None).status == "FAIL"


def test_process_sha_mismatch_warns() -> None:
    report = run_check(process={"pid": "42", "sha": "old", "expected_sha": "new"})
    assert report.status == "WARN"


def test_notify_unconfigured_warns() -> None:
    assert run_check(notify_configured=False).status == "WARN"


def test_format_pass_is_single_line() -> None:
    text = format_report(run_check())
    assert text.startswith("PASS · heartbeat 0.5s")
    assert "\n" not in text


def test_format_fail_lists_checks() -> None:
    report = run_check(llm=(10, 0))
    text = format_report(report)
    assert text.startswith("FAIL · heartbeat")
    assert "- FAIL llm: 0/10" in text


def test_report_to_dict_is_jsonable() -> None:
    import json

    data = report_to_dict(run_check())
    assert data["status"] == "PASS"
    assert json.dumps(data)


def test_send_report_calls_notifier() -> None:
    calls: list[tuple[str, str]] = []

    class FakeNotifier:
        def notify(self, title: str, message: str) -> None:
            calls.append((title, message))

    assert send_report(FakeNotifier(), run_check()) is True
    assert calls[0][0] == "[预测套利健康检查] PASS"
    assert calls[0][1].startswith("PASS ·")


def test_send_report_returns_false_on_failure() -> None:
    class BrokenNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("boom")

    assert send_report(BrokenNotifier(), run_check()) is False


def test_dashboard_git_sha_reads_startup_log(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "legacy_dashboard" / "launchd.out.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        'dashboard_runtime: {"pid": 44548, "git_sha": '
        '"64809e3f45d8c8f1a53cf80d98c497027dc5d590", "source_state": "clean"}\n'
        'dashboard_runtime: {"pid": 94923, "git_sha": '
        '"ca308877fec207773786c16e98317fcec77aba70", "source_state": "clean"}'
    )
    assert _dashboard_git_sha(tmp_path) == "ca308877fec207773786c16e98317fcec77aba70"


def test_dashboard_git_sha_returns_none_without_log(tmp_path: Path) -> None:
    assert _dashboard_git_sha(tmp_path) is None
