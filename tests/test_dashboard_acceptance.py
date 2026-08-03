from datetime import datetime, timedelta
from decimal import Decimal
from collections.abc import Mapping
import copy
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from open_trader import dashboard_acceptance
import open_trader.prediction_arbitrage_acceptance as prediction_acceptance
from open_trader.prediction_arbitrage_acceptance import SCENARIO_IDS, scenario_results, validate_registry
from open_trader.dashboard_acceptance import (
    _is_actionable_console_error,
    classify_result,
    dashboard_signature,
    validate_dashboard_payload,
    validate_prediction_payload,
    validate_quotes_payload,
)
from open_trader.strategy_drawdown import (
    automatic_bootstrap_strategy_drawdown,
    strategy_parameter_hash,
)


MISSING_FRESH = object()


def test_dashboard_acceptance_allows_current_market_versions() -> None:
    assert "v11" in dashboard_acceptance.TREND_ACCEPTED_STRATEGY_VERSIONS["CN"]
    assert "v9" in dashboard_acceptance.TREND_ACCEPTED_STRATEGY_VERSIONS["US"]
    assert "v9" in dashboard_acceptance.TREND_ACCEPTED_STRATEGY_VERSIONS["HK"]


def test_prediction_acceptance_registry_is_exact_and_ordered() -> None:
    assert len(SCENARIO_IDS) == 63
    assert len(set(SCENARIO_IDS)) == 63
    assert SCENARIO_IDS[:10] == tuple(f"MON-{index:02d}" for index in range(1, 11))
    ui_ids = tuple(item for item in SCENARIO_IDS if item.startswith("UI-"))
    assert ui_ids == tuple(f"UI-{index:02d}" for index in range(1, 15))
    assert SCENARIO_IDS[-3:] == ("OPS-01", "OPS-02", "OPS-03")
    assert validate_registry(scenario_results()) == []


def test_prediction_live_acceptance_reports_authenticated_no_submit_evidence() -> None:
    live = [
        row for row in scenario_results(live_available=True)
        if row.scenario_id.startswith("LIVE-")
    ]

    assert {row.status for row in live} == {"PASS"}
    assert {row.detail for row in live} == {"authenticated no-submit preflight"}


def test_make_acceptance_wires_prediction_registry_before_dashboard_verifier() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")
    registry = "python -m open_trader.prediction_arbitrage_acceptance"
    assert registry in makefile
    assert makefile.index(registry) < makefile.index("python -m open_trader.dashboard_acceptance")
    assert '--config "$(WORKTREE_ROOT)/config/prediction_arbitrage.json"' in makefile


@pytest.mark.parametrize(("result", "available"), (("PASS", True), ("BLOCKED", False)))
def test_prediction_live_acceptance_requires_passed_no_submit_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: str,
    available: bool,
) -> None:
    config = tmp_path / "prediction.json"
    config.write_text(
        json.dumps(
            {
                "signer_address": "0x1111111111111111111111111111111111111111",
                "wallet_address": "0x2222222222222222222222222222222222222222",
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def preflight_report(self) -> dict[str, object]:
            return {"result": result}

    monkeypatch.setattr(
        prediction_acceptance.PolymarketTradingClient,
        "from_keychain",
        lambda _: Client(),
    )

    assert prediction_acceptance._live_environment_available(config) is available


def test_prediction_payload_validation_fails_closed_for_stale_actionable_rows() -> None:
    payload = {
        "status": "degraded",
        "health": {"status": "degraded", "degraded_reasons": ["heartbeat_stale"]},
        "stale": True,
        "events": [{"title": "event", "volume_24h": "100"}],
        "event_count": 1,
        "opportunities": [{"opportunity_id": "opp", "actionable": True}],
        "breaker": {"open": False},
    }
    errors = validate_prediction_payload(payload)
    assert any("actionable" in error for error in errors)
    payload["opportunities"] = []
    assert validate_prediction_payload(payload) == []


def test_prediction_payload_validation_requires_health_and_complete_actionable_facts() -> None:
    payload = {
        "status": "healthy",
        "events": [],
        "event_count": 0,
        "opportunities": [{"opportunity_id": "opp", "actionable": True}],
        "breaker": {"open": False},
    }
    errors = validate_prediction_payload(payload)
    assert any("health" in error for error in errors)
    assert any("actionable opportunity 字段不完整" in error for error in errors)

    payload["health"] = {"status": "degraded", "degraded_reasons": ["heartbeat_stale"]}
    errors = validate_prediction_payload(payload)
    assert any("异常/执行锁定" in error for error in errors)


def serialized_trend_account(
    *, fresh: object = MISSING_FRESH,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_date": "2026-07-14",
        "net_value": "100000",
        "available_cash": "50000",
        "positions": [],
        "exceptions": [],
    }
    if fresh is not MISSING_FRESH:
        payload["fresh"] = fresh
    return payload


def serialized_trend_position() -> dict[str, object]:
    return {
        "symbol": "VIXY",
        "name": "ProShares VIX",
        "asset_class": "etf",
        "quantity": "10",
        "avg_cost_price": None,
        "market_value": "500",
    }


def test_make_acceptance_allows_an_isolated_dashboard_url_and_log() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")

    assert "WORKTREE_ROOT := $(CURDIR)" in makefile
    assert "REPOSITORY_ROOT :=" in makefile
    assert "PYTHONSAFEPATH=1" in makefile
    assert 'PYTHONPATH="$(WORKTREE_ROOT):$(WORKTREE_ROOT)/src"' in makefile
    assert '"$(WORKTREE_ROOT)/tests" -q' in makefile
    assert 'DASHBOARD_URL ?= http://127.0.0.1:8766' in makefile
    assert (
        'DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/frontend_gateway/launchd.out.log'
        in makefile
    )
    assert 'LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767' in makefile
    assert (
        'LEGACY_DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/legacy_dashboard/launchd.out.log'
        in makefile
    )
    assert "test:\n\t.venv/bin/python -m pytest -q" in makefile
    assert "acceptance: test" not in makefile
    assert "EXPECTED_CN" not in makefile
    assert '--url "$(DASHBOARD_URL)"' in makefile
    assert '--log "$(DASHBOARD_LOG)"' in makefile
    assert "--expected-cn" not in makefile
    assert "WAIT_SECONDS" not in makefile
    assert "--wait-seconds" not in makefile


def test_browser_ignores_chrome_unattributed_404_but_not_app_errors() -> None:
    assert not _is_actionable_console_error(
        "Failed to load resource: the server responded with a status of 404 (Not Found)"
    )
    assert _is_actionable_console_error("Uncaught TypeError: failed")


def test_acceptance_screenshot_cleanup_removes_only_exact_expected_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard_acceptance, "ACCEPTANCE_SCREENSHOT_DIR", tmp_path
    )
    expected = dashboard_acceptance.ACCEPTANCE_SCREENSHOT_NAMES
    for name in (*expected, "keep-me.png"):
        (tmp_path / name).write_bytes(b"old")

    started_at_ns = dashboard_acceptance._prepare_acceptance_screenshots()

    assert isinstance(started_at_ns, int) and started_at_ns > 0
    assert all(not (tmp_path / name).exists() for name in expected)
    assert (tmp_path / "keep-me.png").read_bytes() == b"old"


def test_acceptance_browser_viewport_and_screenshot_matrix_is_exact() -> None:
    assert dashboard_acceptance.ACCEPTANCE_BROWSER_VIEWPORTS == (
        ("wide_desktop", {"width": 1920, "height": 1080}),
        ("desktop", {"width": 1440, "height": 1000}),
        ("tablet", {"width": 760, "height": 1000}),
        ("mobile", {"width": 375, "height": 844}),
    )
    assert dashboard_acceptance.ACCEPTANCE_SCREENSHOT_NAMES == (
        "wide_desktop-portfolio.png",
        "1920-trend-report.png",
        "desktop-portfolio.png",
        "1440-trend-report.png",
        "1440-trend-review.png",
        "tablet-portfolio.png",
        "760-trend-report.png",
        "mobile-portfolio.png",
        "375-trend-report.png",
        "375-trend-review.png",
    )


def test_live_trend_review_screenshot_capture_uses_acceptance_matrix(
    tmp_path: Path,
) -> None:
    screenshots: list[tuple[str, bool]] = []

    class Page:
        def __init__(self, width: int) -> None:
            self.viewport_size = {"width": width}

        def screenshot(self, *, path: str, full_page: bool) -> None:
            screenshots.append((path, full_page))

    dashboard_acceptance._capture_trend_review_screenshot(
        Page(1440), "eastmoney", tmp_path
    )
    dashboard_acceptance._capture_trend_review_screenshot(
        Page(375), "eastmoney", tmp_path
    )
    dashboard_acceptance._capture_trend_review_screenshot(
        Page(760), "eastmoney", tmp_path
    )

    assert screenshots == [
        (str(tmp_path / "1440-trend-review.png"), True),
        (str(tmp_path / "375-trend-review.png"), True),
    ]


def test_tablet_trend_cards_use_the_actual_viewport_width() -> None:
    source = inspect.getsource(dashboard_acceptance._check_account_holdings)

    assert 'box["x"] + box["width"] <= width + 1' in source
    assert 'box["x"] + box["width"] <= 376' not in source


def test_acceptance_screenshot_validation_requires_current_nonempty_exact_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard_acceptance, "ACCEPTANCE_SCREENSHOT_DIR", tmp_path
    )
    tmp_path.mkdir(exist_ok=True)
    started_at_ns = 2_000_000_000
    for name in dashboard_acceptance.ACCEPTANCE_SCREENSHOT_NAMES:
        path = tmp_path / name
        path.write_bytes(b"fresh")
        os.utime(path, ns=(started_at_ns, started_at_ns))

    assert dashboard_acceptance._validate_acceptance_screenshots(
        started_at_ns
    ) == []

    stale = tmp_path / dashboard_acceptance.ACCEPTANCE_SCREENSHOT_NAMES[0]
    os.utime(stale, ns=(started_at_ns - 1, started_at_ns - 1))
    empty = tmp_path / dashboard_acceptance.ACCEPTANCE_SCREENSHOT_NAMES[1]
    empty.write_bytes(b"")
    missing = tmp_path / dashboard_acceptance.ACCEPTANCE_SCREENSHOT_NAMES[2]
    missing.unlink()

    errors = dashboard_acceptance._validate_acceptance_screenshots(started_at_ns)

    assert any(stale.name in error and "过期" in error for error in errors)
    assert any(empty.name in error and "空文件" in error for error in errors)
    assert any(missing.name in error and "缺失" in error for error in errors)


def test_acceptance_uses_absolute_shared_reports_dir_from_payload(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    reports = tmp_path / "shared" / "reports"
    worktree.mkdir()
    reports.mkdir(parents=True)

    assert dashboard_acceptance._effective_reports_dir(
        {"reports_dir": str(reports)}, process_cwd=worktree
    ) == reports.resolve()


def test_acceptance_resolves_relative_reports_dir_against_process_cwd(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    reports = worktree / "shared" / "reports"
    reports.mkdir(parents=True)

    assert dashboard_acceptance._effective_reports_dir(
        {"reports_dir": "shared/reports"}, process_cwd=worktree
    ) == reports.resolve()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reports_dir": None},
        {"reports_dir": ""},
        {"reports_dir": 123},
        {"reports_dir": "../reports"},
        {"reports_dir": "missing/reports"},
    ],
)
def test_acceptance_rejects_invalid_reports_dir_configuration(
    tmp_path: Path, payload: dict[str, object],
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    if payload.get("reports_dir") == "../reports":
        (tmp_path / "reports").mkdir()

    with pytest.raises(ValueError, match="Dashboard reports_dir"):
        dashboard_acceptance._effective_reports_dir(
            payload, process_cwd=worktree
        )


def _run_acceptance_main_with_reports(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    report_dirs: list[Path],
    *,
    legacy_pid: int = 456,
    browser_log_text: str = "",
    log_is_directory: bool = False,
    log_read_error: OSError | None = None,
    controller_errors: list[str] | None = None,
    public_calls: list[str] | None = None,
) -> tuple[int, dict[str, object], list[Path | None]]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    payloads = iter({"reports_dir": str(path)} for path in report_dirs)
    quote_payloads = iter((valid_quotes_payload(),))
    browser_reports: list[Path | None] = []
    started_at = datetime.fromisoformat("2026-08-01T12:00:00+08:00")
    gateway_log = tmp_path / "gateway.log"
    legacy_log = tmp_path / "legacy.log"
    gateway_log.write_text(
        "frontend_gateway_runtime: "
        + json.dumps({
            "pid": 123,
            "git_sha": "accepted-sha",
            "cwd": str(worktree.resolve()),
            "source_state": "clean",
            "started_at": "2026-08-01T12:00:01+08:00",
        })
        + "\n",
        encoding="utf-8",
    )
    if log_is_directory:
        legacy_log.mkdir()
    else:
        legacy_log.write_text(
            "dashboard_runtime: "
            + json.dumps({
                "pid": legacy_pid,
                "git_sha": "accepted-sha",
                "cwd": str(worktree.resolve()),
                "source_state": "clean",
                "started_at": "2026-08-01T12:00:01+08:00",
            })
            + "\n",
            encoding="utf-8",
        )
    if log_read_error is not None:
        original_read_text = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == legacy_log:
                raise log_read_error
            return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        dashboard_acceptance, "_project_data_dir", lambda root: tmp_path / "data"
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_latest_phillips_expectation",
        lambda data_dir: (Decimal("1"), "2026-07"),
    )
    listeners = {
        "http://127.0.0.1:8766": (123, worktree.resolve()),
        "http://127.0.0.1:8767": (legacy_pid, worktree.resolve()),
    }
    health = {
        "http://127.0.0.1:8766": {
            **_runtime_health(
                worktree.resolve(),
                module="frontend_gateway",
                schema="open_trader.frontend_gateway.health.v1",
                pid=123,
            ),
            "upstream_status": "ok",
        },
        "http://127.0.0.1:8767": _runtime_health(
            worktree.resolve(),
            module="legacy_dashboard",
            schema="open_trader.legacy_dashboard.health.v1",
            pid=legacy_pid,
        ),
    }
    monkeypatch.setattr(dashboard_acceptance, "_listener", lambda url: listeners[url])
    monkeypatch.setattr(
        dashboard_acceptance.subprocess,
        "check_output",
        lambda *args, **kwargs: "accepted-sha\n",
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_process_started_at",
        lambda *_args: started_at,
    )
    monkeypatch.setattr(
        dashboard_acceptance, "_source_changes", lambda *_args: []
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_expected_cn_holdings",
        lambda *_args: 2,
    )
    def record_public(url: str) -> None:
        if public_calls is not None:
            public_calls.append(url)

    def fetch_payload(url: str) -> dict[str, object]:
        record_public(url)
        return next(payloads)

    def fetch_quotes(url: str) -> dict[str, object]:
        record_public(url)
        return next(quote_payloads)

    monkeypatch.setattr(dashboard_acceptance, "_fetch_payload", fetch_payload)
    monkeypatch.setattr(dashboard_acceptance, "_fetch_quotes_payload", fetch_quotes)
    def health_payload(url: str, path: str) -> dict[str, object]:
        assert path == "/healthz"
        return health[url]

    monkeypatch.setattr(dashboard_acceptance, "_fetch_json_path", health_payload)
    monkeypatch.setattr(
        dashboard_acceptance.time,
        "sleep",
        lambda seconds: pytest.fail(f"acceptance slept for {seconds} seconds"),
    )
    monkeypatch.setattr(
        dashboard_acceptance, "validate_dashboard_payload", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "validate_integrated_candidate",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_trend_controller_errors",
        lambda *args, **kwargs: list(controller_errors or []),
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_account_sync_worker_errors",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_configured_simulate_account_ids",
        lambda *_args: {"tiger": 1, "phillips": 2, "eastmoney": 3},
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_simulated_accounts",
        lambda *_args: ({}, [], None),
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_history_endpoints",
        lambda *_args: ({}, []),
    )
    def browser_check(
        url: str, expected_cn: int, payload: dict[str, object],
        reports_dir: Path | None = None,
        simulate_payloads: object = None,
        history_expectations: object = None,
    ) -> tuple[list[str], None]:
        del simulate_payloads, history_expectations
        record_public(url)
        browser_reports.append(reports_dir)
        if browser_log_text:
            with legacy_log.open("a", encoding="utf-8") as handle:
                handle.write(browser_log_text)
        return [], None

    monkeypatch.setattr(dashboard_acceptance, "_browser_check", browser_check)
    status = dashboard_acceptance.main([
        "--expected-root", str(worktree),
        "--log", str(gateway_log),
        "--legacy-log", str(legacy_log),
    ])
    result = json.loads(capsys.readouterr().out)
    return status, result, browser_reports


def test_acceptance_main_passes_external_api_reports_dir_to_browser_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    external = tmp_path / "shared" / "reports"
    external.mkdir(parents=True)

    status, result, browser_reports = _run_acceptance_main_with_reports(
        monkeypatch, capsys, tmp_path, [external, external]
    )

    assert status == 0
    assert result["status"] == "PASS"
    assert browser_reports == [external.resolve()]


def test_acceptance_main_reports_distinct_dual_runtime_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()

    status, result, _ = _run_acceptance_main_with_reports(
        monkeypatch, capsys, tmp_path, [reports, reports]
    )

    assert status == 0
    assert result["pid"] == 123
    assert result["gateway_pid"] == 123
    assert result["legacy_pid"] == 456


def test_acceptance_rejects_missing_legacy_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard_acceptance,
        "_listener",
        lambda _url: (_ for _ in ()).throw(
            RuntimeError("端口 8767 没有唯一监听进程")
        ),
    )

    pid, cwd, started_at, errors = dashboard_acceptance._runtime_evidence(
        "Legacy Dashboard",
        url="http://127.0.0.1:8767",
        expected_schema="open_trader.legacy_dashboard.health.v1",
        expected_module="legacy_dashboard",
        expected_root=tmp_path,
        expected_sha="accepted-sha",
    )

    assert pid is None
    assert cwd == tmp_path.resolve()
    assert started_at is None
    assert any("Legacy Dashboard" in error and "唯一监听" in error for error in errors)


def test_acceptance_rejects_listener_cwd_and_running_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    monkeypatch.setattr(
        dashboard_acceptance, "_listener", lambda _url: (456, wrong.resolve())
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_process_started_at",
        lambda _pid: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    monkeypatch.setattr(dashboard_acceptance, "_source_changes", lambda _cwd: [])
    monkeypatch.setattr(
        dashboard_acceptance.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "old-sha\n",
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_fetch_json_path",
        lambda *_args: _runtime_health(
            tmp_path,
            module="legacy_dashboard",
            schema="open_trader.legacy_dashboard.health.v1",
            pid=456,
        ),
    )

    _, _, _, errors = dashboard_acceptance._runtime_evidence(
        "Legacy Dashboard",
        url="http://127.0.0.1:8767",
        expected_schema="open_trader.legacy_dashboard.health.v1",
        expected_module="legacy_dashboard",
        expected_root=tmp_path,
        expected_sha="accepted-sha",
    )

    assert any("工作目录" in error for error in errors)
    assert any("运行 Git SHA" in error for error in errors)


def test_make_acceptance_wires_gateway_and_legacy_runtime_logs() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")

    assert "logs/frontend_gateway/launchd.out.log" in makefile
    assert "LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767" in makefile
    assert "logs/legacy_dashboard/launchd.out.log" in makefile
    assert '--legacy-url "$(LEGACY_DASHBOARD_URL)"' in makefile
    assert '--legacy-log "$(LEGACY_DASHBOARD_LOG)"' in makefile


def test_acceptance_main_rejects_same_gateway_and_legacy_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()

    status, result, _ = _run_acceptance_main_with_reports(
        monkeypatch,
        capsys,
        tmp_path,
        [reports, reports],
        legacy_pid=123,
    )

    assert status == 1
    assert result["status"] == "FAIL"
    assert any("不同 PID" in error for error in result["errors"])


def test_acceptance_business_and_browser_checks_stay_on_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    calls: list[str] = []

    status, _, _ = _run_acceptance_main_with_reports(
        monkeypatch,
        capsys,
        tmp_path,
        [reports, reports],
        public_calls=calls,
    )

    assert status == 0
    assert calls
    assert set(calls) == {"http://127.0.0.1:8766"}


def test_acceptance_main_fails_when_reports_dir_changes_during_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = tmp_path / "shared" / "reports-one"
    second = tmp_path / "shared" / "reports-two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    status, result, browser_reports = _run_acceptance_main_with_reports(
        monkeypatch, capsys, tmp_path, [first, second]
    )

    assert status == 1
    assert result["status"] == "FAIL"
    assert "账户刷新前后的 Dashboard reports_dir 不一致" in result["errors"]
    assert browser_reports == [second.resolve()]


def test_acceptance_main_fails_when_actual_refresh_changes_frozen_advice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "shared" / "reports"
    reports.mkdir(parents=True)
    signatures = iter((("first",), ("second",)))
    monkeypatch.setattr(
        dashboard_acceptance,
        "trend_advice_signature",
        lambda _payload: next(signatures),
    )

    status, result, _ = _run_acceptance_main_with_reports(
        monkeypatch, capsys, tmp_path, [reports, reports]
    )

    assert status == 1
    assert result["status"] == "FAIL"
    assert "实盘刷新改写了冻结建议、Kelly 或模拟统计" in result["errors"]


def test_acceptance_main_fails_on_traceback_written_during_browser_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "shared" / "reports"
    reports.mkdir(parents=True)

    status, result, _ = _run_acceptance_main_with_reports(
        monkeypatch,
        capsys,
        tmp_path,
        [reports, reports],
        browser_log_text="Traceback (most recent call last):\nBrokenPipeError",
    )

    assert status == 1
    assert result["status"] == "FAIL"
    assert "日志包含错误标记：Traceback (most recent call last)" in result["errors"]


def test_acceptance_main_fails_on_controller_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "shared" / "reports"
    reports.mkdir(parents=True)

    status, result, _ = _run_acceptance_main_with_reports(
        monkeypatch,
        capsys,
        tmp_path,
        [reports, reports],
        controller_errors=["tiger 控制器不可用或阻塞"],
    )

    assert status == 1
    assert result["status"] == "FAIL"
    assert "tiger 控制器不可用或阻塞" in result["errors"]


@pytest.mark.parametrize(
    ("options", "error_type"),
    [
        ({"log_is_directory": True}, "IsADirectoryError"),
        ({"log_read_error": FileNotFoundError("log vanished")}, "FileNotFoundError"),
    ],
)
def test_acceptance_main_reports_log_read_errors_as_json_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: dict[str, object],
    error_type: str,
) -> None:
    reports = tmp_path / "shared" / "reports"
    reports.mkdir(parents=True)

    try:
        status, result, _ = _run_acceptance_main_with_reports(
            monkeypatch,
            capsys,
            tmp_path,
            [reports, reports],
            **options,
        )
    except OSError as exc:
        pytest.fail(f"acceptance main leaked {type(exc).__name__}: {exc}")

    assert status == 1
    assert result["status"] == "FAIL"
    assert any(
        f"日志读取失败：{error_type}" in error for error in result["errors"]
    )


def test_acceptance_rejects_api_projection_that_drops_frozen_action(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_us_tiger" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": "VIXY"}],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "excluded": {},
        "industry_concentration": [],
        "data_sources": [],
    }), encoding="utf-8")
    projected = {
        "available": True,
        "broker": "tiger",
        "market": "US",
        "report_date": "2026-07-15",
        "data_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "sell_actions": [],
        "buy_actions": [],
        "hold_actions": [],
        "review_actions": [],
        "counts": {"sell": 0, "buy": 0, "hold": 0, "review": 0},
        "audit": {
            "artifact": "2026-07-15.json",
            "candidates": [],
            "excluded": {},
            "industry_concentration": [],
            "data_sources": [],
        },
    }

    with pytest.raises(AssertionError, match="冻结报告动作与 API 投影不一致"):
        dashboard_acceptance._check_trend_artifact_projection(
            reports, "tiger", projected
        )


def test_acceptance_allows_dashboard_only_holding_projection_fields(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_us_tiger" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [{
                "action": "HOLD", "symbol": "EOG", "reason": "trend_intact",
            }],
            "top10_candidates": [],
        },
        "signal_snapshots": {"holdings": {"EOG": {"phase": "立夏"}}},
        "excluded": {},
        "industry_concentration": [],
        "data_sources": [],
    }), encoding="utf-8")
    projected = {
        "available": True,
        "broker": "tiger",
        "market": "US",
        "report_date": "2026-07-15",
        "data_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "sell_actions": [],
        "buy_actions": [],
        "hold_actions": [{
            "action": "HOLD", "symbol": "EOG", "reason": "trend_intact",
            "phase": "立夏",
            "trend_report_state": "included",
            "option_anomaly": {
                "available": False,
                "status": "missing",
                "reason": "富途未返回该标的期权异动",
            },
        }],
        "review_actions": [],
        "counts": {"sell": 0, "buy": 0, "hold": 1, "review": 0},
        "audit": {
            "artifact": "2026-07-15.json",
            "candidates": [],
            "excluded": {},
            "industry_concentration": [],
            "data_sources": [],
        },
    }

    dashboard_acceptance._check_trend_artifact_projection(
        reports, "tiger", projected
    )


def test_acceptance_recognizes_only_strict_partial_sell_actions() -> None:
    partial = {
        "action": "SELL_PARTIAL",
        "symbol": "AAPL",
        "reason": "overheat_take_profit",
        "target_fraction": "0.30",
        "position_started_for": "2026-07-01",
        "estimated_shares": 3,
        "lot_size": 1,
        "overheat_signals": ["boiling"],
    }

    assert not dashboard_acceptance._trend_action_needs_review(partial)
    assert dashboard_acceptance._trend_action_needs_review(
        {**partial, "overheat_signals": []}
    )
    assert dashboard_acceptance._trend_action_needs_review(
        {**partial, "estimated_shares": 301, "lot_size": 100}
    )
    assert dashboard_acceptance._trend_action_needs_review(
        {**partial, "overheat_signals": ["boiling", "boiling"]}
    )
    assert dashboard_acceptance._trend_action_needs_review(
        {**partial, "overheat_signals": [{"signal": "boiling"}]}
    )


def test_acceptance_suppresses_partial_when_same_symbol_has_full_exit(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_a_share" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    partial = {
        "action": "SELL_PARTIAL",
        "symbol": "SH.600001",
        "reason": "overheat_take_profit",
        "target_fraction": "0.30",
        "position_started_for": "2026-07-01",
        "estimated_shares": 300,
        "lot_size": 100,
        "overheat_signals": ["boiling"],
    }
    full = {"action": "SELL_ALL", "symbol": "600001", "reason": "danger_signal"}
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "formal_actions": [partial, full],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "excluded": {},
        "industry_concentration": [],
        "data_sources": [],
    }), encoding="utf-8")
    projected = {
        "available": True,
        "broker": "eastmoney",
        "market": "CN",
        "report_date": "2026-07-15",
        "data_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "sell_actions": [full],
        "buy_actions": [],
        "hold_actions": [],
        "review_actions": [],
        "counts": {"sell": 1, "buy": 0, "hold": 0, "review": 0},
        "audit": {
            "artifact": "2026-07-15.json",
            "candidates": [],
            "excluded": {},
            "industry_concentration": [],
            "data_sources": [],
        },
    }

    dashboard_acceptance._check_trend_artifact_projection(
        reports, "eastmoney", projected
    )


def test_acceptance_rejects_unsafe_trend_artifact_name(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="产物文件名无效"):
        dashboard_acceptance._check_trend_artifact_projection(
            tmp_path,
            "tiger",
            {"available": True, "audit": {"artifact": "../secret.json"}},
        )


def test_acceptance_checks_complete_cn_signal_candidate_projection(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_a_share" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    complete = [
        {"symbol": "688046", "eligible": True, "rank": 1},
        {
            "symbol": "600000", "eligible": False, "rank": None,
            "excluded_reasons": ["strength_below_95"],
        },
    ]
    review = {
        "action": "MANUAL_REVIEW", "symbol": "600036", "name": "招商银行",
        "reason": "holding_kline_unavailable",
    }
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T20:00:00+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [review],
            "top10_candidates": [complete[0]],
        },
        "signal_snapshots": {"candidates": complete},
    }), encoding="utf-8")
    projected = {
        "report_date": "2026-07-15",
        "data_date": "2026-07-14",
        "generated_at": "2026-07-15T20:00:00+08:00",
        "sell_actions": [], "buy_actions": [], "hold_actions": [],
        "review_actions": [review],
        "counts": {"sell": 0, "buy": 0, "hold": 0, "review": 1},
        "audit": {
            "artifact": artifact.name, "candidates": complete, "excluded": {},
            "industry_concentration": [], "data_sources": [],
        },
    }

    dashboard_acceptance._check_trend_artifact_projection(
        reports, "eastmoney", projected
    )


@pytest.mark.parametrize("field", ["industry", "filter_price", "close"])
@pytest.mark.parametrize("value", [None, "", "-"])
def test_acceptance_rejects_missing_cn_buy_fact(
    tmp_path: Path, field: str, value: object,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_a_share" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    buy = {
        "action": "BUY", "symbol": "688046", "name": "药康生物",
        "industry": "医疗服务", "filter_price": "29.14", "close": "28.81",
    }
    buy[field] = value
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T20:00:00+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "formal_actions": [buy], "holding_decisions": [],
            "top10_candidates": [],
        },
        "signal_snapshots": {"candidates": []},
        "excluded": {}, "industry_concentration": [], "data_sources": [],
    }), encoding="utf-8")
    projected = {
        "report_date": "2026-07-15", "data_date": "2026-07-14",
        "generated_at": "2026-07-15T20:00:00+08:00",
        "sell_actions": [], "buy_actions": [buy], "hold_actions": [],
        "review_actions": [],
        "counts": {"sell": 0, "buy": 1, "hold": 0, "review": 0},
        "audit": {
            "artifact": artifact.name, "candidates": [], "excluded": {},
            "industry_concentration": [], "data_sources": [],
        },
    }

    with pytest.raises(AssertionError, match="A 股正式买入缺少"):
        dashboard_acceptance._check_trend_artifact_projection(
            reports, "eastmoney", projected
        )


@pytest.mark.parametrize(
    "fresh", [False, MISSING_FRESH, None, "yes"]
)
def test_acceptance_accepts_actionable_buy_for_non_realtime_account(
    tmp_path: Path, fresh: object,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_us_tiger" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    buy = {"action": "BUY", "symbol": "VIXY"}
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=fresh),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [buy],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "excluded": {},
        "industry_concentration": [],
        "data_sources": [],
    }), encoding="utf-8")
    projected = {
        "report_date": "2026-07-15",
        "data_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "sell_actions": [],
        "buy_actions": [
            {
                **buy,
                "execution": {
                    "status": "missed",
                    "filled_qty": "",
                    "target_qty": "",
                    "avg_fill_price": "",
                    "order_ids": [],
                    "updated_at": "2026-07-15T16:00:00-04:00",
                    "reason": "buy_window_closed",
                },
            }
        ],
        "hold_actions": [],
        "review_actions": [],
        "counts": {"sell": 0, "buy": 1, "hold": 0, "review": 0},
        "audit": {
            "artifact": artifact.name,
            "candidates": [],
            "excluded": {},
            "industry_concentration": [],
            "data_sources": [],
        },
    }

    dashboard_acceptance._check_trend_artifact_projection(
        reports, "tiger", projected
    )


@pytest.mark.parametrize(
    "account",
    [
        None,
        {},
        {**serialized_trend_account(), "source_date": ""},
        {**serialized_trend_account(), "source_date": "not-a-date"},
        {**serialized_trend_account(), "source_date": "2026-13"},
        {**serialized_trend_account(), "source_date": "2026-02-30"},
        {**serialized_trend_account(), "net_value": "NaN"},
        {**serialized_trend_account(), "available_cash": None},
        {**serialized_trend_account(), "positions": ["not-a-position"]},
        {**serialized_trend_account(), "positions": [{}]},
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "symbol": ""}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [{**serialized_trend_position(), "name": ""}],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "asset_class": ""}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "quantity": "NaN"}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "market_value": None}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "avg_cost_price": "Infinity"}
            ],
        },
        {**serialized_trend_account(), "exceptions": [1]},
    ],
)
def test_acceptance_rejects_missing_or_malformed_account(
    tmp_path: Path, account: object,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_us_tiger" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": "VIXY"}],
            "holding_decisions": [],
            "top10_candidates": [],
        },
    }
    if account is not None:
        payload["account"] = account
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    projected = {
        "report_date": "2026-07-15",
        "data_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "buy_actions": [{"action": "BUY", "symbol": "VIXY"}],
        "audit": {"artifact": artifact.name},
    }

    with pytest.raises(AssertionError, match="账户快照无效"):
        dashboard_acceptance._check_trend_artifact_projection(
            reports, "tiger", projected
        )


def trend_reports() -> dict[str, dict[str, object]]:
    return {
        "tiger": {
            "available": True, "broker": "tiger", "broker_label": "老虎",
            "market": "US", "market_label": "美股", "report_date": "2026-07-15",
            "data_date": "2026-07-14", "generated_at": "2026-07-15T11:30:36+08:00",
            "account_status": "已更新", "buy_window": "美股常规交易时段",
            "sell_actions": [{"symbol": "AAPL", "name": "苹果", "close": "200", "strength": "99", "reason": "danger_signal", "active_line": "190"}],
            "buy_actions": [{"symbol": "VIXY", "name": "波动率ETF", "close": "19", "strength": "98", "industry": "ETF", "target_weight": "0.04", "estimated_shares": "5000", "target_amount": "25142.16", "estimated_initial_line": "18.50", "option_anomaly": {"available": True, "status": "ok", "run_date": "2026-07-15", "summary": "期权波动率偏高。", "signal": "watch", "confidence": "中", "suggested_constraint": "仅观察", "categories": []}}],
            "hold_actions": [{"symbol": "SPY", "name": "标普ETF", "close": "510", "strength": "97", "reason": "trend_intact", "active_line": "500", "option_anomaly": {"available": False, "status": "missing", "run_date": "", "reason": "富途未返回该标的期权异动"}}],
            "review_actions": [{"symbol": "QQQ", "name": "纳指ETF", "close": None, "strength": None, "reason": "holding_signal_unknown"}],
            "counts": {"sell": 1, "buy": 1, "hold": 1, "review": 1},
            "audit": {
                "candidates": [{"symbol": "VIXY", "name": "波动率ETF", "strength": "5000"}],
                "excluded": {"QQQ": ["already_held"]},
                "account_exceptions": ["现金类资产不参与趋势判断：CASH（cash）"],
                "industry_concentration": [["科技", 1, "0.25"]],
                "data_sources": ["Trend Animals", "Futu US daily K-line"],
                "actual_api_cost": "1.00",
            },
        },
        "phillips": {
            "available": True, "broker": "phillips", "broker_label": "辉立",
            "market": "HK", "market_label": "港股", "report_date": "2026-07-15",
            "data_date": "2026-07-14", "generated_at": "2026-07-15T11:31:00+08:00",
            "account_status": "已更新", "buy_window": "09:30–10:00",
            "sell_actions": [], "buy_actions": [], "hold_actions": [],
            "review_actions": [], "counts": {"sell": 0, "buy": 0, "hold": 0, "review": 0},
            "audit": {
                "candidates": [], "excluded": {}, "industry_concentration": [],
                "data_sources": ["Trend Animals"], "estimated_api_cost": "1.20",
                "actual_api_cost": None,
            },
        },
        "eastmoney": {
            "available": True, "broker": "eastmoney", "broker_label": "东方财富",
            "market": "CN", "market_label": "A股", "report_date": "2026-07-15",
            "data_date": "2026-07-14", "generated_at": "2026-07-15T20:00:00+08:00",
            "account_status": "已更新", "buy_window": "09:30–10:00",
            "sell_actions": [{
                "symbol": "601398", "name": "工商银行", "close": "7.2",
                "temperature_prev": "温", "temperature_curr": "温",
                "strength": "91.3", "reason": "left_trend_right_side",
                "active_line": "7.0", "entry_hints": ["强度 91.3，低于入场线 95"],
            }],
            "buy_actions": [{
                "symbol": "688046", "name": "药康生物", "filter_price": "29.14",
                "close": "28.81", "temperature_prev": "温", "temperature_curr": "热",
                "phase": "立夏", "strength": "99.9", "industry": "医疗服务",
                "industry_temperature": "热", "market_cap": "110", "amount": "6",
                "target_weight": "0.04", "target_amount": "27061.98",
                "estimated_shares": 900, "estimated_initial_line": "24.55",
            }],
            "hold_actions": [{
                "symbol": "600900", "name": "长江电力", "close": "28.0",
                "temperature_prev": "热", "temperature_curr": "热",
                "strength": "98.7", "reason": "trend_intact", "active_line": "27.8",
                "entry_hints": ["不是新的温转热或温转沸入场信号"],
            }],
            "review_actions": [{
                "symbol": "600036", "name": "招商银行", "close": "45.2",
                "temperature_prev": "热", "temperature_curr": "热",
                "strength": "97", "reason": "holding_kline_unavailable",
                "active_line": "42.0", "entry_hints": ["筛选价数据不可用"],
            }],
            "counts": {"sell": 1, "buy": 1, "hold": 1, "review": 1},
            "audit": {
                "candidates": [{
                    "symbol": "600000", "name": "浦发银行", "strength": "94",
                    "eligible": False, "rank": None,
                    "excluded_reasons": ["strength_below_95"],
                }],
                "excluded": {"600000": ["strength_below_95"]},
                "industry_concentration": [],
                "data_sources": ["Trend Animals", "Futu CN calendar/QFQ daily K-line"],
                "actual_api_cost": "2.00",
            },
        },
    }


def trend_reviews() -> dict[str, dict[str, object]]:
    reviews: dict[str, dict[str, object]] = {}
    for broker, market, market_label, broker_label in (
        ("tiger", "US", "美股", "老虎"),
        ("phillips", "HK", "港股", "辉立"),
        ("eastmoney", "CN", "A股", "东方财富"),
    ):
        reviews[broker] = {
            "available": True,
            "broker": broker,
            "broker_label": broker_label,
            "market": market,
            "market_label": market_label,
            "sample_counts": {"discipline": 31, "actual": 29, "required": 30},
            "common_cutoff": "2026-07-17",
            "strategy_snapshot": {
                "strategy_id": f"trend/{market}/v1",
                "strategy_name": f"{market_label}短线右侧趋势",
                "strategy_version": "v1",
                "process_version": "abc1234",
                "parameters": {"position_limit": 10},
                "parameter_rows": [
                    {"group": "仓位执行", "name": "持仓上限", "value": "10 笔"},
                    {"group": "退出保护", "name": "初始保护线", "value": "成交均价减 2.0 倍 ATR14"},
                ],
            },
            "metrics": {
                key: {
                    series: {"value": value, "reason": None}
                    for series, value in (
                        ("discipline", "12.6"),
                        ("actual", "9.4"),
                        ("benchmark", "7.8"),
                    )
                }
                for key, _label, _percent
                in dashboard_acceptance.TREND_REVIEW_METRIC_SPECS
            },
        }
    return reviews


def valid_payload() -> dict[str, object]:
    cn = [
        {
            "market": "CN",
            "symbol": str(index),
            "portfolio_weight_hkd": "10.00%",
            "agent_report": {"available": False},
        }
        for index in range(5)
    ]
    other = [{
        "market": "US",
        "symbol": "MSFT",
        "brokers": "tiger",
        "portfolio_weight_hkd": "50.00%",
        "agent_report": {"available": True},
        "tradingagents_summary": {"available": True},
        "technical_facts": {"available": True},
        "decision_facts": {
            "kline": {"available": True},
            "news_sentiment": {"available": True},
        },
        "futu_skill_facts": {
            "news_sentiment": {"available": True},
            "technical_anomaly": {"available": True},
            "capital_anomaly": {"available": True},
            "derivatives_anomaly": {"available": True},
        },
    }]
    return {
        "holdings": cn + other,
        "cash_rows": [],
        "account_sync": {
            "status": "ok",
            "controller": {"status": "ok", "heartbeat_at": "2026-07-21T09:31:00+08:00"},
            "brokers": {
                "futu": {
                    "status": "ok", "display": "同步正常",
                    "data_as_of": "2026-07-31T13:48:44+08:00",
                },
                "tiger": {
                    "status": "ok", "display": "同步正常",
                    "data_as_of": "2026-07-31T13:49:01+08:00",
                },
                "phillips": {
                    "status": "ok", "display": "同步正常",
                    "data_as_of": "2026-07-29",
                },
                "eastmoney": {
                    "status": "ok", "display": "同步正常",
                    "data_as_of": "2026-07-30",
                },
            },
        },
        "backtest_universe": {"holdings": [
            {"market": "CN", "symbol": row["symbol"]} for row in cn
        ]},
        "trend_reports": trend_reports(),
        "trend_reviews": trend_reviews(),
        "trend_controllers": trend_controllers(),
    }


def test_acceptance_checks_grouped_broker_source_times() -> None:
    payload = valid_payload()
    text_by_selector = {
        "#source-status-list": (
            "实时账户 富途账户 同步正常 · 13:48 老虎账户 同步正常 · 13:49 "
            "券商结单 辉立账户 数据截至 · 07-29 "
            "东方财富账户 数据截至 · 07-30"
        ),
        '#source-status-list [data-broker="futu"]': "富途账户 同步正常 · 13:48",
        '#source-status-list [data-broker="tiger"]': "老虎账户 同步正常 · 13:49",
        '#source-status-list [data-broker="phillips"]': "辉立账户 数据截至 · 07-29",
        '#source-status-list [data-broker="eastmoney"]': "东方财富账户 数据截至 · 07-30",
    }

    class Locator:
        def __init__(self, text: str | None) -> None:
            self.text = text

        def count(self) -> int:
            return int(self.text is not None)

        def inner_text(self) -> str:
            assert self.text is not None
            return self.text

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(text_by_selector.get(selector))

    dashboard_acceptance._check_source_status_panel(Page(), payload)


def test_acceptance_source_panel_uses_current_page_dashboard_payload() -> None:
    initial = valid_payload()
    current = copy.deepcopy(initial)
    current_account_sync = current["account_sync"]
    assert isinstance(current_account_sync, dict)
    current_brokers = current_account_sync["brokers"]
    assert isinstance(current_brokers, dict)
    current_futu = current_brokers["futu"]
    assert isinstance(current_futu, dict)
    current_futu["data_as_of"] = "2026-07-31T13:49:44+08:00"

    class DashboardPage:
        def evaluate(self, expression: str) -> object:
            assert expression == "() => state.dashboard"
            return current

    assert dashboard_acceptance._page_dashboard_payload(DashboardPage()) is current


def trend_controllers() -> dict[str, dict[str, object]]:
    return {
        broker: {
            "market": market,
            "effective_mode": "execute",
            "executor_host": "ray-mac",
            "local_host": "ray-mac",
            "health": "healthy",
            "blocking": False,
            "reason": "",
            "pid": 4242,
            "working_directory": "/srv/open_trader",
            "git_sha": "abc1234",
            "phase": "monitoring",
            "heartbeat_at": "2026-07-21T09:31:00+08:00",
            "last_success": {
                "status": "missed_window",
                "market": market,
                "date": "2026-07-20",
                "submitted_count": 0,
                "artifact_paths": [],
            },
            "blocker": None,
            "next_check_at": "2026-07-21T09:31:05+08:00",
        }
        for broker, market in (
            ("tiger", "US"),
            ("phillips", "HK"),
            ("eastmoney", "CN"),
        )
    }


def integrated_v4_payload(
    tmp_path: Path,
    *,
    current_live_versions: bool = False,
) -> tuple[dict[str, object], Path, dict[str, int]]:
    from open_trader.trend_review import _report_hash

    reports_dir = tmp_path / "reports"
    account_ids = {"tiger": 102, "phillips": 103, "eastmoney": 101}
    payload = valid_payload()
    payload["data_dir"] = str(tmp_path / "data")
    payload["kelly_lab"] = {
        "available": True,
        "template_count": 1,
        "templates": [{"strategy_id": "trend_pullback_20d"}],
    }
    (tmp_path / "data/latest").mkdir(parents=True)
    (tmp_path / "data/latest/kelly_strategy_templates.json").write_text(
        json.dumps({
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [{"strategy_id": "trend_pullback_20d"}],
        }),
        encoding="utf-8",
    )
    (tmp_path / "data/latest/trend_api_stats.json").write_text(
        json.dumps({
            "sources": [
                {
                    "source": "actual",
                    "broker": broker,
                    "market": market,
                    "statistics_cutoff_at": "2026-07-20T08:59:59+08:00",
                }
                for broker, market in dashboard_acceptance.TREND_SIMULATE_MARKETS.items()
            ],
        }),
        encoding="utf-8",
    )
    labels = {"tiger": "老虎", "phillips": "辉立", "eastmoney": "东方财富"}
    directories = {
        "tiger": "trend_us_tiger",
        "phillips": "trend_hk_phillips",
        "eastmoney": "trend_a_share",
    }
    for broker, market in dashboard_acceptance.TREND_SIMULATE_MARKETS.items():
        strategy_version = (
            ("v10" if market == "CN" else "v8")
            if current_live_versions
            else ("v7" if market == "CN" else "v4")
        )
        pending = market == "HK"
        lot_size = 100 if market in {"CN", "HK"} else 1
        risk_summary = {
            "status": "active",
            "status_label": "风险预算内",
            "single_entry_risk_limit_pct": "0.004",
            "portfolio_risk_limit_pct": "0.04",
            "abnormal_loss_buffer_pct": "0.01",
            "total_risk_budget_target_pct": "0.05",
            "disclaimer": "5% 是风险预算目标，不是最大损失保证。",
            "kelly_phase": "active_all_samples",
            "kelly_eligible_sample_count": 30,
            "kelly_selected_sample_count": 30,
            "kelly_cap": "0.01",
            "kelly_source": "合格的富途模拟闭环；实盘结果不参与计算",
        }
        buy = {
            "action": "BUY",
            "symbol": {"CN": "600001", "HK": "00700", "US": "AAPL"}[market],
            "target_weight": "0.04",
            "estimated_shares": lot_size * 3,
            "lot_size": lot_size,
        }
        frozen = {
            "execution_date": "2026-07-20",
            "as_of_date": "2026-07-17",
            "generated_at": "2026-07-20T09:00:00+08:00",
            "metadata": {
                "market": market,
                "broker": broker,
                "simulate_acc_id": account_ids[broker],
            },
            "account": serialized_trend_account(fresh=True),
            "strategy_snapshot": {
                "strategy_id": (
                    f"trend_animals_warm_to_hot/{market}/{strategy_version}"
                ),
                "strategy_version": strategy_version,
                "process_version": "a" * 40,
                "parameters": {
                    "single_entry_risk_limit": "0.004",
                    "portfolio_risk_limit": "0.04",
                    "abnormal_loss_buffer": "0.01",
                    "drawdown_limit": "0.05",
                    **(
                        {"lot_size_source": "Futu 每标的整手"}
                        if market == "HK"
                        else {"lot_size": lot_size}
                    ),
                    "target_weight": (
                        {"热": "0.04", "沸": "0.02"}
                        if market == "CN"
                        else "0.04"
                    ),
                },
            },
            "strategy_judgments": {
                "formal_actions": [] if pending else [buy],
                "holding_decisions": [],
                "top10_candidates": [],
                "risk_skips": [],
            },
            "risk_summary": risk_summary,
            "drawdown_summary": {
                "state_status": "ok",
                "status": "pending" if pending else "active",
                "status_label": "等待下一交易日" if pending else "纪律内",
                "entry_allowed": not pending,
                "drawdown_pct": "0",
                "drawdown_limit_pct": "0.05",
                "pause_reason": (
                    "回撤基准将在 2026-07-21 起允许新开仓" if pending else ""
                ),
                "bootstrap_event": {
                    "event_id": "automatic-bootstrap-audit",
                    "baseline_equity": "100000",
                    "source_date": "2026-07-17",
                    "accepted_git_sha": "a" * 40,
                    "parameter_hash": "b" * 64,
                    "actor": "acceptance",
                    "occurred_at": "2026-07-20T08:00:00+08:00",
                    "entry_eligible_from": "2026-07-21" if pending else "2026-07-20",
                },
            },
            "data_sources": [f"Futu {market} SIMULATE account"],
        }
        frozen["drawdown_summary"]["bootstrap_event"][  # type: ignore[index]
            "parameter_hash"
        ] = strategy_parameter_hash(
            frozen["strategy_snapshot"]["parameters"]  # type: ignore[index]
        )
        artifact = reports_dir / directories[broker] / "2026-07-20.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps(frozen), encoding="utf-8")
        report = payload["trend_reports"][broker]  # type: ignore[index]
        assert isinstance(report, dict)
        report.update({
            "available": True,
            "data_status": "current",
            "account_fresh": True,
            "broker": broker,
            "broker_label": labels[broker],
            "market": market,
            "artifact": artifact.name,
            "report_sha256": _report_hash(frozen),
            "strategy_version": strategy_version,
            "buy_actions": [] if pending else [buy],
            "sell_actions": [],
            "hold_actions": [],
            "review_actions": [],
            "risk_skips": [],
            "risk_summary": {
                **risk_summary,
                "trade_stats": {
                    "available": True,
                    "statistics_cutoff_at": "2026-07-20T08:59:59+08:00",
                    "actual_broker": broker,
                    "actual_broker_label": labels[broker],
                    "simulation": {"eligible_sample_count": 30},
                    "actual": {"eligible_sample_count": 2},
                },
            },
            "drawdown_summary": frozen["drawdown_summary"],
            "actual_overlay": {
                "available": True,
                "broker": broker,
                "broker_label": labels[broker],
                "market": market,
                "notice": (
                    "只读执行辅助；实盘变化不会改写模拟建议、Kelly、模拟统计或报告哈希；"
                    "系统不会自动交易真实账户。"
                ),
                "items": [{"deviation_label": "已跟随"}],
                "outside_positions": [],
            },
            "audit": {"artifact": artifact.name},
        })
    return payload, reports_dir, account_ids


def test_acceptance_validates_integrated_templates_and_three_market_reports(
    tmp_path: Path,
) -> None:
    payload, reports_dir, account_ids = integrated_v4_payload(tmp_path)

    assert dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    ) == []


def test_acceptance_validates_current_live_strategy_versions(
    tmp_path: Path,
) -> None:
    payload, reports_dir, account_ids = integrated_v4_payload(
        tmp_path, current_live_versions=True
    )

    assert dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    ) == []


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        ("kelly_strategy_templates.json", "Kelly 模板"),
        ("trend_api_stats.json", "交易统计来源"),
    ],
)
def test_acceptance_reports_malformed_integrated_artifact_container(
    tmp_path: Path, artifact: str, expected: str,
) -> None:
    payload, reports_dir, account_ids = integrated_v4_payload(tmp_path)
    (tmp_path / "data/latest" / artifact).write_text("[]", encoding="utf-8")

    errors = dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    )

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("template", "Kelly 模板"),
        ("account", "模拟账户"),
        ("risk", "单笔风险"),
        ("lot", "整手"),
        ("stats", "实盘统计券商"),
        ("cutoff", "来源截止时间"),
        ("overlay", "实盘辅助"),
        ("drawdown_missing", "回撤状态缺失"),
    ],
)
def test_acceptance_rejects_integrated_contract_drift(
    tmp_path: Path, mutation: str, expected: str,
) -> None:
    payload, reports_dir, account_ids = integrated_v4_payload(tmp_path)
    report = payload["trend_reports"]["tiger"]  # type: ignore[index]
    assert isinstance(report, dict)
    if mutation == "template":
        payload["kelly_lab"]["templates"] = []  # type: ignore[index]
    elif mutation == "account":
        artifact = reports_dir / "trend_us_tiger/2026-07-20.json"
        frozen = json.loads(artifact.read_text(encoding="utf-8"))
        frozen["metadata"]["simulate_acc_id"] = 999
        artifact.write_text(json.dumps(frozen), encoding="utf-8")
        from open_trader.trend_review import _report_hash
        report["report_sha256"] = _report_hash(frozen)
    elif mutation == "risk":
        report["risk_summary"]["single_entry_risk_limit_pct"] = "0.4"  # type: ignore[index]
    elif mutation == "lot":
        report["buy_actions"][0]["estimated_shares"] = 3.5  # type: ignore[index]
    elif mutation == "stats":
        report["risk_summary"]["trade_stats"]["actual_broker"] = "eastmoney"  # type: ignore[index]
    elif mutation == "cutoff":
        report["risk_summary"]["trade_stats"][  # type: ignore[index]
            "statistics_cutoff_at"
        ] = "2026-07-21T00:00:00+08:00"
    elif mutation == "drawdown_missing":
        report["drawdown_summary"]["state_status"] = "missing"  # type: ignore[index]
    else:
        report["actual_overlay"]["broker"] = "eastmoney"  # type: ignore[index]

    errors = dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    )

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
        ("mutation", "expected"),
        [
            ("process", "冻结 Kelly/回撤策略身份"),
        ("stale", "当前真实数据"),
        ("account", "模拟账户快照不是最新"),
    ],
)
def test_acceptance_rejects_noncandidate_or_stale_integrated_report(
    tmp_path: Path, mutation: str, expected: str,
) -> None:
    payload, reports_dir, account_ids = integrated_v4_payload(tmp_path)
    report = payload["trend_reports"]["tiger"]  # type: ignore[index]
    assert isinstance(report, dict)
    if mutation == "process":
        artifact = reports_dir / "trend_us_tiger/2026-07-20.json"
        frozen = json.loads(artifact.read_text(encoding="utf-8"))
        frozen["strategy_snapshot"]["process_version"] = "other-sha"
        artifact.write_text(json.dumps(frozen), encoding="utf-8")
        from open_trader.trend_review import _report_hash
        report["report_sha256"] = _report_hash(frozen)
    elif mutation == "stale":
        report["data_status"] = "stale"
    else:
        report["account_fresh"] = False

    errors = dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    )

    assert any(expected in error for error in errors)


def test_acceptance_rejects_frozen_parameter_audit_identity_mismatch(
    tmp_path: Path,
) -> None:
    from open_trader.trend_review import _report_hash

    payload, reports_dir, account_ids = integrated_v4_payload(tmp_path)
    report = payload["trend_reports"]["tiger"]  # type: ignore[index]
    assert isinstance(report, dict)
    artifact = reports_dir / "trend_us_tiger/2026-07-20.json"
    frozen = json.loads(artifact.read_text(encoding="utf-8"))
    frozen["drawdown_summary"]["bootstrap_event"]["parameter_hash"] = "c" * 64
    artifact.write_text(json.dumps(frozen), encoding="utf-8")
    report["report_sha256"] = _report_hash(frozen)
    report["drawdown_summary"] = frozen["drawdown_summary"]

    errors = dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    )

    assert any("冻结策略参数与回撤审计身份" in error for error in errors)


def test_acceptance_allows_only_the_audited_v4_overheat_trim_compatibility(
    tmp_path: Path,
) -> None:
    from open_trader.trend_review import _report_hash

    payload, reports_dir, account_ids = integrated_v4_payload(tmp_path)
    report = payload["trend_reports"]["tiger"]  # type: ignore[index]
    assert isinstance(report, dict)
    artifact = reports_dir / "trend_us_tiger/2026-07-20.json"
    frozen = json.loads(artifact.read_text(encoding="utf-8"))
    parameters = frozen["strategy_snapshot"]["parameters"]
    assert isinstance(parameters, dict)
    old_parameters = dict(parameters)
    updated_parameters = {
        **old_parameters,
        "overheat_trim_fraction": "0.30",
        "overheat_trim_once_per_position": True,
        "overheat_trim_signals": ["boiling", "champagne"],
        "overheat_trim_rounding": "floor_to_market_lot",
        "overheat_trim_below_lot": "no_order_terminal",
        "full_exit_precedes_partial_exit": True,
    }
    decision = automatic_bootstrap_strategy_drawdown(
        tmp_path / "state",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v4",
        strategy_version="v4",
        parameters=old_parameters,
        baseline_equity=Decimal("100000"),
        source_date="2026-07-17",
        accepted_git_sha="a" * 40,
        actor="acceptance",
        occurred_at="2026-07-20T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-20",
    )
    decision = automatic_bootstrap_strategy_drawdown(
        tmp_path / "state",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v4",
        strategy_version="v4",
        parameters=updated_parameters,
        baseline_equity=None,
        source_date=None,
        accepted_git_sha="b" * 40,
        actor="acceptance",
        occurred_at="2026-07-21T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from=None,
    )
    frozen["strategy_snapshot"]["parameters"] = updated_parameters
    frozen["drawdown_summary"]["bootstrap_event"]["parameter_hash"] = (
        strategy_parameter_hash(old_parameters)
    )
    frozen["drawdown_summary"]["parameter_compatibility_event"] = decision[
        "parameter_compatibility_event"
    ]
    artifact.write_text(json.dumps(frozen), encoding="utf-8")
    report["report_sha256"] = _report_hash(frozen)
    report["drawdown_summary"] = frozen["drawdown_summary"]

    assert dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    ) == []

    frozen["strategy_snapshot"]["parameters"] = old_parameters
    artifact.write_text(json.dumps(frozen), encoding="utf-8")
    report["report_sha256"] = _report_hash(frozen)
    report["drawdown_summary"] = frozen["drawdown_summary"]

    rollback_errors = dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    )
    assert any("冻结策略参数与回撤审计身份" in error for error in rollback_errors)

    frozen["strategy_snapshot"]["parameters"] = updated_parameters
    frozen["strategy_snapshot"]["parameters"]["overheat_trim_fraction"] = "0.31"
    artifact.write_text(json.dumps(frozen), encoding="utf-8")
    report["report_sha256"] = _report_hash(frozen)
    report["drawdown_summary"] = frozen["drawdown_summary"]

    errors = dashboard_acceptance.validate_integrated_candidate(
        payload,
        expected_root=tmp_path,
        expected_sha="candidate-sha",
        reports_dir=reports_dir,
        account_ids=account_ids,
    )
    assert any("冻结策略参数与回撤审计身份" in error for error in errors)


def test_trend_advice_signature_allows_overlay_refresh_only(tmp_path: Path) -> None:
    first, _reports_dir, _account_ids = integrated_v4_payload(tmp_path)
    second = copy.deepcopy(first)
    second_report = second["trend_reports"]["tiger"]  # type: ignore[index]
    second_report["actual_overlay"]["items"] = [{"deviation_label": "超买"}]  # type: ignore[index]

    assert dashboard_acceptance.trend_advice_signature(
        first
    ) == dashboard_acceptance.trend_advice_signature(second)

    second_report["risk_summary"]["trade_stats"]["actual"] = {  # type: ignore[index]
        "eligible_sample_count": 3,
    }
    assert dashboard_acceptance.trend_advice_signature(
        first
    ) != dashboard_acceptance.trend_advice_signature(second)

    second = copy.deepcopy(first)
    second_report = second["trend_reports"]["tiger"]  # type: ignore[index]
    second_report["buy_actions"][0]["estimated_shares"] = 4  # type: ignore[index]
    assert dashboard_acceptance.trend_advice_signature(
        first
    ) != dashboard_acceptance.trend_advice_signature(second)


def test_acceptance_checks_integrated_risk_copy_and_text_status() -> None:
    report = {
        "report_date": "2026-07-20",
        "risk_summary": {
            "status": "active", "status_label": "风险预算内",
            "trade_stats": {"actual_broker_label": "东方财富"},
        },
        "drawdown_summary": {
            "status_label": "纪律内",
            "bootstrap_event": {
                "event_id": "automatic-bootstrap-audit",
                "baseline_equity": "100000",
                "source_date": "2026-07-17",
                "accepted_git_sha": "candidate-sha",
                "parameter_hash": "parameter-hash",
                "actor": "acceptance",
                "occurred_at": "2026-07-20T08:00:00+08:00",
                "entry_eligible_from": "2026-07-20",
            },
            "recovery_event": {
                "event_id": "snapshot-recovery-audit",
                "snapshot": "snapshot.json",
                "state_sha256": "state-hash",
                "actor": "acceptance",
                "occurred_at": "2026-07-20T08:30:00+08:00",
            },
        },
    }
    text = " ".join((
        "组合计划风险 风险预算内 组合剩余风险 单笔风险上限 异常损失缓冲 不得用于开仓",
        "Kelly 阶段 当前 Kelly 上限 富途模拟盘交易统计 东方财富实盘交易统计",
        "策略累计回撤 纪律内",
        "基准已自动建立 回撤基准审计详情 100,000 2026-07-17 automatic-bootstrap-audit ",
        "candidate-sha parameter-hash acceptance 2026-07-20T08:00:00+08:00 2026-07-20 ",
        "状态恢复审计详情 snapshot-recovery-audit snapshot.json state-hash 2026-07-20T08:30:00+08:00",
        "5% 是风险预算目标，不是最大损失保证。",
    ))

    clicked: list[str] = []

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def count(self) -> int:
            return int(self.selector not in {
                ".trend-simulation-overlay",
                ".trend-actual-overlay",
            })

        def inner_text(self) -> str:
            return text

        def all_inner_texts(self) -> list[str]:
            assert self.selector == ".trend-stage:visible"
            return ["正式买入 30.59 保护线 23.43"]

        def locator(self, selector: str) -> "Locator":
            return Locator(f"{self.selector} {selector}")

        def click(self) -> None:
            clicked.append(self.selector)

        def get_attribute(self, name: str) -> str | None:
            if name == "open":
                return None
            assert name == "data-risk-status"
            return "active"

    class Root:
        def inner_text(self) -> str:
            return text

        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    dashboard_acceptance._check_integrated_trend_ui(
        Root(), report, "eastmoney"
    )
    assert ".trend-risk-summary .trend-drawdown-bootstrap-audit summary" in clicked
    assert ".trend-risk-summary .trend-drawdown-recovery-audit summary" in clicked


def test_acceptance_checks_displayed_current_lifecycle_cards_and_industry_context() -> None:
    rows = [
        {"group": "候选来源", "name": "组合", "value": "冻结"},
        {"group": "入场过滤", "name": "强度", "value": "不低于 95"},
        {"group": "候选排序", "name": "顺序", "value": "强度降序"},
        {"group": "仓位执行", "name": "仓位", "value": "4%"},
        {"group": "退出保护", "name": "初始保护线", "value": "ATR14"},
        {"group": "退出保护", "name": "退出条件", "value": "危险信号"},
        {"group": "其他", "name": "未知", "value": "历史只读"},
    ]
    titles = ["入场门槛", "候选排序", "仓位与执行", "持有管理", "退出规则", "其他设置"]
    workspace_text = " ".join(
        [*(str(row[key]) for row in rows for key in ("group", "name", "value")),
         "实际 API 成本 1.25 单位", "科技 当前温度 热 温度方向 上升 趋势强度 97.5",
         "温转热数量 3 右侧个数占比 60% → 80% 右侧市值占比 70% → 90%",
         "结构差较前值持平 0 个百分点 该指标不是账户仓位或上涨概率"]
    )

    class Summary:
        def __init__(self, title: str) -> None:
            self.title = title

        def count(self) -> int:
            return 1

        def inner_text(self) -> str:
            return f"{self.title} 1 项"

        def focus(self) -> None:
            return None

        def click(self) -> None:
            return None

        def evaluate(self, expression: str) -> bool:
            assert expression == "element => element === document.activeElement"
            return True

    class Count:
        def count(self) -> int:
            return 1

    class Card:
        def __init__(self, title: str) -> None:
            self.summary = Summary(title)

        def locator(self, selector: str) -> Summary | Count:
            if selector == "summary":
                return self.summary
            assert selector == ".trend-discipline-category-body"
            return Count()

        def get_attribute(self, name: str) -> str:
            assert name == "open"
            return ""

    class Cards:
        def __init__(self) -> None:
            self.items = [Card(title) for title in titles]

        def count(self) -> int:
            return len(self.items)

        def locator(self, selector: str) -> "Summaries":
            assert selector == "summary"
            return Summaries(self.items)

        def nth(self, index: int) -> Card:
            return self.items[index]

    class Summaries:
        def __init__(self, cards: list[Card]) -> None:
            self.cards = cards

        def all_inner_texts(self) -> list[str]:
            return [card.summary.inner_text() for card in self.cards]

    class Context:
        def count(self) -> int:
            return 1

        def inner_text(self) -> str:
            return workspace_text

    class DisciplineWorkspace:
        def count(self) -> int:
            return 1

        def get_attribute(self, name: str) -> str | None:
            assert name == "open"
            return None

        def locator(self, selector: str) -> Summary | Cards:
            if selector == ":scope > summary":
                return Summary("纪律 6 类 · 7 项 · 本报告生成时参数")
            assert selector == ".trend-discipline-category"
            return Cards()

    class Root:
        def inner_text(self) -> str:
            return workspace_text

        def locator(self, selector: str) -> DisciplineWorkspace | Context:
            if selector == ".trend-discipline-workspace":
                return DisciplineWorkspace()
            assert selector == ".trend-industry-context"
            return Context()

    dashboard_acceptance._check_frozen_trend_disciplines(
        Root(),
        {
            "strategy_parameter_rows": [],
            "current_strategy_parameter_rows": rows,
            "api_cost": {"label": "实际 API 成本 1.25 单位"},
            "industry_context_status": {
                "ordering_mode": "context_with_history",
                "current_complete": True,
            },
            "industry_contexts": [{
                "industry": "科技", "temperature": "热", "strength": "97.5",
                "warm_to_hot_count": 3,
                "aggregate_right_count_ratio": "0.8",
                "aggregate_right_market_cap_ratio": "0.9",
                "prior_aggregate_right_count_ratio": "0.6",
                "prior_aggregate_right_market_cap_ratio": "0.7",
                "valid": True,
            }],
        },
        "eastmoney",
    )
    empty_page = tabbed_account_page(valid_payload())
    empty_page.trend_broker = "tiger"
    dashboard_acceptance._check_frozen_trend_disciplines(
        empty_page.locator("#trend-report-workspace:visible"),
        empty_page.reports["tiger"],
        "tiger",
    )


def test_acceptance_rejects_visible_numbers_over_two_decimal_places() -> None:
    dashboard_acceptance._check_visible_decimal_precision(
        "模拟持仓 485 / 1,296 成本 30.59 保护线 23.43", "模拟盘"
    )
    with pytest.raises(AssertionError, match="超过两位小数"):
        dashboard_acceptance._check_visible_decimal_precision(
            "成本 30.594999", "模拟盘"
        )


def test_acceptance_formats_arbitrary_size_number_without_integer_conversion() -> None:
    integer = "00" + "1" * 4_998
    grouped = re.sub(r"\B(?=(\d{3})+(?!\d))", ",", integer)

    assert dashboard_acceptance._display_number(f"+{integer}.005") == (
        f"+{grouped}.01"
    )


def test_acceptance_checks_exact_trend_review_content() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    dashboard_acceptance._check_trend_review(
        page, section, "tiger", payload["trend_reviews"]["tiger"]
    )

    assert page.opened_reviews == ["tiger"]
    assert page.review_benchmark_checks == ["discipline", "actual"]
    assert page.review_style_checks == ["tiger"]
    assert page.review_geometry_checks == ["tiger"]


def test_acceptance_rejects_trend_review_benchmark_drift() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.review_benchmark_values["actual"][0] = "8.0%"
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="市场基准"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


def test_acceptance_rejects_trend_review_panel_style_drift() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.review_panel_radius = "12px"
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="圆角"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


def test_acceptance_rejects_trend_review_label_style_drift() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.review_label_border_width = "1px"
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="series"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


def test_acceptance_rejects_trend_review_header_span_badge_style() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.review_header_span_border_width = "1px"
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="badge"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


def test_acceptance_rejects_trend_review_header_left_order_drift() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.review_header_left_texts = [
        "美股趋势复盘", "老虎｜美股", "美股短线右侧趋势｜第 1 版",
    ]
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="header 左侧"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


@pytest.mark.parametrize("target", ("reason", "header"))
def test_acceptance_rejects_arbitrary_english_trend_review_chrome(
    target: str,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    if target == "reason":
        page.review_metric_reason = "Ready"
    else:
        page.review_header_left_texts = [
            "老虎｜美股 Ready", "美股趋势复盘", "美股短线右侧趋势｜第 1 版",
        ]
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="拉丁界面词"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


def test_acceptance_allows_spaced_a_share_market_name() -> None:
    dashboard_acceptance._assert_no_trend_review_latin(
        ["A 股短线右侧趋势｜第 1 版"], "eastmoney", "header 左侧"
    )


def test_acceptance_rejects_375_trend_review_overflow() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": 375, "height": 844}
    page.review_document_width = 376
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="375"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


def test_acceptance_rejects_375_trend_review_clipped_long_text() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": 375, "height": 844}
    page.review_long_text_scroll_width = 324
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="长文本"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


@pytest.mark.parametrize(
    "override",
    (
        {"whiteSpace": "nowrap"},
        {"textOverflow": "ellipsis"},
        {"overflow": "hidden"},
        {"scrollWidth": 324, "clientWidth": 323},
        {"scrollHeight": 41, "clientHeight": 40},
    ),
)
def test_acceptance_rejects_375_trend_review_text_layout_drift(
    override: dict[str, object],
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": 375, "height": 844}
    page.review_text_layout_override = override
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    with pytest.raises(AssertionError, match="文本"):
        dashboard_acceptance._check_trend_review(
            page, section, "tiger", payload["trend_reviews"]["tiger"]
        )


def test_trend_review_acceptance_fake_rejects_marker_only_expressions() -> None:
    page = tabbed_account_page(valid_payload())

    for marker in (
        "trend-review-style-contract", "trend-review-geometry-contract",
    ):
        with pytest.raises(AssertionError):
            page.evaluate(f"() => {{ // {marker}\n }}")


def test_trend_review_acceptance_fake_rejects_broken_selector_or_api() -> None:
    payload = valid_payload()
    captured: list[str] = []

    class CapturingPage(TabbedAccountPage):
        def evaluate(
            self, expression: str, argument: object | None = None,
        ) -> object:
            if "trend-review-" in expression:
                captured.append(expression)
            return super().evaluate(expression, argument)

    page = CapturingPage(payload)
    section = dashboard_acceptance._select_account_tab(page, "tiger")
    dashboard_acceptance._check_trend_review(
        page, section, "tiger", payload["trend_reviews"]["tiger"]
    )
    style_expression, geometry_expression = captured

    with pytest.raises(AssertionError):
        page.evaluate(style_expression.replace(
            ".trend-review-comparison", ".broken-comparison", 1
        ))
    with pytest.raises(AssertionError):
        page.evaluate(geometry_expression.replace(
            "getBoundingClientRect", "getBrokenRect", 1
        ))
    page.trend_broker = "tiger"
    with pytest.raises(AssertionError):
        page.evaluate(geometry_expression.replace(
            "documentWidth: document.documentElement.scrollWidth",
            "documentWidth: 375",
            1,
        ))


def valid_quotes_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "fetched_at": "2026-07-15T15:03:13+08:00",
        "us_session_status": "active",
        "quotes": {
            "US.DRAM": {
                "market": "US", "symbol": "DRAM", "last_price": "61.5",
                "price_session": "overnight", "price_time": "2026-07-15 03:03:01",
                "current_session_quote": True, "market_state": "OVERNIGHT",
            }
        },
    }


def test_validate_quotes_payload_accepts_one_selected_us_session_price() -> None:
    assert validate_quotes_payload(valid_quotes_payload()) == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("last_price", "", "价格无效"),
        ("price_session", "", "时段缺失"),
        ("market_state", "", "市场状态缺失"),
        ("price_time", "", "当前时段行情时间缺失"),
    ],
)
def test_validate_quotes_payload_rejects_incomplete_current_quote(
    field: str, value: object, expected: str,
) -> None:
    payload = valid_quotes_payload()
    payload["quotes"]["US.DRAM"][field] = value  # type: ignore[index]
    assert any(expected in error for error in validate_quotes_payload(payload))


def trend_account_text() -> str:
    return (
        "富途期权增强 "
        "老虎趋势美股趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14 "
        "辉立短线港股趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14 "
        "东方财富偏短线趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14"
    )


def trend_workspace_text(
    broker: str, report: dict[str, object] | None = None,
) -> str:
    if broker == "eastmoney":
        return (
            "东方财富｜A股 当天趋势报告 报告 2026-07-15 数据 2026-07-14 "
            "生成 2026-07-15T20:00:00+08:00 账户 已更新 "
            "买入 1 卖出 1 持有 1 复核 1 "
            "优先处理 · 卖出触发 正式买入计划 需要确认 · 人工复核 "
            "09:30–10:00 · 正式买入计划 "
            "盘中持续 · 已有持仓 筛选价（Trend Animals） "
            "执行参考价（Futu 前复权） 全部卖出 正式买入 继续持有 "
            "人工复核 纪律 本报告未提供该类纪律参数 "
            "当前行业上下文未提供，无法确认排序；未使用当前规则 审计详情"
        )
    if broker == "phillips":
        return (
            "辉立｜港股 当天趋势报告 报告 2026-07-15 数据 2026-07-14 "
            "生成 2026-07-15T11:31:00+08:00 账户 已更新 "
        "买入 0 卖出 0 持有 0 复核 0 "
        "优先处理 · 卖出触发 09:30–10:00 · 正式买入计划 无 盘中持续 · 已有持仓 "
            "全部卖出 正式买入 继续持有 纪律 本报告未提供该类纪律参数 "
            "当前行业上下文未提供，无法确认排序；未使用当前规则 审计详情"
        )
    return (
        "老虎｜美股 当天趋势报告 报告 2026-07-15 数据 2026-07-14 "
        "生成 2026-07-15T11:30:36+08:00 账户 已更新 "
        "买入 1 卖出 1 持有 1 复核 1 "
        "优先处理 · 卖出触发 美股常规交易时段 · 正式买入计划 "
        "需要确认 · 人工复核 盘中持续 · 已有持仓 全部卖出 正式买入 继续持有 "
        "纪律 本报告未提供该类纪律参数 "
        "当前行业上下文未提供，无法确认排序；未使用当前规则 审计详情"
    )


def trend_review_workspace_text(broker: str) -> str:
    review = trend_reviews()[broker]
    snapshot = review["strategy_snapshot"]
    return (
        f"{review['broker_label']}｜{review['market_label']} "
        f"{review['market_label']}趋势复盘 {snapshot['strategy_name']}｜第 1 版 "
        "返回持仓看板 纪律模拟 31 笔 实际执行 29 / 30，数据不足 "
        "共同截止日 2026-07-17 "
        "纪律模拟与市场 期间净收益率 相对市场超额收益 最大回撤 卡玛比率 夏普比率 "
        "实际执行与市场 期间净收益率 相对市场超额收益 最大回撤 卡玛比率 夏普比率 "
        "纪律模拟 实际执行 同期市场"
    )


def trend_stage_texts(broker: str) -> list[str]:
    if broker == "eastmoney":
        return [
            "优先处理 · 卖出触发\n601398 工商银行 全部卖出 7.2 温 → 温 "
            "91.3 右侧趋势已结束 7.0 强度 91.3，低于入场线 95",
            "09:30–10:00 · 正式买入计划\n688046 药康生物 正式买入 29.14 "
            "28.81 温 → 热 立夏 99.9 医疗服务 热 110 6 4% 27061.98 900 股 24.55",
            "需要确认 · 人工复核\n600036 招商银行 人工复核 45.2 热 → 热 "
            "97 持仓日线数据不可用 42.0 筛选价数据不可用",
            "盘中持续 · 已有持仓\n600900 长江电力 继续持有 28.0 热 → 热 "
            "98.7 趋势保持完好 27.8 不是新的温转热或温转沸入场信号",
        ]
    if broker == "phillips":
        return [
            "优先处理 · 卖出触发\n无",
            "09:30–10:00 · 正式买入计划\n无",
            "盘中持续 · 已有持仓\n无",
        ]
    return [
        "优先处理 · 卖出触发\nAAPL 苹果 全部卖出 200 99 危险信号触发 190",
        "美股常规交易时段 · 正式买入计划\nVIXY 波动率ETF 正式买入 19 98 ETF 4% 25,142.16 5,000 股 18.50",
        "需要确认 · 人工复核\nQQQ 纳指ETF 人工复核 — — 趋势信号不完整 — —",
        "盘中持续 · 已有持仓\nSPY 标普ETF 继续持有 510 97 趋势保持完好 500",
    ]


def trend_audit_text(broker: str) -> str:
    if broker == "eastmoney":
        return (
            "审计详情 为什么没有进入买入名单 候选 1 通过 0 排除 1 "
            "600000 浦发银行 已排除 · 1 项未通过 趋势强度 94 → 要求：不低于 95 "
            "查看全部字段 行业集中度 无 "
            "数据来源：Trend Animals、Futu CN calendar/QFQ daily K-line API 成本：2.00"
        )
    if broker == "phillips":
        return "审计详情 候选榜 无 排除项 无 行业集中度 无 数据来源：Trend Animals API 成本：1.20"
    return (
        "审计详情 候选榜 VIXY 波动率ETF 强度 5,000 排除项 QQQ 当前账户已经持有 "
        "行业集中度 科技 1 0.25 数据来源：Trend Animals、Futu US daily K-line API 成本：1.00"
    )


def trend_audit_sections(broker: str) -> list[str]:
    if broker == "eastmoney":
        return [
            "为什么没有进入买入名单 候选 1 通过 0 排除 1 "
            "600000 浦发银行 已排除 · 1 项未通过 趋势强度 94 → 要求：不低于 95 "
            "查看全部字段",
            "行业集中度 无",
        ]
    if broker == "phillips":
        return ["候选榜 无", "排除项 无", "账户不参与项 无", "行业集中度 无"]
    return [
        "候选榜 VIXY 波动率ETF 强度 5,000",
        "排除项 QQQ 当前账户已经持有",
        "账户不参与项 现金类资产不参与趋势判断：CASH（cash）",
        "行业集中度 科技 1 0.25",
    ]


ACCOUNT_SECTION_TEXTS = {
    "futu": (
        "富途 期权增强 持仓资产 HKD 100 现金 HKD 20 持仓 1 "
        "来源 Futu 时间 2026-07-15"
    ),
    "tiger": (
        "老虎 趋势 · 美股趋势交易 持仓资产 HKD 100 现金 HKD 20 持仓 1 "
        "来源 Tiger 时间 2026-07-15 当天趋势报告 报告日期 2026-07-15 "
        "数据截至 2026-07-14 美股复盘"
    ),
    "phillips": (
        "辉立 短线 · 港股趋势交易 持仓资产 HKD 100 现金 HKD 20 持仓 1 "
        "来源 月结单 时间 2026-07 当天趋势报告 报告日期 2026-07-15 "
        "数据截至 2026-07-14 港股复盘"
    ),
    "eastmoney": (
        "东方财富 偏短线 · 趋势交易 持仓资产 HKD 0 现金 HKD 20 持仓 0 "
        "来源 东方财富 时间 2026-07-15 当天趋势报告 报告日期 2026-07-15 "
        "数据截至 2026-07-14 A股复盘 "
        "当前筛选下没有持仓"
    ),
}

class TabbedAccountLocator:
    def __init__(self, page: "TabbedAccountPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "TabbedAccountLocator":
        return self

    def locator(self, selector: str) -> "TabbedAccountLocator":
        if re.fullmatch(r"#account-\w+-view-panel:visible", selector):
            return self.page.locator(selector)
        if ".cn-trend-report:visible" in self.selector:
            return self.page.locator(
                "#trend-report-workspace:visible"
                + (f" {selector}" if selector else "")
            )
        return self.page.locator(f"{self.selector} {selector}")

    def _require_known_broker(self, broker: str) -> str:
        if broker not in self.page.tab_order:
            raise AssertionError(f"unknown broker: {broker}")
        return broker

    def click(self) -> None:
        match = re.fullmatch(r'#account-tabs \[data-broker="(\w+)"\]', self.selector)
        if match:
            self.page.selected = self._require_known_broker(match.group(1))
            self.page.selected_brokers.append(self.page.selected)
            self.page._record_visible_sections()
            return
        match = re.fullmatch(
            r'#account-(\w+):visible \[data-account-view="(\w+)"\]',
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            assert broker == self.page.selected
            view = match.group(2)
            self.page.account_views[broker] = view
            if view == "report":
                self.page.trend_broker = broker
                self.page.opened_reports.append(broker)
                self.page.active = self.selector
                self.page._record_visible_sections()
            elif view == "real" and self.page.trend_broker == broker:
                self.page.trend_broker = None
                self.page.active = self.selector
                self.page._record_visible_sections()
            return
        if self.selector == '[data-market="CN"]':
            self.page.market = "CN"
            return
        match = re.fullmatch(
            r"#account-(\w+):visible \.trend-report-entry \[data-trend-report\]",
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            self.page.trend_broker = broker
            self.page.opened_reports.append(broker)
            self.page.active = "#return-to-portfolio:visible"
            self.page._record_visible_sections()
            return
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.trend-option-button:nth\((\d+)\)",
            self.selector,
        )
        if match:
            index = int(match.group(1))
            action = self.page.option_available_actions()[index]
            assert action.get("option_anomaly", {}).get("available") is True
            self.page.option_dialog_open = True
            self.page.option_dialog_index = index
            return
        match = re.fullmatch(
            r"#trend-report-workspace:visible \[data-trend-holding-section\] "
            r"\[data-trend-holding-view\]:nth\((\d+)\)",
            self.selector,
        )
        if match:
            index = int(match.group(1))
            assert index in (0, 1)
            self.page.holding_view = "real" if index == 0 else "simulate"
            return
        if self.selector in {
            "#trend-report-workspace:visible dialog.trend-option-dialog:visible button[data-option-anomaly-close]",
            "#trend-report-workspace:visible dialog.trend-option-dialog:visible button[data-option-anomaly-close]:visible",
        }:
            self.page.option_dialog_open = False
            return
        match = re.fullmatch(
            r'#account-(\w+)-view-panel:visible details\.trend-review-disclosure :scope > summary',
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            self.page.opened_reviews.append(broker)
            return
        match = re.fullmatch(
            r'#account-(\w+):visible \[data-trend-review="\w+"\]',
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            self.page.trend_broker = broker
            self.page.trend_kind = "review"
            self.page.opened_reviews.append(broker)
            self.page.active = "#return-to-portfolio:visible"
            self.page._record_visible_sections()
            return
        if self.selector == "#trend-report-workspace:visible .trend-audit summary":
            self.page.active = self.selector
            return
        if self.selector.endswith(" .trend-controller-status :scope > summary"):
            self.page.active = self.selector
            return
        if self.selector.endswith(" .trend-discipline-workspace :scope > summary"):
            self.page.active = self.selector
            return
        if self.selector == (
            '.account-holding-actions button[data-detail-mode="t_signal"]:visible'
        ):
            self.page.workspace_view = "detail"
            return
        if self.selector == "[data-back-to-holdings]:visible":
            self.page.workspace_view = "portfolio"
            return
        if self.selector == '#main-navigation [data-workspace="kelly_lab"]':
            self.page.workspace_view = "kelly"
            return
        if self.selector == '#main-navigation [data-workspace="standard_backtest"]':
            self.page.workspace_view = "backtest"
            return
        if self.selector == "#research-chat-close:visible":
            self.page.research_open = False
            return
        if self.selector in {
            "#return-to-portfolio:visible",
            "#trend-report-workspace:visible [data-close-trend-report]",
        }:
            if self.page.trend_broker is None:
                self.page.workspace_view = "portfolio"
                return
            broker = self.page.trend_broker
            self.page.trend_broker = None
            self.page.active = (
                f'#account-{broker}:visible [data-trend-review="{broker}"]'
                if self.page.trend_kind == "review"
                else f"#account-{broker}:visible .trend-report-entry [data-trend-report]"
            )
            self.page.trend_kind = ""
            self.page._record_visible_sections()
            return
        raise AssertionError(f"unknown click selector: {self.selector}")

    def count(self) -> int:
        if self.selector == "#refresh-quotes":
            return 0
        if self.selector == "text=刷新账户与行情":
            return 0
        if self.selector in {"#quote-status", "#account-sync-status", "#last-refresh"}:
            return 0
        if self.selector == "#source-status-list":
            return 1
        if re.fullmatch(r'#source-status-list \[data-broker="(\w+)"\]', self.selector):
            return 1
        target_selectors = {
            '#account-tabs [role="tab"]:visible, #header-market-filters button:visible, '
            ".strategy-tools button:visible, "
            ".broker-summary-card:visible, .account-holding-actions button:visible",
            ".symbol-detail-panel.inline-symbol-detail:visible button:visible, "
            ".symbol-detail-panel.inline-symbol-detail:visible input:visible, "
            ".symbol-detail-panel.inline-symbol-detail:visible select:visible",
            "#return-to-portfolio:visible, .kelly-lab-panel button:visible",
            "#standard-backtest-workspace button:visible, "
            "#standard-backtest-workspace input:visible, "
            "#standard-backtest-workspace select:visible",
            ".research-chat-modal button:visible, .research-chat-modal input:visible",
            "#return-to-portfolio:visible, #trend-report-workspace:visible button:visible, "
            "#trend-report-workspace:visible summary:visible",
            "#return-to-portfolio:visible, #trend-report-workspace:visible button:visible",
        }
        if self.selector in target_selectors:
            return 1
        if re.fullmatch(
            r"#account-(\w+)-view-panel:visible \.cn-trend-report "
            r"(?:button|summary):visible, #account-\1-view-panel:visible "
            r"\.cn-trend-report summary:visible",
            self.selector,
        ):
            return 1
        if self.selector in VISUAL_CONTRACT_STYLES:
            return 1
        if self.selector == (
            '.account-holding-actions button[data-detail-mode="t_signal"]:visible'
        ):
            return 1
        if self.selector == "[data-back-to-holdings]:visible":
            return int(self.page.workspace_view == "detail")
        if self.selector == '#main-navigation [data-workspace="kelly_lab"]':
            return 1
        if self.selector == ".kelly-lab-panel:visible":
            return int(self.page.workspace_view == "kelly")
        if self.selector == '#main-navigation [data-workspace="standard_backtest"]':
            return 1
        if self.selector == "#standard-backtest-workspace:visible":
            return int(self.page.workspace_view == "backtest")
        if self.selector == ".holdings-panel:visible":
            return int(self.page.workspace_view == "portfolio")
        if self.selector == "[data-research-chat]:visible":
            return 0
        if self.selector in {".research-chat-modal:visible", "#research-chat-close:visible"}:
            return int(self.page.research_open)
        if self.selector == "#account-tabs [data-broker]":
            return 4
        match = re.fullmatch(r'#account-tabs \[data-broker="(\w+)"\]', self.selector)
        if match:
            self._require_known_broker(match.group(1))
            return 1
        if self.selector in {'[data-market="CASH"]', "#cash-detail-panel"}:
            return 0
        if self.selector == ".account-section":
            return 1
        if self.selector == ".account-section:visible":
            return self.page._record_visible_sections()
        match = re.fullmatch(r"#account-(\w+):visible", self.selector)
        if match:
            broker = self._require_known_broker(match.group(1))
            return int(
                self.page.selected == broker
                and (
                    self.page.trend_broker is None
                    or (
                        self.page.trend_broker == broker
                        and self.page.account_views.get(broker) == "report"
                    )
                )
            )
        match = re.fullmatch(r"#account-(\w+)-view-panel:visible", self.selector)
        if match:
            broker = self._require_known_broker(match.group(1))
            return int(
                self.page.selected == broker
                and self.page.trend_broker == broker
                and self.page.account_views.get(broker) == "report"
            )
        match = re.fullmatch(
            r"#account-(\w+)-view-panel:visible \.cn-trend-report:visible",
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            return int(
                self.page.selected == broker
                and self.page.trend_broker == broker
                and self.page.account_views.get(broker) == "report"
                and self.page.reports[broker].get("available") is True
            )
        match = re.fullmatch(
            r'#account-(\w+):visible \[data-account-view="(\w+)"\]',
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            return int(broker != "futu" and self.page.selected == broker)
        match = re.fullmatch(
            r'#account-(\w+):visible \[data-statement-upload="(\w+)"\]:visible',
            self.selector,
        )
        if match:
            section_broker = self._require_known_broker(match.group(1))
            upload_broker = self._require_known_broker(match.group(2))
            return int(
                section_broker == upload_broker
                and section_broker in {"phillips", "eastmoney"}
                and self.page.viewport_size["width"] > 760
            )
        for broker in self.page.tab_order:
            entry = f"#account-{broker}:visible .trend-report-entry"
            if self.selector not in {
                entry,
                f"{entry} [data-trend-report]",
                f"{entry} button",
                f'{entry} button:has-text("当天趋势报告")',
            }:
                continue
            if (
                self.page.trend_broker is not None
                or self.page.selected != broker
            ):
                return 0
            if broker == "futu":
                return 0
            if broker in {"tiger", "phillips", "eastmoney"}:
                return 0
            if self.selector == f"{entry} [data-trend-report]":
                return int(bool(self.page.reports[broker]["available"]))
            return 1
        match = re.fullmatch(
            r'#account-(\w+):visible \[data-trend-review="(\w+)"\]',
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            return int(
                broker == match.group(2)
                and self.page.selected == broker
                and self.page.trend_broker is None
                and bool(self.page.reviews[broker]["available"])
            )
        if self.selector == "#trend-report-workspace:visible":
            return int(self.page.trend_broker is not None)
        if self.selector.endswith(" details.trend-review-disclosure"):
            match = re.search(r"#account-(\w+)-view-panel:visible", self.selector)
            if not match:
                return 0
            broker = self._require_known_broker(match.group(1))
            return int(
                self.page.trend_broker == broker
                and self.page.account_views.get(broker) == "report"
            )
        if self.selector == "#trend-report-workspace:visible .cn-trend-table thead th":
            return 16
        if self.selector == "#trend-report-workspace:visible [data-trend-holding-section]":
            return 1
        if self.selector == (
            "#trend-report-workspace:visible [data-trend-holding-section] "
            "[data-trend-holding-view]"
        ):
            return 2
        match = re.fullmatch(
            r"#trend-report-workspace:visible \[data-trend-holding-section\] "
            r"\[data-trend-holding-view\]:nth\((\d+)\)",
            self.selector,
        )
        if match:
            return int(int(match.group(1)) in (0, 1))
        if self.selector in {
            "#trend-report-workspace:visible [data-trend-holding-section] "
            '[data-trend-holding-panel="real"]',
            "#trend-report-workspace:visible [data-trend-holding-section] "
            '[data-trend-holding-panel="simulate"]',
        }:
            return 1
        visible_panel = re.fullmatch(
            r"#trend-report-workspace:visible \[data-trend-holding-section\] "
            r'\[data-trend-holding-panel="(real|simulate)"\]:visible',
            self.selector,
        )
        if visible_panel:
            view = visible_panel.group(1)
            return int(view == self.page.holding_view)
        holding_panel_match = re.fullmatch(
            r"#trend-report-workspace:visible \[data-trend-holding-section\] "
            r'\[data-trend-holding-panel="(real|simulate)"\] (.*)',
            self.selector,
        )
        if holding_panel_match:
            view, suffix = holding_panel_match.groups()
            report = self.page.reports.get(str(self.page.trend_broker), {})
            items_key = "real_position_actions" if view == "real" else "hold_actions"
            items = report.get(items_key)
            items = items if isinstance(items, list) else []
            if suffix == ".cn-trend-table":
                return int(view == "simulate" or report.get("real_position_status") == "available")
            if suffix == ".cn-trend-table thead th":
                return 10 if (view == "simulate" or report.get("real_position_status") == "available") else 0
            if suffix == ".cn-trend-card":
                return len(items)
        if self.selector in {
            "#trend-report-workspace:visible .trend-option-button",
            "#trend-report-workspace:visible .cn-trend-buy .trend-option-button",
            "#trend-report-workspace:visible .cn-trend-hold .trend-option-button",
        }:
            return self.page.option_button_count()
        if self.selector == "#trend-report-workspace:visible .trend-option-button:disabled":
            return 0
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.trend-option-button:nth\((\d+)\)",
            self.selector,
        )
        if match:
            index = int(match.group(1))
            return int(index < self.page.option_button_count())
        if self.selector == "#trend-report-workspace:visible dialog.trend-option-dialog:visible":
            return int(self.page.option_dialog_open)
        if self.selector in {
            "#trend-report-workspace:visible dialog.trend-option-dialog:visible button[data-option-anomaly-close]",
            "#trend-report-workspace:visible dialog.trend-option-dialog:visible button[data-option-anomaly-close]:visible",
        }:
            return 2 if self.page.option_dialog_open else 0
        if self.selector == "#return-to-portfolio:visible":
            return int(self.page.trend_broker is not None)
        if self.selector == "#trend-report-workspace:visible [data-close-trend-report]":
            return int(self.page.trend_broker is not None)
        if self.selector == "#trend-report-workspace:visible .trend-review-header-side":
            return int(self.page.trend_kind == "review")
        if self.selector == "#trend-report-workspace:visible .trend-review-header-side > *":
            return 4 if self.page.trend_kind == "review" else 0
        if self.selector == "#trend-report-workspace:visible .trend-review-parameters":
            return 0
        if self.selector == "#trend-report-workspace:visible .trend-review-comparison":
            return 2 if self.page.trend_kind == "review" else 0
        match = re.fullmatch(
            r'#trend-report-workspace:visible \.trend-review-comparison'
            r'\[data-series="(discipline|actual)"\](.*)',
            self.selector,
        )
        if match and self.page.trend_kind == "review":
            suffix = match.group(2)
            if suffix == "":
                return 1
            if suffix == " .trend-review-metric":
                return 5
            if suffix == " .trend-review-series":
                return 10
            if re.fullmatch(r" \.trend-review-metric:nth\(\d+\) \.trend-review-series", suffix):
                return 2
        if self.selector == "#trend-report-workspace:visible .trend-review-chart":
            return 0
        if self.selector == ".workspace-grid:visible":
            return int(self.page.trend_broker is None)
        if self.selector == "#trend-report-workspace:visible .cn-trend-report":
            return int(self.page.trend_broker is not None)
        if self.selector == "#trend-report-workspace:visible .trend-controller-status":
            return int(
                self.page.trend_broker is not None
                and self.page.trend_broker != self.page.missing_controller_broker
            )
        if self.selector.endswith(" .trend-controller-status :scope > summary"):
            return 1
        if self.selector == "#trend-report-workspace:visible .trend-discipline-workspace":
            return int(self.page.trend_broker is not None)
        if self.selector.endswith(" .trend-discipline-category"):
            return 6 if self.page.trend_broker is not None else 0
        if re.search(r"\.trend-discipline-category(?:\:nth\(\d+\))? summary$", self.selector):
            return 6 if self.page.trend_broker is not None else 0
        if re.search(r"\.trend-discipline-category:nth\(\d+\) \.trend-discipline-category-body$", self.selector):
            return 1 if self.page.trend_broker is not None else 0
        if self.selector.endswith(" .trend-discipline-card"):
            return 6 if self.page.trend_broker is not None else 0
        if re.search(r"\.trend-discipline-card summary$", self.selector):
            return 6 if self.page.trend_broker is not None else 0
        if re.search(r"\.trend-discipline-card:nth\(\d+\) \.trend-discipline-card-count$", self.selector):
            return 1 if self.page.trend_broker is not None else 0
        if self.selector.endswith(" .trend-industry-context"):
            return 1 if self.page.trend_broker is not None else 0
        if self.selector == "#trend-report-workspace:visible .trend-discipline[open]":
            return int(self.page.trend_broker == "eastmoney") * (
                0 if self.page.viewport_size["width"] <= 760 else 2
            )
        if self.selector == "#trend-report-workspace:visible .trend-discipline":
            return 2 if self.page.trend_broker == "eastmoney" else 0
        if self.selector == "#trend-report-workspace:visible .cn-trend-table":
            report = self.page.reports.get(str(self.page.trend_broker), {})
            review = report.get("review_actions")
            return 3 + int(isinstance(review, list) and bool(review)) if self.page.trend_broker is not None else 0
        if self.selector in {
            "#trend-report-workspace:visible .cn-trend-execution",
            "#trend-report-workspace:visible .cn-trend-execution span:first-child",
        }:
            return 0
        if self.selector == (
            "#trend-report-workspace:visible .cn-trend-buy .cn-trend-card"
        ):
            report = self.page.reports.get(str(self.page.trend_broker), {})
            actions = report.get("buy_actions", [])
            return len(actions) if isinstance(actions, list) else 0
        if self.selector == (
            "#trend-report-workspace:visible .cn-trend-buy .cn-trend-card:visible"
        ):
            report = self.page.reports.get(str(self.page.trend_broker), {})
            actions = report.get("buy_actions", [])
            return len(actions) if isinstance(actions, list) else 0
        if self.selector == "#trend-report-workspace:visible .cn-trend-card:visible":
            report = self.page.reports.get(str(self.page.trend_broker), {})
            return sum(
                len(actions) if isinstance(actions, list) else 0
                for actions in (
                    report.get("sell_actions"), report.get("review_actions"),
                    report.get("buy_actions"), report.get("hold_actions"),
                )
            )
        if self.selector in {"#tiger-long-term-panel", "#trade-actions"}:
            return 0
        match = re.fullmatch(
            r"#account-(\w+):visible \.account-holding-row:visible", self.selector
        )
        if match and match.group(1) in self.page.tab_order:
            return self.page.visible_rows(self.selector)
        match = re.fullmatch(
            r"#account-(\w+):visible \.account-empty:visible", self.selector
        )
        if match and match.group(1) in self.page.tab_order:
            return int(self.page.visible_rows(self.selector) == 0)
        if re.fullmatch(
            r'\.account-holding-row:visible:has\('
            r'\.account-holding-market:has-text\("US"\)\) '
            r'\.account-holding-price:nth\(\d+\) \.session-quote',
            self.selector,
        ):
            return 1
        if self.selector == (
            '.account-holding-row:visible:has('
            '.account-holding-market:has-text("US")) .account-holding-price'
        ):
            return int(self.page.selected == "futu" and self.page.market != "CN")
        if self.selector in {
            "#trend-report-workspace:visible .trend-audit",
            "#trend-report-workspace:visible .trend-audit summary",
            "#trend-report-workspace:visible .trend-audit section",
            "#trend-report-workspace:visible .trend-report-header dd",
            "#trend-report-workspace:visible .trend-discipline summary",
            "#trend-report-workspace:visible .cn-trend-buy",
        }:
            return 1
        if self.selector.endswith(" .trend-discipline-workspace :scope > summary"):
            return 1
        if re.fullmatch(
            r'#trend-report-workspace:visible \.cn-trend-buy '
            r'\.cn-trend-card:nth\(\d+\) td\[data-label="'
            r'(行业|筛选价（Trend Animals）|执行参考价（Futu 前复权）)"\]',
            self.selector,
        ):
            return 1
        raise AssertionError(f"unknown count selector: {self.selector}")

    def get_attribute(self, name: str) -> str | None:
        if self.selector == "#trend-report-workspace:visible .trend-discipline-workspace":
            assert name == "open"
            return None
        if self.selector == "#trend-report-workspace:visible .trend-controller-status":
            if name == "open":
                return None
            assert name == "data-health"
            return str(self.page.controllers[str(self.page.trend_broker)]["health"])
        if self.selector == "#trend-report-workspace:visible dialog.trend-option-dialog:visible":
            assert name == "aria-label"
            action = self.page.option_available_actions()[self.page.option_dialog_index]
            identity = " ".join(
                str(action.get(key)).strip()
                for key in ("symbol", "name")
                if action.get(key)
            )
            return f"富途期权异动详情：{identity}"
        match = re.fullmatch(
            r"#account-tabs \[data-broker\]:nth\((\d+)\)", self.selector
        )
        if match:
            assert name == "data-broker"
            return self.page.tab_order[int(match.group(1))]
        match = re.fullmatch(r'#account-tabs \[data-broker="(\w+)"\]', self.selector)
        if match:
            broker = self._require_known_broker(match.group(1))
            assert name == "aria-selected"
            return str(broker == self.page.selected).lower()
        match = re.fullmatch(
            r'#account-(\w+):visible \[data-account-view="(\w+)"\]',
            self.selector,
        )
        if match:
            broker = self._require_known_broker(match.group(1))
            assert name == "aria-selected"
            return str(self.page.account_views[broker] == match.group(2)).lower()
        match = re.fullmatch(
            r"#trend-report-workspace:visible \[data-trend-holding-section\] "
            r"\[data-trend-holding-view\]:nth\((\d+)\)",
            self.selector,
        )
        if match:
            assert name == "aria-selected"
            index = int(match.group(1))
            return str(self.page.holding_view == ("real" if index == 0 else "simulate")).lower()
        if self.selector == "#trend-report-workspace:visible .cn-trend-buy":
            mobile = self.page.viewport_size["width"] <= 760
            return {
                "tabindex": "-1" if mobile else "0",
                "aria-label": (
                    "正式买入计划" if mobile else "正式买入计划，可横向滚动"
                ),
            }[name]
        if re.search(r"\.trend-discipline-category:nth\(\d+\)$", self.selector):
            assert name == "open"
            return ""
        assert self.selector == "#trend-report-workspace:visible .trend-audit"
        assert name == "open"
        return None

    def focus(self) -> None:
        self.page.active = self.selector
        self.page.focus_checks.append(self.selector)

    def is_disabled(self) -> bool:
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.trend-option-button:nth\((\d+)\)",
            self.selector,
        )
        if match:
            index = int(match.group(1))
            if index in self.page.option_disabled_override:
                return self.page.option_disabled_override[index]
            return False
        match = re.fullmatch(
            r'#account-(\w+):visible \.trend-report-entry button'
            r'(?:\:has-text\("当天趋势报告"\))?',
            self.selector,
        )
        assert match
        broker = self._require_known_broker(match.group(1))
        self.page.disabled_reports.add(broker)
        return not bool(self.page.reports[broker]["available"])

    def inner_text(self) -> str:
        if self.selector == "#source-status-list":
            return "实时账户 富途账户 同步正常 · 13:48 老虎账户 同步正常 · 13:49 券商结单 辉立账户 数据截至 · 07-29 东方财富账户 数据截至 · 07-30"
        source_match = re.fullmatch(
            r'#source-status-list \[data-broker="(\w+)"\]', self.selector
        )
        if source_match:
            broker = self._require_known_broker(source_match.group(1))
            labels = {
                "futu": "富途", "tiger": "老虎",
                "phillips": "辉立", "eastmoney": "东方财富",
            }
            source = self.page.payload.get("account_sync", {})
            source = source if isinstance(source, Mapping) else {}
            brokers = source.get("brokers", {})
            brokers = brokers if isinstance(brokers, Mapping) else {}
            broker_source = brokers.get(broker)
            broker_source = broker_source if isinstance(broker_source, Mapping) else {}
            return (
                f"{labels[broker]}账户 "
                f"{dashboard_acceptance._expected_source_copy(broker, broker_source)}"
            )
        if self.selector == "#account-holdings":
            return self.page.section_texts[self.page.selected]
        match = re.fullmatch(r"#account-(\w+):visible", self.selector)
        if match and match.group(1) in self.page.tab_order:
            return self.page.section_texts[match.group(1)]
        match = re.fullmatch(
            r"#account-(\w+):visible \.trend-report-entry", self.selector
        )
        if match and match.group(1) in self.page.tab_order:
            return self.page.entry_texts[match.group(1)]
        match = re.fullmatch(r"#account-(\w+)-view-panel:visible", self.selector)
        if match:
            broker = self._require_known_broker(match.group(1))
            report = self.page.reports[broker]
            if report.get("available") is not True:
                return str(report.get("status_text") or "今日暂无趋势报告")
            return self.page.workspace_texts[broker]
        if self.selector == "#trend-report-workspace:visible" or re.fullmatch(
            r"#account-(\w+)-view-panel:visible \.cn-trend-report:visible",
            self.selector,
        ):
            if self.page.trend_kind == "review":
                broker = str(self.page.trend_broker)
                return trend_review_workspace_text(broker)
            broker = str(self.page.trend_broker)
            return self.page.workspace_texts[broker]
        if self.selector == "#trend-report-workspace:visible .trend-controller-status":
            controller = self.page.controllers[str(self.page.trend_broker)]
            headline = (
                "只读部署，不运行本机控制器"
                if controller["effective_mode"] == "readonly"
                else "控制器不可用"
                if controller["health"] == "unavailable"
                else "执行主机控制器正常"
            )
            last_success = controller["last_success"]
            if isinstance(last_success, Mapping):
                artifacts = last_success.get("artifact_paths")
                artifact_text = (
                    "，".join(str(item) for item in artifacts)
                    if isinstance(artifacts, list) and artifacts
                    else "无"
                )
                last_success = " · ".join((
                    f"状态 {last_success.get('status')}",
                    f"市场 {last_success.get('market')}",
                    f"日期 {last_success.get('date')}",
                    f"提交数 {last_success.get('submitted_count')}",
                    f"产物 {artifact_text}",
                ))
            return " ".join(str(value) for value in (
                "策略控制器", headline,
                "执行模式", controller["effective_mode"],
                "执行主机", controller["executor_host"],
                "本地主机", controller["local_host"],
                "PID", controller["pid"] if controller["pid"] is not None else "—",
                "Git SHA", controller["git_sha"],
                "当前阶段", controller["phase"],
                "心跳", controller["heartbeat_at"],
                "最近成功", last_success,
                "当前阻塞", controller["blocker"],
                "下次检查", controller["next_check_at"],
            ))
        match = re.fullmatch(
            r"#trend-report-workspace:visible \[data-trend-holding-section\] "
            r"\[data-trend-holding-view\]:nth\((\d+)\)",
            self.selector,
        )
        if match:
            return ["真实持仓", "模拟盘持仓"][int(match.group(1))]
        panel_match = re.fullmatch(
            r"#trend-report-workspace:visible \[data-trend-holding-section\] "
            r'\[data-trend-holding-panel="(real|simulate)"\]',
            self.selector,
        )
        if panel_match:
            view = panel_match.group(1)
            report = self.page.reports.get(str(self.page.trend_broker), {})
            if view == "real" and report.get("real_position_status") == "unavailable":
                return str(report.get("real_position_reason") or "数据未提供")
            if view == "real" and report.get("real_position_status") == "legacy":
                return "当前报告未包含真实持仓判断"
            key = "real_position_actions" if view == "real" else "hold_actions"
            items = report.get(key)
            items = items if isinstance(items, list) else []
            values = ["只读"] if view == "real" and report.get("real_position_status") == "available" else []
            values.extend(
                str(value) for item in items if isinstance(item, dict)
                for value in (item.get("symbol"), item.get("name")) if value
            )
            return " ".join(values) or "无"
        if self.selector == "#trend-report-workspace:visible dialog.trend-option-dialog:visible":
            action = self.page.option_available_actions()[self.page.option_dialog_index]
            return f"富途期权异动 {action.get('symbol', '')} {action.get('name', '')}"
        if self.selector == "#trend-report-workspace:visible .trend-audit":
            return trend_audit_text(str(self.page.trend_broker))
        if self.selector.endswith(" .trend-discipline-workspace"):
            return "纪律 6 类 · 0 项 · 本报告生成时参数 本报告未提供该类纪律参数"
        if self.selector.endswith(" .trend-discipline-workspace :scope > summary"):
            return "纪律 6 类 · 0 项 · 本报告生成时参数"
        summary_match = re.search(r"\.trend-discipline-category:nth\((\d+)\) summary$", self.selector)
        if summary_match:
            titles = ["入场门槛", "候选排序", "仓位与执行", "持有管理", "退出规则", "其他设置"]
            return f"{titles[int(summary_match.group(1))]} 0 项 本报告未提供该类纪律参数"
        if self.selector.endswith(" .trend-industry-context"):
            return "行业上下文 当前行业上下文未提供，无法确认排序；未使用当前规则"
        match = re.fullmatch(
            r"#account-(\w+):visible \.account-empty:visible", self.selector
        )
        if match and match.group(1) in self.page.tab_order:
            return "当前筛选下没有持仓"
        if self.selector == "#visible-count":
            return f"{self.page.visible_rows():,} 条"
        if re.fullmatch(
            r'\.account-holding-row:visible:has\('
            r'\.account-holding-market:has-text\("US"\)\) '
            r'\.account-holding-price:nth\(\d+\) \.session-quote:nth\(0\)',
            self.selector,
        ):
            return "夜盘 61.50 · 03:03 ET"
        if self.selector == "body":
            return "持仓与策略"
        if self.selector == '#broker-summary-cards [data-broker="phillips"] strong':
            return "HKD 628,554.06"
        if self.selector == "#trend-report-workspace:visible .cn-trend-buy":
            return trend_workspace_text(str(self.page.trend_broker))
        match = re.fullmatch(
            r'#trend-report-workspace:visible \.cn-trend-buy '
            r'\.cn-trend-card:nth\(\d+\) td\[data-label="([^"]+)"\]',
            self.selector,
        )
        if match:
            buy = self.page.reports["eastmoney"]["buy_actions"][0]
            keys = {
                "行业": "industry",
                "筛选价（Trend Animals）": "filter_price",
                "执行参考价（Futu 前复权）": "close",
            }
            if match.group(1) not in keys:
                raise AssertionError(
                    f"unknown inner_text selector: {self.selector}"
                )
            key = keys[match.group(1)]
            return str(buy[key])
        raise AssertionError(f"unknown inner_text selector: {self.selector}")

    def all_inner_texts(self) -> list[str]:
        if self.selector == (
            "#trend-report-workspace:visible .trend-controller-status dl div"
        ):
            controller = self.page.controllers[str(self.page.trend_broker)]
            return [
                f"{label}\n{value if value not in (None, '') else '—'}"
                for label, value in (
                    ("执行模式", controller["effective_mode"]),
                    ("执行主机", controller["executor_host"]),
                    ("本地主机", controller["local_host"]),
                    ("PID", controller["pid"]),
                    ("Git SHA", controller["git_sha"]),
                    ("当前阶段", controller["phase"]),
                    ("心跳", controller["heartbeat_at"]),
                    ("最近成功", controller["last_success"]),
                    ("当前阻塞", controller["blocker"]),
                    ("下次检查", controller["next_check_at"]),
                )
            ]
        if self.selector == "a:visible, button:visible":
            return ["刷新账户与行情", "策略回测"]
        broker = str(self.page.trend_broker)
        if self.selector == "#trend-report-workspace:visible .trend-review-header > div:first-child > *":
            if self.page.review_header_left_texts is not None:
                return self.page.review_header_left_texts
            review = self.page.reviews[broker]
            snapshot = review["strategy_snapshot"]
            return [
                f"{review['broker_label']}｜{review['market_label']}",
                f"{review['market_label']}趋势复盘",
                f"{snapshot['strategy_name']}｜第 1 版",
            ]
        if self.selector == "#trend-report-workspace:visible .trend-review-header-side > *":
            return [
                "返回持仓看板", "纪律模拟 31 笔",
                "实际执行 29 / 30，数据不足", "共同截止日 2026-07-17",
            ]
        if self.selector == "#trend-report-workspace:visible .trend-review-comparison figcaption":
            return ["纪律模拟与市场", "实际执行与市场"]
        if self.selector == "#trend-report-workspace:visible .trend-review-series strong":
            values = ["12.6%"] * 20
            if self.page.review_metric_reason is not None:
                values[0] = self.page.review_metric_reason
            return values
        match = re.fullmatch(
            r'#trend-report-workspace:visible \.trend-review-comparison'
            r'\[data-series="(discipline|actual)"\] (.*)',
            self.selector,
        )
        if match:
            series, suffix = match.groups()
            metrics = [
                spec[1] for spec in dashboard_acceptance.TREND_REVIEW_METRIC_SPECS
            ]
            if suffix == ".trend-review-metric h3":
                return metrics
            if suffix == ".trend-review-series > span:first-child":
                label = "纪律模拟" if series == "discipline" else "实际执行"
                return [item for _metric in metrics for item in (label, "同期市场")]
            if suffix == '.trend-review-series[data-series="benchmark"] strong':
                self.page.review_benchmark_checks.append(series)
                return self.page.review_benchmark_values[series]
        if self.selector == "#trend-report-workspace:visible .cn-trend-stage":
            return trend_stage_texts(broker)
        if self.selector == "#trend-report-workspace:visible .trend-stage":
            return trend_stage_texts(broker)
        if self.selector in {
            "#trend-report-workspace:visible [data-trend-holding-section] "
            '[data-trend-holding-panel="real"] .cn-trend-table thead th',
            "#trend-report-workspace:visible [data-trend-holding-section] "
            '[data-trend-holding-panel="simulate"] .cn-trend-table thead th',
        }:
            return [
                "标的", "动作", "执行参考价", "温度变化", "节气", "强度", "行业",
                "当前判断", "活动保护线", "持仓提示",
            ]
        if self.selector == "#trend-report-workspace:visible .trend-report-header dd":
            report = self.page.reports[broker]
            return [str(report[key]) for key in (
                "report_date", "data_date", "generated_at", "account_status",
            )]
        if self.selector == (
            '#trend-report-workspace:visible td[data-label="活动保护线"], '
            'td[data-label="预计保护线"]'
        ):
            return ["7", "42", "1,450", "24.55", "27.8"]
        if self.selector == "#trend-report-workspace:visible .trend-audit section":
            return trend_audit_sections(broker)
        if self.selector.endswith(" .trend-discipline-category summary"):
            return [
                f"{title} 0 项 本报告未提供该类纪律参数"
                for title in ("入场门槛", "候选排序", "仓位与执行", "持有管理", "退出规则", "其他设置")
            ]
        if self.selector == "#trend-report-workspace:visible .trend-discipline summary":
            return ["买入纪律", "卖出纪律"]
        match = re.fullmatch(
            r"#account-(\w+):visible \.account-holding-row:visible td:nth-child\(2\)",
            self.selector,
        )
        if match and match.group(1) in self.page.tab_order:
            return ["市场\nCN"] * self.page.visible_rows(self.selector)
        raise AssertionError(f"unknown all_inner_texts selector: {self.selector}")

    def nth(self, index: int) -> "TabbedAccountLocator":
        return self.page.locator(f"{self.selector}:nth({index})")

    def evaluate(self, expression: str) -> bool | dict[str, object]:
        active_expression = "element => element === document.activeElement"
        focus_expression = (
            "element => { const styles = getComputedStyle(element); return {"
            "outlineColor: styles.outlineColor, outlineStyle: styles.outlineStyle, "
            "outlineWidth: styles.outlineWidth}; }"
        )
        overflow_expression = (
            "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, "
            "overflowX: getComputedStyle(element).overflowX})"
        )
        if self.selector == "#trend-report-workspace:visible .cn-trend-buy":
            if expression == active_expression:
                return self.selector == self.page.active
            if expression == focus_expression:
                return {
                    "outlineColor": "rgb(139, 94, 52)",
                    "outlineStyle": "solid",
                    "outlineWidth": "3px",
                }
            if expression == overflow_expression:
                return {
                    "clientWidth": 1500,
                    "scrollWidth": 1600,
                    "overflowX": "auto",
                }
            raise AssertionError(f"unknown evaluate expression: {expression}")
        if expression != active_expression:
            raise AssertionError(f"unknown evaluate expression: {expression}")
        self.page.focus_checks.append(self.selector)
        return self.selector == self.page.active

    def bounding_box(self) -> dict[str, float]:
        return {"x": 20, "width": 100}

    def evaluate_all(self, expression: str) -> list[dict[str, float]]:
        if self.selector.endswith(" .trend-discipline-category summary") and "height" in expression:
            return [{"height": 44}] * 6
        target_expression = (
            "nodes => nodes.map(node => ({"
            "height: node.getBoundingClientRect().height, "
            "label: node.getAttribute('aria-label') || node.textContent.trim() || node.tagName"
            "}))"
        )
        bounds_expression = (
            "nodes => nodes.map(node => node.getBoundingClientRect())"
            ".map(r => ({x:r.x,width:r.width}))"
        )
        if expression == target_expression:
            self.page.target_checks.append(self.selector)
            height = (
                43
                if (
                    self.selector == self.page.undersized_target_selector
                    and self.page.trend_broker == "futu"
                )
                else 44
            )
            return [{"height": height, "label": self.selector}]
        if expression == bounds_expression:
            self.page.bounds_checks.append(self.selector)
            if self.selector == self.page.overflow_bounds_selector:
                return [{"x": 10, "width": 380}]
            return [{"x": 10, "width": 350}]
        raise AssertionError(f"unknown evaluate_all expression: {expression}")


class TabbedAccountPage:
    viewport_size = {"width": 1440, "height": 1000}

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        cn_rows: dict[str, int] | None = None,
    ) -> None:
        source = payload or valid_payload()
        self.payload = source
        self.reports = source["trend_reports"]  # type: ignore[assignment]
        self.reviews = source["trend_reviews"]  # type: ignore[assignment]
        self.controllers = source.get("trend_controllers", trend_controllers())  # type: ignore[assignment]
        self.broker_positions = source.get("broker_positions", [])
        self.section_texts = dict(ACCOUNT_SECTION_TEXTS)
        self.entry_texts = {
            broker: (
                f"当天趋势报告 报告日期 {report.get('report_date', '-')} "
                f"数据截至 {report.get('data_date', '-')}"
                if report.get("available") is True
                else f"当天趋势报告 {report.get('status_text', '')}"
            )
            for broker, report in self.reports.items()
        }
        self.entry_texts.setdefault("futu", "")
        self.workspace_texts = {
            broker: trend_workspace_text(broker, report)
            for broker, report in self.reports.items()
        }
        self.all_rows = {"futu": 1, "tiger": 1, "phillips": 1, "eastmoney": 0}
        self.cn_rows = cn_rows or {"futu": 0, "tiger": 0, "phillips": 0, "eastmoney": 5}
        self.market = "ALL"
        self.selected = "futu"
        self.tab_order = ["futu", "tiger", "phillips", "eastmoney"]
        self.account_views = {broker: "real" for broker in self.tab_order}
        self.selected_brokers: list[str] = []
        self.visible_account_sections = 1
        self.max_visible_account_sections = 1
        self.trend_broker: str | None = None
        self.trend_kind = ""
        self.holding_view = "real"
        self.active: str | None = None
        self.opened_reports: list[str] = []
        self.opened_reviews: list[str] = []
        self.disabled_reports: set[str] = set()
        self.option_button_count_override: int | None = None
        self.option_disabled_override: dict[int, bool] = {}
        self.option_dialog_open = False
        self.option_dialog_index = -1
        self.focus_checks: list[str] = []
        self.target_checks: list[str] = []
        self.bounds_checks: list[str] = []
        self.undersized_target_selector = ""
        self.overflow_bounds_selector = ""
        self.missing_controller_broker = ""
        self.document_overflow_broker = ""
        self.document_overflow_checks: list[str | None] = []
        self.review_style_checks: list[str | None] = []
        self.review_geometry_checks: list[str | None] = []
        self.review_benchmark_checks: list[str] = []
        self.review_benchmark_values = {
            "discipline": ["7.8%", "7.8%", "7.8%", "7.8", "7.8"],
            "actual": ["7.8%", "7.8%", "7.8%", "7.8", "7.8"],
        }
        self.review_panel_radius = "8px"
        self.review_label_border_width = "0px"
        self.review_header_span_border_width = "0px"
        self.review_header_left_texts: list[str] | None = None
        self.review_metric_reason: str | None = None
        self.review_text_layout_override: dict[str, object] = {}
        self.review_long_text_scroll_width = 323
        self.review_document_width: int | None = None
        self.workspace_view = "portfolio"
        self.research_open = False
        self.script_evaluations: list[tuple[str, object | None]] = []

    def option_actions(self) -> list[dict[str, object]]:
        report = self.reports.get(str(self.trend_broker), {})
        return [
            item
            for key in ("buy_actions", "real_position_actions", "hold_actions")
            for item in (report.get(key) if isinstance(report.get(key), list) else [])
            if isinstance(item, dict)
        ]

    def option_available_actions(self) -> list[dict[str, object]]:
        return [
            item for item in self.option_actions()
            if isinstance(item.get("option_anomaly"), dict)
            and item["option_anomaly"].get("available") is True
        ]

    def option_button_count(self) -> int:
        if self.option_button_count_override is not None:
            return self.option_button_count_override
        return len(self.option_available_actions())

    def _record_visible_sections(self) -> int:
        embedded = (
            self.trend_broker is not None
            and self.account_views.get(self.trend_broker) == "report"
        )
        visible = self.visible_account_sections if self.trend_broker is None or embedded else 0
        self.max_visible_account_sections = max(
            self.max_visible_account_sections, visible
        )
        return visible

    def visible_rows(self, selector: str = "") -> int:
        match = re.search(r"#account-(\w+):visible", selector)
        broker = match.group(1) if match else self.selected
        if self.account_views[broker] != "real":
            return 0
        rows = self.cn_rows if self.market == "CN" else self.all_rows
        return rows[broker]

    def locator(self, selector: str) -> TabbedAccountLocator:
        return TabbedAccountLocator(self, selector)

    def evaluate(
        self, expression: str, argument: object | None = None,
    ) -> bool | list[int] | list[dict[str, object]] | dict[str, object] | Mapping[str, object] | int | None:
        if expression == "() => state.dashboard?.broker_positions ?? []":
            return self.broker_positions
        if expression == "broker => state.dashboard?.trend_controllers?.[broker] ?? null":
            return self.controllers.get(str(argument))
        if "openResearchChat" in expression:
            self.script_evaluations.append((expression, argument))
            self.research_open = True
            return None
        if "trend-review-style-contract" in expression:
            required = (
                'document.querySelector("#trend-report-workspace")',
                "document.createElement", "getComputedStyle", "querySelectorAll",
                ".trend-review-comparison",
                ".trend-review-series > span:first-child",
                ".trend-review-header-side > span",
                'workspace.querySelector(".trend-review-header-side")',
                '.trend-review-header-side button',
                "backgroundColor", "borderColor", "borderWidth", "color",
                "borderRadius", "boxShadow", "backgroundImage",
            )
            missing = [fragment for fragment in required if fragment not in expression]
            assert not missing, f"trend review style fake 缺少真实表达式：{missing}"
            self.review_style_checks.append(self.trend_broker)
            tokens = {
                "bg": "rgb(247, 245, 241)",
                "surface": "rgb(255, 254, 250)",
                "surfaceSoft": "rgb(242, 238, 231)",
                "text": "rgb(32, 29, 24)",
                "muted": "rgb(116, 110, 100)",
                "accent": "rgb(139, 94, 52)",
                "line": "rgb(216, 210, 200)",
                "primary": "rgb(36, 33, 29)",
                "shadow": "rgba(68, 55, 38, 0.06) 0px 8px 30px 0px",
            }
            panel = {
                "backgroundColor": tokens["surfaceSoft"],
                "borderColor": tokens["line"],
                "borderWidth": "1px",
                "color": tokens["text"],
                "borderRadius": self.review_panel_radius, "boxShadow": "none",
                "backgroundImage": "none",
            }
            return {
                "tokens": tokens,
                "workspace": {
                    "backgroundColor": tokens["surface"],
                    "borderColor": tokens["line"],
                    "borderWidth": "1px",
                    "color": tokens["text"],
                    "borderRadius": "8px", "boxShadow": tokens["shadow"],
                    "backgroundImage": "none",
                },
                "panels": [panel, panel],
                "side": {
                    "backgroundColor": "rgba(0, 0, 0, 0)",
                    "borderWidth": "0px", "boxShadow": "none",
                    "backgroundImage": "none",
                },
                "button": {
                    "backgroundColor": tokens["surface"],
                    "borderColor": tokens["accent"],
                    "borderWidth": "1px",
                    "color": tokens["accent"],
                    "borderRadius": "7px", "boxShadow": "none",
                    "backgroundImage": "none",
                },
                "headerSpans": [{
                    "backgroundColor": "rgba(0, 0, 0, 0)",
                    "borderColor": tokens["muted"],
                    "borderWidth": self.review_header_span_border_width,
                    "color": tokens["muted"],
                    "borderRadius": "0px", "boxShadow": "none",
                    "backgroundImage": "none",
                }] * 3,
                "labels": [
                    *([{
                        "text": "纪律模拟", "backgroundColor": "rgba(0, 0, 0, 0)",
                        "borderColor": tokens["accent"], "borderWidth": self.review_label_border_width,
                        "color": tokens["accent"], "borderRadius": "0px",
                        "boxShadow": "none", "backgroundImage": "none",
                    }, {
                        "text": "同期市场", "backgroundColor": "rgba(0, 0, 0, 0)",
                        "borderColor": tokens["primary"], "borderWidth": "0px",
                        "color": tokens["primary"], "borderRadius": "0px",
                        "boxShadow": "none", "backgroundImage": "none",
                    }] * 5),
                    *([{
                        "text": "实际执行", "backgroundColor": "rgba(0, 0, 0, 0)",
                        "borderColor": tokens["accent"], "borderWidth": "0px",
                        "color": tokens["accent"], "borderRadius": "0px",
                        "boxShadow": "none", "backgroundImage": "none",
                    }, {
                        "text": "同期市场", "backgroundColor": "rgba(0, 0, 0, 0)",
                        "borderColor": tokens["primary"], "borderWidth": "0px",
                        "color": tokens["primary"], "borderRadius": "0px",
                        "boxShadow": "none", "backgroundImage": "none",
                    }] * 5),
                ],
            }
        if "trend-review-geometry-contract" in expression:
            required = (
                'document.querySelector("#trend-report-workspace")',
                "documentWidth: document.documentElement.scrollWidth",
                ".trend-review-header > div:first-child > *",
                ".trend-review-header-side > *",
                ".trend-review-comparison figcaption",
                ".trend-review-metric h3",
                ".trend-review-series > span:first-child",
                ".trend-review-series strong",
                ".trend-review-comparison",
                "querySelectorAll", "querySelector", "getBoundingClientRect",
                "getComputedStyle", "clientWidth", "scrollWidth",
                "clientHeight", "scrollHeight", "whiteSpace", "textOverflow",
                "overflow", "overflowX", "overflowY",
            )
            missing = [fragment for fragment in required if fragment not in expression]
            assert not missing, f"trend review geometry fake 缺少真实表达式：{missing}"
            self.review_geometry_checks.append(self.trend_broker)
            narrow = self.viewport_size["width"] <= 760
            panel_width = self.viewport_size["width"] - 28 if narrow else 680
            text_counts = (
                (".trend-review-header > div:first-child > *", 3),
                (".trend-review-header-side > *", 4),
                (".trend-review-comparison figcaption", 2),
                (".trend-review-metric h3", 10),
                (".trend-review-series > span:first-child", 20),
                (".trend-review-series strong", 20),
            )
            text_groups: list[dict[str, object]] = []
            first_layout = True
            for selector, count in text_counts:
                layouts = []
                for _index in range(count):
                    layout: dict[str, object] = {
                        "clientWidth": 323,
                        "scrollWidth": (
                            self.review_long_text_scroll_width if first_layout else 323
                        ),
                        "clientHeight": 40, "scrollHeight": 40,
                        "whiteSpace": "normal", "textOverflow": "clip",
                        "overflow": "visible", "overflowX": "visible",
                        "overflowY": "visible",
                    }
                    if first_layout:
                        layout.update(self.review_text_layout_override)
                        first_layout = False
                    layouts.append(layout)
                text_groups.append({"selector": selector, "layouts": layouts})
            return {
                "documentWidth": self.review_document_width or self.viewport_size["width"],
                "side": {"x": 14, "y": 120, "width": 347, "height": 176},
                "sideItems": [
                    {"x": 14, "y": 120, "width": 347, "height": 44},
                    {"x": 14, "y": 172, "width": 347, "height": 20},
                    {"x": 14, "y": 200, "width": 347, "height": 20},
                    {"x": 14, "y": 228, "width": 347, "height": 20},
                ],
                "button": {"x": 14, "y": 120, "width": 347, "height": 44},
                "panels": [
                    {"x": 14, "y": 460, "width": panel_width, "height": 600},
                    {"x": 14 if narrow else 706, "y": 1072 if narrow else 460, "width": panel_width, "height": 600},
                ],
                "textGroups": text_groups,
            }
        assert expression == "document.documentElement.scrollWidth <= window.innerWidth"
        self.document_overflow_checks.append(self.trend_broker)
        return self.trend_broker != self.document_overflow_broker

    def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 500

    def wait_for_function(
        self, expression: str, *, arg: object, timeout: int,
    ) -> None:
        assert '[data-account-view="real"]' in expression
        assert timeout == 10_000
        broker = str(arg)
        assert self.account_views[broker] == "real"


def tabbed_account_page(payload: dict[str, object]) -> TabbedAccountPage:
    return TabbedAccountPage(payload)


def tabbed_cn_page() -> TabbedAccountPage:
    return TabbedAccountPage(cn_rows={
        "futu": 1, "tiger": 0, "phillips": 1, "eastmoney": 0,
    })


def test_acceptance_rejects_missing_trend_controller_card() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"
    page.missing_controller_broker = "tiger"

    with pytest.raises(AssertionError, match="控制器"):
        dashboard_acceptance._check_trend_controller_status(
            page,
            page.locator("#trend-report-workspace:visible"),
            "tiger",
            payload["trend_controllers"]["tiger"],  # type: ignore[index]
        )


def test_acceptance_projects_unavailable_executor_controller() -> None:
    payload = valid_payload()
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    controller.update({  # type: ignore[union-attr]
        "health": "unavailable",
        "blocking": True,
        "phase": "unavailable",
        "blocker": "controller heartbeat is stale",
        "reason": "controller heartbeat is stale",
    })

    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"
    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


def test_acceptance_allows_readonly_controller_without_heartbeat() -> None:
    payload = valid_payload()
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    controller.update({  # type: ignore[union-attr]
        "effective_mode": "readonly",
        "health": "readonly",
        "blocking": False,
        "pid": None,
        "phase": "readonly",
        "heartbeat_at": "",
        "last_success": None,
        "blocker": "local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST",
        "reason": "local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST",
        "next_check_at": "",
    })

    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"
    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


def test_acceptance_checks_readable_mapping_last_success_fields() -> None:
    payload = valid_payload()
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"

    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


@pytest.mark.parametrize("phase", ["reconciling", "recovering_report"])
def test_acceptance_browser_allows_progress_before_first_success(
    phase: str,
) -> None:
    payload = valid_payload()
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    controller.update({"phase": phase, "last_success": None})  # type: ignore[union-attr]
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"

    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


@pytest.mark.parametrize("phase", ["before", "monitoring", "closed"])
def test_acceptance_browser_projects_stable_phase_without_first_success(
    phase: str,
) -> None:
    payload = valid_payload()
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    controller.update({"phase": phase, "last_success": None})  # type: ignore[union-attr]
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"

    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


def test_acceptance_allows_controller_heartbeat_to_advance_during_browser_check(
) -> None:
    payload = valid_payload()
    controller = copy.deepcopy(payload["trend_controllers"]["tiger"])  # type: ignore[index]
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"
    page.controllers["tiger"]["heartbeat_at"] = "2026-07-21T09:31:05+08:00"
    page.controllers["tiger"]["next_check_at"] = "2026-07-21T09:31:10+08:00"

    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


def test_acceptance_refreshes_simulated_positions_before_each_browser_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fetch(_url: str, path: str) -> object:
        calls.append(path)
        return {"available": True, "positions": [{"symbol": path.rsplit("/", 1)[-1]}]}

    monkeypatch.setattr(dashboard_acceptance, "_fetch_json_path", fetch)

    refreshed = dashboard_acceptance._refresh_simulate_payloads(
        "http://dashboard", {"tiger": {}, "phillips": {}}
    )

    assert calls == [
        "/api/trend-simulate-positions/tiger",
        "/api/trend-simulate-positions/phillips",
    ]
    assert [refreshed[broker]["positions"][0]["symbol"] for broker in refreshed] == [
        "tiger", "phillips",
    ]


def test_acceptance_uses_browser_snapshot_when_controller_phase_advances() -> None:
    payload = valid_payload()
    controller = copy.deepcopy(payload["trend_controllers"]["tiger"])  # type: ignore[index]
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"
    page.controllers["tiger"]["phase"] = "reconciling"

    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


@pytest.mark.parametrize(
    "rendered_heartbeat",
    ["not-a-time", "2026-07-21T09:30:59+08:00"],
)
def test_acceptance_rejects_invalid_or_regressed_rendered_controller_heartbeat(
    rendered_heartbeat: str,
) -> None:
    payload = valid_payload()
    controller = copy.deepcopy(payload["trend_controllers"]["tiger"])  # type: ignore[index]
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"
    page.controllers["tiger"]["heartbeat_at"] = rendered_heartbeat

    with pytest.raises(AssertionError, match="心跳"):
        dashboard_acceptance._check_trend_controller_status(
            page,
            page.locator("#trend-report-workspace:visible"),
            "tiger",
            controller,
        )


def test_acceptance_allows_controller_time_to_advance_beyond_one_poll_window() -> None:
    payload = valid_payload()
    controller = copy.deepcopy(payload["trend_controllers"]["tiger"])  # type: ignore[index]
    page = tabbed_account_page(payload)
    page.trend_broker = "tiger"
    page.controllers["tiger"]["heartbeat_at"] = "2026-07-21T09:37:00+08:00"
    page.controllers["tiger"]["next_check_at"] = "2026-07-21T09:37:10+08:00"

    dashboard_acceptance._check_trend_controller_status(
        page,
        page.locator("#trend-report-workspace:visible"),
        "tiger",
        controller,
    )


def test_acceptance_rejects_blocking_batch_with_healthy_controller() -> None:
    payload = valid_payload()
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    report = payload["trend_reports"]["tiger"]  # type: ignore[index]
    assert controller["health"] == "healthy"  # type: ignore[index]
    report.update({  # type: ignore[union-attr]
        "available": False,
        "data_status": "unavailable",
        "execution_batch": None,
        "execution_batch_blocking": True,
        "execution_batch_error": "执行批次无效，已阻止操作投影",
        "status_text": "执行批次无效，已阻止操作投影",
        "artifact": "",
        "report_sha256": "",
        "sell_actions": [],
        "buy_actions": [],
        "hold_actions": [],
        "review_actions": [],
        "risk_skips": [],
        "risk_summary": {},
        "audit": {},
        "counts": {"sell": 0, "buy": 0, "hold": 0, "review": 0},
    })

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert errors == [
        "tiger 当前趋势报告执行批次阻断：执行批次无效，已阻止操作投影"
    ]
    with pytest.raises(AssertionError, match="tiger.*执行批次无效"):
        dashboard_acceptance._check_trend_account_views(
            object(), payload, {}, {}
        )


def test_trend_context_acceptance_uses_visible_number_format() -> None:
    assert dashboard_acceptance._trend_context_display_value(
        "strength", "100.0"
    ) == "100"
    assert dashboard_acceptance._trend_context_display_value(
        "warm_to_hot_count", 3
    ) == "3"
    assert dashboard_acceptance._trend_context_display_value(
        "temperature", "热"
    ) == "热"


def test_check_trend_audit_uses_unknown_when_both_api_costs_are_null() -> None:
    class Locator:
        def __init__(
            self, selector: str = "audit", has_text: str | None = None,
        ) -> None:
            self.selector = selector
            self.has_text = has_text

        def count(self) -> int:
            return {
                "audit": 1,
                "table": 1,
                "header": 5,
                "rows": 1,
                "identity": 1,
                "status": 1,
                "reasons": 1,
                "details": 1,
                "detail-summary": 1,
                "industry-section": 1,
                "excluded-heading": 0,
            }.get(self.selector, 1)

        def get_attribute(self, _name: str) -> None:
            return None

        def locator(
            self, selector: str, **kwargs: object,
        ) -> "Locator":
            if self.selector == "audit" and selector == ".trend-audit-table":
                return Locator("table")
            if self.selector == "audit" and selector == "section h3":
                return Locator(
                    "excluded-heading"
                    if kwargs.get("has_text") == "排除项"
                    else "section-heading",
                    kwargs.get("has_text") if isinstance(
                        kwargs.get("has_text"), str
                    ) else None,
                )
            if self.selector == "audit" and selector == "section":
                return Locator("industry-section", kwargs.get("has_text"))
            if self.selector == "table" and selector == "thead th":
                return Locator("header")
            if self.selector == "table" and selector == ".trend-audit-row":
                return Locator("rows")
            if self.selector == "rows" and selector == 'td[data-label="标的"]':
                return Locator("identity")
            if self.selector == "rows" and selector == (
                'td[data-label="结论"] .trend-audit-status'
            ):
                return Locator("status")
            if self.selector == "rows" and selector == ".trend-audit-reason":
                return Locator("reasons")
            if self.selector == "rows" and selector == ".trend-audit-more":
                return Locator("details")
            if self.selector == "details" and selector == "summary":
                return Locator("detail-summary")
            return Locator(selector, kwargs.get("has_text"))

        def nth(self, index: int) -> "Locator":
            assert self.selector == "rows" and index == 0
            return self

        def click(self) -> None:
            return None

        def all_inner_texts(self) -> list[str]:
            if self.selector == "header":
                return ["标的", "结论", "未通过项目", "已通过的关键事实", "审计"]
            if self.selector == "reasons":
                return ["趋势强度\n94 → 要求：不低于 95"]
            raise AssertionError(f"unexpected all_inner_texts: {self.selector}")

        def inner_text(self) -> str:
            return {
                "audit": "审计详情 为什么没有进入买入名单 候选 1 通过 0 排除 1 "
                "趋势强度 1 行业集中度 无 API 成本：未知",
                "identity": "600000 浦发银行",
                "status": "已排除 · 1 项未通过",
                "industry-section": "行业集中度 无",
                "detail-summary": "查看全部字段",
            }.get(self.selector, "")

        def evaluate(self, expression: str) -> bool:
            assert expression == "node => node.scrollWidth <= node.clientWidth"
            return True

        def evaluate_all(self, expression: str) -> list[dict[str, object]]:
            assert self.selector == "detail-summary"
            assert "getBoundingClientRect" in expression
            return [{"height": 44, "label": "查看全部字段"}]

    class MobilePage:
        viewport_size = {"width": 375, "height": 844}

        def locator(self, selector: str) -> Locator:
            assert selector == ".trend-audit-more summary"
            return Locator("detail-summary")

    report = {
        "audit": {
            "strategy_parameters": {"min_strength": "95"},
            "candidates": [{
                "symbol": "600000",
                "name": "浦发银行",
                "eligible": False,
                "strength": "94",
                "excluded_reasons": ["strength_below_95"],
            }],
            "excluded": {"600000": ["strength_below_95"]},
            "industry_concentration": [],
            "data_sources": [],
            "actual_api_cost": None,
            "estimated_api_cost": None,
        },
    }

    dashboard_acceptance._check_trend_audit(Locator(), report, "eastmoney")
    dashboard_acceptance._check_trend_audit(
        Locator(), report, "eastmoney", page=MobilePage()
    )

    class MissingReasonCountLocator(Locator):
        def inner_text(self) -> str:
            return super().inner_text().replace("趋势强度 1 ", "")

    with pytest.raises(AssertionError, match="原因统计"):
        dashboard_acceptance._check_trend_audit(
            MissingReasonCountLocator(), report, "eastmoney"
        )


def test_first_in_scope_holding_returns_exact_market_and_symbol() -> None:
    assert dashboard_acceptance._first_in_scope_holding(valid_payload()) == ("US", "MSFT", "tiger")
    assert dashboard_acceptance._dashboard_holding_key(
        valid_payload(), "US", "MSFT"
    ) == "US:MSFT::5"


def test_first_in_scope_holding_ignores_current_advice_availability() -> None:
    payload = valid_payload()
    payload["holdings"][-1]["agent_report"]["available"] = False  # type: ignore[index]

    assert dashboard_acceptance._first_in_scope_holding(payload) == (
        "US", "MSFT", "tiger",
    )


def test_acceptance_opens_real_tool_workspaces_and_checks_mobile_targets() -> None:
    class Locator:
        def __init__(self, page: "Page", selector: str) -> None:
            self.page = page
            self.selector = selector

        @property
        def first(self) -> "Locator":
            self.page.first_uses.append(self.selector)
            return self

        def count(self) -> int:
            if self.selector == "#refresh-quotes":
                return 0
            if self.selector == "text=刷新账户与行情":
                return 0
            if self.selector in {"#quote-status", "#account-sync-status", "#last-refresh"}:
                return 0
            if self.selector == "#source-status-list":
                return 1
            target_selectors = {
                '#account-tabs [role="tab"]:visible, #header-market-filters button:visible, '
                ".strategy-tools button:visible, "
                ".broker-summary-card:visible, .account-holding-actions button:visible",
                ".symbol-detail-panel.inline-symbol-detail:visible button:visible, "
                ".symbol-detail-panel.inline-symbol-detail:visible input:visible, "
                ".symbol-detail-panel.inline-symbol-detail:visible select:visible",
                "#return-to-portfolio:visible, .kelly-lab-panel button:visible",
                "#standard-backtest-workspace button:visible, "
                "#standard-backtest-workspace input:visible, "
                "#standard-backtest-workspace select:visible",
                ".research-chat-modal button:visible, .research-chat-modal input:visible",
            }
            if self.selector in target_selectors:
                return 1
            counts = {
                '.account-holding-actions button[data-detail-mode="t_signal"]:visible': self.page.t_signal_count,
                ".account-review-action:visible": int(self.page.t_signal_count == 0),
                "[data-back-to-holdings]:visible": int(self.page.view == "detail"),
                '#main-navigation [data-workspace="kelly_lab"]': 1,
                ".kelly-lab-panel:visible": int(self.page.view == "kelly"),
                "#return-to-portfolio:visible": int(self.page.view != "portfolio"),
                '#main-navigation [data-workspace="standard_backtest"]': 1,
                "#standard-backtest-workspace:visible": int(self.page.view == "backtest"),
                "[data-research-chat]:visible": 0,
                ".research-chat-modal:visible": int(self.page.research_open),
                "#research-chat-close:visible": int(self.page.research_open),
                ".holdings-panel:visible": int(self.page.view == "portfolio"),
            }
            if self.selector not in counts:
                raise AssertionError(f"unknown count selector: {self.selector}")
            return counts[self.selector]

        def click(self) -> None:
            self.page.clicks.append(self.selector)
            if self.selector == '#main-navigation [data-workspace="kelly_lab"]':
                self.page.view = "kelly"
            elif self.selector == '.account-holding-actions button[data-detail-mode="t_signal"]:visible':
                self.page.view = "detail"
            elif self.selector == "[data-back-to-holdings]:visible":
                self.page.view = "portfolio"
            elif self.selector == '#main-navigation [data-workspace="standard_backtest"]':
                self.page.view = "backtest"
            elif self.selector == "#return-to-portfolio:visible":
                self.page.view = "portfolio"
            elif self.selector == "#research-chat-close:visible":
                self.page.research_open = False
            else:
                raise AssertionError(f"unknown click selector: {self.selector}")

        def evaluate_all(self, expression: str) -> list[dict[str, object]]:
            assert "getBoundingClientRect" in expression
            self.page.target_checks.append(self.selector)
            return [{"height": 44, "label": self.selector}]

    class Page:
        viewport_size = {"width": 375, "height": 844}

        def __init__(self, *, t_signal_count: int = 1) -> None:
            self.view = "portfolio"
            self.research_open = False
            self.t_signal_count = t_signal_count
            self.clicks: list[str] = []
            self.evaluations: list[tuple[str, object | None]] = []
            self.target_checks: list[str] = []
            self.first_uses: list[str] = []

        def locator(self, selector: str) -> Locator:
            return Locator(self, selector)

        def evaluate(self, expression: str, argument: object | None = None) -> None:
            assert "openResearchChat" in expression
            assert argument == "US:MSFT:Microsoft:5"
            self.evaluations.append((expression, argument))
            self.research_open = True

    page = Page()

    dashboard_acceptance._check_tool_workspaces(
        page, "US:MSFT:Microsoft:5"
    )

    assert page.clicks == [
        '.account-holding-actions button[data-detail-mode="t_signal"]:visible',
        "[data-back-to-holdings]:visible",
        '#main-navigation [data-workspace="kelly_lab"]', "#return-to-portfolio:visible",
        '#main-navigation [data-workspace="standard_backtest"]', "#return-to-portfolio:visible",
        "#research-chat-close:visible",
    ]
    assert page.first_uses == [
        '.account-holding-actions button[data-detail-mode="t_signal"]:visible',
        "[data-back-to-holdings]:visible",
    ]
    assert len(page.evaluations) == 1
    assert page.target_checks == [
        "#account-tabs [role=\"tab\"]:visible, #header-market-filters button:visible, "
        ".strategy-tools button:visible, "
        ".broker-summary-card:visible, .account-holding-actions button:visible",
        ".symbol-detail-panel.inline-symbol-detail:visible button:visible, "
        ".symbol-detail-panel.inline-symbol-detail:visible input:visible, "
        ".symbol-detail-panel.inline-symbol-detail:visible select:visible",
        "#return-to-portfolio:visible, .kelly-lab-panel button:visible",
        "#standard-backtest-workspace button:visible, "
        "#standard-backtest-workspace input:visible, "
        "#standard-backtest-workspace select:visible",
        ".research-chat-modal button:visible, .research-chat-modal input:visible",
    ]

    degraded_page = Page(t_signal_count=0)
    dashboard_acceptance._check_tool_workspaces(
        degraded_page, "US:MSFT:Microsoft:5"
    )
    assert (
        '.account-holding-actions button[data-detail-mode="t_signal"]:visible'
        not in degraded_page.clicks
    )
    assert "[data-back-to-holdings]:visible" not in degraded_page.clicks


@pytest.mark.parametrize(
    "selector",
    (
        ".broker-summary-card:visible",
        ".symbol-detail-panel.inline-symbol-detail:visible .language-toggle button:visible",
        ".trend-option-button:visible",
    ),
)
def test_acceptance_rejects_undersized_mobile_target(selector: str) -> None:
    class Locator:
        def count(self) -> int:
            return 1

        def evaluate_all(self, expression: str) -> list[dict[str, object]]:
            assert "getBoundingClientRect" in expression
            return [{"height": 43.5, "label": "太小"}]

    page = SimpleNamespace(locator=lambda _selector: Locator())

    with pytest.raises(AssertionError, match="太小.*44px"):
        dashboard_acceptance._check_mobile_targets(page, selector)


def test_tool_workspaces_closes_research_modal_when_target_check_fails() -> None:
    class Locator(TabbedAccountLocator):
        def evaluate_all(self, expression: str) -> list[dict[str, float]]:
            if self.selector == (
                ".research-chat-modal button:visible, "
                ".research-chat-modal input:visible"
            ):
                return [{"height": 38, "label": "输入讨论消息"}]
            return super().evaluate_all(expression)

    class Page(TabbedAccountPage):
        viewport_size = {"width": 375, "height": 844}

        def locator(self, selector: str) -> Locator:
            return Locator(self, selector)

    page = Page(valid_payload())

    with pytest.raises(AssertionError, match="输入讨论消息.*44px"):
        dashboard_acceptance._check_tool_workspaces(page, "US:AAPL:Apple:0")

    assert page.research_open is False


def test_tabbed_acceptance_fake_rejects_unknown_selectors_and_expressions() -> None:
    page = tabbed_account_page(valid_payload())

    with pytest.raises(AssertionError, match="unknown count selector"):
        page.locator(".misspelled-control").count()
    with pytest.raises(AssertionError, match="unknown count selector"):
        page.locator(
            "#account-futu:visible .trend-report-entry .data-trend-reprot"
        ).count()
    with pytest.raises(AssertionError, match="unknown count selector"):
        page.locator(
            "#account-futu:visible .trend-report-entry .misspelled"
        ).count()
    with pytest.raises(AssertionError, match="unknown inner_text selector"):
        page.locator(".totally-wrong strong").inner_text()
    with pytest.raises(AssertionError, match="unknown all_inner_texts selector"):
        page.locator("#visible-count").all_inner_texts()

    page.trend_broker = "eastmoney"
    buy_stage = page.locator("#trend-report-workspace:visible .cn-trend-buy")
    with pytest.raises(AssertionError, match="unknown evaluate expression"):
        buy_stage.evaluate("element => element.clientHeight")
    with pytest.raises(AssertionError, match="unknown evaluate_all expression"):
        buy_stage.evaluate_all("nodes => nodes.length")


def test_tabbed_acceptance_fake_rejects_unknown_broker_everywhere() -> None:
    page = tabbed_account_page(valid_payload())
    original_broker = page.selected
    unknown_tab = page.locator('#account-tabs [data-broker="futtu"]')

    with pytest.raises(AssertionError, match="unknown broker"):
        unknown_tab.count()
    with pytest.raises(AssertionError, match="unknown broker"):
        unknown_tab.click()
    assert page.selected == original_broker
    with pytest.raises(AssertionError, match="unknown broker"):
        unknown_tab.get_attribute("aria-selected")
    with pytest.raises(AssertionError, match="unknown broker"):
        page.locator("#account-futtu:visible").count()
    with pytest.raises(AssertionError, match="unknown broker"):
        page.locator(
            "#account-futtu:visible .trend-report-entry [data-trend-report]"
        ).click()
    with pytest.raises(AssertionError, match="unknown broker"):
        page.locator(
            "#account-futtu:visible .trend-report-entry button"
        ).is_disabled()
    with pytest.raises(AssertionError, match="unknown broker"):
        dashboard_acceptance._select_account_tab(page, "futtu")
    assert page.selected == original_broker


def test_acceptance_formats_grouped_numeric_expectations_without_touching_text() -> None:
    assert dashboard_acceptance._display_number("5000") == "5,000"
    assert dashboard_acceptance._display_number("25142.16") == "25,142.16"
    assert dashboard_acceptance._display_number("+25142.16") == "+25,142.16"
    for value in ("02840", "2026-07-16", "21.13%", "等待确认"):
        assert dashboard_acceptance._plain(value) == value

    dashboard_acceptance._check_action_trend_stages(
            [
                "优先处理 · 卖出触发 无",
                "美股常规交易时段 · 正式买入计划 VIXY 波动率ETF "
                "正式买入 19 98 ETF 4% 25,142.16 5,000 股 1,234.50",
                "盘中持续 · 已有持仓 无",
        ],
        {
            "buy_window": "美股常规交易时段",
            "sell_actions": [], "review_actions": [], "hold_actions": [],
            "buy_actions": [{
                "symbol": "VIXY", "name": "波动率ETF", "close": "19",
                "strength": "98", "industry": "ETF", "target_weight": "0.04",
                "estimated_shares": "5000", "target_amount": "25142.16",
                "estimated_initial_line": "1234.50",
            }],
        },
        "futu",
    )


def test_acceptance_requires_cn_protection_prices_with_at_most_two_decimals() -> None:
    assert dashboard_acceptance._display_price(
        "5.457142857142857142857142857"
    ) == "5.46"
    dashboard_acceptance._check_displayed_protection_prices(["5.46", "24.55", "27.53"])
    with pytest.raises(AssertionError, match="超过两位小数"):
        dashboard_acceptance._check_displayed_protection_prices(
            ["5.457142857142857142857142857"]
        )


VISUAL_CONTRACT_STYLES = {
    "body": {
        "backgroundColor": "rgb(247, 245, 241)",
        "color": "rgb(32, 29, 24)",
    },
    ".current-view-card": {
        "backgroundColor": "rgb(36, 33, 29)",
        "borderTopColor": "rgb(36, 33, 29)",
    },
    ".research-chat-context .status-ok": {
        "backgroundColor": "rgb(231, 244, 236)",
        "color": "rgb(32, 29, 24)",
    },
    **{
        selector: {
            "backgroundColor": "rgb(255, 254, 250)",
            "borderTopColor": "rgb(216, 210, 200)",
        }
        for selector in (
            ".header-brand-panel",
            ".header-assets-panel",
            ".header-source-panel",
            ".holdings-panel",
            ".kelly-lab-panel",
            ".trend-report-workspace",
            ".backtest-workspace",
            ".symbol-detail-panel",
            ".research-chat-modal",
        )
    },
}


def visual_contract_page(*, accent: str = "#8B5E34") -> object:

    class Locator:
        def __init__(self, page: "Page", selector: str) -> None:
            self.page = page
            self.selector = selector

        def count(self) -> int:
            if self.selector == "text=刷新账户与行情":
                return 0
            if self.selector in {"#quote-status", "#account-sync-status", "#last-refresh"}:
                return 0
            if self.selector == "#source-status-list":
                return 1
            return int(self.selector in VISUAL_CONTRACT_STYLES)

        def inner_text(self) -> str:
            assert self.selector == "#source-status-list"
            return "实时账户 · 券商结单"

        def evaluate(self, expression: str) -> dict[str, str]:
            assert self.selector in VISUAL_CONTRACT_STYLES
            self.page.evaluated_selectors.append(self.selector)
            assert "backgroundColor" in expression
            return dict(VISUAL_CONTRACT_STYLES[self.selector])

    class Page:
        def __init__(self) -> None:
            self.expected = dict(dashboard_acceptance.WARM_LEDGER_TOKENS)
            self.expected["--accent"] = accent
            self.token_evaluations: list[list[str]] = []
            self.evaluated_selectors: list[str] = []

        def evaluate(
            self, expression: str, names: list[str] | None = None
        ) -> dict[str, str]:
            assert names == list(dashboard_acceptance.WARM_LEDGER_TOKENS)
            assert "getPropertyValue" in expression
            self.token_evaluations.append(names)
            return self.expected

        def locator(self, selector: str) -> Locator:
            return Locator(self, selector)

    return Page()


def test_acceptance_visual_contract_accepts_exact_warm_ledger() -> None:
    page = visual_contract_page()

    dashboard_acceptance._check_visual_contract(page)

    assert page.token_evaluations == [  # type: ignore[attr-defined]
        list(dashboard_acceptance.WARM_LEDGER_TOKENS)
    ]
    assert page.evaluated_selectors == [  # type: ignore[attr-defined]
        *VISUAL_CONTRACT_STYLES,
    ]


def test_acceptance_visual_contract_rejects_palette_drift() -> None:
    with pytest.raises(AssertionError, match="--accent"):
        dashboard_acceptance._check_visual_contract(
            visual_contract_page(accent="#A16207")
        )


def test_visual_contract_fake_rejects_unknown_selector() -> None:
    page = visual_contract_page()
    locator = page.locator(".misspelled-surface")  # type: ignore[attr-defined]

    assert locator.count() == 0
    with pytest.raises(AssertionError):
        locator.evaluate("getComputedStyle(element).backgroundColor")


def open_report_layout_page(
    *,
    shell_width: float = 1600,
    header_left: float = 176,
    header_right: float = 1744,
    report_left: float = 176,
    report_right: float = 1744,
    holdings_left: float = 176,
    holdings_right: float = 1744,
    client_width: int = 1500,
    scroll_width: int = 1600,
    overflow_x: str = "auto",
) -> tuple[object, object]:
    class Cards:
        def count(self) -> int:
            return 1

    class Stage:
        def evaluate(self, expression: str) -> dict[str, object]:
            if "document.activeElement" in expression:
                return True  # type: ignore[return-value]
            if "outlineColor" in expression:
                return {
                    "outlineColor": "rgb(139, 94, 52)",
                    "outlineStyle": "solid",
                    "outlineWidth": "3px",
                }
            assert "clientWidth" in expression
            assert "scrollWidth" in expression
            assert "overflowX" in expression
            page.overflow_evaluations.append(expression)
            return {
                "clientWidth": client_width,
                "scrollWidth": scroll_width,
                "overflowX": overflow_x,
            }

        def count(self) -> int:
            return 1

        def locator(self, selector: str) -> Cards:
            assert selector == ".cn-trend-card:visible"
            return Cards()

        def get_attribute(self, name: str) -> str:
            return {
                "tabindex": "0",
                "aria-label": "正式买入计划，可横向滚动",
            }[name]

        def focus(self) -> None:
            return None

    class Workspace:
        def locator(self, selector: str) -> Stage:
            assert selector == ".cn-trend-buy"
            return Stage()

    class Page:
        viewport_size = {"width": 1920, "height": 1080}

        def __init__(self) -> None:
            self.geometry_evaluations: list[str] = []
            self.overflow_evaluations: list[str] = []

        def evaluate(self, expression: str) -> dict[str, float]:
            for required in (
                ".dashboard-shell",
                ".dashboard-header",
                ".holdings-panel",
                "#trend-report-workspace",
                "getBoundingClientRect",
            ):
                assert required in expression
            self.geometry_evaluations.append(expression)
            return {
                "shellWidth": shell_width,
                "headerLeft": header_left,
                "headerRight": header_right,
                "reportLeft": report_left,
                "reportRight": report_right,
                "holdingsLeft": holdings_left,
                "holdingsRight": holdings_right,
            }

    page = Page()
    return page, Workspace()


def test_acceptance_open_report_layout_requires_aligned_wide_shell_and_table_scroll() -> None:
    page, workspace = open_report_layout_page()

    dashboard_acceptance._check_open_report_layout(page, workspace, "eastmoney")

    assert len(page.geometry_evaluations) == 1  # type: ignore[attr-defined]
    assert len(page.overflow_evaluations) == 1  # type: ignore[attr-defined]


def test_acceptance_zero_buy_mobile_report_requires_empty_state_without_cards() -> None:
    class Cards:
        def count(self) -> int:
            return 0

    class Stage:
        def count(self) -> int:
            return 1

        def locator(self, selector: str) -> Cards:
            assert selector == ".cn-trend-card:visible"
            return Cards()

        def inner_text(self) -> str:
            return "09:30–10:00 · 正式买入计划\n无"

        def get_attribute(self, name: str) -> str:
            return {"tabindex": "-1", "aria-label": "正式买入计划"}[name]

    class Workspace:
        def locator(self, selector: str) -> Stage:
            assert selector == ".cn-trend-buy"
            return Stage()

    page = SimpleNamespace(viewport_size={"width": 375, "height": 844})

    dashboard_acceptance._check_open_report_layout(
        page, Workspace(), "eastmoney", expected_buy_count=0
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"shell_width": 1598}, "shell"),
        ({"report_left": 178}, "左边线"),
        ({"report_right": 1742}, "右边线"),
        ({"holdings_left": 178}, "持仓.*左边线"),
        ({"holdings_right": 1742}, "持仓.*右边线"),
        ({"overflow_x": "hidden"}, "内部横向滚动"),
        ({"scroll_width": 1500}, "可滚动内容"),
    ],
)
def test_acceptance_open_report_layout_rejects_contract_drift(
    overrides: dict[str, object], message: str,
) -> None:
    page, workspace = open_report_layout_page(**overrides)  # type: ignore[arg-type]

    with pytest.raises(AssertionError, match=message):
        dashboard_acceptance._check_open_report_layout(
            page, workspace, "eastmoney"
        )


def test_browser_check_treats_page_error_as_desktop_failure_and_runs_mobile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        dashboard_acceptance, "ACCEPTANCE_SCREENSHOT_DIR", tmp_path / "screenshots"
    )
    payload = valid_payload()
    reports = payload["trend_reports"]
    visited: list[str] = []
    selectors: list[tuple[str, str]] = []
    clicks: list[tuple[str, str]] = []
    evaluated: list[str] = []
    viewport_widths: list[int] = []
    screenshots: list[tuple[str, str]] = []
    visual_token_evaluations: list[str] = []
    visual_surface_evaluations: list[tuple[str, str]] = []
    geometry_evaluations: list[str] = []
    buy_overflow_evaluations: list[str] = []
    polling_freezes: list[str] = []
    state = {
        "fail_wide_desktop_navigation": True,
        "fail_trend_account_views": False,
    }

    class Locator(TabbedAccountLocator):
        def click(self) -> None:
            clicks.append((self.page.name, self.selector))  # type: ignore[attr-defined]
            super().click()

        def evaluate(self, expression: str) -> object:
            if "getComputedStyle" in expression:
                if self.selector.endswith(".cn-trend-buy"):
                    if "outlineColor" in expression:
                        return {
                            "outlineColor": "rgb(139, 94, 52)",
                            "outlineStyle": "solid",
                            "outlineWidth": "3px",
                        }
                    assert self.selector == (
                        "#trend-report-workspace:visible .cn-trend-buy"
                    )
                    buy_overflow_evaluations.append(self.page.name)  # type: ignore[attr-defined]
                    return {
                        "clientWidth": 1500,
                        "scrollWidth": 1600,
                        "overflowX": "auto",
                    }
                assert self.selector in VISUAL_CONTRACT_STYLES, self.selector
                visual_surface_evaluations.append(
                    (self.page.name, self.selector)  # type: ignore[attr-defined]
                )
                return dict(VISUAL_CONTRACT_STYLES[self.selector])
            return super().evaluate(expression)

    class Page(TabbedAccountPage):
        def __init__(self, name: str, viewport: dict[str, int]) -> None:
            super().__init__(payload)
            self.name = name
            self.viewport_size = viewport

        def on(self, *_args: object) -> None:
            pass

        def goto(self, *_args: object, **_kwargs: object) -> None:
            visited.append(self.name)
            if (
                self.name == "wide_desktop"
                and state["fail_wide_desktop_navigation"]
            ):
                raise RuntimeError("navigation failed")

        def locator(self, selector: str) -> Locator:
            selectors.append((self.name, selector))
            return Locator(self, selector)

        def evaluate(
            self, expression: str, argument: object | None = None
        ) -> object:
            if expression == "() => state.dashboard?.broker_positions ?? []":
                return super().evaluate(expression, argument)
            if expression == "() => state.dashboard":
                return self.payload
            if "clearInterval(state.quoteIntervalId)" in expression:
                polling_freezes.append(self.name)
                return True
            if (
                "trend-review-style-contract" in expression
                or "trend-review-geometry-contract" in expression
            ):
                return super().evaluate(expression, argument)
            if "openResearchChat" in expression:
                return super().evaluate(expression, argument)
            if "gridTemplateColumns" in expression:
                return super().evaluate(expression, argument)
            if "getPropertyValue" in expression:
                assert argument == list(dashboard_acceptance.WARM_LEDGER_TOKENS)
                visual_token_evaluations.append(self.name)
                return dict(dashboard_acceptance.WARM_LEDGER_TOKENS)
            if "const shell" in expression:
                for required in (
                    ".dashboard-shell",
                    ".dashboard-header",
                    ".holdings-panel",
                    "#trend-report-workspace",
                    "getBoundingClientRect",
                ):
                    assert required in expression
                geometry_evaluations.append(self.name)
                return {
                    "shellWidth": 1600,
                    "headerLeft": 176,
                    "headerRight": 1744,
                    "reportLeft": 176,
                    "reportRight": 1744,
                    "holdingsLeft": 176,
                    "holdingsRight": 1744,
                }
            assert expression == "document.documentElement.scrollWidth <= window.innerWidth"
            evaluated.append(self.name)
            return True

        def screenshot(self, *, path: str, full_page: bool) -> None:
            assert full_page is True
            screenshots.append((self.name, path))
            Path(path).write_bytes(b"screenshot")

        def close(self) -> None:
            pass

    class Browser:
        pages = 0

        def new_page(self, **kwargs: object) -> Page:
            names = ("wide_desktop", "desktop", "tablet", "mobile")
            name = names[self.pages]
            self.pages += 1
            viewport = kwargs["viewport"]
            viewport_widths.append(viewport["width"])  # type: ignore[index]
            return Page(name, viewport)  # type: ignore[arg-type]

        def close(self) -> None:
            pass

    class Playwright:
        chromium = type("Chromium", (), {"launch": lambda *_args, **_kwargs: Browser()})()

    class Context:
        def __enter__(self) -> Playwright:
            return Playwright()

        def __exit__(self, *_args: object) -> None:
            pass

    module = ModuleType("playwright.sync_api")
    module.sync_playwright = Context  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    def check_trend_views(
        page: Page,
        _payload: object,
        _simulate_payloads: object,
        _history_expectations: object,
        *,
        screenshot_dir: Path,
    ) -> None:
        if state["fail_trend_account_views"]:
            raise AssertionError("controller unavailable")
        width = page.viewport_size["width"]
        if width in {1440, 375}:
            page.screenshot(
                path=str(screenshot_dir / f"{width}-trend-review.png"),
                full_page=True,
            )

    def check_separated_report_views(
        page: Page,
        _payload: object,
        *,
        screenshot_dir: Path,
    ) -> None:
        width = page.viewport_size["width"]
        page.screenshot(
            path=str(screenshot_dir / f"{width}-trend-report.png"),
            full_page=True,
        )

    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_separated_trend_report_views",
        check_separated_report_views,
        raising=False,
    )
    monkeypatch.setattr(
        dashboard_acceptance, "_check_trend_account_views", check_trend_views
    )
    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, payload, simulate_payloads={}, history_expectations={}
    )

    assert errors == ["wide_desktop：RuntimeError: navigation failed"]
    assert blocker is None
    assert visited == ["wide_desktop", "desktop", "tablet", "mobile"]
    assert viewport_widths == [1920, 1440, 760, 375]

    state["fail_wide_desktop_navigation"] = False
    visited.clear()
    selectors.clear()
    clicks.clear()
    evaluated.clear()
    viewport_widths.clear()
    screenshots.clear()
    visual_token_evaluations.clear()
    visual_surface_evaluations.clear()
    geometry_evaluations.clear()
    buy_overflow_evaluations.clear()
    polling_freezes.clear()
    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, payload, simulate_payloads={}, history_expectations={}
    )

    assert errors == []
    assert blocker is None
    for viewport in ("wide_desktop", "desktop", "tablet", "mobile"):
        assert (viewport, '#broker-summary-cards [data-broker="phillips"]') in selectors
        assert (viewport, '[data-market="CN"]') in selectors
        assert (viewport, '[data-market="CN"]') in clicks
        assert (viewport, 'button[data-broker="eastmoney"]') not in selectors
        assert (viewport, '#visible-count') in selectors
        assert (viewport, '#source-status-list') in selectors
        assert (
            viewport,
            '.account-holding-row:visible:has('
            '.account-holding-market:has-text("US")) .account-holding-price',
        ) in selectors
        assert (viewport, '#account-tabs [data-broker]') in selectors
        assert (viewport, '[data-market="CASH"]') in selectors
        assert (viewport, '#cash-detail-panel') in selectors
        for broker in ("futu", "tiger", "phillips", "eastmoney"):
            tab = f'#account-tabs [data-broker="{broker}"]'
            assert (viewport, tab) in selectors
            assert (viewport, tab) in clicks
            assert (viewport, f"#account-{broker}:visible") in selectors
        assert (
            viewport,
            '#account-futu:visible [data-account-view="report"]',
        ) not in clicks
        for broker in ("tiger", "phillips"):
            assert (
                viewport,
                f'#account-{broker}:visible [data-account-view="report"]',
            ) in clicks
        assert (viewport, '#return-to-portfolio:visible') in clicks
        for broker in ("tiger", "phillips"):
            assert (viewport, f"#account-{broker}-view-panel:visible") in selectors
            assert (
                viewport,
                f"#account-{broker}-view-panel:visible .cn-trend-report:visible",
            ) in selectors
        assert (viewport, '.account-section:visible') in selectors
        assert (viewport, '#account-tiger:visible') in selectors
        assert (viewport, '#tiger-long-term-panel') in selectors
        assert (viewport, '#trade-actions') in selectors
        assert (viewport, 'body') in selectors
        assert (viewport, 'a:visible, button:visible') in selectors
        assert (viewport, 'a[href="#account-tiger"]') not in clicks
    for viewport in ("tablet", "mobile"):
        assert (
            viewport,
            "#trend-report-workspace:visible .trend-option-button",
        ) in selectors
    assert set(evaluated) == {"wide_desktop", "desktop", "tablet", "mobile"}
    assert visual_token_evaluations == [
        "wide_desktop", "desktop", "tablet", "mobile",
    ]
    assert polling_freezes == ["wide_desktop", "desktop", "tablet", "mobile"]
    for viewport in ("wide_desktop", "desktop", "tablet", "mobile"):
        assert [
            selector
            for name, selector in visual_surface_evaluations
            if name == viewport
        ] == list(VISUAL_CONTRACT_STYLES)
    assert geometry_evaluations == []
    assert buy_overflow_evaluations == []
    screenshot_dir = dashboard_acceptance.ACCEPTANCE_SCREENSHOT_DIR
    assert screenshots == [
        ("wide_desktop", str(screenshot_dir / "wide_desktop-portfolio.png")),
        ("wide_desktop", str(screenshot_dir / "1920-trend-report.png")),
        ("desktop", str(screenshot_dir / "desktop-portfolio.png")),
        ("desktop", str(screenshot_dir / "1440-trend-report.png")),
        ("desktop", str(screenshot_dir / "1440-trend-review.png")),
        ("tablet", str(screenshot_dir / "tablet-portfolio.png")),
        ("tablet", str(screenshot_dir / "760-trend-report.png")),
        ("mobile", str(screenshot_dir / "mobile-portfolio.png")),
        ("mobile", str(screenshot_dir / "375-trend-report.png")),
        ("mobile", str(screenshot_dir / "375-trend-review.png")),
    ]

    state["fail_trend_account_views"] = True
    screenshots.clear()
    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, payload, simulate_payloads={}, history_expectations={}
    )

    assert blocker is None
    assert all(
        not any(
            error == f"验收截图缺失：{width}-trend-report.png"
            for error in errors
        )
        for width in (1920, 1440, 760, 375)
    )


def test_validate_dashboard_payload_accepts_real_contract() -> None:
    assert validate_dashboard_payload(valid_payload(), expected_cn=5) == []


def _controller_position(symbol: str = "QQQ") -> dict[str, str]:
    return {
        field: "0"
        for field in dashboard_acceptance.DASHBOARD_POSITION_FIELDS
    } | {
        "broker": "tiger",
        "account_alias": "tiger_main",
        "market": "US",
        "asset_class": "stock",
        "symbol": symbol,
        "name": "Test",
        "currency": "USD",
        "quantity": "2",
        "cost_price": "400",
        "cost_value": "800",
        "last_price": "500",
        "price_kind": "live",
        "price_as_of": "2026-07-31T19:52:00-04:00",
        "market_value": "1000",
        "market_value_usd": "1000",
        "market_value_hkd": "7800",
        "cost_value_hkd": "6240",
        "unrealized_pnl": "200",
        "unrealized_pnl_pct": "25.00%",
        "account_weight_hkd": "7.80%",
        "portfolio_weight_hkd": "1.25%",
    }


def test_validate_dashboard_payload_requires_controller_owned_position_fields() -> None:
    payload = valid_payload()
    payload["broker_positions"] = [_controller_position()]
    payload["broker_positions"][0].pop("portfolio_weight_hkd")  # type: ignore[index]

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert any("控制器持仓第 1 行缺少字段" in error for error in errors)


def test_check_controller_owned_rows_uses_current_page_projection() -> None:
    stale_position = _controller_position("DRAM")
    stale_position["last_price"] = "53.38"
    page_position = dict(stale_position)
    page_position["last_price"] = "53.40"
    dom_values = dict(page_position)

    class Page:
        def evaluate(self, expression: str) -> list[dict[str, str]]:
            assert expression == (
                "() => state.dashboard?.broker_positions ?? []"
            )
            return [page_position]

    class Row:
        def get_attribute(self, name: str) -> str:
            return {
                "data-broker": "tiger",
                "data-symbol": "DRAM",
                **{
                    attribute: dom_values[field]
                    for field, attribute in dashboard_acceptance.CONTROLLER_DOM_FIELDS.items()
                },
            }[name]

    class Rows:
        def count(self) -> int:
            return 1

        def nth(self, _index: int) -> Row:
            return Row()

    class Section:
        def locator(self, selector: str) -> Rows:
            assert selector == ".account-holding-row:visible"
            return Rows()

    dashboard_acceptance._check_controller_owned_rows(
        Page(), Section(), "tiger"
    )
    dom_values["last_price"] = stale_position["last_price"]
    with pytest.raises(AssertionError, match="last_price"):
        dashboard_acceptance._check_controller_owned_rows(
            Page(), Section(), "tiger"
        )


def test_validate_dashboard_payload_rejects_unsafe_account_sync_and_wrong_accepted_count() -> None:
    payload = valid_payload()
    payload["account_sync"] = {
        "status": "abnormal",
        "controller": {"status": "stale", "heartbeat_at": "2026-07-21T09:20:00+08:00"},
        "brokers": {
            "futu": {"status": "ok"},
            "tiger": {"status": "stale"},
            "phillips": {"status": "failed"},
            "eastmoney": {"status": "unknown"},
        },
    }
    payload["broker_summaries"] = [{"broker": "tiger", "holding_count": 14}]
    payload["broker_positions"] = [{"broker": "tiger", "market": "US", "symbol": "MSFT"}]

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert "账户同步状态异常" in errors
    assert "账户同步 Worker 不可用" in errors
    assert "tiger 账户同步状态不是正常" in errors
    assert "phillips 账户同步状态不是正常" in errors
    assert "eastmoney 账户同步状态不是正常" in errors
    assert any("tiger 已接受持仓数量不匹配" in error for error in errors)


def test_validate_dashboard_payload_counts_only_actual_accepted_holdings() -> None:
    payload = valid_payload()
    payload["broker_summaries"] = [{"broker": "tiger", "holding_count": 1}]
    payload["broker_positions"] = [
        {"broker": "tiger", "market": "US", "symbol": "MSFT", "quantity": "1"},
        {"broker": "tiger", "market": "CASH", "symbol": "USD", "asset_class": "cash", "quantity": "1"},
        {"broker": "tiger", "market": "US", "symbol": "MONEY", "asset_class": "money_market_fund", "quantity": "1"},
    ]

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert not any("tiger 已接受持仓数量不匹配" in error for error in errors)


def test_check_account_holdings_counts_only_actual_accepted_holdings() -> None:
    payload = valid_payload()
    payload["broker_positions"] = [
        {"broker": "futu", "market": "US", "symbol": "QQQ", "quantity": "1"},
        {"broker": "tiger", "market": "US", "symbol": "MSFT", "quantity": "1"},
        {"broker": "tiger", "market": "CASH", "symbol": "USD", "asset_class": "cash", "quantity": "1"},
        {"broker": "phillips", "market": "HK", "symbol": "0700", "quantity": "1"},
    ]
    page = tabbed_account_page(payload)

    dashboard_acceptance._check_account_holdings(page, payload)


def test_acceptance_rejects_missing_or_unhealthy_account_sync_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        dashboard_acceptance, "_project_data_dir", lambda _root: data_dir
    )
    now = datetime.fromisoformat("2026-07-30T12:10:00+08:00")
    assert dashboard_acceptance._account_sync_worker_errors(
        tmp_path, expected_root=tmp_path, expected_sha="accepted", now=now,
    ) == ["账户同步 Worker 状态缺失"]

    status_path = data_dir / "account_sync/controller_status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "pid": 9999999,
        "working_directory": "/wrong",
        "git_sha": "old",
        "heartbeat_at": "2026-07-30T12:00:00+08:00",
    }), encoding="utf-8")

    errors = dashboard_acceptance._account_sync_worker_errors(
        tmp_path, expected_root=tmp_path, expected_sha="accepted", now=now,
    )

    for required in ("PID 不存活", "工作目录不匹配", "Git SHA 不匹配", "心跳不新鲜"):
        assert any(required in error for error in errors)


def test_acceptance_reads_account_sync_worker_from_shared_project_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    shared_data = tmp_path / "shared-data"
    worktree.mkdir()
    now = datetime(2026, 7, 30, 12, 10, tzinfo=dashboard_acceptance.SHANGHAI)
    status_path = shared_data / "account_sync/controller_status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "pid": os.getpid(),
        "working_directory": str(worktree),
        "git_sha": "accepted",
        "heartbeat_at": now.isoformat(),
    }), encoding="utf-8")
    monkeypatch.setattr(
        dashboard_acceptance, "_project_data_dir", lambda _root: shared_data
    )

    assert dashboard_acceptance._account_sync_worker_errors(
        worktree, expected_root=worktree, expected_sha="accepted", now=now,
    ) == []


def test_acceptance_allows_recent_frozen_report_after_friday_close() -> None:
    report = {
        "data_status": "stale",
        "generated_at": "2026-07-24T18:00:00+08:00",
    }

    assert dashboard_acceptance._trend_report_is_current_or_recent_weekend_snapshot(
        report,
        now=datetime(2026, 7, 25, 9, 0, tzinfo=dashboard_acceptance.SHANGHAI),
    )
    assert not dashboard_acceptance._trend_report_is_current_or_recent_weekend_snapshot(
        report,
        now=datetime(2026, 7, 24, 9, 0, tzinfo=dashboard_acceptance.SHANGHAI),
    )


def test_acceptance_allows_stale_us_report_for_current_shanghai_execution_date() -> None:
    report = {
        "data_status": "stale",
        "report_date": "2026-07-27",
        "generated_at": "2026-07-25T12:42:27+08:00",
    }
    monday_morning = datetime(
        2026, 7, 27, 10, 0, tzinfo=dashboard_acceptance.SHANGHAI
    )

    assert dashboard_acceptance._trend_report_is_current_or_recent_weekend_snapshot(
        report,
        now=monday_morning,
    )


def test_validate_dashboard_payload_rejects_retired_tiger_strategy_payload() -> None:
    payload = valid_payload()
    payload["tiger_" + "long_term_strategy"] = {"status": "shadow"}

    assert any(
        "已退役策略" in error
        for error in validate_dashboard_payload(payload, expected_cn=5)
    )


def test_check_account_holdings_visits_every_broker_tab(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    projections: list[str] = []
    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_trend_artifact_projection",
        lambda _reports_dir, broker, _report: projections.append(broker),
    )

    dashboard_acceptance._check_account_holdings(
        page, payload, reports_dir=tmp_path
    )

    assert page.selected_brokers == ["futu", "tiger", "phillips", "eastmoney"]
    assert page.max_visible_account_sections == 1
    assert page.opened_reports == ["tiger", "phillips"]
    assert page.opened_reviews == ["tiger", "phillips"]
    assert page.disabled_reports == set()
    assert projections == ["tiger", "phillips", "eastmoney"]
    assert page.account_views["tiger"] == page.account_views["phillips"] == "real"


@pytest.mark.parametrize(
    ("broker", "width", "count"),
    [
        ("futu", 1440, 0),
        ("tiger", 1440, 0),
        ("phillips", 1440, 1),
        ("eastmoney", 1440, 1),
        ("phillips", 375, 0),
        ("eastmoney", 375, 0),
    ],
)
def test_check_statement_upload_enforces_desktop_only_controls(
    broker: str,
    width: int,
    count: int,
) -> None:
    checked: list[str] = []

    class Locator:
        def count(self) -> int:
            return count

    class Section:
        def locator(self, selector: str) -> Locator:
            checked.append(selector)
            return Locator()

    dashboard_acceptance._check_statement_upload(  # type: ignore[attr-defined]
        Section(), broker, width
    )

    assert checked == [f'[data-statement-upload="{broker}"]:visible']


def test_option_anomaly_acceptance_checks_enabled_dialog_and_disabled_rows() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)

    dashboard_acceptance._check_account_holdings(page, payload)

    assert page.option_dialog_index == 0
    assert page.option_dialog_open is False
    assert page.opened_reports == ["tiger", "phillips"]


def test_option_anomaly_acceptance_rejects_missing_row_button() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.option_button_count_override = 0

    with pytest.raises(AssertionError, match="期权按钮数量"):
        dashboard_acceptance._check_account_holdings(page, payload)


def test_option_anomaly_acceptance_rejects_wrong_disabled_state() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.option_disabled_override = {0: True}

    with pytest.raises(AssertionError, match="错误置灰"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize("width", (760, 375))
def test_option_anomaly_acceptance_checks_mobile_controls(width: int) -> None:
    page = tabbed_account_page(valid_payload())
    page.viewport_size = {"width": width, "height": 844}

    dashboard_acceptance._check_account_holdings(page, valid_payload())

    assert all(
        broker in page.document_overflow_checks
        for broker in ("tiger", "phillips")
    )

def test_acceptance_rejects_unavailable_eastmoney_report_for_screenshot(
    tmp_path: Path,
) -> None:
    payload = valid_payload()
    report = payload["trend_reports"]["eastmoney"]  # type: ignore[index]
    report.update(available=False, status_text="今日报告不可用")
    page = tabbed_account_page(payload)

    with pytest.raises(AssertionError, match="eastmoney.*不可用"):
        dashboard_acceptance._check_account_holdings(
            page, payload, screenshot_dir=tmp_path
        )


@pytest.mark.parametrize("broker", ("tiger", "phillips"))
def test_acceptance_rejects_unavailable_review_when_daily_report_is_unavailable(
    broker: str,
) -> None:
    payload = valid_payload()
    payload["trend_reports"][broker].update(  # type: ignore[index]
        available=False, status_text="今日报告不可用"
    )
    payload["trend_reviews"][broker]["available"] = False  # type: ignore[index]
    page = tabbed_account_page(payload)

    with pytest.raises(AssertionError, match=f"{broker} 趋势复盘不可用"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize("broker", ("tiger", "phillips"))
def test_acceptance_validates_available_review_when_daily_report_is_unavailable(
    broker: str,
) -> None:
    payload = valid_payload()
    payload["trend_reports"][broker].update(  # type: ignore[index]
        available=False, status_text="今日报告不可用"
    )
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": 375, "height": 844}

    dashboard_acceptance._check_account_holdings(page, payload)

    assert broker in page.opened_reviews


def test_select_account_tab_rejects_multiple_visible_sections() -> None:
    page = tabbed_account_page(valid_payload())
    page.visible_account_sections = 2

    with pytest.raises(AssertionError, match="同时显示多个账户区块"):
        dashboard_acceptance._select_account_tab(page, "futu")

    assert page.max_visible_account_sections == 2


def test_check_account_holdings_rejects_reordered_broker_tabs() -> None:
    page = tabbed_account_page(valid_payload())
    page.tab_order = ["tiger", "futu", "phillips", "eastmoney"]

    with pytest.raises(AssertionError, match="Tab 顺序"):
        dashboard_acceptance._check_account_holdings(page, valid_payload())


@pytest.mark.parametrize(
    "legacy", ("数据日", "账户源", "最近保护提醒", "策略指标待接入"),
)
def test_check_account_holdings_rejects_legacy_trend_summary_copy(legacy: str) -> None:
    page = tabbed_account_page(valid_payload())
    page.section_texts["futu"] += f" {legacy}"

    with pytest.raises(AssertionError, match=f"旧趋势摘要.*{legacy}"):
        dashboard_acceptance._check_account_holdings(page, valid_payload())


def session_price_page(
    *,
    cells: tuple[tuple[str, ...], ...] = (("夜盘 61.50 · 03:03 ET",),),
    viewport_width: int = 1440,
    box: dict[str, float] | None = None,
) -> object:
    class Locator:
        def __init__(self, items: tuple[object, ...]) -> None:
            self.items = items

        def inner_text(self) -> str:
            return str(self.items[0])

        def count(self) -> int:
            return len(self.items)

        def nth(self, index: int) -> "Locator":
            return Locator((self.items[index],))

        def locator(self, selector: str) -> "Locator":
            assert selector == ".session-quote"
            return Locator(self.items[0])  # type: ignore[arg-type]

        def bounding_box(self) -> dict[str, float]:
            return box or {"x": 20, "width": 100}

    class Page:
        viewport_size = {"width": viewport_width, "height": 844}

        def locator(self, selector: str) -> Locator:
            if selector == (
                ".account-holding-row:visible "
                ".account-holding-price .session-quote"
            ):
                return Locator(tuple(price for cell in cells for price in cell))
            assert selector == (
                '.account-holding-row:visible:has('
                '.account-holding-market:has-text("US")) .account-holding-price'
            )
            return Locator(cells)

    return Page()


def test_check_session_prices_accepts_compact_session_price() -> None:
    dashboard_acceptance._check_session_prices(session_price_page())


@pytest.mark.parametrize(
    "quotes",
    [(), ("夜盘 61.50 · 03:03 ET", "盘前 62.00 · 04:03 ET")],
    ids=("missing", "duplicate"),
)
def test_check_session_prices_requires_exactly_one_quote_per_us_price_cell(
    quotes: tuple[str, ...],
) -> None:
    page = session_price_page(cells=(("夜盘 60.50 · 02:03 ET",), quotes))

    with pytest.raises(AssertionError, match="恰好一个分时段价格"):
        dashboard_acceptance._check_session_prices(page)


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (
            session_price_page(cells=(("夜盘 61.50 盘前 62.00 · 03:03 ET",),)),
            "多个时段",
        ),
        (session_price_page(cells=(("夜盘 61.50 · 03:03",),)), "时间或回退说明"),
        (session_price_page(cells=(("夜盘 61.50 · 15:03 CST",),)), "重复展示"),
        (
            session_price_page(
                viewport_width=390, box={"x": 350, "width": 50},
            ),
            "超出视口",
        ),
    ],
)
def test_check_session_prices_rejects_broken_contract(
    page: object, expected: str,
) -> None:
    with pytest.raises(AssertionError, match=expected):
        dashboard_acceptance._check_session_prices(page)


@pytest.mark.parametrize(
    "forbidden",
    (
        "TIGER · LONG TERM",
        "broad_us_growth",
        "semiconductor",
        "INELIGIBLE",
        "LONG",
        "CASH",
        "insufficient_sma200_history",
        "state_change",
        "provenance_incomplete",
        "calibration_required",
    ),
)
def test_check_page_safety_rejects_visible_internal_statuses(forbidden: str) -> None:
    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def count(self) -> int:
            return 0

        def inner_text(self) -> str:
            assert self.selector == "body"
            return f"持仓与策略 {forbidden}"

        def all_inner_texts(self) -> list[str]:
            return ["刷新账户与行情"]

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    with pytest.raises(AssertionError, match=forbidden):
        dashboard_acceptance._check_page_safety(Page())


@pytest.mark.parametrize(
    ("selector", "control_text", "expected"),
    (
        ("#tiger-long-term-panel", "", "独立老虎长线面板"),
        ("#trade-actions", "", "交易动作面板"),
        ("a:visible, button:visible", "立即下单", "下单入口"),
    ),
)
def test_check_page_safety_rejects_removed_panels_and_order_controls(
    selector: str, control_text: str, expected: str,
) -> None:
    class Locator:
        def __init__(self, current: str) -> None:
            self.current = current

        def count(self) -> int:
            return int(self.current == selector and not control_text)

        def inner_text(self) -> str:
            assert self.current == "body"
            return "持仓与策略"

        def all_inner_texts(self) -> list[str]:
            return [control_text] if self.current == selector and control_text else []

    class Page:
        def locator(self, current: str) -> Locator:
            return Locator(current)

    with pytest.raises(AssertionError, match=expected):
        dashboard_acceptance._check_page_safety(Page())


def test_check_page_safety_only_reads_visible_text_not_javascript_source() -> None:
    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def count(self) -> int:
            return 0

        def inner_text(self) -> str:
            assert self.selector == "body"
            return "持仓与策略"

        def all_inner_texts(self) -> list[str]:
            return ["策略回测", "刷新账户与行情"]

    class Page:
        javascript_source = "INELIGIBLE state_change calibration_required"

        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    dashboard_acceptance._check_page_safety(Page())


def test_check_tiger_tab_selects_tiger_and_shows_only_its_section() -> None:
    page = tabbed_account_page(valid_payload())

    dashboard_acceptance._check_tiger_tab(page)

    assert page.selected_brokers == ["tiger"]
    assert page.locator(
        '#account-tabs [data-broker="tiger"]'
    ).get_attribute("aria-selected") == "true"
    assert page.max_visible_account_sections == 1


def test_cn_filter_checks_each_broker_tab_without_all_accounts_view() -> None:
    page = tabbed_cn_page()

    dashboard_acceptance._check_cn_filter(page, expected_cn=2)

    assert page.selected_brokers == ["futu", "tiger", "phillips", "eastmoney"]
    assert page.max_visible_account_sections == 1


def test_cn_filter_restores_real_view_before_counting() -> None:
    page = TabbedAccountPage(cn_rows={
        "futu": 0, "tiger": 0, "phillips": 0, "eastmoney": 1,
    })
    page.account_views["eastmoney"] = "report"

    dashboard_acceptance._check_cn_filter(page, expected_cn=1)

    assert all(
        page.account_views[broker] == "real"
        for broker in ("tiger", "phillips", "eastmoney")
    )


def test_cn_filter_accepts_grouped_visible_count_for_large_account() -> None:
    page = TabbedAccountPage(cn_rows={
        "futu": 0, "tiger": 0, "phillips": 0, "eastmoney": 5000,
    })

    dashboard_acceptance._check_cn_filter(page, expected_cn=5000)

    assert page.selected_brokers == ["futu", "tiger", "phillips", "eastmoney"]


@pytest.mark.parametrize(
    "missing",
        (
            "富途", "老虎", "辉立", "东方财富", "期权增强",
            "美股趋势交易", "港股趋势交易",
        ),
)
def test_check_account_holdings_rejects_missing_profile_or_metric(missing: str) -> None:
    page = tabbed_account_page(valid_payload())
    for broker, text in page.section_texts.items():
        page.section_texts[broker] = text.replace(missing, "")
    for broker, text in page.entry_texts.items():
        page.entry_texts[broker] = text.replace(missing, "")

    with pytest.raises(AssertionError):
        dashboard_acceptance._check_account_holdings(page, valid_payload())


def test_validate_dashboard_payload_rejects_bad_counts_and_weights() -> None:
    payload = valid_payload()
    payload["holdings"][0]["portfolio_weight_hkd"] = "9.99%"  # type: ignore[index]
    payload["backtest_universe"] = {"holdings": []}

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert "组合权重合计不是 100.00%：99.99%" in errors
    assert "A 股回测标的数量不是 5：0" in errors


def test_validate_dashboard_payload_checks_eastmoney_statement_total_assets() -> None:
    payload = valid_payload()
    for row in payload["holdings"][:5]:  # type: ignore[index]
        row.update({"brokers": "eastmoney", "currency": "CNY", "market_value": "10"})
    payload["cash_rows"] = [{
        "market": "CASH", "symbol": "CNY_CASH", "brokers": "eastmoney",
        "currency": "CNY", "market_value": "50", "portfolio_weight_hkd": "0.00%",
    }]

    assert validate_dashboard_payload(
        payload, expected_cn=5, expected_eastmoney_cny=Decimal("100")
    ) == []

    errors = validate_dashboard_payload(
        payload, expected_cn=5, expected_eastmoney_cny=Decimal("101")
    )
    assert "东方财富总资产不匹配：100 != 101 CNY" in errors


def test_acceptance_parser_does_not_hardcode_mark_to_market_eastmoney_total() -> None:
    from open_trader.dashboard_acceptance import build_parser

    args = build_parser().parse_args([])

    assert args.expected_eastmoney_cny is None
    assert not hasattr(args, "wait_seconds")


def test_validate_dashboard_payload_checks_latest_phillips_statement() -> None:
    payload = valid_payload()
    payload["broker_summaries"] = [{
        "broker": "phillips", "detail_available": True,
        "portfolio_value_hkd": "628554.05",
    }]
    payload["source_statuses"] = [{
        "broker": "phillips", "display_text": "同步正常"
    }]
    payload["account_sync"]["brokers"]["phillips"]["data_as_of"] = "2026-07-10"  # type: ignore[index]

    errors = validate_dashboard_payload(
        payload, expected_cn=5,
        expected_phillips_total=Decimal("628554.06"),
        expected_phillips_period="2026-07",
    )

    assert "辉立总资产不匹配：628554.05 != 628554.06 HKD" in errors
    assert not any("行数" in error for error in errors)


def test_latest_phillips_expectation_uses_newest_archived_pdf(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / "statements/phillips/2026-06-30/statement.pdf"
    latest = tmp_path / "statements/phillips/2026-07-10/statement.pdf"
    old.parent.mkdir(parents=True)
    latest.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    latest.write_bytes(b"latest")

    def parse(_self, path, _month):
        assert path == latest
        return SimpleNamespace(
            positions=[SimpleNamespace(currency="HKD", market_value=Decimal("100"))],
            cash_balances=[SimpleNamespace(currency="HKD", cash_balance=Decimal("20"))],
        )

    monkeypatch.setattr("open_trader.parsers.phillips.PhillipsStatementParser.parse", parse)

    assert dashboard_acceptance._latest_phillips_expectation(tmp_path) == (
        Decimal("120"), "2026-07",
    )


def test_validate_dashboard_payload_rejects_empty_phillips_account_card() -> None:
    payload = valid_payload()
    payload["broker_summaries"] = [{
        "broker": "phillips", "detail_available": False, "portfolio_value_hkd": ""
    }]
    payload["source_statuses"] = [{
        "broker": "phillips", "display_text": "暂无月结单明细"
    }]

    errors = validate_dashboard_payload(
        payload, expected_cn=5, expected_phillips_total=Decimal("628554.06")
    )

    assert "辉立账户卡没有可用月结单资产" in errors


def test_classify_result_has_only_three_states() -> None:
    assert classify_result([], browser_blocker=None) == "PASS"
    assert classify_result(["API failed"], browser_blocker=None) == "FAIL"
    assert classify_result([], browser_blocker="Chrome unavailable") == "BLOCKED"
    assert classify_result(["API failed"], browser_blocker="Chrome unavailable") == "FAIL"


def test_dashboard_acceptance_does_not_require_daily_ai_sources() -> None:
    payload = valid_payload()
    for holding in payload["holdings"]:  # type: ignore[index]
        holding["agent_report"] = {"available": True}
        for key in (
            "tradingagents_summary",
            "technical_facts",
            "decision_facts",
            "futu_skill_facts",
        ):
            holding.pop(key, None)

    assert validate_dashboard_payload(payload, expected_cn=5) == []


def test_dashboard_signature_ignores_live_values_but_detects_structural_change() -> None:
    first = valid_payload()
    second = valid_payload()
    first["last_refresh"] = "one"
    second["last_refresh"] = "two"
    second["holdings"][0]["market_value_hkd"] = "123.45"  # type: ignore[index]
    second["holdings"][0]["portfolio_weight_hkd"] = "9.99%"  # type: ignore[index]
    assert dashboard_signature(first) == dashboard_signature(second)

    second["holdings"][0]["brokers"] = "changed"  # type: ignore[index]
    assert dashboard_signature(first) != dashboard_signature(second)


def simulate_snapshot(
    code: str = "US.NDAQ", quantity: str = "13", cost_price: str = "94.25",
) -> dict[str, object]:
    return {
        "positions": [{
            "code": code,
            "qty": quantity,
            "cost_price": cost_price,
        }],
    }


def simulate_api_payload(
    *,
    symbol: str = "NDAQ",
    quantity: str = "13",
    cost_price: str = "94.25",
    attribution_status: str = "unlinked",
    report: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "available": True,
        "broker": "tiger",
        "market": "US",
        "positions": [{
            "market": "US",
            "symbol": symbol,
            "quantity": quantity,
            "cost_price": cost_price,
            "attribution_status": attribution_status,
            "report": report,
        }],
        "error": "",
    }


def write_current_attribution(
    root: Path,
    *,
    artifact: str = "old.json",
    version: str = "v1",
    recorded_at: str = "2026-07-20T10:00:00-04:00",
) -> dict[str, str]:
    from open_trader.trend_review import _report_hash

    payload = {
        "execution_date": "2026-07-17",
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_snapshot": {"strategy_version": version},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": "NDAQ"}],
        },
    }
    report_sha256 = _report_hash(payload)
    report_path = root / "reports" / "trend_us_tiger" / artifact
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    event_path = (
        root / "data/trend_review/ledgers/US/actions/2026-07-17/action"
        / f"{artifact}.json"
    )
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(json.dumps({
        "date": "2026-07-17",
        "market": "US",
        "symbol": "NDAQ",
        "side": "buy",
        "status": "filled",
        "filled_qty": "13",
        "recorded_at": recorded_at,
        "report_sha256": report_sha256,
        "strategy_version": version,
    }), encoding="utf-8")
    return {
        "artifact": artifact,
        "execution_date": "2026-07-17",
        "strategy_version": version,
        "report_sha256": report_sha256,
    }


def write_current_terminal_sell(
    root: Path,
    report: dict[str, str],
    *,
    status: str,
    reason: str | None,
) -> None:
    event = {
        "date": "2026-07-17",
        "market": "US",
        "symbol": "NDAQ",
        "side": "sell",
        "status": status,
        "filled_qty": "40",
        "recorded_at": "2026-07-20T10:01:00-04:00",
        "report_sha256": report["report_sha256"],
        "strategy_version": report["strategy_version"],
    }
    if reason is not None:
        event["reason"] = reason
    path = root / "data/trend_review/ledgers/US/actions/2026-07-17/action/sell.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event), encoding="utf-8")


@pytest.mark.parametrize(
    ("field", "value"),
    [("symbol", "AAPL"), ("quantity", "12"), ("cost_price", "94.26")],
)
def test_acceptance_rejects_simulated_api_facts_that_differ_from_direct_futu(
    tmp_path: Path, field: str, value: str,
) -> None:
    payload = simulate_api_payload(**{field: value})

    with pytest.raises(AssertionError, match="模拟盘持仓.*不匹配"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            payload,
            tmp_path / "data",
            tmp_path / "reports",
        )


def test_acceptance_accepts_zero_simulated_positions(tmp_path: Path) -> None:
    dashboard_acceptance._validate_simulated_positions(
        "tiger",
        {"positions": []},
        {**simulate_api_payload(), "positions": []},
        tmp_path / "data",
        tmp_path / "reports",
    )


def test_acceptance_classifies_unavailable_configured_futu_account_as_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_client(**_kwargs: object) -> object:
        raise RuntimeError("OpenD unavailable")

    monkeypatch.setattr(
        dashboard_acceptance, "FutuSimulateOrderExecutionClient", unavailable_client
    )
    payloads, errors, blocker = dashboard_acceptance._check_simulated_accounts(
        "http://dashboard.test",
        {"futu_host": "127.0.0.1", "futu_port": 11111},
        {"tiger": 1, "phillips": 2, "eastmoney": 3},
        tmp_path / "data",
        tmp_path / "reports",
    )

    assert payloads == {}
    assert errors == []
    assert "OpenD unavailable" in str(blocker)
    assert classify_result(
        [], browser_blocker=None, external_blocker=blocker
    ) == "BLOCKED"


def test_acceptance_treats_dashboard_simulate_fallback_as_fail_when_futu_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def account_snapshot(self) -> dict[str, object]:
            return simulate_snapshot()

        def close(self) -> None:
            pass

    def fetcher(_url: str, path: str) -> dict[str, object]:
        broker = path.rsplit("/", 1)[-1]
        market = dashboard_acceptance.TREND_SIMULATE_MARKETS[broker]
        return {
            "available": False,
            "broker": broker,
            "market": market,
            "positions": [],
            "error": "using cached report plan",
        }

    monkeypatch.setattr(
        dashboard_acceptance,
        "FutuSimulateOrderExecutionClient",
        lambda **_kwargs: Client(),
    )
    monkeypatch.setattr(dashboard_acceptance, "_fetch_json_path", fetcher)
    _payloads, errors, blocker = dashboard_acceptance._check_simulated_accounts(
        "http://dashboard.test",
        {"futu_host": "127.0.0.1", "futu_port": 11111},
        {"tiger": 1, "phillips": 2, "eastmoney": 3},
        tmp_path / "data",
        tmp_path / "reports",
    )

    assert blocker is None
    assert len(errors) == 3
    assert all("Dashboard 模拟盘不可用" in error for error in errors)


def test_acceptance_accepts_explicitly_unlinked_legacy_simulated_position(
    tmp_path: Path,
) -> None:
    dashboard_acceptance._validate_simulated_positions(
        "tiger",
        simulate_snapshot(),
        simulate_api_payload(),
        tmp_path / "data",
        tmp_path / "reports",
    )


def test_acceptance_rejects_traceable_position_declared_unlinked(
    tmp_path: Path,
) -> None:
    write_current_attribution(tmp_path)

    with pytest.raises(AssertionError, match="报告归因"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            simulate_api_payload(),
            tmp_path / "data",
            tmp_path / "reports",
        )


def test_acceptance_rejects_hidden_current_attribution_conflict(
    tmp_path: Path,
) -> None:
    write_current_attribution(tmp_path, artifact="v1.json", version="v1")
    write_current_attribution(
        tmp_path,
        artifact="v2.json",
        version="v2",
        recorded_at="2026-07-20T10:01:00-04:00",
    )

    with pytest.raises(AssertionError, match="报告归因冲突"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            simulate_api_payload(),
            tmp_path / "data",
            tmp_path / "reports",
        )


@pytest.mark.parametrize(
    ("event_status", "reason", "attribution_status"),
    [
        ("incomplete", "position_zero_confirmed", "unlinked"),
        ("incomplete", None, "linked"),
        ("failed", "position_zero_confirmed", "linked"),
        ("submitted", "position_zero_confirmed", "linked"),
        ("missed", "position_zero_confirmed", "linked"),
    ],
)
def test_acceptance_clears_only_terminal_incomplete_sell(
    tmp_path: Path,
    event_status: str,
    reason: str | None,
    attribution_status: str,
) -> None:
    report = write_current_attribution(tmp_path)
    write_current_terminal_sell(
        tmp_path, report, status=event_status, reason=reason
    )

    dashboard_acceptance._validate_simulated_positions(
        "tiger",
        simulate_snapshot(),
        simulate_api_payload(
            attribution_status=attribution_status,
            report=report if attribution_status == "linked" else None,
        ),
        tmp_path / "data",
        tmp_path / "reports",
    )


def test_acceptance_rejects_hidden_unlinked_simulated_position(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="模拟盘持仓.*不匹配"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            {**simulate_api_payload(), "positions": []},
            tmp_path / "data",
            tmp_path / "reports",
        )


def test_acceptance_rejects_unavailable_simulated_api_with_substitute_rows(
    tmp_path: Path,
) -> None:
    payload = {
        **simulate_api_payload(),
        "available": False,
        "error": "OpenD unavailable",
    }

    with pytest.raises(AssertionError, match="不可用.*替代持仓"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            payload,
            tmp_path / "data",
            tmp_path / "reports",
        )


@pytest.mark.parametrize("wrong_field", ["report_sha256", "strategy_version"])
def test_acceptance_rejects_linked_simulated_position_with_wrong_report_identity(
    tmp_path: Path, wrong_field: str,
) -> None:
    report = write_current_attribution(tmp_path)
    report[wrong_field] = "0" * 64 if wrong_field == "report_sha256" else "v2"

    with pytest.raises(AssertionError, match="报告身份"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            simulate_api_payload(attribution_status="linked", report=report),
            tmp_path / "data",
            tmp_path / "reports",
        )


def _write_acceptance_history_artifact(
    reports_dir: Path,
    artifact: str,
    *,
    execution_date: str,
    symbol: str,
) -> tuple[dict[str, object], str]:
    from open_trader.trend_review import _report_hash

    payload: dict[str, object] = {
        "execution_date": execution_date,
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_snapshot": {"strategy_version": "v1"},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": symbol}],
        },
    }
    path = reports_dir / "trend_us_tiger" / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, _report_hash(payload)


def _write_acceptance_action(
    data_dir: Path, *, report_sha256: str, symbol: str = "NDAQ",
) -> dict[str, str]:
    event = {
        "date": "2026-07-17",
        "market": "US",
        "symbol": symbol,
        "side": "buy",
        "status": "missed",
        "recorded_at": "2026-07-18T08:27:12+08:00",
        "report_sha256": report_sha256,
    }
    path = (
        data_dir / "trend_review/ledgers/US/actions/2026-07-17/action/event.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(event), encoding="utf-8")
    return event


def test_acceptance_rejects_history_that_drops_ledger_referenced_old_action(
    tmp_path: Path,
) -> None:
    reports_dir = tmp_path / "reports"
    _, old_hash = _write_acceptance_history_artifact(
        reports_dir, "old.json", execution_date="2026-07-17", symbol="NDAQ"
    )
    _write_acceptance_history_artifact(
        reports_dir, "new.json", execution_date="2026-07-20", symbol="AAPL"
    )
    _write_acceptance_action(tmp_path / "data", report_sha256=old_hash)
    history = [{
        "available": True,
        "artifact": "new.json",
        "execution_date": "2026-07-20",
        "strategy_version": "v1",
    }]

    with pytest.raises(AssertionError, match="old.json.*历史报告"):
        dashboard_acceptance._validate_history_projection(
            tmp_path / "data", reports_dir, "tiger", history, {}
        )


def test_acceptance_does_not_require_synthetic_protection_report_history(
    tmp_path: Path,
) -> None:
    from open_trader import trend_review

    data_dir = tmp_path / "data"
    execution_date = "2026-07-17"
    action_key = trend_review.trend_action_key(
        "US", execution_date, "US.EOG", "sell"
    )
    report_hash = trend_review._report_hash(
        trend_review._protection_report("EOG", "protection-1")
    )
    evidence = {
        "market": "US",
        "date": execution_date,
        "strategy_version": "protection-v1",
        "report_sha256": report_hash,
        "action_index": 0,
        "symbol": "EOG",
        "futu_code": "US.EOG",
        "side": "sell",
        "sell_goal": "position_zero",
    }
    trend_review._write_action_event(
        data_dir=data_dir,
        market="US",
        execution_date=execution_date,
        action_key=action_key,
        payload={
            **evidence,
            "status": "reason_added",
            "reason_id": "protection-1",
            "reason": "protection_event",
        },
        recorded_at="2026-07-17T10:15:00-04:00",
    )
    trend_review._write_action_event(
        data_dir=data_dir,
        market="US",
        execution_date=execution_date,
        action_key=action_key,
        payload={**evidence, "status": "submitted"},
        recorded_at="2026-07-17T10:15:01-04:00",
    )

    assert dashboard_acceptance._validate_history_projection(
        data_dir, tmp_path / "reports", "tiger", [], {}
    ) == []


def test_acceptance_keeps_ledger_referenced_action_in_exact_historical_report(
    tmp_path: Path,
) -> None:
    reports_dir = tmp_path / "reports"
    _, old_hash = _write_acceptance_history_artifact(
        reports_dir, "old.json", execution_date="2026-07-17", symbol="NDAQ"
    )
    _write_acceptance_history_artifact(
        reports_dir, "new.json", execution_date="2026-07-20", symbol="AAPL"
    )
    event = _write_acceptance_action(tmp_path / "data", report_sha256=old_hash)
    history = [
        {
            "available": True,
            "artifact": artifact,
            "execution_date": execution_date,
            "strategy_version": "v1",
        }
        for artifact, execution_date in (
            ("new.json", "2026-07-20"), ("old.json", "2026-07-17")
        )
    ]
    exact = {
        "old.json": {
            "artifact": "old.json",
            "report_sha256": old_hash,
            "strategy_version": "v1",
            "report_date": "2026-07-17",
            "audit": {"artifact": "old.json"},
            "strategy_parameter_rows": [{
                "group": "退出保护",
                "name": "退出条件",
                "value": "危险信号时全部卖出",
            }],
            "buy_actions": [{
                "symbol": "NDAQ",
                "execution": {
                    "status": "missed",
                    "updated_at": event["recorded_at"],
                },
            }],
        }
    }

    expectations = dashboard_acceptance._validate_history_projection(
        tmp_path / "data", reports_dir, "tiger", history, exact
    )

    assert expectations[0]["artifact"] == "old.json"
    assert expectations[0]["strategy_parameter_rows"] == exact["old.json"][
        "strategy_parameter_rows"
    ]


def test_acceptance_allows_exact_history_to_lag_a_duplicate_terminal_event(
    tmp_path: Path,
) -> None:
    reports_dir = tmp_path / "reports"
    _, old_hash = _write_acceptance_history_artifact(
        reports_dir, "old.json", execution_date="2026-07-17", symbol="NDAQ"
    )
    projected_event = _write_acceptance_action(
        tmp_path / "data", report_sha256=old_hash
    )
    newer_event = {
        **projected_event,
        "recorded_at": "2026-07-18T08:27:19+08:00",
    }
    newer_path = (
        tmp_path
        / "data/trend_review/ledgers/US/actions/2026-07-17/action/newer.json"
    )
    newer_path.write_text(json.dumps(newer_event), encoding="utf-8")
    history = [{
        "available": True,
        "artifact": "old.json",
        "execution_date": "2026-07-17",
        "strategy_version": "v1",
    }]
    exact = {
        "old.json": {
            "artifact": "old.json",
            "report_sha256": old_hash,
            "strategy_version": "v1",
            "report_date": "2026-07-17",
            "audit": {"artifact": "old.json"},
            "buy_actions": [{
                "symbol": "NDAQ",
                "execution": {
                    "status": "missed",
                    "updated_at": projected_event["recorded_at"],
                },
            }],
        }
    }

    expectations = dashboard_acceptance._validate_history_projection(
        tmp_path / "data", reports_dir, "tiger", history, exact
    )

    assert expectations[0]["event"] == newer_event


def test_acceptance_rejects_latest_exact_api_identity_that_differs_from_local_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_hash = "a" * 64
    local_report = {
        "artifact": "latest.json",
        "execution_date": "2026-07-17",
        "strategy_version": "v1",
        "report_sha256": local_hash,
    }
    monkeypatch.setattr(
        dashboard_acceptance,
        "_reports_by_hash",
        lambda *_args, **_kwargs: {local_hash: local_report},
    )
    monkeypatch.setattr(dashboard_acceptance, "_action_events", lambda *_args: [])

    def fetch(_url: str, path: str) -> object:
        if path.endswith("/history"):
            return [{
                "available": True,
                "artifact": "latest.json",
                "execution_date": "2026-07-17",
                "strategy_version": "v1",
            }]
        return {
            "artifact": "latest.json",
            "report_date": "2026-07-17",
            "strategy_version": "v2",
            "report_sha256": "b" * 64,
        }

    monkeypatch.setattr(dashboard_acceptance, "_fetch_json_path", fetch)

    expectations, errors = dashboard_acceptance._check_history_endpoints(
        "http://dashboard.test", tmp_path / "data", tmp_path / "reports"
    )

    assert expectations == {}
    assert len(errors) == 3
    assert all("精确历史报告身份不匹配" in error for error in errors)


def test_acceptance_rejects_dirty_dashboard_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "src/open_trader").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / ".gitignore").write_text(
        ".venv/\n.superpowers/\nconfig/daily_premarket.env\n",
        encoding="utf-8",
    )
    tracked_test = tmp_path / "tests/test_dashboard_acceptance.py"
    tracked_test.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(tmp_path), "-c", "user.name=Codex",
            "-c", "user.email=codex@example.invalid", "commit", "-qm", "baseline",
        ],
        check=True,
    )

    tracked_test.write_text("modified\n", encoding="utf-8")
    (tmp_path / "src/open_trader/new_module.py").write_text("", encoding="utf-8")
    for ignored in (
        ".venv/cache",
        ".superpowers/sdd/report.md",
        "config/daily_premarket.env",
    ):
        path = tmp_path / ignored
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignored\n", encoding="utf-8")

    assert set(dashboard_acceptance._source_changes(tmp_path)) == {
        "M tests/test_dashboard_acceptance.py",
        "?? src/open_trader/new_module.py",
    }


def _runtime_health(
    tmp_path: Path, *, module: str, schema: str, pid: int,
) -> dict[str, object]:
    return {
        "schema_version": schema,
        "module": module,
        "pid": pid,
        "started_at": "2026-08-01T12:00:01+08:00",
        "cwd": str(tmp_path),
        "git_sha": "accepted-sha",
        "source_state": "clean",
    }


def test_acceptance_accepts_matching_gateway_health(tmp_path: Path) -> None:
    payload = {
        **_runtime_health(
            tmp_path,
            module="frontend_gateway",
            schema="open_trader.frontend_gateway.health.v1",
            pid=123,
        ),
        "upstream_status": "ok",
    }

    assert dashboard_acceptance._runtime_health_errors(
        payload,
        name="Frontend Gateway",
        expected_schema="open_trader.frontend_gateway.health.v1",
        expected_module="frontend_gateway",
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
        expected_upstream_status="ok",
    ) == []


@pytest.mark.parametrize(
    ("payload_pid", "expected_pid"),
    [(123.0, 123), (True, 1)],
)
def test_acceptance_rejects_gateway_health_wrong_pid_type(
    tmp_path: Path, payload_pid: object, expected_pid: int,
) -> None:
    payload = {
        **_runtime_health(
            tmp_path,
            module="frontend_gateway",
            schema="open_trader.frontend_gateway.health.v1",
            pid=payload_pid,  # type: ignore[arg-type]
        ),
        "upstream_status": "ok",
    }

    errors = dashboard_acceptance._runtime_health_errors(
        payload,
        name="Frontend Gateway",
        expected_schema="open_trader.frontend_gateway.health.v1",
        expected_module="frontend_gateway",
        pid=expected_pid,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
        expected_upstream_status="ok",
    )

    assert any("PID" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "wrong.v1", "schema"),
        ("module", "legacy_dashboard", "模块"),
        ("pid", 999, "PID"),
        ("cwd", "/wrong/worktree", "工作目录"),
        ("git_sha", "old-sha", "Git SHA"),
        ("source_state", "dirty", "源码状态"),
        ("started_at", "2026-08-01T11:59:59+08:00", "启动时间"),
        ("upstream_status", "unavailable", "upstream"),
    ],
)
def test_acceptance_rejects_gateway_health_mismatch(
    tmp_path: Path, field: str, value: object, message: str,
) -> None:
    payload = {
        **_runtime_health(
            tmp_path,
            module="frontend_gateway",
            schema="open_trader.frontend_gateway.health.v1",
            pid=123,
        ),
        "upstream_status": "ok",
        field: value,
    }

    errors = dashboard_acceptance._runtime_health_errors(
        payload,
        name="Frontend Gateway",
        expected_schema="open_trader.frontend_gateway.health.v1",
        expected_module="frontend_gateway",
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
        expected_upstream_status="ok",
    )

    assert any(message in error for error in errors)


def test_acceptance_reads_gateway_runtime_prefix(tmp_path: Path) -> None:
    runtime = {
        "pid": 123,
        "git_sha": "accepted-sha",
        "cwd": str(tmp_path),
        "source_state": "clean",
        "started_at": "2026-08-01T12:00:01+08:00",
    }
    log = tmp_path / "gateway.log"
    log.write_text(
        f"frontend_gateway_runtime: {json.dumps(runtime)}\n",
        encoding="utf-8",
    )

    assert dashboard_acceptance._log_errors(
        log,
        name="Frontend Gateway",
        prefix="frontend_gateway_runtime: ",
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
    ) == []


@pytest.mark.parametrize(
    ("cwd_present", "cwd"),
    [(False, None), (True, None), (True, ""), (True, False), (True, 0), (True, 123)],
)
def test_acceptance_rejects_runtime_log_with_invalid_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cwd_present: bool,
    cwd: object,
) -> None:
    monkeypatch.chdir(tmp_path)
    runtime = {
        "pid": 123,
        "git_sha": "accepted-sha",
        "source_state": "clean",
        "started_at": "2026-08-01T12:00:01+08:00",
    }
    if cwd_present:
        runtime["cwd"] = cwd
    log = tmp_path / "gateway.log"
    log.write_text(
        f"frontend_gateway_runtime: {json.dumps(runtime)}\n",
        encoding="utf-8",
    )

    errors = dashboard_acceptance._log_errors(
        log,
        name="Frontend Gateway",
        prefix="frontend_gateway_runtime: ",
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
    )

    assert any("工作目录" in error for error in errors)


@pytest.mark.parametrize(
    ("record_pid", "expected_pid"),
    [(123.0, 123), (True, 1)],
)
def test_acceptance_rejects_gateway_runtime_wrong_pid_type(
    tmp_path: Path, record_pid: object, expected_pid: int,
) -> None:
    runtime = {
        "pid": record_pid,
        "git_sha": "accepted-sha",
        "cwd": str(tmp_path),
        "source_state": "clean",
        "started_at": "2026-08-01T12:00:01+08:00",
    }
    log = tmp_path / "gateway.log"
    log.write_text(
        f"frontend_gateway_runtime: {json.dumps(runtime)}\n",
        encoding="utf-8",
    )

    errors = dashboard_acceptance._log_errors(
        log,
        name="Frontend Gateway",
        prefix="frontend_gateway_runtime: ",
        pid=expected_pid,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat(
            "2026-08-01T12:00:00+08:00"
        ),
    )

    assert any("没有候选" in error for error in errors)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"pid": 122}, "PID"),
        ({"git_sha": "old-sha"}, "Git SHA"),
        ({"source_state": "dirty"}, "源码状态"),
        ({"started_at": "2026-07-18T11:59:59+08:00"}, "启动时间"),
    ],
)
def test_acceptance_rejects_log_not_bound_to_candidate_process(
    tmp_path: Path, record: dict[str, object], message: str,
) -> None:
    runtime = {
        "pid": 123,
        "git_sha": "accepted-sha",
        "cwd": str(tmp_path),
        "source_state": "clean",
        "started_at": "2026-07-18T12:00:01+08:00",
        **record,
    }
    log = tmp_path / "dashboard.log"
    log.write_text(f"dashboard_runtime: {json.dumps(runtime)}\n", encoding="utf-8")

    assert any(message in error for error in dashboard_acceptance._log_errors(
        log,
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat("2026-07-18T12:00:00+08:00"),
    ))


def test_acceptance_rejects_timezone_naive_runtime_start_without_crashing(
    tmp_path: Path,
) -> None:
    runtime = {
        "pid": 123,
        "git_sha": "accepted-sha",
        "cwd": str(tmp_path),
        "source_state": "clean",
        "started_at": "2026-07-18T12:00:01",
    }
    log = tmp_path / "dashboard.log"
    log.write_text(f"dashboard_runtime: {json.dumps(runtime)}\n", encoding="utf-8")

    errors = dashboard_acceptance._log_errors(
        log,
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=datetime.fromisoformat("2026-07-18T12:00:00+08:00"),
    )

    assert any("启动时间无效" in error for error in errors)


def test_simulated_position_wait_has_bounded_timeout() -> None:
    calls: list[tuple[str, object, int | None]] = []

    class Page:
        def wait_for_function(
            self, expression: str, *, arg: object, timeout: int | None = None,
        ) -> None:
            calls.append((expression, arg, timeout))

    dashboard_acceptance._wait_for_simulate_positions(Page(), "tiger", 1)

    assert calls == [(
        dashboard_acceptance.SIMULATE_POSITIONS_READY_EXPRESSION,
        {"broker": "tiger", "expected": 1},
        30_000,
    )]


def test_dashboard_api_fetch_allows_slow_live_simulate_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int | None] = []

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(_url: str, *, timeout: int | None = None) -> Response:
        calls.append(timeout)
        return Response()

    monkeypatch.setattr(dashboard_acceptance, "urlopen", fake_urlopen)

    assert dashboard_acceptance._fetch_json_path(
        "http://dashboard.test", "/api/trend-simulate-positions/tiger"
    ) == {}
    assert calls == [30]


def test_acceptance_rejects_appended_stale_log_content(tmp_path: Path) -> None:
    started = datetime.fromisoformat("2026-07-18T12:00:00+08:00")
    runtime = {
        "pid": 123,
        "git_sha": "accepted-sha",
        "cwd": str(tmp_path),
        "source_state": "clean",
        "started_at": "2026-07-18T12:00:01+08:00",
    }
    log = tmp_path / "dashboard.log"
    log.write_text(
        "stale clean log content\n"
        f"dashboard_runtime: {json.dumps(runtime)}\n",
        encoding="utf-8",
    )

    errors = dashboard_acceptance._log_errors(
        log,
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=started,
    )

    assert any("新日志" in error for error in errors)


def test_acceptance_rejects_log_older_than_candidate_process(tmp_path: Path) -> None:
    started = datetime.fromisoformat("2026-07-18T12:00:00+08:00")
    runtime = {
        "pid": 123,
        "git_sha": "accepted-sha",
        "cwd": str(tmp_path),
        "source_state": "clean",
        "started_at": "2026-07-18T12:00:01+08:00",
    }
    log = tmp_path / "dashboard.log"
    log.write_text(f"dashboard_runtime: {json.dumps(runtime)}\n", encoding="utf-8")
    old = started.timestamp() - 1
    os.utime(log, (old, old))

    errors = dashboard_acceptance._log_errors(
        log,
        pid=123,
        expected_sha="accepted-sha",
        expected_cwd=tmp_path,
        process_started_at=started,
    )

    assert any("修改时间" in error for error in errors)


def _controller_runtime_payload(
    tmp_path: Path,
    *,
    now: datetime,
) -> dict[str, object]:
    payload = valid_payload()
    controllers = payload["trend_controllers"]
    assert isinstance(controllers, dict)
    for pid, (broker, controller) in enumerate(controllers.items(), start=4210):
        assert isinstance(controller, dict)
        controller.update({
            "pid": pid,
            "working_directory": str(tmp_path),
            "git_sha": "accepted-sha",
            "heartbeat_at": now.isoformat(),
        })
        market = str(controller["market"]).lower()
        logs = tmp_path / "logs/daily_premarket"
        logs.mkdir(parents=True, exist_ok=True)
        runtime = {
            "pid": pid,
            "git_sha": "accepted-sha",
            "cwd": str(tmp_path),
            "verified_at": now.isoformat(),
            "stderr_offset": 0,
        }
        (logs / f"launchd-trend-controller-{market}.out.log").write_text(
            f"controller_runtime: {json.dumps(runtime)}\n", encoding="utf-8"
        )
        (logs / f"launchd-trend-controller-{market}.err.log").write_text(
            "", encoding="utf-8"
        )
    return payload


def _controller_runtime_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    now: datetime,
    payload: dict[str, object],
) -> list[str]:
    monkeypatch.setattr(dashboard_acceptance.os, "kill", lambda *_args: None)
    monkeypatch.setattr(
        dashboard_acceptance, "_process_cwd", lambda _pid: tmp_path.resolve()
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_process_started_at",
        lambda _pid: now - timedelta(seconds=1),
    )
    return dashboard_acceptance._trend_controller_errors(
        payload,
        expected_root=tmp_path,
        expected_sha="accepted-sha",
        now=now,
    )


@pytest.mark.parametrize(
    "phase", ["reconciling", "recovering_report", "recovering_review"]
)
def test_acceptance_rejects_fresh_blocked_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    controller.update({  # type: ignore[union-attr]
        "health": "unavailable",
        "blocking": True,
        "phase": phase,
        "last_success": None,
        "blocker": "report generation failed",
    })

    errors = _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    )

    assert any("tiger" in error and "阻塞" in error for error in errors)


@pytest.mark.parametrize(
    "phase", ["reconciling", "recovering_report", "recovering_review"]
)
def test_acceptance_accepts_healthy_in_progress_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    controller["phase"] = phase  # type: ignore[index]

    assert _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    ) == []


def test_acceptance_allows_progress_controllers_before_first_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    controllers = payload["trend_controllers"]
    controllers["phillips"].update({  # type: ignore[index,union-attr]
        "phase": "recovering_report",
        "last_success": None,
    })
    controllers["eastmoney"].update({  # type: ignore[index,union-attr]
        "phase": "reconciling",
        "last_success": None,
    })

    errors = _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    )

    assert "phillips 控制器尚无首次成功状态" not in errors
    assert "eastmoney 控制器尚无首次成功状态" not in errors
    assert errors == []


def test_acceptance_accepts_matching_controller_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)

    assert _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    ) == []


@pytest.mark.parametrize("phase", ["before", "monitoring", "closed"])
def test_acceptance_rejects_stable_controller_without_first_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    controller = payload["trend_controllers"]["tiger"]  # type: ignore[index]
    controller.update({"phase": phase, "last_success": None})  # type: ignore[union-attr]

    errors = _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    )

    assert any("tiger" in error and "成功" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("working_directory", "/wrong/review", "工作目录"),
        ("git_sha", "old-sha", "Git SHA"),
        ("heartbeat_at", "2026-07-21T09:20:00+08:00", "心跳"),
    ],
)
def test_acceptance_rejects_controller_status_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    payload["trend_controllers"]["tiger"][field] = value  # type: ignore[index]

    errors = _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    )

    assert any("tiger" in error and message in error for error in errors)


def test_acceptance_rejects_missing_controller_working_directory_from_expected_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    payload["trend_controllers"]["tiger"]["working_directory"] = ""  # type: ignore[index]
    monkeypatch.chdir(tmp_path)

    errors = _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    )

    assert any("tiger" in error and "工作目录" in error for error in errors)


def test_acceptance_rejects_missing_controller_log_cwd_from_expected_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    pid = payload["trend_controllers"]["tiger"]["pid"]  # type: ignore[index]
    runtime = {
        "pid": pid,
        "git_sha": "accepted-sha",
        "verified_at": now.isoformat(),
        "stderr_offset": 0,
    }
    stdout = (
        tmp_path
        / "logs/daily_premarket/launchd-trend-controller-us.out.log"
    )
    stdout.write_text(
        f"controller_runtime: {json.dumps(runtime)}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    errors = _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    )

    assert any("US" in error and "工作目录" in error for error in errors)


def test_acceptance_rejects_dead_controller_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    dead_pid = payload["trend_controllers"]["tiger"]["pid"]  # type: ignore[index]

    def kill(pid: int, signal: int) -> None:
        del signal
        if pid == dead_pid:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(dashboard_acceptance.os, "kill", kill)
    monkeypatch.setattr(
        dashboard_acceptance, "_process_cwd", lambda _pid: tmp_path.resolve()
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_process_started_at",
        lambda _pid: now - timedelta(seconds=1),
    )

    errors = dashboard_acceptance._trend_controller_errors(
        payload,
        expected_root=tmp_path,
        expected_sha="accepted-sha",
        now=now,
    )

    assert any("tiger" in error and "PID" in error for error in errors)


def test_acceptance_rejects_fresh_controller_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromisoformat("2026-07-21T09:31:00+08:00")
    payload = _controller_runtime_payload(tmp_path, now=now)
    stderr = (
        tmp_path
        / "logs/daily_premarket/launchd-trend-controller-us.err.log"
    )
    stderr.write_text("Traceback (most recent call last):\n", encoding="utf-8")

    errors = _controller_runtime_errors(
        tmp_path, monkeypatch, now=now, payload=payload
    )

    assert any("US" in error and "stderr" in error for error in errors)


def test_acceptance_derives_cn_count_from_canonical_portfolio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    latest = data / "latest"
    latest.mkdir(parents=True)
    (latest / "portfolio.csv").write_text(
        "market,asset_class,total_quantity\nCN,stock,10\nCN,stock,0\nUS,stock,2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_acceptance, "_project_data_dir", lambda _root: data)

    assert dashboard_acceptance._expected_cn_holdings(tmp_path) == 1


def test_acceptance_local_missing_futu_configuration_is_fail(tmp_path: Path) -> None:
    payloads, errors, blocker = dashboard_acceptance._check_simulated_accounts(
        "http://dashboard.test",
        {"futu_host": "", "futu_port": 0},
        {"tiger": 0, "phillips": 0, "eastmoney": 0},
        tmp_path / "data",
        tmp_path / "reports",
    )

    assert payloads == {}
    assert errors == ["Dashboard 缺少有效 Futu OpenD 配置"]
    assert blocker is None
    assert classify_result(errors, browser_blocker=None) == "FAIL"


def test_acceptance_cli_has_no_test_only_config_or_expected_cn_options() -> None:
    destinations = {action.dest for action in dashboard_acceptance.build_parser()._actions}

    assert "config" not in destinations
    assert "expected_cn" not in destinations
