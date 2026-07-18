from datetime import datetime
from decimal import Decimal
import inspect
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest

from open_trader import dashboard_acceptance
from open_trader.dashboard_acceptance import (
    REQUIRED_SOURCE_PATHS,
    _is_actionable_console_error,
    classify_result,
    dashboard_signature,
    validate_dashboard_payload,
    validate_quotes_payload,
)


MISSING_FRESH = object()


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
    assert 'DASHBOARD_LOG ?= /tmp/open_trader_dashboard_8766.log' in makefile
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
        "tablet-portfolio.png",
        "760-trend-report.png",
        "mobile-portfolio.png",
        "375-trend-report.png",
    )


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
    browser_log_text: str = "",
    log_is_directory: bool = False,
    log_read_error: OSError | None = None,
) -> tuple[int, dict[str, object], list[Path | None]]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    payloads = iter({"reports_dir": str(path)} for path in report_dirs)
    quote_payloads = iter((valid_quotes_payload(),))
    browser_reports: list[Path | None] = []
    log_path = tmp_path / "dashboard.log"
    if log_is_directory:
        log_path.mkdir()
    else:
        log_path.write_text(
            'dashboard_runtime: {"pid": 123, "git_sha": "accepted-sha", '
            '"cwd": "' + str(worktree.resolve()) + '", "source_state": "clean", '
            '"started_at": "2026-07-18T12:00:01+08:00"}\n',
            encoding="utf-8",
        )
    if log_read_error is not None:
        original_read_text = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == log_path:
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
    monkeypatch.setattr(
        dashboard_acceptance, "_listener", lambda url: (123, worktree.resolve())
    )
    monkeypatch.setattr(
        dashboard_acceptance.subprocess,
        "check_output",
        lambda *args, **kwargs: "accepted-sha\n",
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_process_started_at",
        lambda *_args: datetime.fromisoformat("2026-07-18T12:00:00+08:00"),
    )
    monkeypatch.setattr(
        dashboard_acceptance, "_source_changes", lambda *_args: []
    )
    monkeypatch.setattr(
        dashboard_acceptance,
        "_expected_cn_holdings",
        lambda *_args: 2,
    )
    monkeypatch.setattr(
        dashboard_acceptance, "_fetch_payload", lambda url: next(payloads)
    )
    monkeypatch.setattr(
        dashboard_acceptance, "_fetch_quotes_payload", lambda url: next(quote_payloads)
    )
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
        browser_reports.append(reports_dir)
        if browser_log_text:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(browser_log_text)
        return [], None

    monkeypatch.setattr(dashboard_acceptance, "_browser_check", browser_check)
    status = dashboard_acceptance.main([
        "--expected-root", str(worktree),
        "--log", str(log_path),
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
        "futu": {
            "available": True, "broker": "futu", "broker_label": "富途",
            "market_label": "美股 / 港股", "report_date": "2026-07-15",
            "data_date": "2026-07-14", "generated_at": "2026-07-15T11:31:00+08:00",
            "attention_markets": [
                {
                    "market": "US", "market_label": "美股", "data_status": "current",
                    "data_date": "2026-07-15", "items": [{"symbol": "VIXY"}],
                },
                {
                    "market": "HK", "market_label": "港股", "data_status": "stale",
                    "data_date": "2026-07-14", "items": [{"symbol": "00700"}],
                },
            ],
        },
        "tiger": {
            "available": True, "broker": "tiger", "broker_label": "老虎",
            "market_label": "美股", "report_date": "2026-07-15",
            "data_date": "2026-07-14", "generated_at": "2026-07-15T11:30:36+08:00",
            "account_status": "已更新", "buy_window": "美股常规交易时段",
            "sell_actions": [{"symbol": "AAPL", "name": "苹果", "close": "200", "strength": "99", "reason": "danger_signal", "active_line": "190"}],
            "buy_actions": [{"symbol": "VIXY", "name": "波动率ETF", "close": "19", "strength": "98", "industry": "ETF", "target_weight": "0.04", "estimated_shares": "5000", "target_amount": "25142.16", "estimated_initial_line": "18.50"}],
            "hold_actions": [{"symbol": "SPY", "name": "标普ETF", "close": "510", "strength": "97", "reason": "trend_intact", "active_line": "500"}],
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
                for key in (
                    "period_net_return", "market_excess_return",
                    "max_drawdown", "calmar", "sharpe",
                )
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
        "backtest_universe": {"holdings": [
            {"market": "CN", "symbol": row["symbol"]} for row in cn
        ]},
        "trend_reports": trend_reports(),
        "trend_reviews": trend_reviews(),
    }


def test_acceptance_checks_exact_trend_review_content() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    section = dashboard_acceptance._select_account_tab(page, "tiger")

    dashboard_acceptance._check_trend_review(
        page, section, "tiger", payload["trend_reviews"]["tiger"]
    )

    assert page.opened_reviews == ["tiger"]


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
        "富途期权增强跨市场期权关注期权关注美股港股 "
        "老虎趋势美股趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14 "
        "辉立短线港股趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14 "
        "东方财富偏短线趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14"
    )


def trend_workspace_text(
    broker: str, report: dict[str, object] | None = None,
) -> str:
    if broker == "futu":
        markets = report["attention_markets"] if report else []
        return " ".join([
            "期权关注",
            *(option_attention_market_text(market) for market in markets),
        ])
    if broker == "eastmoney":
        return (
            "东方财富｜A股 当天趋势报告 报告日期 2026-07-15 数据截至 2026-07-14 "
            "生成时间 2026-07-15T20:00:00+08:00 账户状态 已更新 "
            "正式买入 1 全部卖出 1 继续持有 1 人工复核 1 "
            "优先处理 · 卖出触发 需要确认 · 人工复核 "
            "09:30–10:00 · 正式买入计划 "
            "盘中持续 · 已有持仓 筛选价（Trend Animals） "
            "执行参考价（Futu 前复权） 全部卖出 正式买入 继续持有 "
            "人工复核 买入纪律 卖出纪律 审计详情"
        )
    if broker == "phillips":
        return (
            "辉立｜港股 当天趋势报告 报告日期 2026-07-15 数据截至 2026-07-14 "
            "生成时间 2026-07-15T11:31:00+08:00 账户状态 已更新 "
            "正式买入 0 全部卖出 0 继续持有 0 人工复核 0 "
            "优先处理 · 卖出触发 需要确认 · 人工复核 "
            "09:30–10:00 · 正式买入计划 无 盘中持续 · 已有持仓 审计详情"
        )
    return (
        "老虎｜美股 当天趋势报告 报告日期 2026-07-15 数据截至 2026-07-14 "
        "生成时间 2026-07-15T11:30:36+08:00 账户状态 已更新 "
        "正式买入 1 全部卖出 1 继续持有 1 人工复核 1 "
        "优先处理 · 卖出触发 需要确认 · 人工复核 "
        "美股常规交易时段 · 正式买入计划 盘中持续 · 已有持仓 审计详情"
    )


def option_attention_market_status(market: dict[str, object]) -> str:
    status = market.get("data_status")
    if status == "current":
        return "今日已更新"
    if status == "stale":
        return f"数据截至 {market.get('data_date')}；今日未更新"
    return "暂时不可用"


def option_attention_market_text(market: dict[str, object]) -> str:
    items = market.get("items", [])
    assert isinstance(items, list)
    return " ".join([
        str(market.get("market_label")),
        option_attention_market_status(market),
        *(str(item.get("symbol")) for item in items),
    ])


def trend_review_workspace_text(broker: str) -> str:
    review = trend_reviews()[broker]
    snapshot = review["strategy_snapshot"]
    return (
        f"{review['broker_label']}｜{review['market_label']} "
        f"{review['market_label']}趋势复盘 {snapshot['strategy_name']}｜版本 v1 "
        "当前策略参数 仓位执行 持仓上限 10 笔 "
        "退出保护 初始保护线 成交均价减 2.0 倍 ATR14 "
        "收益与回撤 期间净收益率 相对市场超额收益 最大回撤 "
        "风险调整收益 卡玛比率 夏普比率 纪律模拟 实际执行 市场基准"
    )


def trend_stage_texts(broker: str) -> list[str]:
    if broker == "eastmoney":
        return [
            "优先处理 · 卖出触发\n601398 工商银行 全部卖出 7.2 温 → 温 "
            "91.3 右侧趋势已结束 7.0 强度 91.3，低于入场线 95",
            "需要确认 · 人工复核\n600036 招商银行 人工复核 45.2 热 → 热 "
            "97 持仓日线数据不可用 42.0 筛选价数据不可用",
            "09:30–10:00 · 正式买入计划\n688046 药康生物 正式买入 29.14 "
            "28.81 温 → 热 立夏 99.9 医疗服务 热 110 6 4% 27061.98 900 股 24.55",
            "盘中持续 · 已有持仓\n600900 长江电力 继续持有 28.0 热 → 热 "
            "98.7 趋势保持完好 27.8 不是新的温转热或温转沸入场信号",
        ]
    if broker == "phillips":
        return [
            "优先处理 · 卖出触发\n无",
            "需要确认 · 人工复核\n无",
            "09:30–10:00 · 正式买入计划\n无",
            "盘中持续 · 已有持仓\n无",
        ]
    return [
        "优先处理 · 卖出触发\nAAPL 苹果 全部卖出 200 99 危险信号触发 190",
        "需要确认 · 人工复核\nQQQ 纳指ETF 人工复核 — — 趋势信号不完整 — —",
        "美股常规交易时段 · 正式买入计划\nVIXY 波动率ETF 正式买入 19 98 ETF 4% 25,142.16 5,000 股 18.50",
        "盘中持续 · 已有持仓\nSPY 标普ETF 继续持有 510 97 趋势保持完好 500",
    ]


def trend_audit_text(broker: str) -> str:
    if broker == "eastmoney":
        return (
            "审计详情 完整候选审计 600000 浦发银行 强度 94 "
            "排除项 600000 趋势强度低于 95 行业集中度 无 "
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
            "完整候选审计 600000 浦发银行 强度 94",
            "排除项 600000 趋势强度低于 95",
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
        "富途 期权增强 · 跨市场期权关注 持仓资产 HKD 100 现金 HKD 20 持仓 1 "
        "来源 Futu 时间 2026-07-15 期权关注 美股 港股"
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

OPTION_ATTENTION_COLUMN_LABELS = [
    "标的",
    "分类",
    "右侧状态",
    "趋势温度",
    "趋势节气",
    "本地 / 全球强度",
    "上周 / 上月",
    "右侧天数 / 累计涨幅",
    "危险 / 沸腾 / 开香槟",
    "来源动作",
]


class TabbedAccountLocator:
    def __init__(self, page: "TabbedAccountPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "TabbedAccountLocator":
        return self

    def locator(self, selector: str) -> "TabbedAccountLocator":
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
        if self.selector == "#open-kelly-lab":
            self.page.workspace_view = "kelly"
            return
        if self.selector == "#open-standard-backtest":
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
        target_selectors = {
            '#account-tabs [role="tab"]:visible, #header-market-filters button:visible, '
            ".strategy-tools button:visible, #refresh-quotes:visible, "
            ".broker-summary-card:visible, .account-holding-actions button:visible, "
            ".trend-report-entry button:visible",
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
        if self.selector in VISUAL_CONTRACT_STYLES:
            return 1
        if self.selector == "#open-kelly-lab":
            return 1
        if self.selector == ".kelly-lab-panel:visible":
            return int(self.page.workspace_view == "kelly")
        if self.selector == "#open-standard-backtest":
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
                self.page.trend_broker is None and self.page.selected == broker
            )
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
                f'{entry} button:has-text("期权关注")',
            }:
                continue
            if (
                self.page.trend_broker is not None
                or self.page.selected != broker
            ):
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
        if self.selector == (
            "#trend-report-workspace:visible .option-attention-table tbody"
        ):
            return len(self.page.option_attention_header_spans)
        if self.selector == (
            '#trend-report-workspace:visible .option-attention-table '
            'thead th[scope="col"]'
        ):
            return len(self.page.option_attention_column_headers)
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.option-attention-table "
            r"tbody:nth\((\d+)\) \.option-attention-market-content span",
            self.selector,
        )
        if match:
            return len(self.page.option_attention_header_spans[int(match.group(1))])
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.option-attention-table "
            r"tbody:nth\((\d+)\) \.option-attention-row",
            self.selector,
        )
        if match:
            return len(self.page.option_attention_row_labels[int(match.group(1))])
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.option-attention-table "
            r"tbody:nth\((\d+)\) \.option-attention-row:nth\((\d+)\) td",
            self.selector,
        )
        if match:
            market_index, row_index = map(int, match.groups())
            return len(self.page.option_attention_row_labels[market_index][row_index])
        if self.selector == "#return-to-portfolio:visible":
            return int(self.page.trend_broker is not None)
        if self.selector == "#trend-report-workspace:visible [data-close-trend-report]":
            return int(self.page.trend_broker is not None)
        if self.selector == "#trend-report-workspace:visible .trend-review-chart":
            return 2 if self.page.trend_kind == "review" else 0
        if self.selector == "#trend-report-workspace:visible .trend-review-parameter-table > div":
            review = self.page.reviews.get(str(self.page.trend_broker), {})
            snapshot = review.get("strategy_snapshot", {})
            rows = snapshot.get("parameter_rows", []) if isinstance(snapshot, dict) else []
            return len(rows) if self.page.trend_kind == "review" else 0
        if self.selector == ".workspace-grid:visible":
            return int(self.page.trend_broker is None)
        if self.selector == "#trend-report-workspace:visible .cn-trend-report":
            return int(self.page.trend_broker is not None)
        if self.selector == "#trend-report-workspace:visible .trend-discipline[open]":
            return int(self.page.trend_broker == "eastmoney") * (
                0 if self.page.viewport_size["width"] <= 760 else 2
            )
        if self.selector == "#trend-report-workspace:visible .trend-discipline":
            return 2 if self.page.trend_broker == "eastmoney" else 0
        if self.selector == "#trend-report-workspace:visible .cn-trend-table":
            return 4 if self.page.trend_broker is not None else 0
        if self.selector in {
            "#trend-report-workspace:visible .cn-trend-execution",
            "#trend-report-workspace:visible .cn-trend-execution span:first-child",
        }:
            report = self.page.reports.get(str(self.page.trend_broker), {})
            return sum(
                len(actions) if isinstance(actions, list) else 0
                for actions in (report.get("sell_actions"), report.get("buy_actions"))
            )
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
        if re.fullmatch(
            r'#trend-report-workspace:visible \.cn-trend-buy '
            r'\.cn-trend-card:nth\(\d+\) td\[data-label="'
            r'(行业|筛选价（Trend Animals）|执行参考价（Futu 前复权）)"\]',
            self.selector,
        ):
            return 1
        raise AssertionError(f"unknown count selector: {self.selector}")

    def get_attribute(self, name: str) -> str | None:
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.option-attention-table "
            r"tbody:nth\((\d+)\) \.option-attention-row:nth\((\d+)\) "
            r"td:nth\((\d+)\)",
            self.selector,
        )
        if match:
            assert name == "data-label"
            market_index, row_index, cell_index = map(int, match.groups())
            return self.page.option_attention_row_labels[market_index][row_index][
                cell_index
            ]
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
        if self.selector == "#trend-report-workspace:visible .cn-trend-buy":
            mobile = self.page.viewport_size["width"] <= 760
            return {
                "tabindex": "-1" if mobile else "0",
                "aria-label": (
                    "正式买入计划" if mobile else "正式买入计划，可横向滚动"
                ),
            }[name]
        assert self.selector == "#trend-report-workspace:visible .trend-audit"
        assert name == "open"
        return None

    def focus(self) -> None:
        self.page.active = self.selector
        self.page.focus_checks.append(self.selector)

    def is_disabled(self) -> bool:
        match = re.fullmatch(
            r'#account-(\w+):visible \.trend-report-entry button'
            r'(?:\:has-text\("(?:当天趋势报告|期权关注)"\))?',
            self.selector,
        )
        assert match
        broker = self._require_known_broker(match.group(1))
        self.page.disabled_reports.add(broker)
        return not bool(self.page.reports[broker]["available"])

    def inner_text(self) -> str:
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
        if self.selector == "#trend-report-workspace:visible":
            if self.page.trend_kind == "review":
                return trend_review_workspace_text(str(self.page.trend_broker))
            broker = str(self.page.trend_broker)
            return self.page.workspace_texts[broker]
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.option-attention-table "
            r"tbody:nth\((\d+)\)",
            self.selector,
        )
        if match:
            index = int(match.group(1))
            return " ".join([
                *self.page.option_attention_header_spans[index],
                *self.page.option_attention_symbol_cells[index],
            ])
        if self.selector == "#trend-report-workspace:visible .trend-audit":
            return trend_audit_text(str(self.page.trend_broker))
        match = re.fullmatch(
            r"#account-(\w+):visible \.account-empty:visible", self.selector
        )
        if match and match.group(1) in self.page.tab_order:
            return "当前筛选下没有持仓"
        if self.selector == "#visible-count":
            return f"{self.page.visible_rows():,} 条"
        if self.selector == "#last-refresh":
            return "刷新于 2026-07-15 15:03:13 CST"
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
            '#trend-report-workspace:visible .option-attention-table '
            'thead th[scope="col"]'
        ):
            return self.page.option_attention_column_headers
        match = re.fullmatch(
            r"#trend-report-workspace:visible \.option-attention-table "
            r"tbody:nth\((\d+)\) \.option-attention-market-content span",
            self.selector,
        )
        if match:
            return self.page.option_attention_header_spans[int(match.group(1))]
        match = re.fullmatch(
            r'#trend-report-workspace:visible \.option-attention-table '
            r'tbody:nth\((\d+)\) \.option-attention-row td\[data-label="标的"\]',
            self.selector,
        )
        if match:
            return self.page.option_attention_symbol_cells[int(match.group(1))]
        if self.selector == "a:visible, button:visible":
            return ["刷新账户与行情", "策略回测"]
        broker = str(self.page.trend_broker)
        if self.selector == "#trend-report-workspace:visible .trend-review-parameter-table > div":
            rows = self.page.reviews[broker]["strategy_snapshot"]["parameter_rows"]
            return [f"{row['group']} {row['name']} {row['value']}" for row in rows]
        if self.selector == "#trend-report-workspace:visible .trend-review-chart figcaption":
            return ["收益与回撤", "风险调整收益"]
        if self.selector == "#trend-report-workspace:visible .trend-review-metric h3":
            return ["期间净收益率", "相对市场超额收益", "最大回撤", "卡玛比率", "夏普比率"]
        if self.selector == "#trend-report-workspace:visible .cn-trend-stage":
            return trend_stage_texts(broker)
        if self.selector == "#trend-report-workspace:visible .trend-stage":
            return trend_stage_texts(broker)
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
        if self.selector == "#trend-report-workspace:visible .trend-discipline summary":
            return ["买入纪律", "卖出纪律"]
        if self.selector == (
            "#trend-report-workspace:visible .cn-trend-execution span:first-child"
        ):
            report = self.page.reports[broker]
            actions = [
                *report.get("sell_actions", []),
                *report.get("buy_actions", []),
            ]
            labels = {
                "pending": "待执行",
                "submitted": "已提交",
                "partially_filled": "部分成交",
                "filled": "全部成交",
                "failed": "失败",
                "blocked": "受阻",
                "missed": "错过",
                "incomplete": "未完成",
                "early_revision_executed": "早期版本已执行",
            }
            return [
                labels.get((action.get("execution") or {}).get("status"), "待执行")
                for action in actions
            ]
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
        self.reports = (payload or valid_payload())["trend_reports"]  # type: ignore[assignment,index]
        self.reviews = (payload or valid_payload())["trend_reviews"]  # type: ignore[assignment,index]
        self.section_texts = dict(ACCOUNT_SECTION_TEXTS)
        self.entry_texts = {
            broker: (
                (
                    "期权关注 美股 港股"
                    if broker == "futu"
                    else f"当天趋势报告 报告日期 {report.get('report_date', '-')} "
                    f"数据截至 {report.get('data_date', '-')}"
                )
                if report.get("available") is True
                else f"{'期权关注' if broker == 'futu' else '当天趋势报告'} {report.get('status_text', '')}"
            )
            for broker, report in self.reports.items()
        }
        self.workspace_texts = {
            broker: trend_workspace_text(broker, report)
            for broker, report in self.reports.items()
        }
        markets = self.reports["futu"]["attention_markets"]
        self.option_attention_header_spans = [
            [str(market.get("market_label")), option_attention_market_status(market)]
            for market in markets
        ]
        self.option_attention_symbol_cells = [
            [f"{item.get('symbol')} 标的名称" for item in market.get("items", [])]
            for market in markets
        ]
        self.option_attention_column_headers = list(OPTION_ATTENTION_COLUMN_LABELS)
        self.option_attention_row_labels = [
            [list(OPTION_ATTENTION_COLUMN_LABELS) for _item in market.get("items", [])]
            for market in markets
        ]
        self.option_attention_column_counts: list[int] | None = None
        self.option_attention_grid_checks: list[str | None] = []
        self.all_rows = {"futu": 1, "tiger": 1, "phillips": 1, "eastmoney": 0}
        self.cn_rows = cn_rows or {"futu": 0, "tiger": 0, "phillips": 0, "eastmoney": 5}
        self.market = "ALL"
        self.selected = "futu"
        self.tab_order = ["futu", "tiger", "phillips", "eastmoney"]
        self.selected_brokers: list[str] = []
        self.visible_account_sections = 1
        self.max_visible_account_sections = 1
        self.trend_broker: str | None = None
        self.trend_kind = ""
        self.active: str | None = None
        self.opened_reports: list[str] = []
        self.opened_reviews: list[str] = []
        self.disabled_reports: set[str] = set()
        self.focus_checks: list[str] = []
        self.target_checks: list[str] = []
        self.bounds_checks: list[str] = []
        self.undersized_target_selector = ""
        self.overflow_bounds_selector = ""
        self.document_overflow_broker = ""
        self.document_overflow_checks: list[str | None] = []
        self.workspace_view = "portfolio"
        self.research_open = False
        self.script_evaluations: list[tuple[str, object | None]] = []

    def _record_visible_sections(self) -> int:
        visible = self.visible_account_sections if self.trend_broker is None else 0
        self.max_visible_account_sections = max(
            self.max_visible_account_sections, visible
        )
        return visible

    def visible_rows(self, selector: str = "") -> int:
        match = re.search(r"#account-(\w+):visible", selector)
        broker = match.group(1) if match else self.selected
        rows = self.cn_rows if self.market == "CN" else self.all_rows
        return rows[broker]

    def locator(self, selector: str) -> TabbedAccountLocator:
        return TabbedAccountLocator(self, selector)

    def evaluate(
        self, expression: str, argument: object | None = None,
    ) -> bool | list[int] | None:
        if "openResearchChat" in expression:
            self.script_evaluations.append((expression, argument))
            self.research_open = True
            return None
        if "gridTemplateColumns" in expression:
            self.option_attention_grid_checks.append(self.trend_broker)
            counts = self.option_attention_column_counts
            if counts is None:
                column_count = 1 if self.viewport_size["width"] <= 460 else 2
                counts = [
                    column_count
                    for rows in self.option_attention_row_labels
                    for _row in rows
                ]
            return counts
        assert expression == "document.documentElement.scrollWidth <= window.innerWidth"
        self.document_overflow_checks.append(self.trend_broker)
        return self.trend_broker != self.document_overflow_broker

    def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 500


def tabbed_account_page(payload: dict[str, object]) -> TabbedAccountPage:
    return TabbedAccountPage(payload)


def tabbed_cn_page() -> TabbedAccountPage:
    return TabbedAccountPage(cn_rows={
        "futu": 1, "tiger": 0, "phillips": 1, "eastmoney": 0,
    })


def test_check_trend_audit_uses_unknown_when_both_api_costs_are_null() -> None:
    class Locator:
        def __init__(self, selector: str = "audit") -> None:
            self.selector = selector

        def count(self) -> int:
            return 1

        def get_attribute(self, _name: str) -> None:
            return None

        def locator(self, selector: str) -> "Locator":
            return Locator(selector)

        def click(self) -> None:
            return None

        def all_inner_texts(self) -> list[str]:
            assert self.selector == "section"
            return ["候选榜 无", "排除项 无", "账户不参与项 无", "行业集中度 无"]

        def inner_text(self) -> str:
            return "审计详情 API 成本：未知"

    report = {
        "audit": {
            "candidates": [],
            "excluded": {},
            "industry_concentration": [],
            "data_sources": [],
            "actual_api_cost": None,
            "estimated_api_cost": None,
        },
    }

    dashboard_acceptance._check_trend_audit(Locator(), report, "futu")


def nested_get(row: dict[str, object], path: tuple[str, ...]) -> dict[str, object]:
    value: object = row
    for key in path:
        value = value[key]  # type: ignore[index]
    return value  # type: ignore[return-value]


@pytest.mark.parametrize("path", REQUIRED_SOURCE_PATHS)
def test_validate_dashboard_payload_rejects_each_missing_current_source(
    path: tuple[str, ...],
) -> None:
    payload = valid_payload()
    source = nested_get(payload["holdings"][-1], path)  # type: ignore[index]
    source["available"] = False
    source["status"] = "stale_source_hash"

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert any("US.MSFT" in error and path[-1] in error for error in errors)


def test_validate_dashboard_payload_ignores_missing_sources_without_current_advice() -> None:
    payload = valid_payload()
    payload["holdings"][0]["tradingagents_summary"] = {  # type: ignore[index]
        "available": False,
        "status": "stale_source_hash",
    }

    assert validate_dashboard_payload(payload, expected_cn=5) == []


def test_validate_dashboard_payload_accepts_explicitly_unsupported_source() -> None:
    payload = valid_payload()
    source = payload["holdings"][-1]["futu_skill_facts"]["technical_anomaly"]  # type: ignore[index]
    source.update(
        available=False,
        unsupported=True,
        status="error",
        summary="富途接口不支持技术异动：US.MSFT",
    )

    assert validate_dashboard_payload(payload, expected_cn=5) == []


def test_first_in_scope_holding_returns_exact_market_and_symbol() -> None:
    assert dashboard_acceptance._first_in_scope_holding(valid_payload()) == ("US", "MSFT", "tiger")
    assert dashboard_acceptance._dashboard_holding_key(
        valid_payload(), "US", "MSFT"
    ) == "US:MSFT::5"


def test_first_in_scope_holding_rejects_payload_without_current_advice() -> None:
    payload = valid_payload()
    payload["holdings"][-1]["agent_report"]["available"] = False  # type: ignore[index]

    with pytest.raises(AssertionError, match="advice-backed holding"):
        dashboard_acceptance._first_in_scope_holding(payload)


def test_acceptance_opens_real_tool_workspaces_and_checks_mobile_targets() -> None:
    class Locator:
        def __init__(self, page: "Page", selector: str) -> None:
            self.page = page
            self.selector = selector

        def count(self) -> int:
            target_selectors = {
                '#account-tabs [role="tab"]:visible, #header-market-filters button:visible, '
                ".strategy-tools button:visible, #refresh-quotes:visible, "
                ".broker-summary-card:visible, .account-holding-actions button:visible, "
                ".trend-report-entry button:visible",
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
                "#open-kelly-lab": 1,
                ".kelly-lab-panel:visible": int(self.page.view == "kelly"),
                "#return-to-portfolio:visible": int(self.page.view != "portfolio"),
                "#open-standard-backtest": 1,
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
            if self.selector == "#open-kelly-lab":
                self.page.view = "kelly"
            elif self.selector == "#open-standard-backtest":
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

        def __init__(self) -> None:
            self.view = "portfolio"
            self.research_open = False
            self.clicks: list[str] = []
            self.evaluations: list[tuple[str, object | None]] = []
            self.target_checks: list[str] = []

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
        "#open-kelly-lab", "#return-to-portfolio:visible",
        "#open-standard-backtest", "#return-to-portfolio:visible",
        "#research-chat-close:visible",
    ]
    assert len(page.evaluations) == 1
    assert page.target_checks == [
        "#account-tabs [role=\"tab\"]:visible, #header-market-filters button:visible, "
        ".strategy-tools button:visible, #refresh-quotes:visible, "
        ".broker-summary-card:visible, .account-holding-actions button:visible, "
        ".trend-report-entry button:visible",
        ".symbol-detail-panel.inline-symbol-detail:visible button:visible, "
        ".symbol-detail-panel.inline-symbol-detail:visible input:visible, "
        ".symbol-detail-panel.inline-symbol-detail:visible select:visible",
        "#return-to-portfolio:visible, .kelly-lab-panel button:visible",
        "#standard-backtest-workspace button:visible, "
        "#standard-backtest-workspace input:visible, "
        "#standard-backtest-workspace select:visible",
        ".research-chat-modal button:visible, .research-chat-modal input:visible",
    ]


@pytest.mark.parametrize(
    "selector",
    (
        ".broker-summary-card:visible",
        ".symbol-detail-panel.inline-symbol-detail:visible .language-toggle button:visible",
        ".trend-report-entry button:visible",
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


def test_check_decision_tabs_uses_exact_holding_and_checks_every_panel() -> None:
    selectors: list[str] = []
    clicks: list[str] = []

    class Locator:
        def __init__(
            self, kind: str, index: int = 0, visible: tuple[bool, ...] = (True,),
        ) -> None:
            self.kind = kind
            self.index = index
            self.visible = visible

        def count(self) -> int:
            return {
                "button": len(self.visible), "tabs": 5, "failed": 0, "panel": 1,
                "account-tab": 1, "account-section": 1, "account-sections": 1,
            }[self.kind]

        @property
        def first(self) -> "Locator":
            return Locator(self.kind, self.index, self.visible[:1])

        def click(self) -> None:
            if self.kind == "button":
                assert self.visible[0], "clicked hidden duplicate"
            clicks.append(self.kind)

        def all_inner_texts(self) -> list[str]:
            return ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]

        def nth(self, index: int) -> "Locator":
            return Locator("tab", index)

        def get_attribute(self, name: str) -> str:
            if self.kind == "account-tab":
                assert name == "aria-selected"
                return "true"
            assert name == "aria-controls"
            return f"decision-panel-{self.index}"

        def inner_text(self) -> str:
            if self.index == 0:
                return "回测闸门 夏普比率 1.2 卡玛比率 0.8"
            if self.index == 2:
                return "当前价 710.55"
            return "source data"

    class Page:
        def locator(self, selector: str) -> Locator:
            selectors.append(selector)
            if selector == '#account-tabs [data-broker="tiger"]':
                return Locator("account-tab")
            if selector == "#account-tiger:visible":
                return Locator("account-section")
            if selector == ".account-section:visible":
                return Locator("account-sections")
            button_selector = (
                'button[data-detail-mode="decision"]'
                '[data-detail-market="US"]'
                '[data-detail-symbol="MSFT"]'
            )
            if selector == button_selector:
                return Locator("button", visible=(False, True))
            if selector == f"{button_selector}:visible":
                return Locator("button")
            if selector == ".decision-tab-list [data-decision-tab]":
                return Locator("tabs")
            if selector == ".decision-tab-list .decision-tab-failed":
                return Locator("failed")
            match = re.search(r"decision-panel-(\d+)", selector)
            return Locator("panel", int(match.group(1)) if match else 0)

    dashboard_acceptance._check_decision_tabs(Page(), "US", "MSFT", "tiger")

    assert selectors[0] == '#account-tabs [data-broker="tiger"]'
    assert selectors[3] == (
        'button[data-detail-mode="decision"]'
        '[data-detail-market="US"]'
        '[data-detail-symbol="MSFT"]:visible'
    )
    assert clicks == ["account-tab", "button", "tab", "tab", "tab", "tab", "tab"]


def test_check_decision_tabs_rejects_stale_initial_panel_after_tab_click() -> None:
    class Locator:
        def __init__(self, kind: str, index: int = 0) -> None:
            self.kind = kind
            self.index = index

        def count(self) -> int:
            if self.kind in {"button", "initial-panel", "account-tab", "account-section", "account-sections"}:
                return 1
            if self.kind == "tabs":
                return 5
            return 0

        @property
        def first(self) -> "Locator":
            return self

        def click(self) -> None:
            pass

        def all_inner_texts(self) -> list[str]:
            return ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]

        def nth(self, index: int) -> "Locator":
            return Locator("tab", index)

        def get_attribute(self, name: str) -> str:
            if self.kind == "account-tab":
                assert name == "aria-selected"
                return "true"
            assert name == "aria-controls"
            return f"decision-panel-{self.index}"

        def inner_text(self) -> str:
            return "source data 夏普比率 1.2 卡玛比率 0.8"

    class Page:
        def locator(self, selector: str) -> Locator:
            if selector == '#account-tabs [data-broker="futu"]':
                return Locator("account-tab")
            if selector == "#account-futu:visible":
                return Locator("account-section")
            if selector == ".account-section:visible":
                return Locator("account-sections")
            if selector.startswith('button[data-detail-mode="decision"]'):
                return Locator("button")
            if selector == ".decision-tab-list [data-decision-tab]":
                return Locator("tabs")
            if selector == ".decision-tab-panel:visible":
                return Locator("initial-panel")
            if selector == "#decision-panel-0:visible":
                return Locator("initial-panel")
            return Locator("missing")

    with pytest.raises(AssertionError, match="TradingAgents"):
        dashboard_acceptance._check_decision_tabs(Page(), "US", "MSFT", "futu")


def test_acceptance_formats_grouped_numeric_expectations_without_touching_text() -> None:
    assert dashboard_acceptance._display_number("5000") == "5,000"
    assert dashboard_acceptance._display_number("25142.16") == "25,142.16"
    assert dashboard_acceptance._display_number("+25142.16") == "+25,142.16"
    for value in ("02840", "2026-07-16", "21.13%", "等待确认"):
        assert dashboard_acceptance._plain(value) == value

    dashboard_acceptance._check_action_trend_stages(
        [
            "优先处理 · 卖出触发 无",
            "需要确认 · 人工复核 无",
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
    "#refresh-quotes": {
        "backgroundColor": "rgb(139, 94, 52)",
        "borderTopColor": "rgb(139, 94, 52)",
    },
    ".current-view-card": {
        "backgroundColor": "rgb(36, 33, 29)",
        "borderTopColor": "rgb(36, 33, 29)",
    },
    "#last-refresh": {
        "color": "rgb(116, 110, 100)",
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
            return int(self.selector in VISUAL_CONTRACT_STYLES)

        def focus(self) -> None:
            assert self.selector in VISUAL_CONTRACT_STYLES
            self.page.focused_selectors.append(self.selector)

        def evaluate(self, expression: str) -> dict[str, str]:
            assert self.selector in VISUAL_CONTRACT_STYLES
            self.page.evaluated_selectors.append(self.selector)
            if "outlineColor" in expression:
                assert self.selector == "#refresh-quotes"
                return {
                    "outlineColor": "rgb(139, 94, 52)",
                    "outlineStyle": "solid", "outlineWidth": "3px",
                }
            assert "backgroundColor" in expression
            return dict(VISUAL_CONTRACT_STYLES[self.selector])

    class Page:
        def __init__(self) -> None:
            self.expected = dict(dashboard_acceptance.WARM_LEDGER_TOKENS)
            self.expected["--accent"] = accent
            self.token_evaluations: list[list[str]] = []
            self.evaluated_selectors: list[str] = []
            self.focused_selectors: list[str] = []

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
        "#refresh-quotes",
    ]
    assert page.focused_selectors == ["#refresh-quotes"]  # type: ignore[attr-defined]


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
    visual_focus_calls: list[tuple[str, str]] = []
    visual_focus_evaluations: list[tuple[str, str]] = []
    geometry_evaluations: list[str] = []
    buy_overflow_evaluations: list[str] = []
    state = {"fail_wide_desktop_navigation": True}

    class Locator(TabbedAccountLocator):
        def click(self) -> None:
            clicks.append((self.page.name, self.selector))  # type: ignore[attr-defined]
            super().click()

        def focus(self) -> None:
            if self.selector == "#refresh-quotes":
                visual_focus_calls.append((self.page.name, self.selector))  # type: ignore[attr-defined]
                return
            super().focus()

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
                if "outlineColor" in expression:
                    assert self.selector == "#refresh-quotes"
                    visual_focus_evaluations.append(
                        (self.page.name, self.selector)  # type: ignore[attr-defined]
                    )
                    return {
                        "outlineColor": "rgb(139, 94, 52)",
                        "outlineStyle": "solid",
                        "outlineWidth": "3px",
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
    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_decision_tabs",
        lambda *_args: None,
    )
    def check_trend_views(
        page: Page,
        _payload: object,
        _simulate_payloads: object,
        _history_expectations: object,
        *,
        screenshot_dir: Path,
    ) -> None:
        width = page.viewport_size["width"]
        page.screenshot(
            path=str(screenshot_dir / f"{width}-trend-report.png"),
            full_page=True,
        )

    monkeypatch.setattr(
        dashboard_acceptance, "_check_trend_account_views", check_trend_views
    )
    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, payload, simulate_payloads={}, history_expectations={}
    )

    assert errors == [
        "wide_desktop：RuntimeError: navigation failed",
        "验收截图缺失：wide_desktop-portfolio.png",
        "验收截图缺失：1920-trend-report.png",
    ]
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
    visual_focus_calls.clear()
    visual_focus_evaluations.clear()
    geometry_evaluations.clear()
    buy_overflow_evaluations.clear()
    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_decision_tabs",
        lambda *_args: (_ for _ in ()).throw(AssertionError("decision failed")),
    )

    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, payload, simulate_payloads={}, history_expectations={}
    )

    assert errors == [
        "wide_desktop：AssertionError: decision failed",
        "desktop：AssertionError: decision failed",
        "tablet：AssertionError: decision failed",
        "mobile：AssertionError: decision failed",
    ]
    assert blocker is None
    for viewport in ("wide_desktop", "desktop", "tablet", "mobile"):
        assert (viewport, '#broker-summary-cards [data-broker="phillips"]') in selectors
        assert (viewport, '[data-market="CN"]') in selectors
        assert (viewport, '[data-market="CN"]') in clicks
        assert (viewport, 'button[data-broker="eastmoney"]') not in selectors
        assert (viewport, '#visible-count') in selectors
        assert (viewport, '#last-refresh') in selectors
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
            '#account-futu:visible .trend-report-entry [data-trend-report]',
        ) in clicks
        assert (viewport, '#return-to-portfolio:visible') in clicks
        assert (viewport, '#trend-report-workspace:visible') in selectors
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
            "#trend-report-workspace:visible .option-attention-workspace, "
            "#trend-report-workspace:visible .option-attention-table, "
            "#trend-report-workspace:visible .option-attention-market, "
            "#trend-report-workspace:visible .option-attention-row",
        ) in selectors
    assert set(evaluated) == {"wide_desktop", "desktop", "tablet", "mobile"}
    assert visual_token_evaluations == [
        "wide_desktop", "desktop", "tablet", "mobile",
    ]
    for viewport in ("wide_desktop", "desktop", "tablet", "mobile"):
        assert [
            selector
            for name, selector in visual_surface_evaluations
            if name == viewport
        ] == list(VISUAL_CONTRACT_STYLES)
        assert (viewport, "#refresh-quotes") in visual_focus_calls
        assert (viewport, "#refresh-quotes") in visual_focus_evaluations
    assert geometry_evaluations == []
    assert buy_overflow_evaluations == []
    screenshot_dir = dashboard_acceptance.ACCEPTANCE_SCREENSHOT_DIR
    assert screenshots == [
        ("wide_desktop", str(screenshot_dir / "wide_desktop-portfolio.png")),
        ("wide_desktop", str(screenshot_dir / "1920-trend-report.png")),
        ("desktop", str(screenshot_dir / "desktop-portfolio.png")),
        ("desktop", str(screenshot_dir / "1440-trend-report.png")),
        ("tablet", str(screenshot_dir / "tablet-portfolio.png")),
        ("tablet", str(screenshot_dir / "760-trend-report.png")),
        ("mobile", str(screenshot_dir / "mobile-portfolio.png")),
        ("mobile", str(screenshot_dir / "375-trend-report.png")),
    ]


def test_validate_dashboard_payload_accepts_real_contract() -> None:
    assert validate_dashboard_payload(valid_payload(), expected_cn=5) == []


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
    assert page.opened_reports == ["futu"]
    assert page.opened_reviews == []
    assert page.disabled_reports == set()
    assert projections == ["tiger", "phillips", "eastmoney"]
    assert page.focus_checks == [
        "#return-to-portfolio:visible",
        '#account-futu:visible .trend-report-entry [data-trend-report]',
    ]


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


def test_option_attention_acceptance_checks_current_and_stale_status_text() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)

    dashboard_acceptance._check_account_holdings(page, payload)

    assert "今日已更新" in page.workspace_texts["futu"]
    assert "2026-07-15" not in page.workspace_texts["futu"]
    assert "数据截至 2026-07-14；今日未更新" in page.workspace_texts["futu"]
    assert page.option_attention_header_spans == [
        ["美股", "今日已更新"],
        ["港股", "数据截至 2026-07-14；今日未更新"],
    ]
    assert page.option_attention_symbol_cells == [
        ["VIXY 标的名称"], ["00700 标的名称"],
    ]
    assert page.option_attention_column_headers == OPTION_ATTENTION_COLUMN_LABELS
    assert page.option_attention_row_labels == [
        [OPTION_ATTENTION_COLUMN_LABELS],
        [OPTION_ATTENTION_COLUMN_LABELS],
    ]


@pytest.mark.parametrize("width", (760, 375))
def test_option_attention_acceptance_checks_valid_responsive_geometry(
    width: int,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": width, "height": 844}

    dashboard_acceptance._check_account_holdings(page, payload)

    assert (
        "#return-to-portfolio:visible, "
        "#trend-report-workspace:visible button:visible, "
        "#trend-report-workspace:visible summary:visible"
    ) in page.target_checks
    assert (
        "#trend-report-workspace:visible .option-attention-workspace, "
        "#trend-report-workspace:visible .option-attention-table, "
        "#trend-report-workspace:visible .option-attention-market, "
        "#trend-report-workspace:visible .option-attention-row"
    ) in page.bounds_checks
    assert "futu" in page.document_overflow_checks
    assert page.option_attention_grid_checks == ["futu"]


@pytest.mark.parametrize(
    ("width", "column_counts"),
    (
        (760, [2, 1]),
        (375, [1, 2]),
    ),
)
def test_option_attention_acceptance_rejects_wrong_responsive_column_count(
    width: int,
    column_counts: list[int],
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": width, "height": 844}
    page.option_attention_column_counts = column_counts

    with pytest.raises(AssertionError, match="期权关注.*列"):
        dashboard_acceptance._check_account_holdings(page, payload)


def test_option_attention_acceptance_rejects_undersized_mobile_return() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": 375, "height": 844}
    page.undersized_target_selector = (
        "#return-to-portfolio:visible, "
        "#trend-report-workspace:visible button:visible, "
        "#trend-report-workspace:visible summary:visible"
    )

    with pytest.raises(AssertionError, match="高度不足 44px"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize("overflow", ("document", "workspace"))
def test_option_attention_acceptance_rejects_mobile_workspace_overflow(
    overflow: str,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.viewport_size = {"width": 375, "height": 844}
    if overflow == "document":
        page.document_overflow_broker = "futu"
    else:
        page.overflow_bounds_selector = (
            "#trend-report-workspace:visible .option-attention-workspace, "
            "#trend-report-workspace:visible .option-attention-table, "
            "#trend-report-workspace:visible .option-attention-market, "
            "#trend-report-workspace:visible .option-attention-row"
        )

    with pytest.raises(AssertionError, match="横向|超出"):
        dashboard_acceptance._check_account_holdings(page, payload)


def test_option_attention_acceptance_rejects_reordered_column_headings() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.option_attention_column_headers[0:2] = reversed(
        page.option_attention_column_headers[0:2]
    )

    with pytest.raises(AssertionError, match="期权关注.*列标题"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize("mutation", ("duplicate", "omission"))
def test_option_attention_acceptance_rejects_duplicate_or_missing_column_heading(
    mutation: str,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    if mutation == "duplicate":
        page.option_attention_column_headers.insert(
            1, page.option_attention_column_headers[0]
        )
    else:
        page.option_attention_column_headers.pop()

    with pytest.raises(AssertionError, match="期权关注.*列标题"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize(
    "mutation", ("changed", "reordered", "duplicate", "omission")
)
def test_option_attention_acceptance_rejects_invalid_row_data_labels(
    mutation: str,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    labels = page.option_attention_row_labels[0][0]
    if mutation == "changed":
        labels[0] = "错误标签"
    elif mutation == "reordered":
        labels[0:2] = reversed(labels[0:2])
    elif mutation == "duplicate":
        labels.insert(1, labels[0])
    else:
        labels.pop()

    with pytest.raises(AssertionError, match="期权关注.*列标签"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize("affix", ("错误前缀：", "（错误后缀）"))
def test_option_attention_acceptance_rejects_status_affixes(affix: str) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    status = page.option_attention_header_spans[0][1]
    page.option_attention_header_spans[0][1] = (
        f"{status}{affix}" if affix.startswith("（") else f"{affix}{status}"
    )

    with pytest.raises(AssertionError, match="期权关注.*状态"):
        dashboard_acceptance._check_account_holdings(page, payload)


def test_option_attention_acceptance_rejects_extra_header_span() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.option_attention_header_spans[0].append("多余状态")

    with pytest.raises(AssertionError, match="期权关注.*状态"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize(
    "missing_status",
    ("今日已更新", "数据截至 2026-07-14；今日未更新"),
)
def test_option_attention_acceptance_rejects_missing_market_status(
    missing_status: str,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.option_attention_header_spans = [
        [label, status.replace(missing_status, "状态缺失")]
        for label, status in page.option_attention_header_spans
    ]

    with pytest.raises(AssertionError, match="期权关注.*状态"):
        dashboard_acceptance._check_account_holdings(page, payload)


def test_option_attention_acceptance_checks_unavailable_without_data_date() -> None:
    payload = valid_payload()
    unavailable = payload["trend_reports"]["futu"]["attention_markets"][0]  # type: ignore[index]
    unavailable.update(data_status="unavailable")
    unavailable.pop("data_date")
    page = tabbed_account_page(payload)

    dashboard_acceptance._check_account_holdings(page, payload)

    assert "暂时不可用" in page.workspace_texts["futu"]


def test_option_attention_acceptance_accepts_empty_unavailable_markets() -> None:
    payload = valid_payload()
    markets = payload["trend_reports"]["futu"]["attention_markets"]  # type: ignore[index]
    for market in markets:
        market.update(data_status="unavailable", items=[])
        market.pop("data_date", None)
    page = tabbed_account_page(payload)

    dashboard_acceptance._check_account_holdings(page, payload)

    assert page.option_attention_row_labels == [[], []]


def test_option_attention_acceptance_rejects_missing_unavailable_status() -> None:
    payload = valid_payload()
    unavailable = payload["trend_reports"]["futu"]["attention_markets"][0]  # type: ignore[index]
    unavailable.update(data_status="unavailable")
    unavailable.pop("data_date")
    page = tabbed_account_page(payload)
    page.option_attention_header_spans[0][1] = "状态缺失"

    with pytest.raises(AssertionError, match="期权关注.*状态"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize("swapped", ("status", "symbol"))
def test_option_attention_acceptance_rejects_swapped_market_content(
    swapped: str,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    if swapped == "status":
        page.option_attention_header_spans[0][1], page.option_attention_header_spans[1][1] = (
            page.option_attention_header_spans[1][1],
            page.option_attention_header_spans[0][1],
        )
    else:
        page.option_attention_symbol_cells.reverse()

    with pytest.raises(AssertionError, match="futu 期权关注"):
        dashboard_acceptance._check_account_holdings(page, payload)


def test_option_attention_acceptance_rejects_cross_market_duplication() -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    page.option_attention_symbol_cells[0].append("00700 腾讯")
    page.option_attention_symbol_cells[1].append("VIXY 波动率ETF")

    with pytest.raises(AssertionError, match="标的"):
        dashboard_acceptance._check_account_holdings(page, payload)


@pytest.mark.parametrize("mutation", ("extra", "omission"))
def test_option_attention_acceptance_rejects_extra_or_missing_symbol(
    mutation: str,
) -> None:
    payload = valid_payload()
    page = tabbed_account_page(payload)
    if mutation == "extra":
        page.option_attention_symbol_cells[0].append("QQQ 纳指ETF")
    else:
        page.option_attention_symbol_cells[0].clear()

    with pytest.raises(AssertionError, match="标的"):
        dashboard_acceptance._check_account_holdings(page, payload)


def test_option_attention_acceptance_rejects_unknown_data_status() -> None:
    payload = valid_payload()
    market = payload["trend_reports"]["futu"]["attention_markets"][0]  # type: ignore[index]
    market["data_status"] = "future"
    page = tabbed_account_page(payload)

    with pytest.raises(AssertionError, match="数据状态"):
        dashboard_acceptance._check_account_holdings(page, payload)


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


def test_acceptance_rejects_unavailable_futu_report_for_screenshot(
    tmp_path: Path,
) -> None:
    payload = valid_payload()
    report = payload["trend_reports"]["futu"]  # type: ignore[index]
    report.update(available=False, status_text="今日报告不可用")
    page = tabbed_account_page(payload)

    with pytest.raises(AssertionError, match="futu.*不可用.*截图"):
        dashboard_acceptance._check_account_holdings(
            page, payload, screenshot_dir=tmp_path
        )


def test_acceptance_keeps_unavailable_futu_disabled_outside_screenshot_gate() -> None:
    payload = valid_payload()
    report = payload["trend_reports"]["futu"]  # type: ignore[index]
    report.update(available=False, status_text="今日报告不可用")
    page = tabbed_account_page(payload)

    dashboard_acceptance._check_account_holdings(page, payload)

    assert page.disabled_reports == {"futu"}


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
    *, header: str = "刷新于 2026-07-15 15:03:13 CST",
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
            if selector == "#last-refresh":
                return Locator((header,))
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
        (session_price_page(header="刷新于 2026-07-15 15:03:13"), "Header"),
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


def test_cn_filter_accepts_grouped_visible_count_for_large_account() -> None:
    page = TabbedAccountPage(cn_rows={
        "futu": 0, "tiger": 0, "phillips": 0, "eastmoney": 5000,
    })

    dashboard_acceptance._check_cn_filter(page, expected_cn=5000)

    assert page.selected_brokers == ["futu", "tiger", "phillips", "eastmoney"]


@pytest.mark.parametrize(
    "missing",
        (
            "富途", "老虎", "辉立", "东方财富", "期权增强", "跨市场期权关注",
            "美股趋势交易", "港股趋势交易", "期权关注",
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
        "broker": "phillips", "display_text": "2026-07 月结单导入"
    }]

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
            "tiger", simulate_snapshot(), payload, tmp_path
        )


def test_acceptance_accepts_zero_simulated_positions(tmp_path: Path) -> None:
    dashboard_acceptance._validate_simulated_positions(
        "tiger",
        {"positions": []},
        {**simulate_api_payload(), "positions": []},
        tmp_path,
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
        tmp_path,
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
        tmp_path,
    )

    assert blocker is None
    assert len(errors) == 3
    assert all("Dashboard 模拟盘不可用" in error for error in errors)


def test_acceptance_accepts_explicitly_unlinked_legacy_simulated_position(
    tmp_path: Path,
) -> None:
    dashboard_acceptance._validate_simulated_positions(
        "tiger", simulate_snapshot(), simulate_api_payload(), tmp_path
    )


def test_acceptance_rejects_hidden_unlinked_simulated_position(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="模拟盘持仓.*不匹配"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            {**simulate_api_payload(), "positions": []},
            tmp_path,
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
            "tiger", simulate_snapshot(), payload, tmp_path
        )


@pytest.mark.parametrize("wrong_field", ["report_sha256", "strategy_version"])
def test_acceptance_rejects_linked_simulated_position_with_wrong_report_identity(
    tmp_path: Path, wrong_field: str,
) -> None:
    from open_trader.trend_review import _report_hash

    report_payload = {
        "execution_date": "2026-07-17",
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_snapshot": {"strategy_version": "v1"},
    }
    artifact = tmp_path / "trend_us_tiger" / "old.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(report_payload), encoding="utf-8")
    report = {
        "artifact": "old.json",
        "execution_date": "2026-07-17",
        "strategy_version": "v1",
        "report_sha256": _report_hash(report_payload),
    }
    report[wrong_field] = "0" * 64 if wrong_field == "report_sha256" else "v2"

    with pytest.raises(AssertionError, match="报告身份"):
        dashboard_acceptance._validate_simulated_positions(
            "tiger",
            simulate_snapshot(),
            simulate_api_payload(attribution_status="linked", report=report),
            tmp_path,
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        dashboard_acceptance.subprocess,
        "check_output",
        lambda *_args, **_kwargs: " M src/open_trader/dashboard.py\n",
    )

    assert dashboard_acceptance._source_changes(tmp_path) == [
        "M src/open_trader/dashboard.py"
    ]


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
        tmp_path,
    )

    assert payloads == {}
    assert errors == ["Dashboard 缺少有效 Futu OpenD 配置"]
    assert blocker is None
    assert classify_result(errors, browser_blocker=None) == "FAIL"


def test_acceptance_cli_has_no_test_only_config_or_expected_cn_options() -> None:
    destinations = {action.dest for action in dashboard_acceptance.build_parser()._actions}

    assert "config" not in destinations
    assert "expected_cn" not in destinations
