from decimal import Decimal
import json
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
    validate_quote_refresh_cycle,
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
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert 'DASHBOARD_URL ?= http://127.0.0.1:8766' in makefile
    assert 'DASHBOARD_LOG ?= /tmp/open_trader_dashboard_8766.log' in makefile
    assert '--url "$(DASHBOARD_URL)"' in makefile
    assert '--log "$(DASHBOARD_LOG)"' in makefile


def test_browser_ignores_chrome_unattributed_404_but_not_app_errors() -> None:
    assert not _is_actionable_console_error(
        "Failed to load resource: the server responded with a status of 404 (Not Found)"
    )
    assert _is_actionable_console_error("Uncaught TypeError: failed")


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
) -> tuple[int, dict[str, object], list[Path | None]]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    payloads = iter({"reports_dir": str(path)} for path in report_dirs)
    first_quotes = valid_quotes_payload()
    second_quotes = valid_quotes_payload()
    second_quotes["fetched_at"] = "2026-07-15T15:03:14+08:00"
    quote_payloads = iter((first_quotes, second_quotes))
    browser_reports: list[Path | None] = []
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
        dashboard_acceptance, "_fetch_payload", lambda url: next(payloads)
    )
    monkeypatch.setattr(
        dashboard_acceptance, "_fetch_quotes_payload", lambda url: next(quote_payloads)
    )
    monkeypatch.setattr(
        dashboard_acceptance, "validate_dashboard_payload", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(dashboard_acceptance, "_log_errors", lambda path: [])

    def browser_check(
        url: str, expected_cn: int, payload: dict[str, object],
        reports_dir: Path | None = None,
    ) -> tuple[list[str], None]:
        browser_reports.append(reports_dir)
        return [], None

    monkeypatch.setattr(dashboard_acceptance, "_browser_check", browser_check)
    status = dashboard_acceptance.main([
        "--expected-root", str(worktree),
        "--wait-seconds", "0",
        "--log", str(tmp_path / "dashboard.log"),
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


def test_acceptance_main_fails_when_reports_dir_changes_between_refreshes(
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
    assert "两个刷新周期的 Dashboard reports_dir 不一致" in result["errors"]
    assert browser_reports == [second.resolve()]


def test_acceptance_rejects_api_projection_that_drops_frozen_action(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    artifact = reports / "trend_us_futu" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "futu"},
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
        "broker": "futu",
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
            reports, "futu", projected
        )


def test_acceptance_rejects_unsafe_trend_artifact_name(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="产物文件名无效"):
        dashboard_acceptance._check_trend_artifact_projection(
            tmp_path,
            "futu",
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
    artifact = reports / "trend_us_futu" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    buy = {"action": "BUY", "symbol": "VIXY"}
    artifact.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=fresh),
        "metadata": {"market": "US", "broker": "futu"},
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
        "buy_actions": [buy],
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
        reports, "futu", projected
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
    artifact = reports / "trend_us_futu" / "2026-07-15.json"
    artifact.parent.mkdir(parents=True)
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "metadata": {"market": "US", "broker": "futu"},
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
            reports, "futu", projected
        )


def trend_reports() -> dict[str, dict[str, object]]:
    return {
        "futu": {
            "available": True, "broker": "futu", "broker_label": "富途",
            "market_label": "美股", "report_date": "2026-07-15",
            "data_date": "2026-07-14", "generated_at": "2026-07-15T11:30:36+08:00",
            "account_status": "已更新", "buy_window": "美股常规交易时段",
            "sell_actions": [{"symbol": "AAPL", "name": "苹果", "reason": "danger_signal", "active_line": "190"}],
            "buy_actions": [{"symbol": "VIXY", "name": "波动率ETF", "estimated_shares": "5000", "target_amount": "25142.16", "estimated_initial_line": "18.50"}],
            "hold_actions": [{"symbol": "SPY", "name": "标普ETF", "reason": "trend_intact", "active_line": "500"}],
            "review_actions": [{"symbol": "QQQ", "name": "纳指ETF", "reason": "holding_signal_unknown"}],
            "counts": {"sell": 1, "buy": 1, "hold": 1, "review": 1},
            "audit": {
                "candidates": [{"symbol": "VIXY", "name": "波动率ETF", "strength": "5000"}],
                "excluded": {"QQQ": ["already_held"]},
                "industry_concentration": [["科技", 1, "0.25"]],
                "data_sources": ["Trend Animals", "Futu US daily K-line"],
                "actual_api_cost": "1.00",
            },
        },
        "phillips": {
            "available": True, "broker": "phillips", "broker_label": "辉立",
            "market_label": "港股", "report_date": "2026-07-15",
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
        "tiger_long_term_strategy": {
            "status": "shadow",
            "members": [{"symbol": "QQQ"}],
            "gate": {"reasons": ["calibration_required"]},
            "order_requests": [],
        },
    }


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
    ("second_fetched_at", "valid"),
    [
        ("2026-07-15T15:03:13+08:00", False),
        ("2026-07-15T15:03:12+08:00", False),
        ("2026-07-15T15:03:14+08:00", True),
        ("not-a-timestamp", False),
    ],
    ids=("identical", "older", "newer", "invalid"),
)
def test_validate_quote_refresh_cycle_requires_strictly_newer_timestamp(
    second_fetched_at: str, valid: bool,
) -> None:
    first = valid_quotes_payload()
    second = valid_quotes_payload()
    second["fetched_at"] = second_fetched_at

    assert (validate_quote_refresh_cycle(first, second) == []) is valid


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
        "富途短线美股趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14 "
        "老虎长线SMA200 组合策略夏普比率卡玛比率 "
        "辉立短线港股趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14 "
        "东方财富偏短线趋势交易当天趋势报告报告日期2026-07-15数据截至2026-07-14"
    )


def trend_workspace_text(broker: str) -> str:
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
            "卖出 0 买入 0 持有 0 人工复核 0 今日执行检查 "
            "确认全部卖出动作 按顺序考虑允许买入项 盘中观察活动保护线 完成人工复核"
        )
    return (
        "富途｜美股 当天趋势报告 报告日期 2026-07-15 数据截至 2026-07-14 "
        "生成时间 2026-07-15T11:30:36+08:00 账户状态 已更新 "
        "卖出 1 买入 1 持有 1 人工复核 1 今日执行检查 "
        "确认全部卖出动作 按顺序考虑允许买入项 盘中观察活动保护线 完成人工复核"
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
        return ["开盘前\n无", "09:30–10:00\n无", "盘中持续\n无", "人工复核\n无"]
    return [
        "开盘前\nAAPL 苹果 危险信号触发 活动保护线 190",
        "美股常规交易时段\nVIXY 波动率ETF 约 5,000 股 金额上限 25,142.16 预计保护线 18.50",
        "盘中持续\nSPY 标普ETF 趋势保持完好 活动保护线 500",
        "人工复核\nQQQ 纳指ETF 趋势信号不完整",
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
        return ["候选榜 无", "排除项 无", "行业集中度 无"]
    return [
        "候选榜 VIXY 波动率ETF 强度 5,000",
        "排除项 QQQ 当前账户已经持有",
        "行业集中度 科技 1 0.25",
    ]


ACCOUNT_SECTION_TEXTS = {
    "futu": (
        "富途 短线 · 美股趋势交易 持仓资产 HKD 100 现金 HKD 20 持仓 1 "
        "来源 Futu 时间 2026-07-15 当天趋势报告 报告日期 2026-07-15 "
        "数据截至 2026-07-14"
    ),
    "tiger": (
        "老虎 长线 · SMA200 组合策略 持仓资产 HKD 100 现金 HKD 20 持仓 1 "
        "来源 Tiger 时间 2026-07-15 SMA200 策略 影子验证 · 仅供人工复核 "
        "年化收益 最大回撤 夏普比率 卡玛比率"
    ),
    "phillips": (
        "辉立 短线 · 港股趋势交易 持仓资产 HKD 100 现金 HKD 20 持仓 1 "
        "来源 月结单 时间 2026-07 当天趋势报告 报告日期 2026-07-15 "
        "数据截至 2026-07-14"
    ),
    "eastmoney": (
        "东方财富 偏短线 · 趋势交易 持仓资产 HKD 0 现金 HKD 20 持仓 0 "
        "来源 东方财富 时间 2026-07-15 当天趋势报告 报告日期 2026-07-15 "
        "数据截至 2026-07-14 "
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
        return self.page.locator(f"{self.selector} {selector}")

    def click(self) -> None:
        match = re.fullmatch(r'#account-tabs \[data-broker="(\w+)"\]', self.selector)
        if match:
            self.page.selected = match.group(1)
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
            broker = match.group(1)
            self.page.trend_broker = broker
            self.page.opened_reports.append(broker)
            self.page.active = "#return-to-portfolio:visible"
            self.page._record_visible_sections()
            return
        if self.selector.endswith(".trend-audit summary"):
            self.page.active = self.selector
            return
        if self.selector == "#return-to-portfolio:visible" or self.selector.endswith(
            "[data-close-trend-report]"
        ):
            broker = self.page.trend_broker
            self.page.trend_broker = None
            self.page.active = (
                f"#account-{broker}:visible .trend-report-entry [data-trend-report]"
            )
            self.page._record_visible_sections()

    def count(self) -> int:
        if self.selector == "#account-tabs [data-broker]":
            return 4
        if re.fullmatch(r'#account-tabs \[data-broker="\w+"\]', self.selector):
            return 1
        if self.selector in {'[data-market="CASH"]', "#cash-detail-panel"}:
            return 0
        if self.selector == ".account-section":
            return 1
        if self.selector == ".account-section:visible":
            return self.page._record_visible_sections()
        match = re.fullmatch(r"#account-(\w+):visible", self.selector)
        if match:
            return int(
                self.page.trend_broker is None and self.page.selected == match.group(1)
            )
        match = re.fullmatch(
            r"#account-(\w+):visible \.trend-report-entry(?: (.*))?", self.selector
        )
        if match:
            broker, child = match.groups()
            if self.page.trend_broker is not None or self.page.selected != broker:
                return 0
            if broker == "tiger":
                return 0
            if child == "[data-trend-report]":
                return int(bool(self.page.reports[broker]["available"]))
            if child == "button":
                return 1
            return 1
        if self.selector == "#trend-report-workspace:visible":
            return int(self.page.trend_broker is not None)
        if self.selector == "#return-to-portfolio:visible":
            return int(self.page.trend_broker is not None)
        if self.selector == ".workspace-grid:visible":
            return int(self.page.trend_broker is None)
        if self.selector.endswith(".cn-trend-report"):
            return int(self.page.trend_broker == "eastmoney")
        if self.selector.endswith(".trend-discipline[open]"):
            return 0 if self.page.viewport_size["width"] <= 760 else 2
        if self.selector.endswith(".trend-discipline"):
            return 2
        if self.selector.endswith(".cn-trend-table"):
            return 4
        if self.selector.endswith(".cn-trend-buy .cn-trend-card"):
            return 1
        if self.selector.endswith(".cn-trend-card:visible"):
            return 4
        if self.selector in {"#tiger-long-term-panel", "#trade-actions"}:
            return 0
        if self.selector.endswith(".account-holding-row:visible"):
            return self.page.visible_rows(self.selector)
        if self.selector.endswith(".account-empty:visible"):
            return int(self.page.visible_rows(self.selector) == 0)
        if self.selector.endswith(".session-quote"):
            return 1
        if "account-holding-market:has-text(\"US\")" in self.selector:
            return int(self.page.selected == "futu" and self.page.market != "CN")
        return 1

    def get_attribute(self, name: str) -> str | None:
        match = re.fullmatch(
            r"#account-tabs \[data-broker\]:nth\((\d+)\)", self.selector
        )
        if match:
            assert name == "data-broker"
            return self.page.tab_order[int(match.group(1))]
        match = re.fullmatch(r'#account-tabs \[data-broker="(\w+)"\]', self.selector)
        if match:
            assert name == "aria-selected"
            return str(match.group(1) == self.page.selected).lower()
        assert self.selector.endswith(".trend-audit") and name == "open"
        return None

    def is_disabled(self) -> bool:
        match = re.fullmatch(
            r"#account-(\w+):visible \.trend-report-entry button", self.selector
        )
        assert match
        broker = match.group(1)
        self.page.disabled_reports.add(broker)
        return not bool(self.page.reports[broker]["available"])

    def inner_text(self) -> str:
        if self.selector == "#account-holdings":
            return self.page.section_texts[self.page.selected]
        match = re.fullmatch(r"#account-(\w+):visible", self.selector)
        if match:
            return self.page.section_texts[match.group(1)]
        match = re.fullmatch(
            r"#account-(\w+):visible \.trend-report-entry", self.selector
        )
        if match:
            return self.page.entry_texts[match.group(1)]
        if self.selector == "#trend-report-workspace:visible":
            return trend_workspace_text(str(self.page.trend_broker))
        if self.selector.endswith(".trend-audit"):
            return trend_audit_text(str(self.page.trend_broker))
        if self.selector.endswith(".account-empty:visible"):
            return "当前筛选下没有持仓"
        if self.selector == "#visible-count":
            return f"{self.page.visible_rows():,} 条"
        if self.selector == "#last-refresh":
            return "刷新于 2026-07-15 15:03:13 CST"
        if ".session-quote" in self.selector:
            return "夜盘 61.50 · 03:03 ET"
        if self.selector == "body":
            return "持仓与策略"
        if self.selector.endswith(" strong"):
            return "HKD 628,554.06"
        match = re.search(r'td\[data-label="([^"]+)"\]$', self.selector)
        if match:
            buy = self.page.reports["eastmoney"]["buy_actions"][0]
            key = {
                "行业": "industry",
                "筛选价（Trend Animals）": "filter_price",
                "执行参考价（Futu 前复权）": "close",
            }[match.group(1)]
            return str(buy[key])
        raise AssertionError(self.selector)

    def all_inner_texts(self) -> list[str]:
        if self.selector == "a:visible, button:visible":
            return ["刷新账户与行情", "策略回测"]
        broker = str(self.page.trend_broker)
        if self.selector.endswith(".cn-trend-stage"):
            return trend_stage_texts(broker)
        if self.selector.endswith(".trend-stage"):
            return trend_stage_texts(broker)
        if self.selector.endswith(".trend-report-header dd"):
            report = self.page.reports[broker]
            return [str(report[key]) for key in (
                "report_date", "data_date", "generated_at", "account_status",
            )]
        if self.selector.endswith(".trend-audit section"):
            return trend_audit_sections(broker)
        if self.selector.endswith(".trend-discipline summary"):
            return ["买入纪律", "卖出纪律"]
        if self.selector.endswith(".account-holding-row:visible td:nth-child(2)"):
            return ["市场\nCN"] * self.page.visible_rows(self.selector)
        return []

    def nth(self, index: int) -> "TabbedAccountLocator":
        return self.page.locator(f"{self.selector}:nth({index})")

    def evaluate(self, expression: str) -> bool | dict[str, object]:
        if self.selector.endswith(".cn-trend-buy"):
            return {
                "clientWidth": 1500,
                "scrollWidth": 1600,
                "overflowX": "auto",
            }
        assert "document.activeElement" in expression
        self.page.focus_checks.append(self.selector)
        return self.selector == self.page.active

    def bounding_box(self) -> dict[str, float]:
        return {"x": 20, "width": 100}

    def evaluate_all(self, expression: str) -> list[dict[str, float]]:
        assert "getBoundingClientRect" in expression
        return [{"x": 10, "width": 350}]


class TabbedAccountPage:
    viewport_size = {"width": 1440, "height": 1000}

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        cn_rows: dict[str, int] | None = None,
    ) -> None:
        self.reports = (payload or valid_payload())["trend_reports"]  # type: ignore[assignment,index]
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
        self.all_rows = {"futu": 1, "tiger": 1, "phillips": 1, "eastmoney": 0}
        self.cn_rows = cn_rows or {"futu": 0, "tiger": 0, "phillips": 0, "eastmoney": 5}
        self.market = "ALL"
        self.selected = "futu"
        self.tab_order = ["futu", "tiger", "phillips", "eastmoney"]
        self.selected_brokers: list[str] = []
        self.visible_account_sections = 1
        self.max_visible_account_sections = 1
        self.trend_broker: str | None = None
        self.active: str | None = None
        self.opened_reports: list[str] = []
        self.disabled_reports: set[str] = set()
        self.focus_checks: list[str] = []

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

    def evaluate(self, expression: str) -> bool:
        assert expression == "document.documentElement.scrollWidth <= window.innerWidth"
        return True

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
            return ["候选榜 无", "排除项 无", "行业集中度 无"]

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


def test_first_in_scope_holding_rejects_payload_without_current_advice() -> None:
    payload = valid_payload()
    payload["holdings"][-1]["agent_report"]["available"] = False  # type: ignore[index]

    with pytest.raises(AssertionError, match="advice-backed holding"):
        dashboard_acceptance._first_in_scope_holding(payload)


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

    dashboard_acceptance._check_trend_stage(
        "VIXY 波动率ETF 约 5,000 股 金额上限 25,142.16 预计保护线 1,234.50",
        [{
            "symbol": "VIXY", "name": "波动率ETF", "estimated_shares": "5000",
            "target_amount": "25142.16", "estimated_initial_line": "1234.50",
        }],
        kind="buy",
        broker="futu",
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
    client_width: int = 1500,
    scroll_width: int = 1600,
    overflow_x: str = "auto",
) -> tuple[object, object]:
    class Stage:
        def evaluate(self, expression: str) -> dict[str, object]:
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
            }

    page = Page()
    return page, Workspace()


def test_acceptance_open_report_layout_requires_aligned_wide_shell_and_table_scroll() -> None:
    page, workspace = open_report_layout_page()

    dashboard_acceptance._check_open_report_layout(page, workspace, "eastmoney")

    assert len(page.geometry_evaluations) == 1  # type: ignore[attr-defined]
    assert len(page.overflow_evaluations) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"shell_width": 1598}, "shell"),
        ({"report_left": 178}, "左边线"),
        ({"report_right": 1742}, "右边线"),
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            assert self.selector == "#refresh-quotes"
            visual_focus_calls.append((self.page.name, self.selector))  # type: ignore[attr-defined]

        def evaluate(self, expression: str) -> object:
            if "getComputedStyle" in expression:
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
                if self.selector.endswith(".cn-trend-buy"):
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
            if "getPropertyValue" in expression:
                assert argument == list(dashboard_acceptance.WARM_LEDGER_TOKENS)
                visual_token_evaluations.append(self.name)
                return dict(dashboard_acceptance.WARM_LEDGER_TOKENS)
            if "const shell" in expression:
                for required in (
                    ".dashboard-shell",
                    ".dashboard-header",
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
                }
            assert expression == "document.documentElement.scrollWidth <= window.innerWidth"
            evaluated.append(self.name)
            return True

        def screenshot(self, *, path: str, full_page: bool) -> None:
            assert full_page is True
            screenshots.append((self.name, path))

        def close(self) -> None:
            pass

    class Browser:
        pages = 0

        def new_page(self, **kwargs: object) -> Page:
            names = ("wide_desktop", "desktop", "mobile")
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
    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, payload
    )

    assert errors == ["wide_desktop：RuntimeError: navigation failed"]
    assert blocker is None
    assert visited == ["wide_desktop", "desktop", "mobile"]
    assert viewport_widths == [1920, 1440, 375]

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
        "http://dashboard", 5, payload
    )

    assert errors == [
        "wide_desktop：AssertionError: decision failed",
        "desktop：AssertionError: decision failed",
        "mobile：AssertionError: decision failed",
    ]
    assert blocker is None
    for viewport in ("wide_desktop", "desktop", "mobile"):
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
        assert (
            viewport,
            '#account-phillips:visible .trend-report-entry [data-trend-report]',
        ) in clicks
        assert (
            viewport,
            '#account-eastmoney:visible .trend-report-entry [data-trend-report]',
        ) in clicks
        assert (viewport, '#return-to-portfolio:visible') in clicks
        assert (
            viewport,
            '#trend-report-workspace:visible [data-close-trend-report]',
        ) in clicks
        assert (viewport, '#trend-report-workspace:visible') in selectors
        assert (viewport, '#trend-report-workspace:visible .cn-trend-report') in selectors
        assert (viewport, '#trend-report-workspace:visible .cn-trend-stage') in selectors
        buy_rows = '#trend-report-workspace:visible .cn-trend-buy .cn-trend-card'
        assert (viewport, buy_rows) in selectors
        for label in (
            "行业", "筛选价（Trend Animals）", "执行参考价（Futu 前复权）",
        ):
            assert (
                viewport,
                f'{buy_rows}:nth(0) td[data-label="{label}"]',
            ) in selectors
        assert (viewport, '#trend-report-workspace:visible .trend-discipline') in selectors
        assert (viewport, '.workspace-grid:visible') in selectors
        assert (viewport, '.account-section:visible') in selectors
        assert (viewport, '#account-tiger:visible') in selectors
        assert (viewport, '#tiger-long-term-panel') in selectors
        assert (viewport, '#trade-actions') in selectors
        assert (viewport, 'body') in selectors
        assert (viewport, 'a:visible, button:visible') in selectors
        assert (viewport, 'a[href="#account-tiger"]') not in clicks
    assert evaluated == [
        *(["wide_desktop"] * 7), *(["desktop"] * 7), *(["mobile"] * 8),
    ]
    assert visual_token_evaluations == ["wide_desktop", "desktop", "mobile"]
    for viewport in ("wide_desktop", "desktop", "mobile"):
        assert [
            selector
            for name, selector in visual_surface_evaluations
            if name == viewport
        ] == list(VISUAL_CONTRACT_STYLES)
        assert (viewport, "#refresh-quotes") in visual_focus_calls
        assert (viewport, "#refresh-quotes") in visual_focus_evaluations
    assert geometry_evaluations == ["wide_desktop"] * 3
    assert buy_overflow_evaluations == ["wide_desktop", "desktop"]
    screenshot_dir = dashboard_acceptance.ACCEPTANCE_SCREENSHOT_DIR
    assert screenshots == [
        ("wide_desktop", str(screenshot_dir / "wide_desktop-portfolio.png")),
        ("wide_desktop", str(screenshot_dir / "1920-trend-report.png")),
        ("desktop", str(screenshot_dir / "desktop-portfolio.png")),
        ("desktop", str(screenshot_dir / "1440-trend-report.png")),
        ("mobile", str(screenshot_dir / "mobile-portfolio.png")),
        ("mobile", str(screenshot_dir / "375-trend-report.png")),
    ]


def test_validate_dashboard_payload_accepts_real_contract() -> None:
    assert validate_dashboard_payload(valid_payload(), expected_cn=5) == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("status", "failed", "老虎长线策略不是 shadow 状态"),
        ("members", [], "老虎长线策略没有组合成员"),
        ("gate", {"reasons": []}, "老虎长线策略缺少 calibration_required"),
        ("order_requests", [{"symbol": "QQQ"}], "老虎长线策略包含下单请求"),
    ],
)
def test_validate_dashboard_payload_rejects_invalid_tiger_strategy(
    field: str, value: object, expected: str,
) -> None:
    payload = valid_payload()
    payload["tiger_long_term_strategy"][field] = value  # type: ignore[index]

    assert expected in validate_dashboard_payload(payload, expected_cn=5)


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
    assert page.opened_reports == ["futu", "phillips", "eastmoney"]
    assert page.disabled_reports == set()
    assert projections == ["futu", "phillips", "eastmoney"]
    assert page.focus_checks == [
        "#return-to-portfolio:visible",
        '#account-futu:visible .trend-report-entry [data-trend-report]',
        "#return-to-portfolio:visible",
        '#account-phillips:visible .trend-report-entry [data-trend-report]',
        "#return-to-portfolio:visible",
        '#account-eastmoney:visible .trend-report-entry [data-trend-report]',
    ]


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
    ("富途", "老虎", "辉立", "东方财富", "美股趋势交易", "港股趋势交易", "当天趋势报告", "报告日期", "数据截至", "夏普比率", "卡玛比率"),
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
