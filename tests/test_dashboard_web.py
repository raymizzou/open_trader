from __future__ import annotations

import http.client
import json
import os
from dataclasses import replace
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from open_trader.dashboard_quotes import QuoteRefreshResult
from open_trader import dashboard_acceptance
from open_trader.dashboard_web import STATIC_DIR
from open_trader.portfolio import PORTFOLIO_FIELDNAMES
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES

from tests.test_dashboard import (
    dashboard_config,
    portfolio_rows,
    write_csv,
    write_trend_history_report,
)


def _controller_status(*, heartbeat_at: str) -> dict[str, object]:
    return {
        "schema_version": "open_trader.trend_controller.status.v1",
        "effective_mode": "execute",
        "executor_host": "ray-mac",
        "local_host": "ray-mac",
        "pid": 4242,
        "working_directory": "/srv/open_trader",
        "git_sha": "abc1234",
        "phase": "monitoring",
        "heartbeat_at": heartbeat_at,
        "last_success": "report_locked",
        "blocker": None,
        "next_check_at": "2026-07-21T09:31:05+08:00",
    }


@pytest.mark.parametrize(
    ("status", "executor_host", "local_host", "health", "blocking"),
    [
        ("healthy", "ray-mac", "ray-mac", "healthy", False),
        ("missing", "ray-mac", "ray-mac", "unavailable", True),
        ("stale", "ray-mac", "ray-mac", "unavailable", True),
        ("future-stale", "ray-mac", "ray-mac", "unavailable", True),
        ("future-skew", "ray-mac", "ray-mac", "healthy", False),
        ("malformed", "ray-mac", "ray-mac", "unavailable", True),
        ("wrong-host", "ray-mac", "ray-mac", "unavailable", True),
        ("missing", "ray-mac", "readonly-copy", "readonly", False),
        ("missing", "", "readonly-copy", "readonly", False),
    ],
)
def test_dashboard_projects_strict_controller_health(
    tmp_path: Path,
    status: str,
    executor_host: str,
    local_host: str,
    health: str,
    blocking: bool,
) -> None:
    from open_trader.dashboard import _load_trend_controllers

    now = datetime(2026, 7, 21, 9, 31, tzinfo=timezone(timedelta(hours=8)))
    path = tmp_path / "trend_controller/US/status.json"
    if status != "missing":
        path.parent.mkdir(parents=True)
        if status == "malformed":
            path.write_text('{"phase":"monitoring"}', encoding="utf-8")
        else:
            heartbeat = (
                now - timedelta(minutes=3)
                if status == "stale"
                else now + timedelta(seconds=121)
                if status == "future-stale"
                else now + timedelta(seconds=30)
                if status == "future-skew"
                else now
            )
            payload = _controller_status(heartbeat_at=heartbeat.isoformat())
            if status == "wrong-host":
                payload["local_host"] = "retired-host"
            path.write_text(json.dumps(payload), encoding="utf-8")

    controllers = _load_trend_controllers(
        tmp_path,
        executor_host=executor_host,
        now=now,
        hostname_fn=lambda: local_host,
    )

    assert set(controllers) == {"eastmoney", "phillips", "tiger"}
    controller = controllers["tiger"]
    assert controller["effective_mode"] == (
        "execute" if executor_host and executor_host == local_host else "readonly"
    )
    assert controller["executor_host"] == executor_host
    assert controller["local_host"] == local_host
    assert controller["health"] == health
    assert controller["blocking"] is blocking
    if health == "healthy":
        assert controller["pid"] == 4242
        assert controller["git_sha"] == "abc1234"
        assert controller["phase"] == "monitoring"
    if status in {"stale", "future-stale"}:
        assert controller["blocker"] == "controller heartbeat is stale"
    if health == "readonly":
        assert "OPEN_TRADER_TREND_EXECUTOR_HOST" in controller["reason"]


@pytest.mark.parametrize(
    "phase", ["reconciling", "recovering_report", "recovering_review"]
)
def test_dashboard_projects_fresh_controller_blocker_as_unavailable(
    tmp_path: Path, phase: str,
) -> None:
    from open_trader.dashboard import _load_trend_controllers

    now = datetime(2026, 7, 21, 9, 31, tzinfo=timezone(timedelta(hours=8)))
    path = tmp_path / "trend_controller/US/status.json"
    path.parent.mkdir(parents=True)
    payload = _controller_status(heartbeat_at=now.isoformat())
    payload.update({
        "phase": phase,
        "last_success": None,
        "blocker": "report generation failed: upstream unavailable",
    })
    path.write_text(json.dumps(payload), encoding="utf-8")

    controller = _load_trend_controllers(
        tmp_path,
        executor_host="ray-mac",
        now=now,
        hostname_fn=lambda: "ray-mac",
    )["tiger"]

    assert controller["health"] == "unavailable"
    assert controller["blocking"] is True
    assert controller["reason"] == payload["blocker"]


@pytest.mark.parametrize(
    "phase", ["reconciling", "recovering_report", "recovering_review"]
)
def test_dashboard_projects_unblocked_progress_phase_as_healthy(
    tmp_path: Path, phase: str,
) -> None:
    from open_trader.dashboard import _load_trend_controllers

    now = datetime(2026, 7, 21, 9, 31, tzinfo=timezone(timedelta(hours=8)))
    path = tmp_path / "trend_controller/US/status.json"
    path.parent.mkdir(parents=True)
    payload = _controller_status(heartbeat_at=now.isoformat())
    payload.update({"phase": phase, "blocker": None})
    path.write_text(json.dumps(payload), encoding="utf-8")

    controller = _load_trend_controllers(
        tmp_path,
        executor_host="ray-mac",
        now=now,
        hostname_fn=lambda: "ray-mac",
    )["tiger"]

    assert controller["health"] == "healthy"
    assert controller["blocking"] is False
    assert controller["reason"] == ""


@pytest.mark.parametrize(
    "phase",
    ["starting", "blocked", "uncertain", "conflict", "missed"],
)
def test_dashboard_projects_unhealthy_controller_phase_as_unavailable(
    tmp_path: Path, phase: str,
) -> None:
    from open_trader.dashboard import _load_trend_controllers

    now = datetime(2026, 7, 21, 9, 31, tzinfo=timezone(timedelta(hours=8)))
    path = tmp_path / "trend_controller/US/status.json"
    path.parent.mkdir(parents=True)
    payload = _controller_status(heartbeat_at=now.isoformat())
    payload.update({"phase": phase, "blocker": None})
    path.write_text(json.dumps(payload), encoding="utf-8")

    controller = _load_trend_controllers(
        tmp_path,
        executor_host="ray-mac",
        now=now,
        hostname_fn=lambda: "ray-mac",
    )["tiger"]

    assert controller["health"] == "unavailable"
    assert controller["blocking"] is True


@pytest.mark.parametrize("status", ["uncertain", "conflict", "missed"])
def test_dashboard_preserves_terminal_trend_action_status(
    tmp_path: Path, status: str,
) -> None:
    from open_trader.dashboard import _trend_action_executions

    event = tmp_path / "trend_review/ledgers/US/actions/2026-07-20/key/event.json"
    event.parent.mkdir(parents=True)
    event.write_text(json.dumps({
        "report_sha256": "a" * 64,
        "symbol": "TRV",
        "side": "buy",
        "status": status,
        "recorded_at": "2026-07-20T09:31:00-04:00",
    }), encoding="utf-8")

    executions = _trend_action_executions(
        tmp_path, market="US", execution_date="2026-07-20",
        report_sha256="a" * 64,
    )

    assert executions[("TRV", "buy")]["status"] == status


def test_dashboard_uses_latest_action_event_across_timezone_offsets(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard import _trend_action_executions

    root = tmp_path / "trend_review/ledgers/US/actions/2026-07-20/key"
    root.mkdir(parents=True)
    common = {
        "report_sha256": "a" * 64,
        "symbol": "TRV",
        "side": "buy",
    }
    (root / "later-by-name.json").write_text(json.dumps({
        **common,
        "status": "missed",
        "recorded_at": "2026-07-21T09:01:01+08:00",
    }), encoding="utf-8")
    (root / "earlier-by-name.json").write_text(json.dumps({
        **common,
        "status": "filled",
        "recorded_at": "2026-07-21T07:36:30-04:00",
    }), encoding="utf-8")

    executions = _trend_action_executions(
        tmp_path, market="US", execution_date="2026-07-20",
        report_sha256="a" * 64,
    )

    assert executions[("TRV", "buy")]["status"] == "filled"


def test_dashboard_projects_locked_batch_when_latest_report_is_a_revision(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard import (
        load_dashboard_state,
        load_historical_trend_report,
    )
    from open_trader.trend_review import _report_hash

    config = replace(dashboard_config(tmp_path), trend_executor_host="")
    base = write_trend_history_report(
        config.reports_dir,
        "2026-07-17.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
    )
    revised = write_trend_history_report(
        config.reports_dir,
        "2026-07-17-r1.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:30:00+08:00",
    )
    revised["strategy_judgments"]["formal_actions"][0]["symbol"] = "REVISION"
    revision_path = config.reports_dir / "trend_us_tiger/2026-07-17-r1.json"
    revision_path.write_text(json.dumps(revised), encoding="utf-8")
    base_path = config.reports_dir / "trend_us_tiger/2026-07-17.json"
    batch = config.data_dir / "trend_review/ledgers/US/batches/2026-07-20.json"
    batch.parent.mkdir(parents=True)
    batch.write_text(json.dumps({
        "schema_version": "open_trader.trend_review.batch.v1",
        "market": "US",
        "execution_date": "2026-07-20",
        "report_path": str(base_path),
        "report_sha256": _report_hash(base),
        "locked_at": "2026-07-20T09:30:00-04:00",
    }), encoding="utf-8")
    event = (
        config.data_dir
        / "trend_review/ledgers/US/actions/2026-07-20/key/event.json"
    )
    event.parent.mkdir(parents=True)
    event.write_text(json.dumps({
        "report_sha256": _report_hash(base),
        "symbol": "VIXY",
        "side": "buy",
        "status": "missed",
        "recorded_at": "2026-07-20T16:00:00-04:00",
    }), encoding="utf-8")

    report = load_dashboard_state(config).to_dict()["trend_reports"]["tiger"]

    assert report["artifact"] == "2026-07-17.json"
    assert report["report_sha256"] == _report_hash(base)
    assert report["execution_batch"]["report_sha256"] == _report_hash(base)
    assert report["latest_report_sha256"] == _report_hash(revised)
    assert report["revision_anomaly"] is True
    assert report["buy_actions"][0]["symbol"] == "VIXY"
    assert report["buy_actions"][0]["execution"]["status"] == "missed"

    invalid_batch = json.loads(batch.read_text(encoding="utf-8"))
    invalid_batch["locked_at"] = "2026-07-20T09:30:00"
    batch.write_text(json.dumps(invalid_batch), encoding="utf-8")
    report = load_dashboard_state(config).to_dict()["trend_reports"]["tiger"]

    assert report["available"] is False
    assert report["data_status"] == "unavailable"
    assert report["execution_batch"] is None
    assert report["execution_batch_blocking"] is True
    assert report["execution_batch_error"] == "执行批次无效，已阻止操作投影"
    assert report["status_text"] == report["execution_batch_error"]
    assert report["artifact"] == ""
    assert report["report_sha256"] == ""
    assert report["latest_report_sha256"] == ""
    assert report["risk_skips"] == []
    assert report["risk_summary"] == {}
    assert report["drawdown_summary"] == {}
    assert report["actual_overlay"] == {}
    assert report["audit"] == {}
    assert report["counts"] == {"sell": 0, "buy": 0, "hold": 0, "review": 0}
    assert all(
        report[key] == []
        for key in ("sell_actions", "buy_actions", "hold_actions", "review_actions")
    )

    historical = load_historical_trend_report(
        config.data_dir,
        config.reports_dir,
        broker="tiger",
        artifact="2026-07-17-r1.json",
    )

    assert historical["available"] is True
    assert historical["artifact"] == "2026-07-17-r1.json"
    assert historical["execution_batch_blocking"] is False
    assert historical["buy_actions"][0]["symbol"] == "REVISION"

    batch.unlink()
    current_without_batch = load_dashboard_state(config).to_dict()["trend_reports"][
        "tiger"
    ]

    assert current_without_batch["available"] is True
    assert current_without_batch["artifact"] == "2026-07-17-r1.json"
    assert current_without_batch["execution_batch_blocking"] is False
    assert current_without_batch["buy_actions"][0]["symbol"] == "REVISION"


@pytest.mark.parametrize(
    "corruption",
    ["bad-json", "wrong-sha", "missing-artifact", "invalid-report"],
)
def test_dashboard_fails_closed_when_existing_execution_batch_is_invalid(
    tmp_path: Path,
    corruption: str,
) -> None:
    from open_trader.dashboard import load_dashboard_state
    from open_trader.trend_review import _report_hash

    config = replace(dashboard_config(tmp_path), trend_executor_host="")
    locked = write_trend_history_report(
        config.reports_dir,
        "2026-07-17.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
    )
    revised = write_trend_history_report(
        config.reports_dir,
        "2026-07-17-r1.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:30:00+08:00",
    )
    revised["strategy_judgments"]["formal_actions"][0]["symbol"] = "REVISION"
    revised_path = config.reports_dir / "trend_us_tiger/2026-07-17-r1.json"
    revised_path.write_text(json.dumps(revised), encoding="utf-8")
    locked_path = config.reports_dir / "trend_us_tiger/2026-07-17.json"
    batch_path = (
        config.data_dir / "trend_review/ledgers/US/batches/2026-07-20.json"
    )
    batch_path.parent.mkdir(parents=True)
    batch_payload = {
        "schema_version": "open_trader.trend_review.batch.v1",
        "market": "US",
        "execution_date": "2026-07-20",
        "report_path": str(locked_path),
        "report_sha256": _report_hash(locked),
        "locked_at": "2026-07-20T09:30:00-04:00",
    }
    if corruption == "bad-json":
        batch_path.write_text("{broken", encoding="utf-8")
    else:
        if corruption == "wrong-sha":
            batch_payload["report_sha256"] = "f" * 64
        elif corruption == "missing-artifact":
            batch_payload["report_path"] = str(locked_path.with_name("missing.json"))
        else:
            invalid_path = locked_path.with_name("invalid.json")
            invalid_payload: dict[str, object] = {}
            invalid_path.write_text(json.dumps(invalid_payload), encoding="utf-8")
            batch_payload["report_path"] = str(invalid_path)
            batch_payload["report_sha256"] = _report_hash(invalid_payload)
        batch_path.write_text(json.dumps(batch_payload), encoding="utf-8")

    report = load_dashboard_state(config).to_dict()["trend_reports"]["tiger"]

    assert report["available"] is False
    assert report["data_status"] == "unavailable"
    assert report["execution_batch"] is None
    assert report["execution_batch_blocking"] is True
    assert report["execution_batch_error"] == "执行批次无效，已阻止操作投影"
    assert report["artifact"] == ""
    assert report["report_sha256"] == ""
    assert report["latest_report_sha256"] == ""
    assert report["risk_skips"] == []
    assert report["risk_summary"] == {}
    assert report["drawdown_summary"] == {}
    assert report["actual_overlay"] == {}
    assert report["audit"] == {}
    assert report["counts"] == {"sell": 0, "buy": 0, "hold": 0, "review": 0}
    assert all(
        report[key] == []
        for key in ("sell_actions", "buy_actions", "hold_actions", "review_actions")
    )
    assert "REVISION" not in json.dumps(report, ensure_ascii=False)


def relative_luminance(color: str) -> float:
    channels = (int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))
    linear = (
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    red, green, blue = linear
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    foreground_luminance = relative_luminance(foreground)
    background_luminance = relative_luminance(background)
    return (max(foreground_luminance, background_luminance) + 0.05) / (
        min(foreground_luminance, background_luminance) + 0.05
    )


def test_dashboard_static_keeps_existing_columns_and_adds_cn() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    for label in (
        "明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值",
        "港元市值", "账户权重", "组合权重", "盈亏",
    ):
        assert f'"{label}"' in js
    assert 'data-market="CN">A 股</button>' in html
    for forbidden_id in ("a-share-panel", "a-share-card", "cn-panel", "cn-card"):
        assert f'id="{forbidden_id}"' not in html


def test_dashboard_warm_ledger_theme_and_broker_accents() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert "account-holdings-table" in js
    for element_id in (
        "open-standard-backtest", "header-market-filters",
        "current-view-value",
        "broker-summary-cards", "quote-status", "refresh-quotes",
        "source-status-list", "last-refresh", "kelly-lab-panel",
        "open-kelly-lab", "return-to-portfolio", "account-tabs",
        "account-holdings", "symbol-detail-panel",
        "standard-backtest-workspace", "research-chat-layer",
    ):
        assert f'id="{element_id}"' in html
    assert "今日结论" not in html
    assert 'id="trade-actions"' not in html
    for token in (
        "--bg: #f7f5f1;", "--surface: #fffefa;",
        "--surface-soft: #f2eee7;", "--text: #201d18;",
        "--muted: #746e64;", "--accent: #8b5e34;",
        "--line: #d8d2c8;", "--primary: #24211d;",
        "--success: #2f855a;", "--danger: #b42318;",
    ):
        assert token in css
    for broker, color in {
        "futu": "#2563eb", "tiger": "#d97706",
        "phillips": "#15803d", "eastmoney": "#dc2626",
    }.items():
        assert f'.account-tab[data-broker="{broker}"] {{ --broker-accent: {color}; }}' in css
    assert ".account-tab.active" in css
    assert "border-bottom-color: var(--broker-accent);" in css
    assert ".pnl-profit { color: var(--danger);" in css
    assert ".pnl-loss { color: var(--success);" in css
    assert ".tool-workspace-view .header-assets-panel" in css
    assert (
        ".backtest-workspace,\n.kelly-lab-panel,\n.trend-report-workspace,\n"
        ".symbol-detail-panel,\n.research-chat-modal"
    ) in css
    assert "outline: 3px solid var(--accent);" in css
    assert "rgba(37, 99, 235, 0.32)" not in css
    assert ".account-tab:focus-visible" in css
    assert "outline-offset: -3px;" in css
    assert "box-shadow: inset 0 0 0 3px var(--accent);" in css
    table_header_css = css.split("\nth {", 1)[1].split("}", 1)[0]
    assert "background: var(--surface-soft);" in table_header_css
    assert "color: var(--text);" in table_header_css
    assert css.count("\nth {") == 1
    assert "#f9fafb" not in css
    for market_selector in (
        ".market-section-row td", ".market-section-us-stock td",
        ".market-section-us-option td", ".market-section-hk-stock td",
        ".market-section-hk-option td",
    ):
        market_css = css.split(f"{market_selector} {{", 1)[1].split("}", 1)[0]
        assert "background: var(--surface-soft);" in market_css
        assert "border-bottom" in market_css and "var(--line)" in market_css
        assert "color: var(--text);" in market_css
    for market_text_selector in (".market-section-row span", ".market-section-other td"):
        market_text_css = css.split(f"{market_text_selector} {{", 1)[1].split("}", 1)[0]
        assert "color: var(--text);" in market_text_css
    assert "linear-gradient" not in css
    assert "font-variant-numeric: tabular-nums;" in css


def test_dashboard_command_center_css_keeps_accessible_responsive_states() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert "button:focus-visible" in css
    assert "outline: 3px solid var(--accent);" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transition-duration: 0.01ms !important;" in css
    mobile = css.split("@media (max-width: 760px) {", 1)[1]
    assert "min-height: 44px;" in mobile
    assert 'grid-template-areas: "brand" "assets" "source";' in mobile
    assert ".account-tab-list" in mobile
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in mobile
    assert "overflow-x: hidden;" in mobile
    assert ".backtest-form input," in mobile
    assert ".backtest-form select," in mobile
    assert ".decision-tab," in mobile
    assert ".language-toggle button" in mobile
    assert ".trend-report-workspace" in css
    report_css = css.split(".trend-report-workspace {", 1)[1].split("}", 1)[0]
    assert "max-width: none;" in report_css
    buy_css = css.split(".cn-trend-buy {", 1)[1].split("}", 1)[0]
    assert "overflow-x: auto;" in buy_css
    assert ".cn-trend-buy .cn-trend-table" in css
    assert "min-width: 1600px;" in css
    assert ".cn-trend-buy {\n    overflow-x: hidden;\n  }" in mobile
    assert "min-width: 0;" in mobile


def test_dashboard_muted_text_meets_aa_on_approved_soft_surface() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    tokens = dict(re.findall(r"--([\w-]+): (#[0-9a-f]{6});", css))

    assert contrast_ratio(tokens["text"], tokens["surface-soft"]) >= 4.5

    soft_surface_contract = re.search(
        r"([^{}]+) \{\n  --muted: var\(--text\);\n\}", css,
    )
    assert soft_surface_contract is not None
    contract_selectors = {
        selector.strip() for selector in soft_surface_contract.group(1).split(",")
    }
    soft_surface_selectors = {
        selector.strip()
        for selectors in re.findall(
            r"([^{}]+)\{[^{}]*background: var\(--(?:surface-soft|panel-soft)\);[^{}]*\}",
            css,
        )
        for selector in selectors.split(",")
    }
    assert soft_surface_selectors - {".trend-stage"} <= contract_selectors
    assert ".trend-stage" not in contract_selectors
    assert ".trend-stage:not(.cn-trend-stage)" in contract_selectors

    for foreground_selector, surface_selector in (
        (".source-status-row span", ".source-status-row"),
    ):
        block = css.split(f"\n{foreground_selector} {{", 1)[1].split("}", 1)[0]
        assert "color: var(--muted);" in block
        assert surface_selector in contract_selectors


def test_dashboard_success_text_meets_aa_on_every_adjusted_surface() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"--([\w-]+): (#[0-9a-f]{6});", css))

    pairings = (
        (tokens["text"], "#e7f4ec"),
        (tokens["text"], "#f4fbf7"),
        (tokens["success"], tokens["surface"]),
    )
    assert all(
        contrast_ratio(foreground, background) >= 4.5
        for foreground, background in pairings
    )

    status = css.split("\n.status-ok {", 1)[1].split("}", 1)[0]
    opportunity = css.split(
        ".technical-bollinger-card.lower-opportunity .technical-bollinger-header strong {",
        1,
    )[1].split("}", 1)[0]
    safe_loss = css.split("\ntbody tr:hover .pnl-loss,", 1)[1].split("}", 1)[0]
    assert "color: var(--text);" in status
    assert "color: var(--text);" in opportunity
    assert "tbody tr.active-row .pnl-loss" in safe_loss
    assert "background: var(--surface);" in safe_loss
    assert contrast_ratio(tokens["success"], tokens["surface-soft"]) < 4.5
    assert contrast_ratio(tokens["success"], tokens["surface"]) >= 4.5


def test_cn_trend_secondary_text_keeps_muted_tone_on_main_surface() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    contract = re.search(
        r"([^{}]+) \{\n  --muted: var\(--text\);\n\}", css,
    )
    assert contract is not None
    selectors = {selector.strip() for selector in contract.group(1).split(",")}
    assert ".cn-trend-stage" not in selectors
    assert ".trend-stage:not(.cn-trend-stage)" in selectors
    price_sources = css.split("\n.cn-trend-price-sources {", 1)[1].split("}", 1)[0]
    assert "color: var(--muted);" in price_sources


def test_dashboard_account_tabs_register_roving_keyboard_and_panel_semantics() -> None:
    output = run_dashboard_js(r'''
let focused="";
class Element {
  constructor(){this.dataset={};this.hidden=false;this.innerHTML="";this.textContent="";this.listeners={};
    this.attributes={};this.classList={add(){},remove(){},toggle(){},contains(){return false;}};}
  addEventListener(name,listener){this.listeners[name]=listener;}
  setAttribute(name,value){this.attributes[name]=value;}
  querySelectorAll(){return [];}
  querySelector(selector){return {focus(){focused=selector;}};}
}
const nodes={};
document.getElementById=(id)=>nodes[id]||(nodes[id]=new Element());
document.querySelector=()=>nodes["workspace-grid"]||(nodes["workspace-grid"]=new Element());
bindElements();bindEvents();
state.dashboard={summary:{portfolio_value_hkd:"0"},broker_summaries:[],source_statuses:[],cash_rows:[],holdings:[]};
renderAccountHoldings();
const initial={tabs:nodes["account-tabs"].innerHTML,panel:nodes["account-holdings"].innerHTML,labelledBy:nodes["account-holdings"].attributes["aria-labelledby"]};
const press=(key)=>{
  let prevented=false;
  nodes["account-tabs"].listeners.keydown({key,target:{closest(selector){return selector==='[role="tab"][data-broker]'?{dataset:{broker:state.brokerFilter}}:null;}},preventDefault(){prevented=true;}});
  return {broker:state.brokerFilter,focused,prevented};
};
console.log(JSON.stringify({initial,left:press("ArrowLeft"),home:press("Home"),end:press("End"),right:press("ArrowRight")}));
''')
    rendered = json.loads(output)
    assert 'id="account-tab-futu"' in rendered["initial"]["tabs"]
    assert rendered["initial"]["tabs"].count('aria-controls="account-holdings"') == 4
    assert 'aria-selected="true" tabindex="0"' in rendered["initial"]["tabs"]
    assert 'aria-selected="false" tabindex="-1"' in rendered["initial"]["tabs"]
    assert 'id="account-futu" class="account-section"' in rendered["initial"]["panel"]
    assert rendered["initial"]["labelledBy"] == "account-tab-futu"
    for broker in ("tiger", "phillips", "eastmoney"):
        assert f'id="account-{broker}"' not in rendered["initial"]["panel"]
    assert rendered["left"] == {
        "broker": "eastmoney", "focused": '[data-broker="eastmoney"]', "prevented": True,
    }
    assert rendered["home"] == {
        "broker": "futu", "focused": '[data-broker="futu"]', "prevented": True,
    }
    assert rendered["end"] == {
        "broker": "eastmoney", "focused": '[data-broker="eastmoney"]', "prevented": True,
    }
    assert rendered["right"] == {
        "broker": "futu", "focused": '[data-broker="futu"]', "prevented": True,
    }


def test_dashboard_tabpanel_uses_fallback_label_until_real_tabs_exist() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    account_markup = html.split('id="account-holdings"', 1)[1].split(">", 1)[0]
    assert 'aria-label="账户持仓加载中"' in account_markup
    assert "aria-labelledby" not in account_markup

    output = run_dashboard_js(r'''
class Element {
  constructor(){this.innerHTML="";this.textContent="";this.attributes={};
    this.classList={add(){},remove(){},toggle(){},contains(){return false;}};}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
}
const nodes={};
for(const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"]){nodes[id]=new Element();elements[id]=nodes[id];}
const snapshot=()=>({tabs:nodes["account-tabs"].innerHTML,label:nodes["account-holdings"].attributes["aria-label"]||"",labelledBy:nodes["account-holdings"].attributes["aria-labelledby"]||"",panel:nodes["account-holdings"].innerHTML});
state.dashboard=null;state.dashboardError=null;renderAccountHoldings();const loading=snapshot();
state.dashboardError=new Error("offline");renderAccountHoldings();const error=snapshot();
state.dashboardError=null;state.dashboard={summary:{portfolio_value_hkd:"0"},broker_summaries:[],source_statuses:[],cash_rows:[],holdings:[]};renderAccountHoldings();const ready=snapshot();
console.log(JSON.stringify({loading,error,ready}));
''')
    rendered = json.loads(output)
    assert rendered["loading"]["tabs"] == ""
    assert rendered["loading"]["label"] == "账户持仓加载中"
    assert rendered["loading"]["labelledBy"] == ""
    assert "加载中" in rendered["loading"]["panel"]
    assert rendered["error"]["tabs"] == ""
    assert rendered["error"]["label"] == "账户持仓不可用"
    assert rendered["error"]["labelledBy"] == ""
    assert "加载失败" in rendered["error"]["panel"]
    assert 'id="account-tab-futu"' in rendered["ready"]["tabs"]
    assert rendered["ready"]["label"] == ""
    assert rendered["ready"]["labelledBy"] == "account-tab-futu"


def test_dashboard_statement_upload_controls_only_render_for_statement_brokers() -> None:
    output = run_dashboard_js(r'''
state.statementUpload={broker:"",busy:false,message:"",error:false};
console.log(JSON.stringify({
  futu: renderStatementUpload("futu"),
  tiger: renderStatementUpload("tiger"),
  phillips: renderStatementUpload("phillips"),
  eastmoney: renderStatementUpload("eastmoney"),
}));
''')
    rendered = json.loads(output)
    assert rendered["futu"] == rendered["tiger"] == ""
    for broker in ("phillips", "eastmoney"):
        assert f'data-statement-upload="{broker}"' in rendered[broker]
        assert f'data-statement-file="{broker}"' in rendered[broker]
        assert "上传结单" in rendered[broker]
        assert 'accept=".pdf,application/pdf"' in rendered[broker]


def test_dashboard_statement_upload_is_right_aligned_and_desktop_only() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    actions = css.split(".account-section-actions {", 1)[1].split("}", 1)[0]
    assert "margin-left: auto;" in actions
    mobile = css.split("@media (max-width: 760px) {", 1)[1]
    upload = mobile.split(".statement-upload {", 1)[1].split("}", 1)[0]
    assert "display: none;" in upload


def test_dashboard_statement_upload_posts_pdf_and_reloads_dashboard() -> None:
    output = run_dashboard_js(r'''
const calls=[];
globalThis.fetch=async (url, options) => {
  calls.push({url, method:options.method, contentType:options.headers["Content-Type"], body:options.body.name});
  return {ok:true,status:200,json:async()=>({status:"ok",statement_date:"2026-07-10",positions:3})};
};
let reloads=0;
loadDashboard=async()=>{reloads+=1;};
const payload=await uploadStatement("phillips", {name:"statement.pdf",size:100});
console.log(JSON.stringify({calls,reloads,payload}));
''')
    result = json.loads(output)
    assert result["calls"] == [
        {
            "url": "/api/statements/phillips",
            "method": "POST",
            "contentType": "application/pdf",
            "body": "statement.pdf",
        }
    ]
    assert result["reloads"] == 1
    assert result["payload"]["positions"] == 3


def test_dashboard_statement_upload_rejects_extension_and_size_before_fetch() -> None:
    output = run_dashboard_js(r'''
let fetches=0; globalThis.fetch=async()=>{fetches+=1;};
const messages=[];
for (const file of [{name:"statement.txt",size:1},{name:"statement.pdf",size:20*1024*1024+1}]) {
  try { await uploadStatement("phillips", file); } catch (error) { messages.push(error.message); }
}
console.log(JSON.stringify({fetches,messages}));
''')
    assert json.loads(output) == {
        "fetches": 0,
        "messages": ["请选择 PDF 文件", "PDF 不能超过 20 MiB"],
    }


def test_failed_statement_upload_keeps_rendered_stats_cutoff_and_shows_reason() -> None:
    output = run_dashboard_js(r'''
const tradeStats={available:true,statistics_cutoff_at:"2026-07-12T23:59:59+08:00",
  actual_label:"辉立实盘交易统计",
  simulation:{win_rate:null,payoff_ratio:null,payoff_ratio_status:"no_wins",eligible_sample_count:0},
  actual:{win_rate:"1",payoff_ratio:null,payoff_ratio_status:"no_losses",eligible_sample_count:1}};
const base={status:"active",status_label:"风险预算内",portfolio_planned_risk:"0",
  portfolio_planned_risk_pct:"0",portfolio_risk_limit_pct:"0.04",portfolio_remaining_risk:"4000",
  portfolio_remaining_risk_pct:"0.04",single_entry_risk_limit:"400",single_entry_risk_limit_pct:"0.004",
  abnormal_loss_buffer:"1000",abnormal_loss_buffer_pct:"0.01",disclaimer:"风险提示",
  portfolio_remaining_risk_note:"说明",trade_stats:tradeStats};
const before=renderTrendRiskSummary(base);
let reloads=0; loadDashboard=async()=>{reloads+=1;};
globalThis.fetch=async()=>({ok:false,status:400,json:async()=>({status:"error",message:"辉立成交表格式无法识别"})});
let reason="";
try { await uploadStatement("phillips", {name:"statement.pdf",size:100}); }
catch (error) { reason=error.message; }
const after=renderTrendRiskSummary(base);
console.log(JSON.stringify({reason,reloads,same:before===after,kept:after.includes("统计截至 2026-07-12T23:59:59+08:00")}));
''')

    assert json.loads(output) == {
        "reason": "辉立成交表格式无法识别",
        "reloads": 0,
        "same": True,
        "kept": True,
    }


def test_dashboard_renders_validated_and_fallback_decision_plans() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
const validatedPlan = {
  available: true,
  mode: "validated_plan",
  status: "waiting",
  run_date: "2026-07-13",
  action_summary: "继续持有，等待条件触发",
  next_condition_id: "trend-exit",
  current_quantity: "400",
  current_weight: "0.078",
  max_weight: "0.10",
  risk_status: "within_limit",
  strategy: {id: "trend_pullback/v1", name_zh: "趋势回调"},
  conditions: [{
    condition_id: "trend-exit", priority: "risk", operator: "<=",
    calculated_value: "57", target_weight: "0", target_quantity: "0",
    suggested_action: "退出", formula: "min(sma50, active_stop)",
    inputs: {sma50: "58", active_stop: "57"}, source_date: "2026-07-10",
    trigger_count: 2,
  }],
  backtests: [{
    range: "1Y", gate: {passed: true},
    strategy: {total_return_pct: "8", max_drawdown_pct: "6", sharpe_ratio: "1.1", calmar_ratio: "1.3"},
    market_benchmark: {symbol: "SPY", total_return_pct: "5.5"},
    market_excess_return_pct: "2.5",
  }],
  previous_review: {run_date: "2026-07-10", status: "triggered", trigger_count: 1, starting_quantity: "400", closing_quantity: "400"},
};
const validated = renderDecisionPlan({decision_plan: validatedPlan});
for (const text of ["今日交易计划", "下一条件", "目标仓位", "回测闸门", "最大回撤", "夏普比率", "卡玛比率", "参数来源", "上期复盘"]) {
  if (!validated.includes(text)) throw new Error("missing " + text + ": " + validated);
}
if (!validated.includes("data-plan-condition")) throw new Error("validated plan has no condition cards");
if (!validated.includes("<dt>卡玛比率</dt><dd>1.30</dd>")) throw new Error("calmar ratio is not readable: " + validated);

const fallbackPlan = {
  available: true,
  mode: "fallback_advice",
  status: "waiting",
  run_date: "2026-07-13",
  max_weight: "0.10",
  backtests: [{
    range: "1Y", strategy_id: "range_mean_reversion/v1", gate: {passed: false},
    strategy: {total_return_pct: "-1", max_drawdown_pct: "8", sharpe_ratio: "-0.03", calmar_ratio: "-0.04"},
    market_benchmark: {symbol: "SPY", total_return_pct: "5.5"},
    market_excess_return_pct: "-6.5",
  }],
  fallback: {
    label: "非执行型建议", reason: "没有策略通过当前回测闸门", recommendation: "禁止加仓",
    max_weight: "0.10", tradingagents: {current_action: "观察", core_reason: "等待趋势确认"},
    facts: [
      {key: "ma20_distance_pct", calculated_value: "-3.2", formula: "(close/sma20-1)*100", inputs: {close: "47"}, source_date: "2026-07-10"},
      {key: "rsi14", calculated_value: "31", formula: "RSI(14)", inputs: {period: "14"}, source_date: "2026-07-10"},
      {key: "bollinger_position", calculated_value: "below_lower", formula: "compare bands", inputs: {close: "47"}, source_date: "2026-07-10"},
    ],
  },
};
const fallback = renderDecisionPlan({decision_plan: fallbackPlan});
for (const text of ["非执行型建议", "禁止加仓", "RSI", "布林带", "为什么没有可执行计划", "回测闸门", "夏普比率", "卡玛比率", "range_mean_reversion/v1"]) {
  if (!fallback.includes(text)) throw new Error("missing " + text + ": " + fallback);
}
if (fallback.includes("data-plan-condition")) throw new Error("fallback rendered executable condition");
`, sandbox);
'''
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_final_tab_uses_plan_contract_and_deep_link_helpers() -> None:
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    final_view = js.split("final: {", 1)[1].split("},", 1)[0]
    assert "holding.decision_plan" in final_view
    assert "renderDecisionPlan(holding)" in final_view
    assert "holding.agent_report" not in final_view
    assert "restoreDecisionDeepLink" in js
    assert "syncDecisionDeepLink" in js
    assert "history.replaceState" in js


def test_backtest_options_payload_exposes_fixed_catalog_and_defaults(tmp_path) -> None:
    from open_trader.dashboard_web import build_standard_backtest_options_payload

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    payload = build_standard_backtest_options_payload(config)

    assert [item["id"] for item in payload["strategies"]] == [
        "trend_pullback/v1", "breakout_momentum/v1", "range_mean_reversion/v1",
    ]
    assert payload["ranges"] == ["6M", "1Y", "3Y", "5Y", "CUSTOM"]
    assert payload["defaults"] == {
        "range": "1Y", "initial_cash": "100000", "max_strategy_weight": "0.10",
        "commission_bps": "10", "slippage_bps": "5",
    }
    assert payload["benchmarks"]["CN"] == "000300"


def test_cn_standard_backtest_owns_futu_provider(tmp_path, monkeypatch) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({"market": "CN", "symbol": "600025", "asset_class": "stock"})
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])
    provider = object()
    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: provider)
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", lambda request, *, price_provider: type("Result", (), {"to_dict": lambda self: {"provider": price_provider}})())

    result = dashboard_web.build_standard_backtest_run_payload(config, {
        "market": "CN", "symbol": "600025", "strategy_id": "trend_pullback/v1",
    })
    assert result["provider"] is provider


def test_standard_backtest_run_rejects_adapter_choice(tmp_path) -> None:
    from open_trader.dashboard_web import build_standard_backtest_run_payload

    config = dashboard_config(tmp_path)
    with pytest.raises(ValueError, match="不支持从界面选择回测执行工具"):
        build_standard_backtest_run_payload(config, {"adapter": "simple"})


def test_standard_backtest_request_parses_percent_and_normalizes_hk_symbol(tmp_path) -> None:
    from decimal import Decimal
    from open_trader.dashboard_web import parse_standard_backtest_request

    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({"market": "HK", "symbol": "700", "asset_class": "stock"})
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])

    parsed = parse_standard_backtest_request(config, {
        "market": "hk", "symbol": "00700", "strategy_id": "trend_pullback/v1",
        "range_preset": "CUSTOM", "custom_start": "2025-01-01",
        "custom_end": "2026-01-01", "max_strategy_weight": "10%",
    })

    assert parsed.market == "HK"
    assert parsed.symbol == "00700"
    assert parsed.max_strategy_weight == Decimal("0.10")
    assert parsed.custom_start == date(2025, 1, 1)


def test_standard_backtest_request_allows_custom_range_without_end_date(tmp_path) -> None:
    from open_trader.dashboard_web import parse_standard_backtest_request

    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({"market": "US", "symbol": "MSFT", "asset_class": "stock"})
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])
    parsed = parse_standard_backtest_request(config, {
        "market": "US", "symbol": "MSFT", "strategy_id": "trend_pullback/v1",
        "range_preset": "CUSTOM", "custom_start": "2025-01-01",
    })
    assert parsed.custom_start == date(2025, 1, 1)
    assert parsed.custom_end is None


@pytest.mark.parametrize("symbol", ["../../outside", "..", "BAD/S", "BAD\\S", "BAD:S", "BAD S"])
def test_standard_backtest_request_rejects_unsafe_symbol_grammar(tmp_path, symbol) -> None:
    from open_trader.dashboard_web import parse_standard_backtest_request

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    with pytest.raises(ValueError, match="标的代码格式无效"):
        parse_standard_backtest_request(config, {
            "market": "US", "symbol": symbol,
            "strategy_id": "trend_pullback/v1", "range_preset": "1Y",
        })


def test_standard_backtest_http_routes_expose_options_and_map_validation_to_400(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        options = read_json(f"http://{host}:{port}/api/backtests/options")
        assert options["defaults"]["range"] == "1Y"
        status, _, payload = post_error_json(
            f"http://{host}:{port}/api/backtests/standard/run",
            json.dumps({"adapter": "simple"}).encode(),
        )
        assert status == 400
        assert payload["message"] == "不支持从界面选择回测执行工具"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_server_ignores_client_disconnect_while_writing_json(
    tmp_path, monkeypatch
) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    request_started = threading.Event()
    release_response = threading.Event()

    def delayed_options(_config: object) -> dict[str, str]:
        request_started.set()
        assert release_response.wait(timeout=5)
        return {"padding": "x" * (5 * 1024 * 1024)}

    monkeypatch.setattr(
        dashboard_web, "build_standard_backtest_options_payload", delayed_options
    )
    server = dashboard_web.create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    unhandled_errors: list[BaseException | None] = []
    handler_completed = threading.Event()
    original_shutdown_request = server.shutdown_request

    def shutdown_request(request: object) -> None:
        try:
            original_shutdown_request(request)  # type: ignore[arg-type]
        finally:
            handler_completed.set()

    server.shutdown_request = shutdown_request  # type: ignore[method-assign]
    server.handle_error = (  # type: ignore[method-assign]
        lambda _request, _address: unhandled_errors.append(sys.exc_info()[1])
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    client = socket.create_connection((host, port), timeout=5)
    try:
        client.sendall(
            b"GET /api/backtests/options HTTP/1.1\r\n"
            b"Host: dashboard\r\nConnection: close\r\n\r\n"
        )
        assert request_started.wait(timeout=5)
        client.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_LINGER,
            struct.pack("ii", 1, 0),
        )
        client.close()
        release_response.set()
        assert handler_completed.wait(timeout=5)
        assert unhandled_errors == []
    finally:
        client.close()
        release_response.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("body", [b"{bad json", b"[]"])
def test_standard_backtest_http_rejects_invalid_json_objects_with_chinese_400(
    tmp_path, body
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        status, _, payload = post_error_json(
            f"http://{host}:{port}/api/backtests/standard/run", body
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert status == 400
    assert payload["message"] == "请求正文必须是有效的 JSON 对象"


@pytest.mark.parametrize(
    ("content_length", "expected_status", "expected_message"),
    [
        ("invalid", 400, "Content-Length 必须是非负整数"),
        ("-1", 400, "Content-Length 必须是非负整数"),
        (str(1024 * 1024 + 1), 413, "请求正文不能超过 1 MiB"),
    ],
)
def test_dashboard_http_rejects_invalid_or_oversized_content_length_before_read(
    tmp_path, content_length, expected_status, expected_message
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.putrequest("POST", "/api/backtests/standard/run")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", content_length)
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert response.status == expected_status
    assert payload["message"] == expected_message


def test_owned_backtest_provider_close_failure_is_execution_error(tmp_path, monkeypatch) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    class Provider:
        def close(self) -> None:
            raise RuntimeError("close boom")

    class Result:
        def to_dict(self) -> dict[str, str]:
            return {"status": "ok"}

    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: Provider())
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", lambda *_, **__: Result())
    request = {"market": "US", "symbol": "VIXY", "strategy_id": "trend_pullback/v1"}

    with pytest.raises(dashboard_web.StandardBacktestExecutionError, match="关闭.*close boom"):
        dashboard_web.build_standard_backtest_run_payload(config, request)


def test_owned_backtest_provider_close_failure_does_not_mask_run_failure(tmp_path, monkeypatch) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    class Provider:
        def close(self) -> None:
            raise RuntimeError("close boom")

    def fail(*args, **kwargs):
        raise RuntimeError("run boom")

    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: Provider())
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", fail)
    request = {"market": "US", "symbol": "VIXY", "strategy_id": "trend_pullback/v1"}

    with pytest.raises(dashboard_web.StandardBacktestExecutionError, match="run boom") as error:
        dashboard_web.build_standard_backtest_run_payload(config, request)
    assert "close boom" not in str(error.value)


@pytest.mark.parametrize(
    ("run_error", "expected"),
    [(None, "行情服务关闭失败：close boom"), ("run boom", "标准策略回测执行失败：run boom")],
)
def test_standard_backtest_http_maps_owned_provider_lifecycle_errors_to_502(
    tmp_path, monkeypatch, run_error, expected
) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    class Provider:
        def close(self) -> None:
            raise RuntimeError("close boom")

    class Result:
        def to_dict(self) -> dict[str, str]:
            return {"status": "ok"}

    def run(*args, **kwargs):
        if run_error:
            raise RuntimeError(run_error)
        return Result()

    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: Provider())
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", run)
    server = dashboard_web.create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        status, _, payload = post_error_json(
            f"http://{host}:{port}/api/backtests/standard/run",
            json.dumps({
                "market": "US", "symbol": "VIXY",
                "strategy_id": "trend_pullback/v1",
            }).encode(),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert status == 502
    assert payload["message"] == expected


def test_dashboard_static_removes_legacy_holding_backtest_ui() -> None:
    source = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "查看回测" not in source
    assert 'data-detail-mode="backtest"' not in source
    assert 'fetch("/api/backtests/run"' not in source
    assert "header-backtest-filters" not in html
    assert "backtest-price-sync-status" not in html


def test_dashboard_has_one_global_backtest_entry_and_no_row_entry() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert html.count('id="open-standard-backtest"') == 1
    assert 'id="standard-backtest-workspace"' in html
    assert 'id="header-backtest-filters"' not in html
    assert 'data-detail-mode="backtest"' not in js
    assert "查看回测" not in js


def test_standard_backtest_workspace_builds_request_without_adapter() -> None:
    output = run_dashboard_js(
        r"""
state.standardBacktest.symbolKey = "US:MSFT";
state.standardBacktest.strategyId = "trend_pullback/v1";
state.standardBacktest.rangePreset = "3Y";
state.standardBacktest.initialCash = "250000";
state.standardBacktest.maxWeight = "10%";
const request = buildStandardBacktestRequest();
if (request.market !== "US" || request.symbol !== "MSFT") throw new Error(JSON.stringify(request));
if (request.strategy_id !== "trend_pullback/v1" || request.range_preset !== "3Y") throw new Error(JSON.stringify(request));
if (request.adapter !== undefined) throw new Error("adapter leaked to UI");
if (request.initial_cash !== "250000") throw new Error(JSON.stringify(request));
if (request.max_strategy_weight !== "10%" || request.commission_bps !== "10") throw new Error(JSON.stringify(request));
console.log("ok");
"""
    )
    assert "ok" in output


def test_standard_backtest_custom_dates_and_safe_error_contract() -> None:
    output = run_dashboard_js(
        r"""
state.standardBacktest.rangePreset = "CUSTOM";
state.standardBacktest.customStart = "";
state.standardBacktest.customEnd = "";
if (validateStandardBacktestDates() !== "自定义区间必须填写开始日期。") throw new Error("missing start");
state.standardBacktest.customStart = "2026-01-02";
state.standardBacktest.customEnd = "2026-01-02";
if (validateStandardBacktestDates() !== "开始日期必须早于结束日期。") throw new Error("equal dates");
state.standardBacktest.customEnd = "";
if (validateStandardBacktestDates() !== "") throw new Error("optional end rejected");
if (safeBacktestErrorMessage({message: "参数有误"}) !== "参数有误") throw new Error("Chinese message lost");
if (safeBacktestErrorMessage({message: "Internal Server Error"}) !== "回测请求失败，请稍后重试。") throw new Error("English leaked");
if (safeBacktestErrorMessage({message: "参数 invalid: Internal Server Error"}) !== "回测请求失败，请稍后重试。") throw new Error("mixed English leaked");
if (safeBacktestErrorMessage({message: "参数 X 无效"}) !== "回测请求失败，请稍后重试。") throw new Error("single Latin leaked");
if (safeBacktestErrorMessage({message: "错误 E"}) !== "回测请求失败，请稍后重试。") throw new Error("Latin code leaked");
if (safeBacktestErrorMessage(null) !== "回测请求失败，请稍后重试。") throw new Error("fallback missing");
console.log("ok");
"""
    )
    assert "ok" in output


def test_standard_backtest_workspace_accessibility_and_hidden_results_contract() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert 'id="backtest-initial-cash"' in html
    assert 'role="group"' in html
    assert "aria-pressed" in js
    assert 'elements["standard-backtest-results"].hidden = false' not in js
    assert 'elements["standard-backtest-results"].innerHTML' not in js


def test_standard_backtest_dom_click_and_submit_flow() -> None:
    output = run_dashboard_js(r"""
class E {
  constructor(){this.dataset={};this.value="";this.hidden=false;this.disabled=false;this.required=false;this.innerHTML="";this.textContent="";this.listeners={};this.classList={add(){},remove(){},toggle(){}};}
  addEventListener(n,f){this.listeners[n]=f;} click(target=this){return this.listeners.click&&this.listeners.click({target,preventDefault(){}});} submit(){return this.listeners.submit({preventDefault(){}});}
  closest(s){if(s==="[data-backtest-source]"&&this.dataset.backtestSource)return this;if(s==="[data-strategy-id]"&&this.dataset.strategyId)return this;if(s==="[data-range-preset]"&&this.dataset.rangePreset)return this;return null;} querySelector(){return null;}
}
const nodes={}; document.getElementById=(id)=>nodes[id]||(nodes[id]=new E()); document.querySelector=()=>new E(); document.getElementById("standard-backtest-results").hidden=true;
const posts=[]; fetch=async(url,init={})=>{
 if(url==="/api/backtests/options")return{ok:true,json:async()=>({strategies:[{id:"trend_pullback/v1",name_zh:"趋势回调",description_zh:"说明"},{id:"breakout_momentum/v1",name_zh:"突破动量",description_zh:"说明"},{id:"range_mean_reversion/v1",name_zh:"区间均值回归",description_zh:"说明"}],ranges:["1Y","3Y","CUSTOM"],defaults:{range:"1Y",initial_cash:"100000",max_strategy_weight:"0.10",commission_bps:"10",slippage_bps:"5"},universe:{holdings:[{market:"US",symbol:"MSFT",name:"微软"}],watchlist:[{market:"HK",symbol:"00700",name:"腾讯"}]}})};
 posts.push({url,body:JSON.parse(init.body)});if(posts.length===2)return{ok:false,json:async()=>{throw new Error("html")}};return{ok:true,json:async()=>({status:"ok"})};};
bindElements();bindEvents();state.brokerFilter="tiger";state.marketFilter="HK";await elements["open-standard-backtest"].click();
if(elements["standard-backtest-workspace"].hidden||state.standardBacktest.symbolKey!=="US:MSFT")throw new Error("open failed");
const watch=new E();watch.dataset.backtestSource="watchlist";elements["backtest-symbol-source"].click(watch);
const range=new E();range.dataset.rangePreset="3Y";elements["backtest-range-controls"].click(range);
elements["backtest-initial-cash"].value="250000";elements["backtest-max-weight"].value="12%";elements["backtest-commission"].value="8";elements["backtest-slippage"].value="3";
await elements["standard-backtest-form"].submit();
if(posts.length!==1||posts[0].url!=="/api/backtests/standard/run"||posts[0].body.adapter!==undefined||posts[0].body.initial_cash!=="250000")throw new Error(JSON.stringify(posts));
if(elements["standard-backtest-results"].hidden||!elements["standard-backtest-results"].innerHTML.includes("回测对比"))throw new Error("results missing");
await elements["standard-backtest-form"].submit();if(elements["standard-backtest-status"].textContent!=="回测请求失败，请稍后重试。")throw new Error("unsafe fallback");
const custom=new E();custom.dataset.rangePreset="CUSTOM";elements["backtest-range-controls"].click(custom);if(!elements["backtest-custom-start"].required||elements["backtest-custom-end"].required)throw new Error("required mismatch");
elements["backtest-custom-start"].value="";await elements["standard-backtest-form"].submit();if(posts.length!==2||elements["standard-backtest-status"].textContent!=="自定义区间必须填写开始日期。")throw new Error("missing start fetched");
elements["backtest-custom-start"].value="2026-01-02";elements["backtest-custom-end"].value="2026-01-02";await elements["standard-backtest-form"].submit();if(posts.length!==2||elements["standard-backtest-status"].textContent!=="开始日期必须早于结束日期。")throw new Error("date order fetched");
elements["return-to-portfolio"].click();if(state.workspaceView!=="portfolio"||state.brokerFilter!=="tiger"||state.marketFilter!=="HK")throw new Error("return failed");await elements["open-standard-backtest"].click();if(state.standardBacktest.initialCash!=="250000"||state.standardBacktest.source!=="watchlist")throw new Error("state lost");
console.log("ok");
""")
    assert "ok" in output


def test_standard_backtest_result_renders_normalized_comparisons_and_details(tmp_path) -> None:
    from tests.test_strategy_backtest import fixture_provider, standard_request
    from open_trader.strategy_backtest import run_standard_backtest

    fixture_result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=fixture_provider("breakout_next_open"),
    ).to_dict()
    fixture_result.update({
        "benchmark_symbol": "<SPY>", "run_id": "<run>",
        "requested_start": "<2025-01-01>", "manifest_path": "data/<manifest>.json",
    })
    fixture_result["strategy"]["trades"][0]["reason"] = "<规则触发>"
    output = run_dashboard_js('''
const target={innerHTML:"",hidden:true}; document.getElementById=(id)=>id==="standard-backtest-results"?target:null;
const fixtureResult=''' + json.dumps(fixture_result, ensure_ascii=False) + r''';
renderStandardBacktestResult(fixtureResult);
for(const expected of ["策略收益","买入持有","&lt;SPY&gt;","相对买入持有","相对市场指数","最大回撤","交易次数","胜率","BUY","EXIT","请求范围","实际数据","breakout_momentum/v1","交易假设","初始资金","最大策略仓位","佣金","滑点","固定参数","突破周期","HOLD（观察）","结果文件"]){if(!target.innerHTML.includes(expected))throw new Error(`missing ${expected}`)}
for(const hostile of ["data/<manifest>","<规则触发>","<2025-01-01>","<run>"]){if(target.innerHTML.includes(hostile))throw new Error("dynamic value not escaped: "+hostile)}
if(target.hidden)throw new Error("result remains hidden"); console.log("ok");
''')
    assert "ok" in output


def test_generated_standard_backtest_payload_renders_finite_price_path_and_marker(tmp_path) -> None:
    from tests.test_strategy_backtest import fixture_provider, standard_request
    from open_trader.strategy_backtest import run_standard_backtest

    fixture_result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=fixture_provider("breakout_next_open"),
    ).to_dict()
    output = run_dashboard_js('''
const result=''' + json.dumps(fixture_result, ensure_ascii=False) + r''';
const chart=renderPriceActionChart(result.strategy.equity_curve,result.strategy.trades);
const path=(chart.match(/class="backtest-price-line" d="([^"]+)"/)||[])[1]||"";
if(!path.includes("M")||!path.includes("L")||/NaN|Infinity/.test(path))throw new Error(`invalid price path: ${path}`);
const marker=(chart.match(/<circle cx="([^"]+)" cy="([^"]+)" r="5"><\/circle>/)||[]);
if(!marker.length||!Number.isFinite(Number(marker[1]))||!Number.isFinite(Number(marker[2])))throw new Error(`invalid marker: ${chart}`);
console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_treats_zero_trades_as_success() -> None:
    output = run_dashboard_js(r'''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const result={strategy:{trades:[],equity_curve:[],total_return_pct:"0",max_drawdown_pct:"0",win_rate_pct:"0",initial_cash:"100",initial_allocated_notional:"10"},buy_hold:{equity_curve:[],total_return_pct:"0"},market_benchmark:{equity_curve:[],total_return_pct:"0"},benchmark_symbol:"SPY"};
renderStandardBacktestResult(result); if(!target.innerHTML.includes("所选区间内没有触发交易")||target.innerHTML.includes("error"))throw new Error(target.innerHTML); console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_isolates_missing_market_benchmark(tmp_path) -> None:
    from tests.test_strategy_backtest import fixture_provider, standard_request
    from open_trader.strategy_backtest import run_standard_backtest

    fixture_result = run_standard_backtest(
        standard_request(tmp_path), price_provider=fixture_provider("missing_benchmark"),
    ).to_dict()
    output = run_dashboard_js('''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const result=''' + json.dumps(fixture_result, ensure_ascii=False) + r''';
renderStandardBacktestResult(result); if(!target.innerHTML.includes("策略收益")||!target.innerHTML.includes("基准行情缺失，无法比较"))throw new Error(target.innerHTML); console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_bounds_large_and_invalid_chart_data() -> None:
    output = run_dashboard_js(r'''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const rows=Array.from({length:50000},(_,i)=>({date:`2025-${String(1+(i%12)).padStart(2,"0")}-${String(1+(i%28)).padStart(2,"0")}-${i}`,equity:String(100000+i),close:String(100+i/100)}));
rows[10].equity="NaN"; rows[11].equity="Infinity"; rows[12].close="bad";
const trades=Array.from({length:700},(_,i)=>({execution_date:rows[i*50].date,action:i%2?"BUY":"HOLD",quantity:"1",raw_price:i===2?"Infinity":rows[i*50].close,execution_price:"100",fees:"1",reason:"记录"}));
const result={strategy:{trades,equity_curve:rows,total_return_pct:"1",max_drawdown_pct:"-1",win_rate_pct:"1"},buy_hold:{equity_curve:rows,total_return_pct:"1"},market_benchmark:{equity_curve:rows,total_return_pct:"1"},benchmark_symbol:"SPY",signals:[],assumptions:{},strategy_definition:{parameters:{}}};
renderStandardBacktestResult(result);
if(/NaN|Infinity/.test(target.innerHTML))throw new Error("non-finite SVG output");
if((target.innerHTML.match(/<tr>/g)||[]).length!==501)throw new Error("trade rows not bounded");
if(!target.innerHTML.includes("仅显示前 500 笔，共 700 笔"))throw new Error("missing trade limit notice");
for(const d of [...target.innerHTML.matchAll(/ d="([^"]*)"/g)].map(x=>x[1]))if((d.match(/[ML]/g)||[]).length>600)throw new Error("chart not downsampled");
console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_aggregates_and_bounds_action_markers() -> None:
    output = run_dashboard_js(r'''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const rows=Array.from({length:1000},(_,i)=>({date:`d${i}`,equity:String(100000+i),close:String(100+i/100)}));
const actions=["BUY","ADD","REDUCE","EXIT"];
const trades=Array.from({length:50000},(_,i)=>({execution_date:rows[i%rows.length].date,action:actions[i%4],quantity:"1",raw_price:i===49999?"Infinity":rows[i%rows.length].close,execution_price:"100",fees:"1",reason:"大量记录"}));
const result={strategy:{trades,equity_curve:rows,total_return_pct:"1",max_drawdown_pct:"-1",win_rate_pct:"1"},buy_hold:{equity_curve:rows,total_return_pct:"1"},market_benchmark:{equity_curve:rows,total_return_pct:"1"},benchmark_symbol:"SPY",signals:[],assumptions:{},strategy_definition:{parameters:{}}};
const chart=renderPriceActionChart(rows,trades);
const markerCount=(chart.match(/<g class="backtest-action-marker/g)||[]).length;
if(markerCount>600)throw new Error(`unbounded markers ${markerCount}`);
if(!chart.includes("×"))throw new Error("aggregated count missing");
if(!chart.includes("另有 ")||!chart.includes("组交易标记未显示"))throw new Error("omitted notice missing");
const aria=(chart.match(/aria-label="([^"]*)"/)||[])[1]||"";
if(aria.length>50000)throw new Error(`unbounded aria ${aria.length}`);
if(/NaN|Infinity/.test(chart))throw new Error("invalid numeric output");
console.log("ok");
''')
    assert "ok" in output


class FakeQuoteService:
    def __init__(self, result: QuoteRefreshResult) -> None:
        self.result = result
        self.refresh_count = 0

    def refresh(self) -> QuoteRefreshResult:
        self.refresh_count += 1
        return self.result


class RaisingQuoteService:
    def refresh(self) -> QuoteRefreshResult:
        raise RuntimeError("boom")


class FakeTrendSimulatePositionService:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def load(self, broker: str) -> dict[str, Any]:
        self.calls.append(broker)
        return {"broker": broker, "positions": []}


class FakeAccountSyncService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.refresh_count = 0

    def refresh_if_due(self) -> object:
        self.refresh_count += 1

        class Result:
            def to_dict(inner_self) -> dict[str, Any]:
                return dict(self.payload)

        return Result()


class FakeStatementImportService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def import_pdf(self, broker: str, body: bytes) -> dict[str, Any]:
        self.calls.append((broker, body))
        return {
            "status": "ok",
            "broker": broker,
            "statement_date": "2026-07-10",
            "positions": 1,
            "cash": 1,
            "warnings": 0,
        }


class FakeBacktestPriceProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[object]:
        from open_trader.kline_technical_facts import DailyKlineBar

        self.requests.append({"futu_symbol": futu_symbol, "start": start, "end": end})
        return [
            DailyKlineBar(
                date="2026-06-19",
                open=41.0,
                high=43.0,
                low=40.0,
                close=42.0,
                volume=1000.0,
            )
        ]


class RaisingBacktestPriceProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[object]:
        self.requests.append({"futu_symbol": futu_symbol, "start": start, "end": end})
        raise RuntimeError("kline unavailable")


def quote_result() -> QuoteRefreshResult:
    return QuoteRefreshResult(
        status="ok",
        requested_count=1,
        quote_count=1,
        missing_count=0,
        fetched_at="2026-06-19T09:30:00+08:00",
        last_success_at="2026-06-19T09:30:00+08:00",
        stale=False,
        quotes={
            "US.MSFT": {
                "market": "US",
                "symbol": "MSFT",
                "name": "Microsoft",
                "futu_symbol": "US.MSFT",
                "status": "ok",
                "last_price": "500",
                "fetched_at": "2026-06-19T09:30:00+08:00",
                "stale": False,
            }
        },
        diagnostic={},
        fallback_count=0,
    )


def read_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        content_length = response.headers["Content-Length"]
        payload = response.read()
        assert content_length == str(len(payload))
        return json.loads(payload.decode("utf-8"))


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        return json.loads(response.read().decode("utf-8"))


def post_error_json(url: str, body: bytes) -> tuple[int, str, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        payload = error.read()
        assert error.headers["Content-Length"] == str(len(payload))
        return (
            error.code,
            error.headers["Content-Type"],
            json.loads(payload.decode("utf-8")),
        )
    raise AssertionError("expected HTTPError")


def post_pdf(url: str, body: bytes) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/pdf"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def post_pdf_error(
    url: str,
    body: bytes,
    *,
    content_type: str = "application/pdf",
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    raise AssertionError("expected HTTPError")


def post_text_error(url: str, body: bytes) -> tuple[int, str, str]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        payload = error.read()
        assert error.headers["Content-Length"] == str(len(payload))
        return (
            error.code,
            error.headers["Content-Type"],
            payload.decode("utf-8"),
        )
    raise AssertionError("expected HTTPError")


def holdings_table_header_labels(html: str) -> list[str]:
    table_prefix = html.split('<tbody id="holdings-body">', 1)[0]
    thead = table_prefix.rsplit("<thead>", 1)[1].split("</thead>", 1)[0]
    labels: list[str] = []
    for segment in thead.split("<th>")[1:]:
        labels.append(segment.split("</th>", 1)[0].strip())
    return labels


def read_error_json(url: str) -> tuple[int, str, dict[str, Any]]:
    try:
        urllib.request.urlopen(url, timeout=5)
    except urllib.error.HTTPError as error:
        payload = error.read()
        assert error.headers["Content-Length"] == str(len(payload))
        return (
            error.code,
            error.headers["Content-Type"],
            json.loads(payload.decode("utf-8")),
        )
    raise AssertionError("expected HTTPError")


def read_text_error(url: str) -> tuple[int, str, str]:
    try:
        urllib.request.urlopen(url, timeout=5)
    except urllib.error.HTTPError as error:
        payload = error.read()
        assert error.headers["Content-Length"] == str(len(payload))
        return (
            error.code,
            error.headers["Content-Type"],
            payload.decode("utf-8"),
        )
    raise AssertionError("expected HTTPError")


def run_dashboard_js(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    runner = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} }, console, URLSearchParams };
(async () => {
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  await vm.runInContext(`(async () => {${process.argv[2]}})()`, sandbox);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", runner, str(js_path), script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_dashboard_display_number_formats_numeric_text_only() -> None:
    output = run_dashboard_js(r'''
console.log(JSON.stringify({
  money: formatDisplayNumber("3064187.62"),
  integer: formatDisplayNumber("10000"),
  trailing: formatDisplayNumber("2932.00"),
  signed: formatDisplayNumber("+1234567.50"),
  symbol: formatPlain("02840"),
  percent: formatDisplayNumber("21.13%"),
  input: "100000",
  profit: pnlClass("12.50%"),
  loss: pnlClass("-12.50%"),
}));
''')
    assert json.loads(output) == {
        "money": "3,064,187.62",
        "integer": "10,000",
        "trailing": "2,932",
        "signed": "+1,234,567.5",
        "symbol": "02840",
        "percent": "21.13%",
        "input": "100000",
        "profit": "pnl-profit",
        "loss": "pnl-loss",
    }


def test_dashboard_numbers_never_show_more_than_two_decimal_places() -> None:
    output = run_dashboard_js(r'''
console.log(JSON.stringify([
  formatDisplayNumber("485.0"),
  formatDisplayNumber("1296"),
  formatDisplayNumber("30.594999999999995"),
  formatDisplayNumber("23.428857142857142857"),
]));
''')
    assert json.loads(output) == ["485", "1,296", "30.59", "23.43"]


def test_dashboard_trend_stages_format_only_numeric_fields_losslessly() -> None:
    output = run_dashboard_js(r'''
const cn = [
  renderCnSellOrHoldStage("卖出", [{
    symbol:"02840",name:"SPDR 金",close:"24.545714285714",strength:"99.876",
    temperature_prev:"温",temperature_curr:"热",reason:"left_trend_right_side",
    active_line:"9007199254740993",entry_hints:["编号 00001234"],
    execution:{status:"partially_filled",filled_qty:"13.129",target_qty:"23.428",
      avg_fill_price:"207.185",order_ids:["00001234"],updated_at:"2026-07-22T09:30:00+08:00"},
  }], "sell"),
  renderCnBuyStage({buy_window:"09:30–10:00",buy_actions:[{
    symbol:"600001",name:"测试",filter_price:"1234567.505",close:"24.545714285714",
    temperature_prev:"温",temperature_curr:"热",phase:"立夏",strength:"99.876",
    industry:"科技",industry_temperature:"热",market_cap:"12345.678",amount:"2.345",
    target_weight:"0.04123456",target_amount:"39970.419",estimated_shares:"9007199254740993",
    estimated_initial_line:"23.428857142857",
  }],risk_skips:[{symbol:"600002",name:"跳过",filter_price:"10",close:"10",
    temperature_prev:"温",temperature_curr:"热",phase:"立夏",strength:"96",
    industry:"科技",industry_temperature:"热",market_cap:"100",amount:"2",
    target_weight:"0.04123456",target_amount:"8888.888"}]}),
].join("");
const us = [
  renderMarketSellOrHoldStage("持有", [{
    symbol:"00001234",name:"编号测试",close:"30.594999999999995",strength:"90.444",
    reason:"trend_intact",active_line:"28.305071428571",
  }], "hold"),
  renderMarketBuyStage({buy_window:"常规时段",buy_actions:[{
    symbol:"EA",name:"艺电",close:"207.185",strength:"99.876",industry:"通讯服务",
    target_weight:"0.04123456",target_amount:"4941.499",estimated_shares:"9007199254740993",
    estimated_initial_line:"205.46930",execution:{status:"partially_filled",
      filled_qty:"13.129",target_qty:"23.428",avg_fill_price:"207.185",
      order_ids:["00001234"],updated_at:"2026-07-22T09:30:00+08:00"},
  }],risk_skips:[]}),
].join("");
console.log(JSON.stringify({cn,us}));
''')
    rendered = json.loads(output)
    combined = rendered["cn"] + rendered["us"]
    for expected in (
        "1,234,567.51", "24.55", "99.88", "12,345.68", "2.35",
        "39,970.42", "8,888.89", "9,007,199,254,740,993 股", "23.43", "30.59",
        "90.44", "28.31", "4,941.5", "205.47", "成交 13.13 / 23.43",
        "均价 207.19", "目标仓位 4.12%",
    ):
        assert expected in combined
    for preserved in (
        "02840 SPDR 金", "600001 测试", "00001234 编号测试",
        "订单 00001234", "2026-07-22T09:30:00+08:00", "编号 00001234",
    ):
        assert preserved in combined
    for raw in (
        "24.545714285714", "99.876", "12345.678", "2.345", "39970.419",
        "30.594999999999995", "90.444", "28.305071428571", "4941.499", "8888.888",
        "13.129", "23.428", "207.185",
    ):
        assert raw not in combined


def test_dashboard_display_number_preserves_lossless_integer_semantics() -> None:
    output = run_dashboard_js(r'''
console.log(JSON.stringify([
  formatDisplayNumber("9007199254740993"),
  formatDisplayNumber("+1234567.50"),
  formatDisplayNumber("00001234"),
]));
''')
    assert json.loads(output) == [
        "9,007,199,254,740,993",
        "+1,234,567.5",
        "00,001,234",
    ]


def test_dashboard_account_table_formats_values_but_not_symbol() -> None:
    output = run_dashboard_js(r'''
console.log(renderAccountTable([{key:"futu:HK:02840:0",holding:{},display:{
  market:"HK",symbol:"02840",name:"SPDR 金",total_quantity:"10000",
  avg_cost_price:"2932.00",market_value_hkd:"31845000.00",
  account_weight:"3.28%",portfolio_weight:"1.04%",unrealized_pnl_pct:"-1.26%"
}}]));
''')
    assert "10,000" in output
    assert "2,932" in output
    assert "HKD 31,845,000" in output
    assert ">02840<" in output
    assert 'class="number-cell account-holding-pnl pnl-loss"' in output


def test_dashboard_formats_named_read_only_numeric_surfaces_only() -> None:
    output = run_dashboard_js(r'''
const quote = renderQuotePrice({market:"HK"}, {last_price:"1234567.50"});
const kelly = [
  renderKellyStrategyCapital({capital:{
    available:true,currency:"USD",budget:"1234567.50",occupied_notional:"10000.00",
    available_notional:"1224567.50",utilization_pct:"0.80",open_buy_order_count:"10000",
    realized_pnl:"+2932.00",position_notional:"10000.00",reserved_order_notional:"0",
  }}),
  renderKellyOrderSync({order_sync:{
    status:"ok",environment:"SIMULATE",last_synced_at:"2026-07-16 09:30",
    order_count:"10000",fill_count:"2932",orders:[{
      market:"HK",symbol:"02840",order_id:"00001234",submitted_at:"2026-07-16 09:30",
      order_price:"1234567.50",order_qty:"10000",filled_qty:"2932",avg_fill_price:"2932.00",status:"filled",
    }],
  }}),
  renderKellyOrderExecution({order_execution:{
    status:"ok",environment:"SIMULATE",last_executed_at:"2026-07-16 09:31",
    execution_count:"10000",dry_run_count:"2932",submitted_count:"0",skipped_count:"0",failed_count:"0",
    executions:[{futu_code:"HK.02840",executed_at:"2026-07-16 09:31",side:"buy",price:"1234567.50",qty:"10000",planned_notional:"29320000.00",futu_order_id:"00001234",execution_status:"dry_run"}],
  }}),
].join("");
const backtest = [
  renderBacktestComparisonMetrics({
    strategy:{total_return_pct:"21.13",max_drawdown_pct:"-12.50",win_rate_pct:"50.00",trades:Array.from({length:10000},()=>({quantity:"1"}))},
    buy_hold:{total_return_pct:"10.00"},strategy_excess_return_pct:"11.13",
  }),
  renderBacktestTradeTable({strategy:{trades:[{execution_date:"2026-07-16",action:"BUY",quantity:"10000",execution_price:"2932.00",fees:"1234.50",reason:"记录"}]}}),
  renderBacktestRunAssumptions({
    requested_start:"2026-01-01",requested_end:"2026-07-16",actual_start:"2026-01-02",actual_end:"2026-07-16",
    strategy_id:"trend_pullback/v1",adapter_version:"v1",run_id:"00001234",
    assumptions:{initial_cash:"100000",max_strategy_weight:"0.10",commission_bps:"1000",slippage_bps:"5"},
    strategy_definition:{name_zh:"趋势回调",description_zh:"说明",parameters:{sma_long:"10000"}},
    strategy:{trades:[{fees:"1234.50"}]},signals:[],
  }),
].join("");
const trend = renderTrendReportWorkspace({
  broker_label:"富途",market_label:"港股",report_date:"2026-07-16",data_date:"2026-07-15",
  generated_at:"2026-07-16 09:30",account_status:"正常",counts:{sell:"10000",buy:"2932",hold:"0",review:"0"},audit:{},
});
const decision = Object.fromEntries(decisionMetricCells({
  strategy:{target_1:"1234567.50",view:"bullish"},trade_action:{status:"pending"},
}));
console.log(JSON.stringify({quote,kelly,backtest,trend,decision,input:state.standardBacktest.initialCash}));
''')
    rendered = json.loads(output)
    assert rendered["quote"] == "1,234,567.5"
    for expected in (
        "USD 1,234,567.5", "USD +2,932", "<dt>订单</dt>",
        "<dd>10,000</dd>", "HK.02840", "00001234", "1,234,567.5",
        "29,320,000",
    ):
        assert expected in rendered["kelly"]
    for expected in (
        "10,000", "21.13%", "2026-07-16", "2,932", "1,234.5",
        "100,000", "1,000 基点", "00001234",
    ):
        assert expected in rendered["backtest"]
    assert "全部卖出 10,000" in rendered["trend"]
    assert "正式买入 2,932" in rendered["trend"]
    assert "2026-07-16" in rendered["trend"]
    assert rendered["decision"]["目标价"] == ">= 1,234,567.5"
    assert rendered["input"] == "100000"


def test_dashboard_formats_remaining_kelly_trend_and_backtest_statistics() -> None:
    output = run_dashboard_js(r'''
state.workspaceView = "kelly_lab";
state.dashboard = {kelly_lab:{available:true,experiment_count:"10000",experiments:[{
  experiment_id:"stats",experiment_name:"统计",market:"HK",status:"running",
  market_capital_pool:{currency:"HKD",amount:"29320000.00"},
  stats:{
    sample_stage:"insufficient",completed_samples:"10000",open_samples:"2932",
    winning_samples:"10000",losing_samples:"2932",raw_win_rate:"21.13%",
    payoff_ratio:"1234.50",skipped_order_count:"10000",last_recomputed_at:"2026-07-16 09:30",
  },
}]}};
const kelly = renderKellyLabPanel();
const trend = [
  renderMarketBuyStage({buy_window:"09:30–10:00",buy_actions:[{symbol:"02840",name:"SPDR 金",estimated_shares:"10000",target_amount:"29320000.00",estimated_initial_line:"1234567.50"}]}),
  renderMarketSellOrHoldStage("盘中持续 · 已有持仓", [{symbol:"02840",name:"SPDR 金",reason:"trend_intact",active_line:"1234567.50"}], "hold"),
  renderTrendAudit({
    candidates:[{symbol:"02840",name:"SPDR 金",strength:"10000"}],
    excluded:{},industry_concentration:[["科技","10000","2932.00"]],
    data_sources:[],actual_api_cost:"1234.50",
  }),
].join("");
const grouped = renderPriceActionChart(
  [{date:"same",close:"100"}],
  Array.from({length:10000},()=>({execution_date:"same",action:"BUY",raw_price:"100"})),
);
const rows = Array.from({length:10600},(_,index)=>({date:`d${index}`,close:"100"}));
const omitted = renderPriceActionChart(rows, rows.map((row)=>({execution_date:row.date,action:"BUY",raw_price:"100"})));
console.log(JSON.stringify({kelly,trend,grouped,omitted}));
''')
    rendered = json.loads(output)
    for expected in (
        "10,000 个实验", "HKD 29,320,000", "10,000 赢 / 2,932 亏",
        "1,234.5", "21.13%", "2026-07-16 09:30",
    ):
        assert expected in rendered["kelly"]
    assert ">02840 SPDR 金<" in rendered["trend"]
    for expected in (
        "10,000 股", "金额上限", "29,320,000", "预计保护线", "1,234,567.5",
        "活动保护线", "强度 10,000", "科技｜10,000｜2,932",
        "API 成本：1,234.5",
    ):
        assert expected in rendered["trend"]
    assert "×10,000" in rendered["grouped"]
    assert "共 10,000 笔" in rendered["grouped"]
    assert "另有 10,291 组交易标记未显示" in rendered["omitted"]


def test_dashboard_summary_count_fields_format_counts_not_percentages() -> None:
    output = run_dashboard_js(r'''
const mount = () => ({textContent:"",style:{}});
for (const id of [
  "current-view-value","current-view-holding-value","current-view-holding-weight","current-view-cash-note","current-view-label",
  "summary-value","summary-holding-value","summary-holding-weight","summary-cash-note","summary-holding-bar",
  "summary-brokers","summary-detail-month","summary-health","summary-health-note",
]) elements[id] = mount();
state.dashboard = {summary:{
  portfolio_value_hkd:"30000.00",holding_value_hkd:"20000.00",holding_weight_hkd:"21.13%",
  cash_like_value_hkd:"10000.00",cash_like_weight_hkd:"3.28%",holding_count:"10000",broker_count:"2932",
},holdings:[],cash_rows:[],broker_summaries:[]};
renderHeaderSummary();
const header = {cash:elements["current-view-cash-note"].textContent,weight:elements["current-view-holding-weight"].textContent};
renderSummary();
console.log(JSON.stringify({header,summary:{cash:elements["summary-cash-note"].textContent,brokers:elements["summary-brokers"].textContent,weight:elements["summary-holding-weight"].textContent}}));
''')
    rendered = json.loads(output)
    assert rendered["header"]["cash"] == "现金类资产 HKD 10,000 · 持仓 10,000"
    assert rendered["header"]["weight"] == "21.13%"
    assert rendered["summary"]["cash"] == "现金类资产 HKD 10,000 · 3.28% · 持仓 10,000"
    assert rendered["summary"]["brokers"] == "2,932 个"
    assert rendered["summary"]["weight"] == "21.13%"


def test_dashboard_account_count_renderers_format_each_count_field() -> None:
    output = run_dashboard_js(r'''
state.dashboard = {broker_summaries:[{
  broker:"futu",display_name:"富途",portfolio_value_hkd:"30000.00",holding_count:"10000",source_status:"real_time",
}],source_statuses:[]};
const tabs = renderAccountTabs([{broker:"futu",rows:new Array(10000)}]);
const section = renderAccountSection({
  broker:"futu",rows:[],profile:{horizon:"长期",strategy:"策略"},
  summary:{portfolio_value_hkd:"30000.00",holding_value_hkd:"20000.00",cash_like_value_hkd:"10000.00",holding_count:"10000"},
});
console.log(JSON.stringify({tabs,section,cards:renderBrokerSummaryCards(),label:currentViewLabel(10000)}));
''')
    rendered = json.loads(output)
    assert "富途<span>10,000</span>" in rendered["tabs"]
    assert "<span>持仓 10,000</span>" in rendered["section"]
    assert '<span class="summary-note">持仓 10,000 · 实时</span>' in rendered["cards"]
    assert rendered["label"].endswith("10,000 条")


def test_dashboard_renders_tiger_trade_available_separately_from_cash() -> None:
    output = run_dashboard_js(r'''
const group=(broker,available)=>({
  broker,rows:[],profile:{horizon:"长期",strategy:"策略"},
  summary:{broker,portfolio_value_hkd:"715000.00",holding_value_hkd:"263000.00",
    cash_like_value_hkd:"451097.00",available_to_trade_hkd:available,holding_count:"8",
    cash_components:[
      {label:"USD 现金",value_hkd:"-31208.00"},
      {label:"华泰港元货币市场基金A",value_hkd:"482305.00"},
    ]},
});
console.log(JSON.stringify({
  tiger:renderAccountSection(group("tiger","488032.24")),
  futu:renderAccountSection(group("futu","488032.24")),
}));
''')
    rendered = json.loads(output)
    assert "现金 HKD 451,097" in rendered["tiger"]
    assert "可交易额度 HKD 488,032.24" in rendered["tiger"]
    assert "现金构成" in rendered["tiger"]
    assert "USD 现金" in rendered["tiger"]
    assert "华泰港元货币市场基金A" in rendered["tiger"]
    assert "可交易额度" not in rendered["futu"]
    assert "现金构成" not in rendered["futu"]


def test_dashboard_broker_cards_always_render_four_accounts_and_derive_aliases() -> None:
    output = run_dashboard_js(r'''
state.dashboard={
  broker_summaries:[{broker:"futu",account_alias:"futu_summary",portfolio_value_hkd:"1000"}],
  source_statuses:[{broker:"eastmoney",status:"failed",display_text:"同步失败：账单缺失"}],
  cash_rows:[{broker:"tiger",account_alias:"tiger_cash"}],
  holdings:[
    {market:"HK",symbol:"02840",brokers:"phillips",broker_details:[{broker:"phillips",account_alias:"phillips_detail"}]},
    {market:"CN",symbol:"600519",brokers:"eastmoney",broker_details:[{broker:"eastmoney",account_alias:"eastmoney_detail"}]},
  ],
};
const cards=renderBrokerSummaryCards();
const groups=accountHoldingGroups();
console.log(JSON.stringify({cards,sections:Object.fromEntries(groups.map((group)=>[group.broker,renderAccountSection(group)]))}));
''')
    rendered = json.loads(output)
    assert rendered["cards"].count('class="broker-summary-card"') == 4
    for broker, label, alias in (
        ("futu", "富途", "futu_summary"),
        ("tiger", "老虎", "tiger_cash"),
        ("phillips", "辉立", "phillips_detail"),
        ("eastmoney", "东方财富", "eastmoney_detail"),
    ):
        assert f'data-broker="{broker}"' in rendered["cards"]
        assert label in rendered["cards"]
        assert alias in rendered["cards"]
        assert alias in rendered["sections"][broker]
    assert "同步失败：账单缺失" in rendered["cards"]


def test_dashboard_empty_payload_keeps_all_broker_cards_and_static_placeholders() -> None:
    output = run_dashboard_js(r'''
state.dashboard={broker_summaries:[],source_statuses:[],cash_rows:[],holdings:[]};
console.log(renderBrokerSummaryCards());
''')
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert output.count('class="broker-summary-card"') == 4
    for broker, label in (
        ("futu", "富途"), ("tiger", "老虎"),
        ("phillips", "辉立"), ("eastmoney", "东方财富"),
    ):
        assert f'data-broker="{broker}"' in output
        assert label in output
        assert f'data-broker-placeholder="{broker}"' in html


def test_dashboard_visible_count_formats_the_filtered_row_count() -> None:
    output = run_dashboard_js(r'''
const mount = () => ({innerHTML:"",textContent:"",classList:{add(){},remove(){}}});
for (const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"]) elements[id] = mount();
accountHoldingGroups = () => [{
  broker:"futu",profile:{horizon:"长期",strategy:"策略"},summary:{},
  rows:new Array(10000).fill({display:{market:"US"}}),
}];
renderAccountSection = () => "";
state.dashboard = {};
state.dashboardError = null;
renderAccountHoldings();
console.log(elements["visible-count"].textContent);
''')
    assert output.strip() == "10,000 条"


def test_dashboard_broker_detail_formats_each_numeric_field_and_pnl_class() -> None:
    output = run_dashboard_js(r'''
console.log(renderBrokerDetailSection([
  {broker:"futu",account_alias:"00001234",quantity:"10000",cost_price:"2932.00",last_price:"1234567.50",market_value:"29320000.00",unrealized_pnl:"+1234.50"},
  {broker:"tiger",account_alias:"loss",quantity:"2",cost_price:"3",last_price:"4",market_value:"5",unrealized_pnl:"-12.50%"},
  {broker:"phillips",account_alias:"zero",quantity:"0",cost_price:"0",last_price:"0",market_value:"0",unrealized_pnl:"0.00%"},
]));
''')
    assert "<td>00001234</td>" in output
    assert '<td class="number-cell">10,000</td>' in output
    assert '<td class="number-cell">2,932</td>' in output
    assert '<td class="number-cell">1,234,567.5</td>' in output
    assert '<td class="number-cell">29,320,000</td>' in output
    assert '<td class="number-cell pnl-profit">+1,234.5</td>' in output
    assert '<td class="number-cell pnl-loss">-12.5%</td>' in output
    assert '<td class="number-cell">0%</td>' in output


def test_dashboard_action_card_formats_price_and_quantity_fields_only() -> None:
    output = run_dashboard_js(r'''
console.log(renderActionCard({
  market:"HK",symbol:"02840",status:"ready",limit_price:"1234567.50",suggested_quantity:"10000",
  order_value_hkd:"29320000.00",reason:"等待人工确认",
}));
''')
    assert "<strong>HK.02840</strong>" in output
    assert "<div><span>限价</span><strong>1,234,567.5</strong></div>" in output
    assert "<div><span>数量</span><strong>10,000</strong></div>" in output
    assert "<div><span>金额</span><strong>HKD 29,320,000</strong></div>" in output


def test_dashboard_kelly_realized_pnl_classes_cover_all_polarities() -> None:
    output = run_dashboard_js(r'''
const render = (realized_pnl) => renderKellyStrategyCapital({capital:{
  available:true,currency:"USD",budget:"1",occupied_notional:"0",available_notional:"1",
  utilization_pct:"0",open_buy_order_count:"0",realized_pnl,position_notional:"0",reserved_order_notional:"0",
}});
console.log(JSON.stringify({profit:render("+1234.50"),loss:render("-1234.50"),zero:render("0.00")}));
''')
    rendered = json.loads(output)
    assert '<div class="primary">\n            <dt>可用资金</dt>\n            <dd>USD 1</dd>' in rendered["profit"]
    assert '<div class="pnl-profit">\n            <dt>已实现盈亏</dt>\n            <dd>USD +1,234.5</dd>' in rendered["profit"]
    assert '<div class="pnl-loss">\n            <dt>已实现盈亏</dt>\n            <dd>USD -1,234.5</dd>' in rendered["loss"]
    assert '<div>\n            <dt>已实现盈亏</dt>\n            <dd>USD 0</dd>' in rendered["zero"]
    assert "pnl-profit" not in rendered["zero"]
    assert "pnl-loss" not in rendered["zero"]


def test_dashboard_signed_pnl_formats_signs_groups_and_only_actual_pnl() -> None:
    output = run_dashboard_js(r'''
const account = renderAccountTable([{key:"futu:US:AAPL:0",holding:{},display:{
  market:"US",symbol:"AAPL",name:"Apple",total_quantity:"1",avg_cost_price:"1",
  market_value_hkd:"1",account_weight:"12.50%",portfolio_weight:"6.25%",unrealized_pnl_pct:"16.67%",
}}]);
const backtest = renderBacktestComparisonMetrics({
  strategy:{total_return_pct:12.5,max_drawdown_pct:8.25,trades:[],win_rate_pct:60},
  buy_hold:{total_return_pct:0},market_benchmark:{total_return_pct:"+3.50"},benchmark_symbol:"SPY",
  strategy_excess_return_pct:-2.5,market_excess_return_pct:9.25,
});
const plan = renderDecisionPlanBacktests([{
  range:"1Y",strategy_id:"demo",strategy:{total_return_pct:12.5,max_drawdown_pct:8.25,sharpe_ratio:1,calmar_ratio:1},
  market_benchmark:{symbol:"SPY",total_return_pct:0},market_excess_return_pct:"+3.50",gate:{passed:true},
}]);
console.log(JSON.stringify({
  values:[formatSignedPnl("1234567.50"),formatSignedPnl("+1234567.50"),formatSignedPnl("-1234567.50"),formatSignedPnl("0.00"),formatSignedPnl("12.50%")],
  drawdowns:[drawdownPercent(8.25),drawdownPercent(-8.25),drawdownPercent(0)],account,backtest,plan,
}));
''')
    rendered = json.loads(output)
    assert rendered["values"] == [
        "+1,234,567.5", "+1,234,567.5", "-1,234,567.5", "0", "+12.5%",
    ]
    assert rendered["drawdowns"] == ["-8.25%", "-8.25%", "0%"]
    assert ">+16.67%</td>" in rendered["account"]
    assert ">12.50%</td>" in rendered["account"]  # generic account weight stays unsigned
    assert '<strong class="pnl-profit">+12.5%</strong>' in rendered["backtest"]
    assert '<span>最大回撤</span><strong class="pnl-loss">-8.25%</strong>' in rendered["backtest"]
    assert ">60.00%</strong>" in rendered["backtest"]
    assert ">+12.5%</dd>" in rendered["plan"]
    assert '<dt>最大回撤</dt><dd class="pnl-loss">-8.25%</dd>' in rendered["plan"]


def test_dashboard_signed_pnl_covers_kelly_sample_pnl() -> None:
    output = run_dashboard_js(r'''
const kelly=renderKellyParameterDerivation({sample_stage:"sufficient",avg_net_win_pct:"12.50%",avg_net_loss_pct:"-8.25%"});
console.log(kelly);
''')
    assert "+12.5% / -8.25%" in output


def test_dashboard_remaining_numeric_leaves_group_only_numeric_values() -> None:
    output = run_dashboard_js(r'''
const condition=renderDecisionPlanCondition({
  condition_id:"00001234",priority:"ordinary",trigger_count:5000,operator:">=",calculated_value:"25142.16",
  suggested_action:"观察",target_weight:"0.1",target_quantity:"25142.16",source_date:"2026-07-16",
},0);
const facts=[
  renderDecisionPlanFact({key:"rsi14",calculated_value:"25142.16"}),
  renderDecisionPlanFact({key:"ma20_distance_pct",calculated_value:"21.13%"}),
].join("");
const keywords=renderDomesticKeywordTags([{keyword:"00001234",count:5000}]);
const bollinger=renderBollingerBand({lower:"1234567.50",middle:"2000000.00",upper:"3000000.00"},"25142.16")
  +renderBollingerMetrics({middle:"2000000.00",distance_pct:"21.13%"},"1234567.50","neutral");
const technical=renderTechnicalFactRows(technicalFactRows({timeframes:[{
  timeframe_label:"2026-07-16",current_price:"1234567.50",rsi:"21.13%",trend:"等待确认",
  macd:{macd:"1234567.50",signal:"2000000.00",histogram:"3000000.00"},
  atr:{value:"1234567.50",percent_of_price:"21.13%"},
  support_resistance:{support_levels:["1234567.50"],resistance_levels:["2000000.00"]},
  moving_averages:{ma20:"1234567.50"},
}]}));
const action=renderTradeDecisionBand({
  action:"HOLD",limit_price:"1234567.50",suggested_quantity:"25142.16",order_value_hkd:"29320000.00",
  stop_price:"2000000.00",reason:"等待确认",
},{total_quantity:"5000"});
const review=renderPreviousDecisionReview({
  run_date:"2026-07-16",status:"triggered",trigger_count:5000,starting_quantity:"25142.16",closing_quantity:"00001234",
});
console.log(JSON.stringify({condition,facts,keywords,bollinger,technical,action,review}));
''')
    rendered = json.loads(output)
    assert "已触发 5,000 次" in rendered["condition"]
    assert "目标数量</span><strong>25,142.16</strong>" in rendered["condition"]
    assert 'data-plan-condition="00001234"' in rendered["condition"]
    assert "25,142.16" in rendered["facts"] and "21.13%" in rendered["facts"]
    assert ">00001234</span>" in rendered["keywords"] and ">5,000</em>" in rendered["keywords"]
    for expected in ("1,234,567.5", "2,000,000", "3,000,000"):
        assert expected in rendered["bollinger"]
    assert "21.13%" in rendered["bollinger"]
    assert "2026-07-16 当前价" in rendered["technical"]
    assert "1,234,567.5" in rendered["technical"]
    assert "21.13%" in rendered["technical"] and "等待确认" in rendered["technical"]
    for expected in ("MACD 1,234,567.5", "Signal 2,000,000", "Hist 3,000,000", "MA20 1,234,567.5"):
        assert expected in rendered["technical"]
    for expected in ("1,234,567.5", "25,142.16", "HKD 29,320,000", "2,000,000"):
        assert expected in rendered["action"]
    assert "上期复盘 · 2026-07-16" in rendered["review"]
    assert "条件触发 <strong>5,000 次</strong>" in rendered["review"]
    assert "期初数量 <strong>25,142.16</strong>" in rendered["review"]
    assert "本期期初数量 <strong>00,001,234</strong>" in rendered["review"]


def test_dashboard_formats_numeric_suggested_notional_in_both_action_views() -> None:
    output = run_dashboard_js(r'''
const action={
  market:"US",symbol:"AAPL",action:"HOLD",status:"ready",suggested_notional:"29320000.00",
  notional_currency:"USD",reason:"等待人工确认",
};
console.log(JSON.stringify({band:renderTradeDecisionBand(action,{}),card:renderActionCard(action)}));
''')
    rendered = json.loads(output)
    assert "USD 29,320,000" in rendered["band"]
    assert "USD 29,320,000" in rendered["card"]


def test_dashboard_t_signal_formats_only_price_numeric_leaves() -> None:
    output = run_dashboard_js(r'''
const signal={
  price:{last_price:"1234567.50",day_change_pct:"21.13%",vwap:"2000000.00",day_low:"1234567.50",day_high:"3000000.00"},
  technical:{rsi_5m:"21.13%",volume_ratio_5m:"00001234",price_position:"below_vwap_reclaim"},
  liquidity:{depth_status:"pass"},
  timeline:[{event_at:"2026-07-16T09:30:00+08:00",event_type:"signal_created",message_zh:"编号 00001234"}],
};
console.log(JSON.stringify({details:renderTSignalDetails(signal),timeline:renderTSignalTimeline(signal)}));
''')
    rendered = json.loads(output)
    for expected in (
        "1,234,567.5", "2,000,000", "1,234,567.5 / 3,000,000",
    ):
        assert expected in rendered["details"]
    assert "21.13%" in rendered["details"]
    assert "21.13%%" not in rendered["details"]
    assert "00001234" in rendered["details"]
    assert "2026-07-16T09:30:00+08:00" in rendered["timeline"]
    assert "编号 00001234" in rendered["timeline"]


def test_dashboard_decision_target_fallback_formats_only_numeric_tokens() -> None:
    output = run_dashboard_js(r'''
const target = (value) => Object.fromEntries(decisionMetricCells({strategy:{target_range:value}}))["目标价"];
console.log(JSON.stringify({
  lower:target(">= 1234567.50"),range:target("1234567.50 - 2000000.00"),
  date:target("2026-07-16"),identifier:target("编号 00001234"),numericId:target("00001234-56"),
  percent:target("21.13%"),text:target("等待确认"),
}));
''')
    assert json.loads(output) == {
        "lower": ">= 1,234,567.5",
        "range": "1,234,567.5 - 2,000,000",
        "date": "2026-07-16",
        "identifier": "编号 00001234",
        "numericId": "00001234-56",
        "percent": "21.13%",
        "text": "等待确认",
    }


def test_dashboard_workspace_navigation_uses_one_shared_state_machine() -> None:
    output = run_dashboard_js(r'''
const element=()=>({hidden:false,innerHTML:"",classList:{values:new Set(),add(...n){n.forEach(x=>this.values.add(x))},remove(...n){n.forEach(x=>this.values.delete(x))},toggle(n,f){f?this.add(n):this.remove(n)},contains(n){return this.values.has(n)}}});
for(const id of ["dashboard-shell","workspace-grid","kelly-lab-panel","holdings-panel","standard-backtest-workspace","trend-report-workspace","return-to-portfolio"])elements[id]=element();
const snapshot=(requested)=>{
  setWorkspaceView(requested);
  const hiddenClass=(id)=>elements[id].classList.contains("hidden");
  return {
    requested,view:state.workspaceView,
    shellTool:elements["dashboard-shell"].classList.contains("tool-workspace-view"),
    returnHidden:elements["return-to-portfolio"].hidden,
    returnHiddenClass:hiddenClass("return-to-portfolio"),
    gridHiddenClass:hiddenClass("workspace-grid"),
    holdingsHiddenClass:hiddenClass("holdings-panel"),
    kellyHiddenClass:hiddenClass("kelly-lab-panel"),
    backtestHidden:elements["standard-backtest-workspace"].hidden,
    backtestHiddenClass:hiddenClass("standard-backtest-workspace"),
    trendHidden:elements["trend-report-workspace"].hidden,
    trendHiddenClass:hiddenClass("trend-report-workspace"),
  };
};
for(const view of ["kelly_lab","standard_backtest","trend_report","portfolio","invalid"]){
  console.log(JSON.stringify(snapshot(view)));
}
''')
    states = [json.loads(line) for line in output.splitlines()]
    assert states == [
        {
            "requested": "kelly_lab", "view": "kelly_lab", "shellTool": True,
            "returnHidden": False, "returnHiddenClass": False, "gridHiddenClass": False,
            "holdingsHiddenClass": True, "kellyHiddenClass": False,
            "backtestHidden": True, "backtestHiddenClass": True,
            "trendHidden": True, "trendHiddenClass": True,
        },
        {
            "requested": "standard_backtest", "view": "standard_backtest", "shellTool": True,
            "returnHidden": False, "returnHiddenClass": False, "gridHiddenClass": True,
            "holdingsHiddenClass": True, "kellyHiddenClass": True,
            "backtestHidden": False, "backtestHiddenClass": False,
            "trendHidden": True, "trendHiddenClass": True,
        },
        {
            "requested": "trend_report", "view": "trend_report", "shellTool": True,
            "returnHidden": False, "returnHiddenClass": False, "gridHiddenClass": True,
            "holdingsHiddenClass": True, "kellyHiddenClass": True,
            "backtestHidden": True, "backtestHiddenClass": True,
            "trendHidden": False, "trendHiddenClass": False,
        },
        {
            "requested": "portfolio", "view": "portfolio", "shellTool": False,
            "returnHidden": True, "returnHiddenClass": True, "gridHiddenClass": False,
            "holdingsHiddenClass": False, "kellyHiddenClass": True,
            "backtestHidden": True, "backtestHiddenClass": True,
            "trendHidden": True, "trendHiddenClass": True,
        },
        {
            "requested": "invalid", "view": "portfolio", "shellTool": False,
            "returnHidden": True, "returnHiddenClass": True, "gridHiddenClass": False,
            "holdingsHiddenClass": False, "kellyHiddenClass": True,
            "backtestHidden": True, "backtestHiddenClass": True,
            "trendHidden": True, "trendHiddenClass": True,
        },
    ]


def test_dashboard_workspace_bindings_open_kelly_and_return_without_resetting_filters() -> None:
    output = run_dashboard_js(r'''
class Element {
  constructor(){this.hidden=false;this.innerHTML="";this.textContent="";this.listeners={};this.classes=new Set();this.classList={add:(...names)=>names.forEach((name)=>this.classes.add(name)),remove:(...names)=>names.forEach((name)=>this.classes.delete(name)),toggle:(name,force)=>force?this.classes.add(name):this.classes.delete(name)};}
  addEventListener(name,listener){this.listeners[name]=listener;}
  click(){if(typeof this.listeners.click!=="function")throw new Error("missing click binding");return this.listeners.click({target:this,preventDefault(){}});}
}
const nodes={};
document.getElementById=(id)=>nodes[id]||(nodes[id]=new Element());
bindElements();
bindEvents();
state.brokerFilter="tiger";
state.marketFilter="HK";
elements["open-kelly-lab"].click();
if(state.workspaceView!=="kelly_lab")throw new Error("Kelly binding did not open the workspace");
if(elements["kelly-lab-panel"].classes.has("hidden")||!elements["holdings-panel"].classes.has("hidden")||elements["return-to-portfolio"].hidden||elements["return-to-portfolio"].classes.has("hidden"))throw new Error("Kelly binding did not render the workspace");
elements["return-to-portfolio"].click();
console.log(JSON.stringify({
  view:state.workspaceView,
  broker:state.brokerFilter,
  market:state.marketFilter,
  returnHidden:elements["return-to-portfolio"].hidden,
  holdingsHidden:elements["holdings-panel"].classes.has("hidden"),
}));
''')
    assert json.loads(output) == {
        "view": "portfolio",
        "broker": "tiger",
        "market": "HK",
        "returnHidden": True,
        "holdingsHidden": False,
    }


def test_dashboard_derives_account_groups_from_existing_broker_details() -> None:
    output = run_dashboard_js(r'''
state.dashboard = {
  summary: {portfolio_value_hkd: "3000", cash_like_value_hkd: "700"}, broker_summaries: [
    {broker: "futu", portfolio_value_hkd: "1000", cash_like_value_hkd: "300"},
    {broker: "tiger", portfolio_value_hkd: "2000", cash_like_value_hkd: "400"},
    {broker: "phillips", portfolio_value_hkd: "0", cash_like_value_hkd: "0"},
    {broker: "eastmoney", portfolio_value_hkd: "0", cash_like_value_hkd: "0"},
  ], source_statuses: [], cash_rows: [],
  holdings: [{market: "US", symbol: "QQQ", brokers: "futu;tiger", broker_details: [
    {broker: "futu", account_alias: "futu_1", market: "US", symbol: "QQQ", quantity: "1", market_value_hkd: "700", cost_value: "600", unrealized_pnl: "100"},
    {broker: "tiger", account_alias: "tiger_1", market: "US", symbol: "QQQ", quantity: "2", market_value_hkd: "1600", cost_value: "1100", unrealized_pnl: "500"},
  ]}],
};
console.log(JSON.stringify(accountHoldingGroups().map((group) => ({
  broker: group.broker, horizon: group.profile.horizon,
  rows: group.rows.map((row) => ({key: row.key, quantity: row.display.total_quantity, accountWeight: row.display.account_weight})),
}))));
''')
    groups = json.loads(output)
    assert [group["broker"] for group in groups] == ["futu", "tiger", "phillips", "eastmoney"]
    assert groups[0]["rows"] == [{"key": "futu:US:QQQ:0", "quantity": "1", "accountWeight": "70.00%"}]
    assert groups[1]["rows"] == [{"key": "tiger:US:QQQ:0", "quantity": "2", "accountWeight": "80.00%"}]


def test_dashboard_account_rows_reprice_with_unmapped_assets_and_negative_cash() -> None:
    output = run_dashboard_js(r'''
state.dashboard = {
  summary: {portfolio_value_hkd: "5000", cash_like_value_hkd: "300"}, broker_summaries: [
    {broker: "futu", portfolio_value_hkd: "2000", cash_like_value_hkd: "100"},
    {broker: "tiger", portfolio_value_hkd: "3000", cash_like_value_hkd: "-200"},
  ], cash_rows: [], holdings: [{
    market: "US", symbol: "QQQ", brokers: "futu;tiger", total_quantity: "3",
    cost_value: "210", fx_to_hkd: "7.8", market_value_hkd: "2300",
    broker_details: [
      {broker: "futu", quantity: "1", cost_value: "60", fx_to_hkd: "7.8", market_value_hkd: "700", unrealized_pnl: "30"},
      {broker: "tiger", quantity: "2", cost_value: "150", fx_to_hkd: "8", market_value_hkd: "1600", unrealized_pnl: "50"},
    ],
  }],
};
state.quotes = {qqq: {market: "US", symbol: "QQQ", last_price: "100"}};
console.log(JSON.stringify(accountHoldingGroups().slice(0, 2).map((group) => {
  const display = group.rows[0].display;
  return {
    broker: group.broker,
    marketValueHkd: display.market_value_hkd,
    accountWeight: display.account_weight,
    overallWeight: display.portfolio_weight,
    pnl: display.unrealized_pnl,
    pnlPercent: display.unrealized_pnl_pct,
  };
})));
''')

    assert json.loads(output) == [
        {
            "broker": "futu",
            "marketValueHkd": "780.00",
            "accountWeight": "37.50%",
            "overallWeight": "15.35%",
            "pnl": "40.00",
            "pnlPercent": "66.67%",
        },
        {
            "broker": "tiger",
            "marketValueHkd": "1600.00",
            "accountWeight": "53.33%",
            "overallWeight": "31.50%",
            "pnl": "50.00",
            "pnlPercent": "33.33%",
        },
    ]


def test_dashboard_account_rows_do_not_turn_unknown_values_into_zero() -> None:
    output = run_dashboard_js(r'''
const display = accountDisplayRow(
  {market: "US", symbol: "QQQ"},
  {broker: "futu", quantity: "", cost_price: "", market_value_hkd: "", cost_value: "0", unrealized_pnl: "0"},
  {broker: "futu", portfolio_value_hkd: ""},
  "",
);
console.log(JSON.stringify({
  quantity: display.total_quantity,
  costPrice: display.avg_cost_price,
  accountWeight: display.account_weight,
  portfolioWeight: display.portfolio_weight,
  pnlPercent: display.unrealized_pnl_pct,
}));
''')

    assert json.loads(output) == {
        "quantity": "-",
        "costPrice": "-",
        "accountWeight": "-",
        "portfolioWeight": "-",
        "pnlPercent": "-",
    }


def test_dashboard_matches_holding_to_backend_canonical_quote() -> None:
    output = run_dashboard_js(
        r'''
state.quotes = {
  "backend-owned-key": {
    market: "CN",
    symbol: "600025",
    futu_symbol: "SH.600025",
    last_price: "9.81",
  },
};
console.log(JSON.stringify(quoteForHolding({ market: "CN", symbol: "600025" })));
'''
    )

    assert json.loads(output)["futu_symbol"] == "SH.600025"


def test_dashboard_derives_live_holding_values_from_futu_quote() -> None:
    output = run_dashboard_js(
        r'''
const holding = quoteAdjustedHolding({
  market: "CN",
  symbol: "600025",
  total_quantity: "6000",
  cost_value: "53346",
  fx_to_hkd: "1.08",
  market_value: "57720",
  market_value_hkd: "62337.60",
  unrealized_pnl_pct: "8.20%",
}, { last_price: "9.81" });
console.log(JSON.stringify({
  market_value: holding.market_value,
  market_value_hkd: holding.market_value_hkd,
  unrealized_pnl_pct: holding.unrealized_pnl_pct,
}));
'''
    )

    assert json.loads(output) == {
        "market_value": "58860.00",
        "market_value_hkd": "63568.80",
        "unrealized_pnl_pct": "10.34%",
    }


@pytest.mark.parametrize(
    ("quantity", "cost_value", "last_price", "expected"),
    [
        (
            "2", "3", "2",
            {
                "market_value": "400.00",
                "market_value_hkd": "3120.00",
                "unrealized_pnl": "100.00",
                "unrealized_pnl_pct": "33.33%",
            },
        ),
        (
            "-2", "-3", "1",
            {
                "market_value": "-200.00",
                "market_value_hkd": "-1560.00",
                "unrealized_pnl": "100.00",
                "unrealized_pnl_pct": "33.33%",
            },
        ),
    ],
    ids=("long", "short"),
)
def test_dashboard_account_option_row_uses_selected_quote_with_standard_multiplier(
    quantity: str, cost_value: str, last_price: str, expected: dict[str, str],
) -> None:
    scenario = json.dumps({
        "quantity": quantity,
        "cost_value": cost_value,
        "last_price": last_price,
    })
    output = run_dashboard_js(f"const scenario = {scenario};\n" + r'''
state.quotes = {selected: {
  market: "US", symbol: "DRAM260731P55000", last_price: scenario.last_price,
}};
const display = accountDisplayRow(
  {
    market: "US", symbol: "DRAM260731P55000", asset_class: "option",
    fx_to_hkd: "7.8", unrealized_pnl_pct: "-999%",
  },
  {
    broker: "tiger", quantity: scenario.quantity,
    cost_value: scenario.cost_value, market_value: "999",
    market_value_hkd: "999", unrealized_pnl: "999",
  },
  {broker: "tiger", portfolio_value_hkd: "10000"},
  "20000",
);
console.log(JSON.stringify({
  market_value: display.market_value,
  market_value_hkd: display.market_value_hkd,
  unrealized_pnl: display.unrealized_pnl,
  unrealized_pnl_pct: display.unrealized_pnl_pct,
}));
''')

    assert json.loads(output) == expected


def test_dashboard_non_us_option_preserves_unit_multiplier() -> None:
    output = run_dashboard_js(r'''
const holding = quoteAdjustedHolding({
  market: "HK", asset_class: "option", total_quantity: "2",
  cost_value: "3", fx_to_hkd: "1",
}, {last_price: "2"});
console.log(JSON.stringify({
  market_value: holding.market_value,
  unrealized_pnl: holding.unrealized_pnl,
  unrealized_pnl_pct: holding.unrealized_pnl_pct,
}));
''')

    assert json.loads(output) == {
        "market_value": "4.00",
        "unrealized_pnl": "1.00",
        "unrealized_pnl_pct": "33.33%",
    }


@pytest.mark.parametrize(
    ("market", "asset_class"),
    [("US", "stock"), ("HK", "option")],
    ids=("negative-stock", "negative-non-us-option"),
)
def test_dashboard_negative_non_us_option_or_stock_keeps_stale_values(
    market: str, asset_class: str,
) -> None:
    scenario = json.dumps({"market": market, "asset_class": asset_class})
    output = run_dashboard_js(f"const scenario = {scenario};\n" + r'''
const holding = {
  market: scenario.market, asset_class: scenario.asset_class,
  total_quantity: "-2", cost_value: "-3", fx_to_hkd: "1",
  market_value: "stale-market", market_value_hkd: "stale-hkd",
  unrealized_pnl: "stale-pnl", unrealized_pnl_pct: "stale-pct",
};
const adjusted = quoteAdjustedHolding(holding, {last_price: "2"});
console.log(JSON.stringify({
  same: adjusted === holding,
  market_value: adjusted.market_value,
  market_value_hkd: adjusted.market_value_hkd,
  unrealized_pnl: adjusted.unrealized_pnl,
  unrealized_pnl_pct: adjusted.unrealized_pnl_pct,
}));
''')

    assert json.loads(output) == {
        "same": True,
        "market_value": "stale-market",
        "market_value_hkd": "stale-hkd",
        "unrealized_pnl": "stale-pnl",
        "unrealized_pnl_pct": "stale-pct",
    }


def test_dashboard_account_detail_uses_own_percentage_when_quote_price_is_missing() -> None:
    output = run_dashboard_js(r'''
state.quotes = {missing: {market: "US", symbol: "QQQ", last_price: ""}};
const display = accountDisplayRow(
  {market: "US", symbol: "QQQ", unrealized_pnl_pct: "77.77%"},
  {broker: "futu", quantity: "1", cost_value: "100", unrealized_pnl: "20"},
  {broker: "futu", portfolio_value_hkd: "1000"},
  "2000",
);
console.log(display.unrealized_pnl_pct);
''')

    assert output.strip() == "20.00%"


def test_dashboard_renders_one_compact_us_session_price_and_header_time() -> None:
    output = run_dashboard_js(r'''
const sessions = {
  overnight: "夜盘",
  pre_market: "盘前",
  regular: "盘中",
  after_hours: "盘后",
};
for (const [key, label] of Object.entries(sessions)) {
  const html = renderQuotePrice({market:"US"}, {
    last_price:"61.50", price_session:key,
    price_time:"2026-07-15 03:03:01.150", current_session_quote:true,
  });
  if(!html.includes(label) || !html.includes(`data-session="${key}"`))throw new Error(`${key}: ${html}`);
}
const active = renderQuotePrice({market:"US", asset_class:"stock"}, {
  last_price:"61.50", price_session:"overnight",
  price_time:"2026-07-15 03:03:01.150", current_session_quote:true,
});
if(!active.includes("夜盘") || !active.includes("61.5") || !active.includes("03:03 ET"))throw new Error(active);
if((active.match(/61\.5/g)||[]).length!==1)throw new Error("price repeated: "+active);
const fallback = renderQuotePrice({market:"US", asset_class:"option"}, {
  last_price:"0.59", price_session:"regular", price_time:"",
  current_session_quote:false,
});
if(!fallback.includes("盘中") || !fallback.includes("上一有效价"))throw new Error(fallback);
const hk = renderQuotePrice({market:"HK", asset_class:"stock"}, {last_price:"510"});
if(hk!=="510")throw new Error("non-US changed: "+hk);
if(quoteRefreshText({fetched_at:"2026-07-15T15:03:13+08:00",stale:false})!=="刷新于 2026-07-15 15:03:13 CST")throw new Error("bad header time");
if(quoteRefreshText({last_success_at:"2026-07-15T14:59:00+08:00",stale:true})!=="上次成功 2026-07-15 14:59:00 CST")throw new Error("bad stale time");
if(quoteStatusText({status:"ok",us_session_status:"closed",fallback_count:0,missing_count:0})!=="美股休市")throw new Error("bad closed status");
if(quoteStatusText({status:"partial",us_session_status:"active",fallback_count:2,missing_count:0})!=="部分标的当前时段无报价")throw new Error("bad fallback status");
console.log("ok");
''')
    assert "ok" in output


def test_dashboard_session_labels_use_distinct_semantic_colors() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    for session, color in {
        "overnight": "#6941C6",
        "pre_market": "#B54708",
        "regular": "#175CD3",
        "after_hours": "#027A48",
    }.items():
        assert (
            f'.session-quote-label[data-session="{session}"] {{\n'
            f"  color: {color};\n"
            "}"
        ) in css


def test_dashboard_live_holdings_recalculate_values_and_weights() -> None:
    output = run_dashboard_js(
        r'''
state.dashboard = {
  holdings: [{
    market: "CN",
    symbol: "600025",
    total_quantity: "10",
    cost_value: "50",
    fx_to_hkd: "1",
    market_value: "50",
    market_value_hkd: "50",
    unrealized_pnl_pct: "0.00%",
    portfolio_weight_hkd: "50.00%",
  }],
  cash_rows: [{ market_value_hkd: "50" }],
};
state.quotes = {
  anything: { market: "CN", symbol: "600025", last_price: "10" },
};
console.log(JSON.stringify(getHoldings()[0]));
'''
    )

    holding = json.loads(output)
    assert holding["market_value_hkd"] == "100.00"
    assert holding["unrealized_pnl_pct"] == "100.00%"
    assert holding["portfolio_weight_hkd"] == "66.67%"


def test_dashboard_trading_decision_tabs() -> None:
    output = run_dashboard_js(
        r'''
function assertOrdered(html, labels) {
  let cursor = -1;
  for (const label of labels) {
    const next = html.indexOf(label, cursor + 1);
    if (next <= cursor) throw new Error("tab order mismatch: " + html);
    cursor = next;
  }
}
const holding = {
  market: "US",
  symbol: "NVDA",
  name: "英伟达",
  total_quantity: "10",
  agent_report: { available: true, error: "" },
  decision_plan: {
    available: true,
    mode: "validated_plan",
    status: "waiting",
    run_date: "2026-07-13",
    action_summary: "继续持有，等待条件触发",
    max_weight: "0.10",
    strategy: {id: "trend_pullback/v1", name_zh: "趋势回调"},
    conditions: [],
    backtests: [],
  },
  tradingagents_summary: {
    available: true,
    error: "",
    ta_view: "偏多",
    current_action: "持有",
    core_reason: "趋势仍在",
  },
  decision_facts: {
    kline: { available: true, fields: { trend: "上涨" } },
    news_sentiment: { available: false, error: "新闻任务失败" },
  },
  futu_skill_facts: {},
};
state.selectedDecisionTab = "final";
let html = renderTradingDecisionTabs(holding);
assertOrdered(html, ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]);
if ((html.match(/role="tabpanel"/g) || []).length !== 1) throw new Error(html);
if (!html.includes('data-decision-tab="news"') || !html.includes("decision-tab-failed")) throw new Error(html);
if (!html.includes("今日交易计划") || html.includes("大模型决策模板") || html.includes("<h4>TradingAgents</h4>")) throw new Error(html);
state.selectedDecisionTab = "tradingagents";
html = renderTradingDecisionTabs(holding);
if (!html.includes("<h4>TradingAgents</h4>") || html.includes("今日交易计划")) throw new Error(html);
const missingSummary = {
  ...holding,
  tradingagents_summary: {
    available: false,
    error: "TradingAgents summary is unavailable for current advice",
    ta_view: "低配",
    current_action: "持有",
    core_reason: "缺失",
  },
};
state.selectedDecisionTab = "tradingagents";
html = renderTradingDecisionTabs(missingSummary);
const tradingagentsTab = html.match(/<button[^>]*data-decision-tab="tradingagents"[^>]*>/)[0];
if (!tradingagentsTab.includes("decision-tab-failed") || !html.includes("status-failed") || !html.includes("TradingAgents summary is unavailable for current advice")) throw new Error(html);
state.selectedDecisionTab = "news";
html = renderTradingDecisionTabs(holding);
if ((html.match(/role="tabpanel"/g) || []).length !== 1 || !html.includes("新闻任务失败")) throw new Error(html);
state.selectedDecisionTab = "futu";
html = renderTradingDecisionTabs(holding);
if ((html.match(/role="tabpanel"/g) || []).length !== 1 || !html.includes("数据未生成")) throw new Error(html);

const technicalHolding = {
  ...holding,
  decision_facts: {},
  technical_facts: {
    available: true,
    status: "usable",
    facts: { timeframes: [{ timeframe_label: "日线" }] },
  },
};
state.selectedDecisionTab = "kline";
html = renderTradingDecisionTabs(technicalHolding);
if (html.includes("decision-tab-empty") || !html.includes("趋势 / K 线")) throw new Error(html);

const staleTechnicalHolding = {
  ...holding,
  decision_facts: { kline: { available: false, error: "" } },
  technical_facts: {
    available: false,
    status: "stale_run_date",
    error: "technical facts run date does not match latest advice",
  },
};
state.selectedDecisionTab = "kline";
html = renderTradingDecisionTabs(staleTechnicalHolding);
if (!html.includes("status-failed") || !html.includes("technical facts run date does not match latest advice") || html.includes("数据未生成")) throw new Error(html);

let renders = 0;
renderHoldings = () => { renders += 1; };
handleSymbolDetailClick({ target: { closest: (selector) => selector === "[data-decision-tab]" ? { dataset: { decisionTab: "kline" } } : null } });
if (state.selectedDecisionTab !== "kline" || renders !== 1) throw new Error("tab click did not render");
state.selectedDecisionTab = "news";
showSymbolDetail("US|NVDA", "decision");
if (state.selectedDecisionTab !== "final") throw new Error("new holding did not reset tab");
console.log("ok");
'''
    )

    assert "ok" in output


def test_dashboard_news_tab_uses_futu_skill_news_sentiment() -> None:
    output = run_dashboard_js(
        r'''
const holding = {
  decision_facts: {},
  futu_skill_facts: {
    news_sentiment: {
      available: true,
      domestic_discussion: { summary: "国内投资者关注存储链联动" },
    },
  },
};
state.selectedDecisionTab = "news";
const html = renderTradingDecisionTabs(holding);
const tab = html.match(/<button[^>]*data-decision-tab="news"[^>]*>/)[0];
if (tab.includes("decision-tab-failed")) throw new Error(tab);
if (!html.includes("富途社区 / 国内讨论") || !html.includes("国内投资者关注存储链联动")) throw new Error(html);
console.log("ok");
'''
    )

    assert "ok" in output


class FakeResearchChatService:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.messages: list[dict[str, str]] = []
        self.finalized: list[str] = []

    def create_session(self, *, market: str, symbol: str) -> dict[str, Any]:
        self.created.append({"market": market, "symbol": symbol})
        return {
            "schema_version": "open_trader.research_chat_session.v1",
            "session_id": "20260620T103000-US-VIXY",
            "market": market,
            "symbol": symbol,
            "research_bundle_dir": "data/research_data/US/VIXY/2026-06-19",
            "status": "active",
            "created_at": "2026-06-20T10:30:00+08:00",
            "updated_at": "2026-06-20T10:30:00+08:00",
            "messages": [],
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        return {
            "schema_version": "open_trader.research_chat_session.v1",
            "session_id": session_id,
            "market": "US",
            "symbol": "VIXY",
            "research_bundle_dir": "data/research_data/US/VIXY/2026-06-19",
            "status": "active",
            "created_at": "2026-06-20T10:30:00+08:00",
            "updated_at": "2026-06-20T10:30:00+08:00",
            "messages": [],
        }

    def append_message(self, *, session_id: str, content: str) -> dict[str, Any]:
        self.messages.append({"session_id": session_id, "content": content})
        return {
            **self.get_session(session_id),
            "messages": [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "assistant reply"},
            ],
        }

    def finalize_session(self, *, session_id: str) -> dict[str, Any]:
        self.finalized.append(session_id)
        return {
            "status": "ok",
            "conclusion": {
                "schema_version": "user.llm_conclusion.v1",
                "status": "present",
                "content": "确认减仓 100 股。",
            },
            "dashboard_view": {
                "schema_version": "dashboard.research_view.v1",
                "available": True,
                "market": "US",
                "symbol": "VIXY",
            },
        }


class RaisingResearchChatService(FakeResearchChatService):
    def get_session(self, session_id: str) -> dict[str, Any]:
        raise RuntimeError(f"chat boom: {session_id}")


def test_dashboard_static_assets_include_local_shell() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert "Open Trader" in html
    assert "持仓实时看板" in html
    assert "刷新账户与行情" in html
    assert "accountSyncReloadNeeded" in js
    assert "renderBacktestPriceSyncStatus" not in js
    assert "全部市场" in html
    assert "symbol-detail-panel" in html
    assert "dashboard-header" in html
    assert "header-market-filters" in html
    assert "account-holdings" in html
    assert "header-broker-filters" not in html
    assert "header-backtest-filters" not in html
    assert "backtest-price-sync-status" not in html
    assert "data-backtest=\"READY\"" not in html
    assert "current-view-value" in html
    assert "broker-summary-cards" in html
    assert "source-status-list" in html
    assert "account-tabs" in html
    assert "cash-detail-panel" not in html
    assert "research-chat-modal" in html
    assert "research-chat-messages" in html
    assert "research-chat-input" in html
    assert "生成最终结论" in html
    assert "filter-panel" not in html
    assert "summary-grid" not in html
    assert "数据健康" not in html
    assert "当前视图" in html
    assert "富途暂无数据" in html
    assert "老虎暂无数据" in html
    assert "futuAnomalySignalsPlugin" in js
    assert "translateFutuSignalValue" in js
    assert ".futu-signal-module-grid" in css

    assert "辉立暂无数据" in html
    assert "right-rail" not in html
    assert "今日交易动作" not in html
    assert "实时连接与任务" not in html
    assert 'id="trade-actions"' not in html
    assert 'id="action-count"' not in html
    for compatibility_id in (
        "market-filters",
        "broker-filters",
        "summary-value",
        "summary-holding-bar",
        "summary-holding-value",
        "summary-holding-weight",
        "summary-cash-note",
        "summary-refresh-status",
        "summary-refresh-note",
        "summary-brokers",
        "summary-detail-month",
        "summary-health",
        "summary-health-note",
    ):
        assert f'id="{compatibility_id}"' in html
    assert "缺行情" in js
    assert "数据已过期" in js
    assert "dashboardError" in js
    assert "scheduleQuotePolling" in js
    assert "selectedHoldingKey" in js
    assert "renderSymbolDetail" in js
    assert "showSymbolDetail" in js
    assert "back-to-holdings" in js
    assert "detailLanguage" in js
    assert "data-detail-language" in js
    assert "中文" in js
    assert "English" in js
    assert "renderChineseAgentSummary" in js
    assert "renderEnglishSourceBlock" in js
    assert "renderChineseStrategyTerms" in js
    assert "summary_zh" in js
    assert "renderAnalysisStrategySection" in js
    assert "currentDecisionAction" in js
    assert "desiredActionText" in js
    assert "operationRows" in js
    assert "watchPointText" in js
    assert "decisionMetricCells" in js
    assert "finalConclusionItems" in js
    assert "renderResearchConclusions" in js
    assert "openResearchChat" in js
    assert "sendResearchChatMessage" in js
    assert "finalizeResearchChat" in js
    assert "投研给出的结论" in js
    assert "我和 LLM 探讨后的结论" in js
    assert "renderAnalystDialogue" in js
    assert "sourceReviewText" in js
    assert "分析与交易策略" in js
    assert "当前希望你做什么" in js
    assert "操作指令" in js
    assert "今天重点关注" in js
    assert "分析师对话" in js
    assert "最终结论" in js
    assert "失败条件" in js
    assert "只读 · 需要人工确认" in js
    assert "今天暂无触发中的交易动作" in js
    assert "查看英文原文" in js
    assert ".analysis-strategy-section" in css
    assert ".decision-dashboard" in css
    assert ".decision-card.primary" in css
    assert ".decision-metric-strip" in css
    decision_plugin_card_css = css.split(".decision-plugin-card {", 1)[1].split("}", 1)[0]
    assert "align-content: start;" in decision_plugin_card_css
    kelly_experiment_card_css = css.split(".kelly-experiment-card {", 1)[1].split("}", 1)[0]
    assert "align-content: start;" in kelly_experiment_card_css
    assert ".decision-fact-grid" in css
    assert ".technical-fact-grid" in css
    assert ".analyst-dialogue" in css
    assert ".final-conclusion-list" in css
    assert ".research-conclusion-grid" in css
    assert ".research-chat-layer" in css
    assert "height: min(760px, calc(100vh - 36px));" in css
    assert "min-height: min(620px, calc(100vh - 36px));" in css
    assert ".broker-detail-section" in css
    assert "holding_value_hkd" in js
    assert "cash_like_value_hkd" in js
    assert "percentBarWidth" in js
    assert "隐藏英文原文" in js
    assert 'firstValue(strategy, ["plan_text_zh", "rationale_zh"])' not in js
    assert "暂无中文策略译文" not in js
    assert "交易决策" in js
    assert "基于已接入的交易决策与市场事实数据展示" in js
    assert "大模型决策模板" in js
    assert 'selectedDecisionTab: "final"' in js
    assert "const DECISION_TABS" in js
    assert "decisionFactsPlugin" in js
    assert "decision_facts" in js
    assert "futuSkillNewsSentimentPlugin" in js
    assert "futu_skill_facts" in js
    assert "富途社区 / 国内讨论" in js
    assert "讨论关键词" in js
    assert "国内讨论结论" in js
    assert "domestic-list" in js
    assert "domestic-keyword-list" in js
    assert ".domestic-list" in css
    assert ".domestic-keyword-list" in css
    assert "technical_facts" in js
    assert "technicalFactRows" in js
    assert "插件管理" not in js
    assert "策略阈值" not in js
    assert "暂无 TradingAgents 报告" in js
    assert "暂无交易策略" in js
    assert "暂无触发中的交易动作" in js
    assert "查看原始报告" in js
    assert "使用历史报告回退" in js
    assert "Math.max(1000" in js
    assert "减仓" in js
    assert "待确认" in js
    assert "观察中" in js
    assert "达到第一目标价" in js
    assert "暂无触发中的交易计划" in js
    assert ".dashboard-shell" in css
    assert ".dashboard-header" in css
    assert 'grid-template-areas: "brand brand" "assets source";' in css
    assert ".header-brand-panel" in css
    assert "grid-area: brand;" in css
    assert ".header-assets-panel" in css
    assert "grid-area: assets;" in css
    assert ".header-source-panel" in css
    assert "grid-area: source;" in css
    assert ".header-filter-block" in css
    assert ".segmented-control" in css
    assert ".current-view-label" in css
    assert ".current-view-card" in css
    assert ".current-view-breakdown" in css
    assert ".broker-summary-cards" in css
    assert ".broker-summary-card" in css
    assert ".broker-summary-empty" in css
    assert ".source-header-row" in css
    assert ".source-status-list" in css
    assert ".source-status-row" in css
    assert ".cash-detail-panel" not in css
    assert "function closeStandardBacktest(" not in js
    assert "function closeTrendReport(" not in js
    assert ".market-section-row" in css
    assert ".market-section-us-stock" in css
    assert ".market-section-us-option" in css
    assert ".market-section-hk-stock" in css
    assert ".market-section-hk-option" in css
    assert ".symbol-cell" in css
    scoped_table_selector = ".holdings-panel > .table-wrap > table"
    assert scoped_table_selector in css
    global_table_css = css.split(scoped_table_selector, 1)[0]
    assert "table-layout: fixed;" not in global_table_css
    assert "min-width: 1120px;" in css
    assert "table-layout: fixed;" in css
    symbol_column_selector = (
        ".holdings-panel > .table-wrap > table > thead > tr > th:nth-child(3) {"
    )
    assert symbol_column_selector in css
    assert ".holdings-panel > .table-wrap > table th:nth-child(3) {" not in css
    assert ".holdings-panel > .table-wrap > table > thead > tr > th:nth-child(1) {" in css
    assert ".holdings-panel > .table-wrap > table > thead > tr > th:nth-child(10) {" in css
    symbol_column_css = css.split(symbol_column_selector, 1)[1].split("}", 1)[0]
    assert "width: 170px;" in symbol_column_css
    number_cell_css = css.split(".number-cell {", 1)[1].split("}", 1)[0]
    assert "text-align: right;" in number_cell_css
    market_section_other_css = css.split(".market-section-other td {", 1)[1].split("}", 1)[0]
    assert "border-bottom-color: var(--line);" in market_section_other_css
    assert "grid-template-columns: minmax(0, 1fr) 300px;" not in css
    assert ".right-rail" not in css
    assert 'grid-template-areas: "brand source" "assets assets";' not in css
    assert 'grid-template-areas: "brand" "assets" "source";' in css
    assert ".symbol-detail-panel" in css
    assert ".language-toggle" in css
    assert ".english-source" in css
    assert ".detail-metric-grid" in css
    assert "renderAgentReportSection(holding.agent_report, holding)" not in js
    assert "renderStrategySection(holding.strategy, holding)" not in js
    assert "renderTradeActionSection(holding)" not in js
    assert ".raw-report" in css
    assert "renderActionQueueSummary" in js
    assert "sortedTradeActions" in js
    assert "tradeActionCounts" in js
    assert "openTradeActionDetail" in js
    assert "renderTradeDecisionBand" in js
    assert "renderTradeImpactGrid" in js
    assert "renderRationaleDialogue" in js
    assert "rationaleRows" in js
    assert "sourceRows" in js
    assert "hasRawEnglishProse" in js
    assert "firstAvailableText(rawText, text)" in js
    assert "短触发理由" in js
    assert "清晰交易策略" in js
    assert "操作方向与价位" in js
    assert "理由对话" in js
    assert "查看完整策略" in js
    assert "需复核" in js
    assert "待处理" in js
    assert "未知值显示 -" not in js
    assert ".action-summary-grid" in css
    assert ".action-card" in css
    assert ".decision-band" in css
    assert ".impact-grid" in css
    assert ".dialogue-row" in css
    wide_css = css.split("@media (max-width: 1180px)", 1)[1].split(
        "@media (max-width: 760px)", 1
    )[0]
    mobile_css = css.split("@media (max-width: 760px)", 1)[1]
    assert (
        ".decision-band,\n"
        "  .impact-grid {\n"
        "    grid-template-columns: repeat(2, minmax(0, 1fr));\n"
        "  }"
    ) in wide_css
    assert (
        ".action-card-metrics,\n"
        "  .action-summary-grid,\n"
        "  .decision-band,\n"
        "  .impact-grid {\n"
        "    grid-template-columns: 1fr;\n"
        "  }"
    ) in mobile_css
    assert ".workspace-grid.detail-mode {" in mobile_css
    assert ".compact-kv div {\n    display: grid;\n    gap: 3px;\n  }" in mobile_css
    assert ".compact-kv dd {\n    text-align: left;\n  }" in mobile_css


def test_trading_decision_tab_css() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert ".decision-tab-list" in css
    assert "overflow-x: auto" in css
    assert "flex-wrap: nowrap" in css
    assert ".decision-tab.active" in css
    assert ".decision-tab-failed" in css
    assert ".decision-tab-panel" in css


def test_dashboard_static_contains_kelly_lab_panel_mount() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="kelly-lab-panel"' in html
    assert 'id="dashboard-shell"' in html
    assert 'id="workspace-grid"' in html
    assert 'id="holdings-panel"' in html
    assert 'id="close-standard-backtest"' not in html


def test_dashboard_static_mounts_account_holdings_without_standalone_tiger_panel() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="account-holdings"' in html
    assert 'id="trend-report-workspace"' in html
    assert 'aria-live="polite"' in html
    assert 'id="tiger-long-term-panel"' not in html
    assert 'id="header-broker-filters"' not in html


def test_dashboard_static_mounts_broker_tabs_and_removes_cash_view() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert 'id="account-tabs"' in html
    assert 'data-market="CASH"' not in html
    assert 'id="cash-detail-panel"' not in html
    assert 'id="open-kelly-lab"' in html
    assert 'id="kelly-lab-panel"' in html
    assert 'state.brokerFilter = "futu"' not in js
    assert 'brokerFilter: "futu"' in js
    assert "function renderAccountTabs(" in js
    assert "function selectBroker(" in js


def test_dashboard_renders_one_selected_broker_tab_and_cards_switch_it() -> None:
    output = run_dashboard_js(r'''
const mount = () => ({innerHTML:"", textContent:"", classList:{add(){},remove(){}}});
for (const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel","current-view-value","current-view-holding-value","current-view-holding-weight","current-view-cash-note","current-view-label"]) elements[id]=mount();
state.dashboard={
  summary:{portfolio_value_hkd:"4000.00"}, source_statuses:[], cash_rows:[],
  broker_summaries:[
    {broker:"futu",display_name:"富途",portfolio_value_hkd:"1000",holding_count:"1"},
    {broker:"tiger",display_name:"老虎",portfolio_value_hkd:"1000",holding_count:"1"},
    {broker:"phillips",display_name:"辉立",portfolio_value_hkd:"1000",holding_count:"0"},
    {broker:"eastmoney",display_name:"东方财富",portfolio_value_hkd:"1000",holding_count:"0"},
  ],
  holdings:[
    {market:"US",symbol:"AAPL",brokers:"futu",broker_details:[{broker:"futu",market:"US",symbol:"AAPL",quantity:"1"}]},
    {market:"US",symbol:"QQQ",brokers:"tiger",broker_details:[{broker:"tiger",market:"US",symbol:"QQQ",quantity:"2"}]},
  ],
};
renderAccountHoldings();
renderHeaderSummary();
const first={broker:state.brokerFilter,tabs:elements["account-tabs"].innerHTML,html:elements["account-holdings"].innerHTML,value:elements["current-view-value"].textContent};
handleBrokerSelection({target:{closest(){return {dataset:{broker:"tiger"}};}}});
const second={broker:state.brokerFilter,tabs:elements["account-tabs"].innerHTML,html:elements["account-holdings"].innerHTML,label:elements["current-view-label"].textContent,value:elements["current-view-value"].textContent};
state.marketFilter="HK";
renderDashboardViews();
const market={label:elements["current-view-label"].textContent,value:elements["current-view-value"].textContent};
console.log(JSON.stringify({first,second,market,cards:renderBrokerSummaryCards()}));
''')
    result = json.loads(output)
    assert result["first"]["broker"] == "futu"
    assert 'aria-selected="true"' in result["first"]["tabs"]
    assert 'id="account-futu"' in result["first"]["html"]
    assert 'id="account-tiger"' not in result["first"]["html"]
    assert result["second"]["broker"] == "tiger"
    assert 'id="account-tiger"' in result["second"]["html"]
    assert "老虎" in result["second"]["label"]
    assert result["first"]["value"] == "HKD 4,000"
    assert result["second"]["value"] == "HKD 4,000"
    assert result["market"]["value"] == "HKD 4,000"
    assert "HK · 老虎 · 0 条" in result["market"]["label"]
    assert 'data-broker="tiger"' in result["cards"]
    assert 'href="#account-tiger"' not in result["cards"]


def test_dashboard_broker_clicks_select_empty_accounts_and_ignore_invalid() -> None:
    output = run_dashboard_js(r'''
class Element {
  constructor(){
    this.dataset={};this.hidden=false;this.innerHTML="";this.textContent="";this.listeners={};
    this.classList={add(){},remove(){},toggle(){},contains(){return false;}};
  }
  addEventListener(name,listener){this.listeners[name]=listener;}
  querySelectorAll(){return [];}
}
const nodes={};
document.getElementById=(id)=>nodes[id]||(nodes[id]=new Element());
document.querySelector=()=>nodes["workspace-grid"]||(nodes["workspace-grid"]=new Element());
bindElements();
bindEvents();
state.dashboard={
  summary:{portfolio_value_hkd:"4000",holding_count:"2"},source_statuses:[],cash_rows:[],
  broker_summaries:ACCOUNT_BROKERS.map((broker)=>({broker,portfolio_value_hkd:"1000",holding_count:broker==="futu"||broker==="tiger"?"1":"0"})),
  holdings:[
    {market:"US",symbol:"AAPL",brokers:"futu",broker_details:[{broker:"futu",market:"US",symbol:"AAPL",quantity:"1"}]},
    {market:"US",symbol:"QQQ",brokers:"tiger",broker_details:[{broker:"tiger",market:"US",symbol:"QQQ",quantity:"2"}]},
  ],
};
const eventFor=(broker)=>({target:{closest(selector){return selector==="[data-broker]"?{dataset:{broker}}:null;}}});
if(typeof nodes["account-tabs"].listeners.click!=="function")throw new Error("account tab click listener missing");
if(typeof nodes["broker-summary-cards"].listeners.click!=="function")throw new Error("broker card click listener missing");
nodes["account-tabs"].listeners.click(eventFor("phillips"));
const phillips={broker:state.brokerFilter,html:nodes["account-holdings"].innerHTML,tabs:nodes["account-tabs"].innerHTML};
nodes["broker-summary-cards"].listeners.click(eventFor("eastmoney"));
const eastmoney={broker:state.brokerFilter,html:nodes["account-holdings"].innerHTML,tabs:nodes["account-tabs"].innerHTML};
const beforeInvalid=nodes["account-holdings"].innerHTML;
nodes["account-tabs"].listeners.click(eventFor("ALL"));
const invalid={broker:state.brokerFilter,unchanged:beforeInvalid===nodes["account-holdings"].innerHTML};
console.log(JSON.stringify({phillips,eastmoney,invalid}));
''')
    result = json.loads(output)
    assert result["phillips"]["broker"] == "phillips"
    assert 'id="account-phillips"' in result["phillips"]["html"]
    assert "当前筛选下没有持仓" in result["phillips"]["html"]
    assert 'data-broker="phillips" aria-selected="true"' in result["phillips"]["tabs"]
    assert result["eastmoney"]["broker"] == "eastmoney"
    assert 'id="account-eastmoney"' in result["eastmoney"]["html"]
    assert "当前筛选下没有持仓" in result["eastmoney"]["html"]
    assert 'data-broker="eastmoney" aria-selected="true"' in result["eastmoney"]["tabs"]
    assert result["invalid"] == {"broker": "eastmoney", "unchanged": True}


def test_dashboard_render_falls_back_to_first_account_broker() -> None:
    output = run_dashboard_js(r'''
const mount=()=>({innerHTML:"",textContent:"",classList:{add(){},remove(){}}});
for(const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"])elements[id]=mount();
state.dashboard={
  summary:{portfolio_value_hkd:"1000"},broker_summaries:[],source_statuses:[],cash_rows:[],
  holdings:[{market:"US",symbol:"AAPL",brokers:"futu",broker_details:[{broker:"futu",market:"US",symbol:"AAPL",quantity:"1"}]}],
};
state.brokerFilter="invalid";
renderAccountHoldings();
console.log(JSON.stringify({broker:state.brokerFilter,tabs:elements["account-tabs"].innerHTML,html:elements["account-holdings"].innerHTML}));
''')
    result = json.loads(output)
    assert result["broker"] == "futu"
    assert 'data-broker="futu" aria-selected="true"' in result["tabs"]
    assert 'id="account-futu"' in result["html"]
    for broker in ("tiger", "phillips", "eastmoney"):
        assert f'id="account-{broker}"' not in result["html"]


def test_dashboard_decision_deep_link_prefers_account_broker_order() -> None:
    output = run_dashboard_js(r'''
globalThis.window={location:{search:"?market=US&symbol=QQQ&decision_tab=news"}};
state.dashboard={
  summary:{portfolio_value_hkd:"3000"},broker_summaries:[],source_statuses:[],cash_rows:[],
  holdings:[{market:"US",symbol:"QQQ",brokers:"tiger;futu",broker_details:[
    {broker:"tiger",market:"US",symbol:"QQQ",quantity:"2"},
    {broker:"futu",market:"US",symbol:"QQQ",quantity:"1"},
  ]}],
};
state.brokerFilter="tiger";
state.decisionDeepLinkRestored=false;
restoreDecisionDeepLink();
console.log(JSON.stringify({broker:state.brokerFilter,key:state.selectedHoldingKey,tab:state.selectedDecisionTab,market:state.marketFilter}));
''')
    result = json.loads(output)
    assert result == {
        "broker": "futu",
        "key": "futu:US:QQQ:0",
        "market": "ALL",
        "tab": "news",
    }


def test_dashboard_broker_switch_clears_stale_decision_deep_link_before_reload() -> None:
    output = run_dashboard_js(r'''
globalThis.window={
  location:{pathname:"/",search:"?market=US&symbol=AAPL&decision_tab=news",hash:""},
  history:{replaceState(_state,_title,url){
    const query=String(url).split("?",2)[1]||"";
    window.location.search=query?`?${query.split("#",1)[0]}`:"";
  }},
};
const mount=()=>({innerHTML:"",textContent:"",classList:{add(){},remove(){}}});
for(const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"])elements[id]=mount();
state.dashboard={
  summary:{portfolio_value_hkd:"3000"},broker_summaries:[],source_statuses:[],cash_rows:[],
  holdings:[{market:"US",symbol:"AAPL",brokers:"futu",broker_details:[
    {broker:"futu",market:"US",symbol:"AAPL",quantity:"1"},
  ]}],
};
state.brokerFilter="futu";
state.selectedHoldingKey="futu:US:AAPL:0";
state.selectedDecisionTab="news";
selectBroker("tiger");
const afterSwitch={search:window.location.search,key:state.selectedHoldingKey,broker:state.brokerFilter};
state.selectedHoldingKey="";
state.decisionDeepLinkRestored=false;
restoreDecisionDeepLink();
console.log(JSON.stringify({afterSwitch,afterReload:{key:state.selectedHoldingKey,broker:state.brokerFilter}}));
''')
    assert json.loads(output) == {
        "afterSwitch": {"search": "", "key": "", "broker": "tiger"},
        "afterReload": {"key": "", "broker": "tiger"},
    }


def test_dashboard_trend_report_entries_and_workspace_interactions() -> None:
    output = run_dashboard_js(r'''
class E {
  constructor(){this.dataset={};this.hidden=false;this.innerHTML="";this.textContent="";this.listeners={};this.classes=new Set();this.scrolled=false;this.classList={add:(...names)=>names.forEach((name)=>this.classes.add(name)),remove:(...names)=>names.forEach((name)=>this.classes.delete(name)),toggle:(name,force)=>force===undefined?(this.classes.has(name)?this.classes.delete(name):this.classes.add(name)):force?this.classes.add(name):this.classes.delete(name),contains:(name)=>this.classes.has(name)};}
  addEventListener(name,listener){this.listeners[name]=listener;}
  click(target=this){return this.listeners.click&&this.listeners.click({target,preventDefault(){}});}
  focus(){document.activeElement=this;}
  closest(selector){
    if(selector==="[data-trend-report]"&&Object.hasOwn(this.dataset,"trendReport"))return this;
    if(selector==="[data-close-trend-report]"&&Object.hasOwn(this.dataset,"closeTrendReport"))return this;
    return null;
  }
}
const nodes={};
document.getElementById=(id)=>nodes[id]||(nodes[id]=new E());
document.querySelector=(selector)=>selector===".workspace-grid"?document.getElementById("workspace-grid"):selector==="#account-futu [data-trend-report]"?open:new E();
bindElements();bindEvents();

const report=(broker,brokerLabel,marketLabel)=>({
  available:true,broker,broker_label:brokerLabel,market_label:marketLabel,
  status_text:"数据截至 2026-07-14；今日未更新",
  report_date:"2026-07-15",data_date:"2026-07-14",generated_at:"2026-07-15T11:30:36+08:00",
  account_status:"账户数据非实时，执行前核对现金与持仓",buy_window:"美股常规交易时段",
  sell_actions:[{symbol:"SELLX",name:"卖出标的",reason:"danger_signal",active_line:"90"}],
  buy_actions:[{symbol:"BUYX",name:"买入标的",estimated_shares:"20",target_amount:"5000",estimated_initial_line:"88"}],
  hold_actions:[{symbol:"HOLDX",name:"持有标的",reason:"trend_intact",active_line:"80"}],
  review_actions:[{symbol:"REVIEWX",name:"复核标的",reason:"holding_signal_unknown"}],
  counts:{sell:1,buy:1,hold:1,review:1},
  audit:{candidates:[{symbol:"CANDX",name:"候选标的",strength:"95"}],excluded:{EXCLUDED:["already_held"]},account_exceptions:["现金类资产不参与趋势判断：FUTU_UNMAPPED_ASSETS（cash）"],industry_concentration:[["科技",1,"0.25"]],data_sources:["Trend Animals"],actual_api_cost:"1.00"},
});
const transition=(previous,current,changed)=>({previous,current,changed});
const attentionItem={market:"US",symbol:"QQQ",name:"纳指 ETF",category:"watch",
  right_side:transition(false,true,true),temperature:transition("温","热",true),phase:transition("谷雨","立夏",true),
  local_strength:"95",global_strength:"90",strength_prev_week:"91",strength_prev_month:"89",
  strength_change:transition("→","↑",true),days:1,gain_since_entry:"0.02",
  danger:transition(false,false,false),boiling:transition(false,false,false),champagne:transition(false,false,false),
  source_broker:"老虎",source_action:"BUY"};
const futu=report("futu","富途","美股 / 港股");
futu.status_text="期权关注";
futu.attention_markets=[
  {market:"US",market_label:"美股",data_status:"current",data_date:"2026-07-16",items:[attentionItem]},
  {market:"HK",market_label:"港股",data_status:"stale",data_date:"2026-07-14",items:[]},
];
state.dashboard={trend_reports:{
  futu,
  tiger:report("tiger","老虎","美股"),
  phillips:report("phillips","辉立","港股"),
  eastmoney:report("eastmoney","东方财富","A股"),
}};
const group=(broker)=>({broker,profile:ACCOUNT_STRATEGY_PROFILES[broker],rows:[],summary:{broker,display_name:broker,portfolio_value_hkd:"1000",holding_value_hkd:"700",cash_like_value_hkd:"300",holding_count:"1"}});
const html=["futu","tiger","phillips","eastmoney"].map((broker)=>renderAccountSection(group(broker))).join("");
if((html.match(/当天趋势报告/g)||[]).length!==0)throw new Error(html);
if((html.match(/期权关注/g)||[]).length<1)throw new Error(html);
if(!html.includes('data-trend-report="futu"'))throw new Error(html);
for(const broker of ["tiger","phillips","eastmoney"]){
  if(html.includes(`data-trend-report="${broker}"`)||!html.includes(`data-account-broker="${broker}" data-account-view="report"`))throw new Error(html);
}
for(const broker of ["tiger","phillips","eastmoney"]){
  const entry=renderTrendReportEntry(broker);
  for(const text of ['data-trend-report="'+broker+'"',"数据截至 2026-07-14；今日未更新","报告日期 2026-07-15","数据截至 2026-07-14"]){
    if(!entry.includes(text))throw new Error(entry);
  }
  if(entry.includes("disabled"))throw new Error(entry);
}

for(const broker of ["tiger","phillips"]){
  const sourceWorkspace=renderTrendReportWorkspace(state.dashboard.trend_reports[broker]);
  if(!sourceWorkspace.includes("当天趋势报告")||!sourceWorkspace.includes("SELLX")||sourceWorkspace.includes("option-attention-table"))throw new Error(sourceWorkspace);
}
state.dashboard.trend_reports.eastmoney.market="CN";
const cnWorkspace=renderTrendReportWorkspace(state.dashboard.trend_reports.eastmoney);
if(!cnWorkspace.includes('class="cn-trend-report"')||cnWorkspace.includes("option-attention-table"))throw new Error(cnWorkspace);

const open=new E();open.dataset.trendReport="futu";
document.getElementById("account-futu").querySelector=()=>open;
elements["account-holdings"].click(open);
if(!elements["workspace-grid"].classList.contains("hidden")||elements["trend-report-workspace"].hidden||elements["trend-report-workspace"].classList.contains("hidden"))throw new Error("workspace state");
if(document.activeElement!==elements["return-to-portfolio"])throw new Error("workspace focus");
const workspace=elements["trend-report-workspace"].innerHTML;
if(!workspace.includes("option-attention-table")||!workspace.includes("QQQ")||!workspace.includes("允许买入"))throw new Error(workspace);
if(workspace.includes("SELLX")||workspace.includes("今日执行检查")||workspace.includes("审计详情"))throw new Error(workspace);

const close=new E();close.dataset.closeTrendReport="";
elements["trend-report-workspace"].click(close);
if(elements["trend-report-workspace"].hidden!==true||!elements["trend-report-workspace"].classList.contains("hidden")||elements["workspace-grid"].classList.contains("hidden")||state.selectedTrendBroker!=="")throw new Error("close state");
if(document.activeElement!==open)throw new Error("trigger focus");

state.dashboard.trend_reports.futu={available:false,status_text:"今日暂无趋势报告",sell_actions:[{symbol:"STALE_ACTION"}]};
const stale=renderAccountSection(group("futu"));
if((stale.match(/期权关注/g)||[]).length<1||!stale.includes("disabled")||!stale.includes("今日暂无趋势报告")||stale.includes("STALE_ACTION")||stale.includes("data-trend-report"))throw new Error(stale);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_renders_controller_facts_and_terminal_action_labels() -> None:
    output = run_dashboard_js(r'''
const report={
  available:true,market:"US",broker:"tiger",broker_label:"老虎",market_label:"美股",
  report_date:"2026-07-21",data_date:"2026-07-20",generated_at:"2026-07-21T08:00:00+08:00",
  account_status:"已更新",buy_window:"美股常规交易时段",counts:{buy:3},sell_actions:[],
  buy_actions:[
    {symbol:"TRV",execution:{status:"uncertain"}},
    {symbol:"ADM",execution:{status:"conflict"}},
    {symbol:"PM",execution:{status:"missed"}},
  ],hold_actions:[],review_actions:[],audit:{},revision_anomaly:true,
  execution_batch:{report_sha256:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  latest_report_sha256:"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
};
const healthy={effective_mode:"execute",executor_host:"ray-mac",local_host:"ray-mac",
  health:"healthy",blocking:false,pid:4242,working_directory:"/srv/open_trader",
  git_sha:"abc1234",phase:"monitoring",heartbeat_at:"2026-07-21T09:31:00+08:00",
  last_success:{status:"<script>alert(1)</script>",market:"US",date:"2026-07-20",
    submitted_count:0,artifact_paths:[]},
  blocker:null,next_check_at:"2026-07-21T09:31:05+08:00"};
state.dashboard={trend_controllers:{tiger:healthy}};
const normal=renderTrendReportWorkspace(report);
for(const text of ["策略控制器","执行模式","execute","执行主机","ray-mac","本地主机",
  "PID","4242","Git SHA","abc1234","当前阶段","monitoring","心跳",
  "最近成功","状态 &lt;script&gt;alert(1)&lt;/script&gt;","市场 US","日期 2026-07-20",
  "提交数 0","产物 无","当前阻塞","下次检查",
  "状态不确定，禁止自动重试","订单事实冲突，禁止提交","已错过策略窗口",
  "发现后续报告版本，执行仍锁定原批次","aaaaaaaaaaaa","bbbbbbbbbbbb"]){
  if(!normal.includes(text))throw new Error(text+"\n"+normal);
}
if(normal.includes("[object Object]") || normal.includes("<script>"))throw new Error(normal);
state.dashboard.trend_controllers.tiger={...healthy,last_success:"report_locked"};
if(!renderTrendReportWorkspace(report).includes("report_locked"))throw new Error("string last_success");
state.dashboard.trend_controllers.tiger={...healthy,last_success:null};
if(!renderTrendReportWorkspace(report).includes("<dt>最近成功</dt><dd>—</dd>"))throw new Error("null last_success");
state.dashboard.trend_controllers.tiger=healthy;
state.dashboard.trend_reports={tiger:{available:false,data_status:"unavailable",
  broker:"tiger",execution_batch:null,execution_batch_blocking:true,
  execution_batch_error:"执行批次无效，已阻止操作投影",
  status_text:"执行批次无效，已阻止操作投影",counts:{buy:0},
  sell_actions:[],buy_actions:[],hold_actions:[],review_actions:[]}};
const batchBlocked=renderEmbeddedTrendReport("tiger");
if(!batchBlocked.includes('class="trend-execution-batch-error"') ||
   !batchBlocked.includes("执行批次无效，已阻止操作投影") ||
   !batchBlocked.includes('class="trend-controller-status"') ||
   batchBlocked.includes("TRV"))throw new Error(batchBlocked);
state.dashboard.trend_controllers.tiger={...healthy,health:"unavailable",blocking:true,
  phase:"unavailable",blocker:"controller heartbeat is stale",reason:"controller heartbeat is stale"};
const blocked=renderTrendReportWorkspace(report);
if(!blocked.includes('class="trend-controller-status blocking"') ||
   !blocked.includes('data-health="unavailable"') ||
   !blocked.includes("控制器不可用"))throw new Error(blocked);
state.dashboard.trend_controllers.tiger={...healthy,effective_mode:"readonly",health:"readonly",
  blocking:false,phase:"readonly",pid:null,reason:"local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST",
  blocker:"local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST"};
const readonly=renderTrendReportWorkspace(report);
if(!readonly.includes("只读部署，不运行本机控制器") || readonly.includes('class="trend-controller-status blocking"')){
  throw new Error(readonly);
}
state.dashboard.trend_reports={tiger:{available:false,status_text:"报告生成中"}};
const missingReport=renderEmbeddedTrendReport("tiger");
if(!missingReport.includes('class="trend-controller-status"') || !missingReport.includes("报告生成中")){
  throw new Error(missingReport);
}
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_controller_card_is_responsive_at_375px() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    rendered = json.loads(run_dashboard_js(r'''
state.dashboard={trend_controllers:{tiger:{effective_mode:"execute",executor_host:"ray-mac",
  local_host:"ray-mac",health:"unavailable",blocking:true,pid:4242,
  working_directory:"/a/very/long/path/to/the/exact/accepted/dashboard/checkout",
  git_sha:"1234567890abcdef1234567890abcdef12345678",phase:"report_generation_blocked",
  heartbeat_at:"2026-07-21T09:31:00+08:00",last_success:{status:"missed_window",
    market:"US",date:"2026-07-20",submitted_count:0,
    artifact_paths:["/a/very/long/path/to/an/execution/artifact.json"]},
  blocker:"controller heartbeat is stale after an intentionally long diagnostic message",
  next_check_at:"2026-07-21T09:31:05+08:00",reason:"controller heartbeat is stale"}}};
console.log(JSON.stringify(renderTrendReportWorkspace({available:true,market:"US",broker:"tiger",
  broker_label:"老虎",market_label:"美股",counts:{},audit:{},sell_actions:[],buy_actions:[],
  hold_actions:[],review_actions:[]})));
'''))
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    errors: list[str] = []
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - local browser availability
            pytest.skip(f"Chrome is required for dashboard DOM checks: {exc}")
        page = browser.new_page(viewport={"width": 375, "height": 844})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(f"<style>{css}</style>{rendered}")

        assert errors == []
        assert page.locator(".trend-controller-status").count() == 1
        assert "状态 missed_window" in page.locator(
            ".trend-controller-status"
        ).inner_text()
        assert "[object Object]" not in page.locator(
            ".trend-controller-status"
        ).inner_text()
        assert page.locator(".trend-controller-status dl").evaluate(
            "node => getComputedStyle(node).gridTemplateColumns.split(' ').length"
        ) == 1
        assert page.locator(".trend-controller-status").evaluate(
            "node => node.scrollWidth <= node.clientWidth"
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        browser.close()


def test_dashboard_account_view_tabs_keep_exact_order_and_futu_unchanged() -> None:
    output = run_dashboard_js(r'''
state.dashboard={
  trend_reports:{
    futu:{available:true,status_text:"期权关注"},
    tiger:{available:true,broker:"tiger",broker_label:"老虎",market_label:"美股"},
  },
  trend_reviews:{tiger:{available:true,market_label:"美股"}},
};
const group=(broker)=>({broker,profile:ACCOUNT_STRATEGY_PROFILES[broker],rows:[],summary:{
  broker,display_name:broker,portfolio_value_hkd:"1000",holding_value_hkd:"700",
  cash_like_value_hkd:"300",holding_count:"0",
}});
const tiger=renderAccountSection(group("tiger"));
const futu=renderAccountSection(group("futu"));
const labels=[...tiger.matchAll(/data-account-view="[^"]+"[^>]*>([^<]+)/g)].map((match)=>match[1].trim());
console.log(JSON.stringify({tiger,futu,labels}));
''')
    rendered = json.loads(output)
    assert rendered["labels"] == ["真实持仓", "模拟盘持仓", "趋势报告", "美股复盘"]
    assert rendered["tiger"].count('role="tab"') == 4
    assert 'data-account-view="real" aria-selected="true" tabindex="0"' in rendered["tiger"]
    assert 'role="tabpanel"' in rendered["tiger"]
    assert "trend-report-entry" not in rendered["tiger"]
    assert 'data-trend-report="futu"' in rendered["futu"]
    assert "data-account-view" not in rendered["futu"]


def test_dashboard_simulate_positions_load_once_and_render_all_states() -> None:
    output = run_dashboard_js(r'''
function mount(){return {innerHTML:"",textContent:"",attributes:{},classList:{add(){},remove(){}},
  setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];},
  querySelector(){return null;}};}
for(const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"]){elements[id]=mount();}
const panel=mount();
const tabs=["real","simulate","report","review"].map((view)=>({dataset:{accountBroker:"tiger",accountView:view},
  tabIndex:-1,setAttribute(){}}));
elements["account-holdings"].querySelector=(selector)=>selector==="#account-tiger-view-panel"?panel:null;
elements["account-holdings"].querySelectorAll=()=>tabs;
const renderPanel=renderAccountViewPanelOnly;
let panelRenders=0;
renderAccountViewPanelOnly=(broker)=>{panelRenders+=1;return renderPanel(broker);};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:{available:true}},trend_reviews:{tiger:{available:true,market_label:"美股"}}};
state.brokerFilter="tiger";
const linked={available:true,broker:"tiger",positions:[{
  broker:"tiger",market:"US",symbol:"AAPL",name:"Apple",currency:"USD",quantity:"2",
  cost_price:"180",last_price:"190",market_value:"380",market_value_hkd:"2964",
  account_weight:"38.00%",portfolio_weight:"38.00%",unrealized_pnl_pct:"5.56%",
  attribution_status:"linked",report:{artifact:"2026-07-17.json",execution_date:"2026-07-20",strategy_version:"v1"},
}]};
let calls=[];
let responsePayload=linked;
globalThis.fetch=async(url)=>{calls.push(url);return {ok:true,json:async()=>responsePayload};};
await setAccountView("tiger","simulate");
const loaded=panel.innerHTML;
const initialPanelRenders=panelRenders;
await setAccountView("tiger","simulate");
const linkedCalls=[...calls];
delete state.trendSimulatePositions.tiger;
responsePayload={available:true,broker:"tiger",positions:[]};
await setAccountView("tiger","real");
await setAccountView("tiger","simulate");
const empty=panel.innerHTML;
delete state.trendSimulatePositions.tiger;
responsePayload={available:false,broker:"tiger",positions:[],error:"OpenD 模拟账户不可用"};
await setAccountView("tiger","real");
await setAccountView("tiger","simulate");
const unavailable=panel.innerHTML;
state.trendSimulatePositions.tiger={...linked,positions:[linked.positions[0],
  {...linked.positions[0],symbol:"MSFT",attribution_status:"unlinked",report:null},
  {...linked.positions[0],symbol:"NVDA",attribution_status:"conflict",report:null}]};
renderAccountViewPanelOnly("tiger");
const attributionStates=panel.innerHTML;
console.log(JSON.stringify({loaded,initialPanelRenders,linkedCalls,allCalls:calls,empty,unavailable,attributionStates}));
''')
    rendered = json.loads(output)
    assert rendered["initialPanelRenders"] == 2
    assert rendered["linkedCalls"] == ["/api/trend-simulate-positions/tiger"]
    assert rendered["allCalls"] == ["/api/trend-simulate-positions/tiger"] * 3
    for label in (
        "明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值",
        "港元市值", "账户权重", "组合权重", "盈亏",
    ):
        assert label in rendered["loaded"]
    assert "报告 2026-07-20 · v1" in rendered["loaded"]
    assert 'data-history-artifact="2026-07-17.json"' in rendered["loaded"]
    assert "交易决策" not in rendered["loaded"]
    assert "做T" not in rendered["loaded"]
    assert "当前无模拟盘持仓" in rendered["empty"]
    assert "OpenD 模拟账户不可用" in rendered["unavailable"]
    assert "当前筛选下没有持仓" not in rendered["unavailable"]
    assert rendered["attributionStates"].count("报告 2026-07-20 · v1") == 1
    assert rendered["attributionStates"].count("未关联历史报告") == 1
    assert rendered["attributionStates"].count("报告关联冲突") == 1


def test_dashboard_manual_refresh_reloads_active_simulated_positions() -> None:
    output = run_dashboard_js(r'''
elements["refresh-quotes"]={disabled:false,textContent:""};
renderQuoteStatus=()=>{};
renderHoldings=()=>{};
state.brokerFilter="tiger";
state.accountViews.tiger="simulate";
state.trendSimulatePositions={tiger:{available:true,positions:[
  {symbol:"GPN",quantity:"485"}, {symbol:"TOST",quantity:"1296"},
]}};
const refreshed={available:true,positions:[
  {symbol:"GPN",quantity:"485"}, {symbol:"TOST",quantity:"1296"},
  {symbol:"HST",quantity:"1633"},
]};
const requests=[];
globalThis.fetch=async(url)=>{
  requests.push(url);
  return {ok:true,json:async()=>url==="/api/quotes"
    ? {quotes:{},account_sync:{status:"skipped"}}
    : refreshed};
};
await refreshQuotes({refreshSimulation:true});
console.log(JSON.stringify({
  requests,
  symbols:state.trendSimulatePositions.tiger.positions.map((position)=>position.symbol),
}));
''')
    rendered = json.loads(output)

    assert rendered["requests"] == [
        "/api/quotes",
        "/api/trend-simulate-positions/tiger",
    ]
    assert rendered["symbols"] == ["GPN", "TOST", "HST"]


def test_dashboard_report_loads_simulation_and_keeps_real_comparison() -> None:
    output = run_dashboard_js(r'''
function mount(){return {innerHTML:"",textContent:"",attributes:{},classList:{add(){},remove(){}},
  setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];},
  querySelector(){return null;}};}
for(const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"]){elements[id]=mount();}
const panel=mount();
elements["account-holdings"].querySelector=(selector)=>selector==="#account-tiger-view-panel"?panel:null;
elements["account-holdings"].querySelectorAll=()=>[];
state.dashboard={
  summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reviews:{tiger:{available:true,market_label:"美股"}},
  trend_reports:{tiger:{
    available:true,broker:"tiger",broker_label:"老虎",market:"US",market_label:"美股",
    risk_summary:{},drawdown_summary:{},actual_overlay:{available:true,
      broker_label:"老虎",status_text:"账户实时同步",notice:"只读对照，不影响模拟建议与自动执行",
      items:[],outside_positions:[]},
    sell_actions:[],buy_actions:[{action:"BUY",symbol:"HST",name:"HOST酒店及度假村",
      estimated_shares:"1635",close:"24.44",estimated_initial_line:"23.428857142857"}],
    hold_actions:[{action:"HOLD",symbol:"GPN",name:"环汇有限公司",close:"80.07",active_line:"74.3550"},
      {action:"HOLD",symbol:"TOST",name:"Toast",close:"30.37",active_line:"28.305071428571"}],
    review_actions:[],risk_skips:[],counts:{},audit:{},
  }},
};
state.brokerFilter="tiger";
const urls=[];
globalThis.fetch=async(url)=>{urls.push(url);return {ok:true,json:async()=>({available:true,broker:"tiger",positions:[
  {symbol:"GPN",name:"环汇有限公司",quantity:"485.0",cost_price:"80.99",last_price:"80.07"},
  {symbol:"TOST",name:"Toast",quantity:"1296.0",cost_price:"30.594999999999995",last_price:"30.37"},
]})};};
await setAccountView("tiger","report");
console.log(JSON.stringify({urls,html:panel.innerHTML}));
''')
    rendered = json.loads(output)
    html = rendered["html"]
    assert rendered["urls"] == ["/api/trend-simulate-positions/tiger"]
    for text in (
        "模拟盘执行状态", "富途", "实盘执行辅助", "老虎", "GPN",
        "模拟持仓 485", "TOST", "模拟持仓 1,296",
    ):
        assert text in html
    assert html.count('data-deviation="followed"') == 2
    assert html.count("一致") == 2
    assert 'data-deviation="pending">待执行' in html
    assert "未持有" not in html


def test_dashboard_historical_report_omits_simulation_reconciliation() -> None:
    output = run_dashboard_js(r'''
const report={
  available:true,broker:"tiger",broker_label:"老虎",market:"US",market_label:"美股",
  report_date:"2026-07-17",data_date:"2026-07-16",counts:{},audit:{},
  risk_summary:{},drawdown_summary:{},
  sell_actions:[{action:"SELL_ALL",symbol:"EXIT",name:"Exit",close:"10",active_line:"9"}],
  buy_actions:[{action:"BUY",symbol:"MISSED",name:"Missed",execution:{status:"missed"}}],
  hold_actions:[],review_actions:[],risk_skips:[],
};
state.trendSimulatePositions.tiger={available:true,broker:"tiger",positions:[
  {symbol:"EXTRA",name:"Outside",quantity:"12",cost_price:"8",last_price:"9"},
]};
const current=renderTrendReportWorkspace(report,true,false);
const historical=renderTrendReportWorkspace(report,true,true);
const loading=renderTrendSimulationOverlay(report,{loading:true});
const unavailable=renderTrendSimulationOverlay(report,{available:false,error:"OpenD 模拟账户不可用"});
console.log(JSON.stringify({current,historical,loading,unavailable}));
''')
    rendered = json.loads(output)
    current = rendered["current"]
    historical = rendered["historical"]
    assert 'class="trend-simulation-overlay"' in current
    assert "模拟盘执行状态" in current
    assert re.search(
        r'data-simulation-symbol="EXIT".*?data-deviation="followed">一致',
        current,
        re.DOTALL,
    )
    assert re.search(
        r'data-simulation-symbol="EXTRA".*?data-deviation="outside_report_addition">报告外持仓',
        current,
        re.DOTALL,
    )
    assert 'class="trend-simulation-overlay"' not in historical
    assert "模拟盘执行状态" not in historical
    assert "EXIT" in historical
    assert "已错过策略窗口" in historical
    for state_html in (rendered["loading"], rendered["unavailable"]):
        assert "data-simulation-symbol" not in state_html
        assert "未持有" not in state_html
    assert "模拟盘持仓加载中" in rendered["loading"]
    assert "OpenD 模拟账户不可用" in rendered["unavailable"]


def test_dashboard_simulation_overlay_escapes_every_hostile_rendered_fact() -> None:
    output = run_dashboard_js(r'''
const attack='"><img src=x onerror=alert(1)>';
const html=renderTrendSimulationOverlay({
  sell_actions:[],hold_actions:[],review_actions:[],risk_skips:[],
  buy_actions:[{action:"BUY",symbol:attack,name:attack,estimated_shares:attack,
    close:attack,estimated_initial_line:attack}],
},{available:true,positions:[{symbol:attack,name:attack,quantity:attack,
  cost_price:attack,last_price:attack}]});
const unavailable=renderTrendSimulationOverlay({}, {available:false,error:attack});
console.log(JSON.stringify({html,unavailable}));
''')
    rendered = json.loads(output)
    assert "<img" not in rendered["html"]
    assert '<img' not in rendered["unavailable"]
    assert 'data-simulation-symbol="&quot;&gt;&lt;IMG' in rendered["html"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered["html"]
    assert "报告数量 &quot;&gt;&lt;img" in rendered["html"]
    assert "模拟持仓 &quot;&gt;&lt;img" in rendered["html"]
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in rendered["unavailable"]


def test_dashboard_report_history_is_inline_exact_and_restores_scroll() -> None:
    output = run_dashboard_js(r'''
function mount(){const classes=new Set();return {innerHTML:"",textContent:"",attributes:{},classList:{
  add(...names){names.forEach((name)=>classes.add(name));},remove(...names){names.forEach((name)=>classes.delete(name));},
  contains(name){return classes.has(name);}},setAttribute(name,value){this.attributes[name]=value;},
  removeAttribute(name){delete this.attributes[name];},querySelector(){return null;}};}
for(const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"]){elements[id]=mount();}
const panel=mount();
elements["account-holdings"].querySelector=(selector)=>selector==="#account-tiger-view-panel"?panel:null;
elements["account-holdings"].querySelectorAll=()=>[];
let restored=-1;
globalThis.window={scrollY:321,scrollTo(_x,y){restored=y;},location:{search:""}};
const current={available:true,broker:"tiger",broker_label:"老虎",market:"US",market_label:"美股",
  report_date:"2026-07-20",data_date:"2026-07-17",counts:{},audit:{}};
const historical={...current,report_date:"2026-07-17",buy_actions:[{symbol:"AAPL",execution:{status:"missed"}}]};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:current},trend_reviews:{tiger:{available:true,market_label:"美股"}}};
state.brokerFilter="tiger";
const urls=[];
globalThis.fetch=async(url)=>{urls.push(url);return {ok:true,json:async()=>url.includes("trend-simulate-positions")
  ? {available:true,broker:"tiger",positions:[]}
  : url.endsWith("2026-07-16.json") ? historical
  : [{available:true,artifact:"2026-07-16.json",execution_date:"2026-07-17",strategy_version:"v1"}]};};
await setAccountView("tiger","report");
const currentHtml=panel.innerHTML;
await openTrendReportHistory("tiger");
const historyHtml=panel.innerHTML;
await loadHistoricalTrendReport("tiger","2026-07-16.json");
const historicalHtml=panel.innerHTML;
showCurrentTrendReport("tiger");
const restoredHtml=panel.innerHTML;
const historyRestored=restored;
delete state.trendReportHistories.tiger;
window.scrollY=456;
await loadHistoricalTrendReport("tiger","2026-07-16.json");
showCurrentTrendReport("tiger");
const directRestored=restored;
console.log(JSON.stringify({urls,currentHtml,historyHtml,historicalHtml,restoredHtml,historyRestored,directRestored,
  view:state.accountViews.tiger,workspaceHidden:elements["workspace-grid"].classList.contains("hidden")}));
''')
    rendered = json.loads(output)
    assert rendered["urls"] == [
        "/api/trend-simulate-positions/tiger",
        "/api/trend-reports/tiger/history",
        "/api/trend-reports/tiger/history/2026-07-16.json",
        "/api/trend-reports/tiger/history/2026-07-16.json",
    ]
    assert "当天趋势报告" in rendered["currentHtml"]
    assert "历史报告" in rendered["currentHtml"]
    assert "返回持仓看板" not in rendered["currentHtml"]
    assert "2026-07-16.json" in rendered["historyHtml"]
    assert "错过" in rendered["historicalHtml"]
    assert "返回当前报告" in rendered["historicalHtml"]
    assert "当天趋势报告" in rendered["restoredHtml"]
    assert rendered["historyRestored"] == 321
    assert rendered["directRestored"] == 456
    assert rendered["view"] == "report"
    assert rendered["workspaceHidden"] is False


def test_dashboard_quote_refresh_does_not_replace_active_report_view() -> None:
    output = run_dashboard_js(r'''
elements["refresh-quotes"]={disabled:false,textContent:""};
state.brokerFilter="tiger";
state.accountViews.tiger="report";
state.trendReportHistories.tiger={open:true};
state.dashboard={marker:"before"};
let failDashboard=false;
globalThis.fetch=async(url)=>url==="/api/quotes"
  ? {ok:true,json:async()=>({quotes:{},account_sync:{status:"ok"}})}
  : failDashboard
    ? {ok:false,status:500}
    : {ok:true,json:async()=>({poll_seconds:0,marker:"after"})};
renderQuoteStatus=()=>{};
let holdingRenders=0;
const frozenPanel={id:"frozen"};
let currentPanel=frozenPanel;
const replacementPanel={replaceWith(panel){currentPanel=panel;}};
elements["account-holdings"]={querySelector(){return currentPanel;}};
renderDashboard=()=>renderDashboardViews();
renderHeaderSummary=()=>{};
renderAccountHoldings=()=>{holdingRenders+=1;currentPanel=replacementPanel;};
await refreshQuotes();
const dashboardAfterSuccess=state.dashboard;
failDashboard=true;
await refreshQuotes();
console.log(JSON.stringify({holdingRenders,panelPreserved:currentPanel===frozenPanel,
  currentReportRefreshed:dashboardAfterSuccess.marker==="after",
  failedReloadPreserved:state.dashboard===dashboardAfterSuccess}));
''')
    assert json.loads(output) == {
        "holdingRenders": 1,
        "panelPreserved": True,
        "currentReportRefreshed": True,
        "failedReloadPreserved": True,
    }


def test_dashboard_account_view_keyboard_and_mobile_acceptance_css() -> None:
    output = run_dashboard_js(r'''
let focused="";
elements["account-holdings"]={innerHTML:"",classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){},
  querySelector(selector){return {innerHTML:"",setAttribute(){},focus(){focused=selector;}};}};
state.accountViews={tiger:"real",phillips:"real",eastmoney:"real"};
state.trendSimulatePositions={tiger:{available:true,positions:[]}};
state.dashboard={summary:{portfolio_value_hkd:"0"},broker_summaries:[],cash_rows:[],holdings:[],
  trend_reports:{tiger:{available:false}},trend_reviews:{tiger:{available:false}}};
state.brokerFilter="tiger";
for(const id of ["account-tabs","visible-count","workspace-grid","symbol-detail-panel"]){elements[id]={innerHTML:"",textContent:"",
  classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){}};}
const press=(view,key)=>{let prevented=false;handleAccountViewTabKeydown({key,target:{closest(){return {dataset:{accountBroker:"tiger",accountView:view}};}},preventDefault(){prevented=true;}});return {view:state.accountViews.tiger,focused,prevented};};
console.log(JSON.stringify({left:press("real","ArrowLeft"),right:press("real","ArrowRight"),home:press("review","Home"),end:press("real","End")}));
''')
    rendered = json.loads(output)
    assert rendered["left"] == {"view": "review", "focused": '[data-account-view="review"]', "prevented": True}
    assert rendered["right"] == {"view": "simulate", "focused": '[data-account-view="simulate"]', "prevented": True}
    assert rendered["home"] == {"view": "real", "focused": '[data-account-view="real"]', "prevented": True}
    assert rendered["end"] == {"view": "review", "focused": '[data-account-view="review"]', "prevented": True}

    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    tabs = css.split(".account-view-tabs {", 1)[1].split("}", 1)[0]
    tab = css.split(".account-view-tab {", 1)[1].split("}", 1)[0]
    selected = css.split('.account-view-tab[aria-selected="true"] {', 1)[1].split("}", 1)[0]
    mobile = css.split("@media (max-width: 760px) {", 1)[1]
    assert "overflow-x: auto;" in tabs
    assert "min-height: 44px;" in tab
    assert "border: 0;" in tab
    assert "font-weight: 700;" in selected
    assert ".account-view-tabs" in mobile
    assert "body {\n    overflow-x: hidden;" not in mobile


def test_dashboard_history_completion_does_not_reopen_after_back() -> None:
    output = run_dashboard_js(r'''
const panel={innerHTML:"",setAttribute(){}};
elements["account-holdings"]={querySelector(){return panel;},querySelectorAll(){return [];}};
elements["visible-count"]={textContent:""};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:{available:true,broker:"tiger",broker_label:"老虎",market_label:"美股",counts:{},audit:{}}},trend_reviews:{}};
state.brokerFilter="tiger";
state.accountViews.tiger="report";
const scrolls=[];
globalThis.window={scrollY:111,scrollTo(_x,y){scrolls.push(y);},location:{search:""}};
let resolveHistory;
let resolveRows;
globalThis.fetch=()=>new Promise((resolve)=>{resolveHistory=resolve;});
const renderPanel=renderAccountViewPanelOnly;
let panelRenders=0;
renderAccountViewPanelOnly=(broker)=>{panelRenders+=1;return renderPanel(broker);};
const request=openTrendReportHistory("tiger");
resolveHistory({ok:true,json:()=>new Promise((resolve)=>{resolveRows=resolve;})});
while(!resolveRows) await Promise.resolve();
showCurrentTrendReport("tiger");
const currentHtml=panel.innerHTML;
panelRenders=0;
scrolls.length=0;
resolveRows([{available:true,artifact:"2026-07-16.json"}]);
await request;
console.log(JSON.stringify({history:state.trendReportHistories.tiger,currentHtml,panelHtml:panel.innerHTML,panelRenders,scrolls}));
''')
    rendered = json.loads(output)
    assert rendered["history"]["open"] is False
    assert rendered["history"]["loading"] is False
    assert rendered["history"]["rows"] == [
        {"available": True, "artifact": "2026-07-16.json"},
    ]
    assert "当天趋势报告" in rendered["currentHtml"]
    assert rendered["panelHtml"] == rendered["currentHtml"]
    assert rendered["panelRenders"] == 0
    assert rendered["scrolls"] == []


def test_dashboard_history_error_does_not_render_in_inactive_account_view() -> None:
    output = run_dashboard_js(r'''
const panel={innerHTML:"",setAttribute(){}};
elements["account-holdings"]={querySelector(){return panel;},querySelectorAll(){return [];}};
elements["visible-count"]={textContent:""};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:{available:true,broker:"tiger",counts:{},audit:{}}},
  trend_reviews:{tiger:{available:true,market_label:"美股",strategy_snapshot:{parameter_rows:[]},metrics:{}}}};
state.brokerFilter="tiger";
state.trendSimulatePositions.tiger={available:true,positions:[]};
const scrolls=[];
globalThis.window={scrollY:222,scrollTo(_x,y){scrolls.push(y);},location:{search:""}};
let resolveHistory;
globalThis.fetch=()=>new Promise((resolve)=>{resolveHistory=resolve;});
const renderPanel=renderAccountViewPanelOnly;
let panelRenders=0;
renderAccountViewPanelOnly=(broker)=>{panelRenders+=1;return renderPanel(broker);};
const results=[];
for(const view of ["real","simulate","review"]){
  state.accountViews.tiger="report";
  delete state.trendReportHistories.tiger;
  const request=openTrendReportHistory("tiger");
  await setAccountView("tiger",view);
  panelRenders=0;
  scrolls.length=0;
  resolveHistory({ok:false,status:500,json:async()=>({})});
  await request;
  results.push({view:state.accountViews.tiger,history:state.trendReportHistories.tiger,panelRenders,scrolls:[...scrolls]});
}
console.log(JSON.stringify(results));
''')
    rendered = json.loads(output)
    assert [entry["view"] for entry in rendered] == ["real", "simulate", "review"]
    for entry in rendered:
        assert entry["history"]["open"] is True
        assert entry["history"]["loading"] is False
        assert entry["history"]["rows"] == []
        assert entry["history"]["error"] == "report history 500"
        assert entry["panelRenders"] == 0
        assert entry["scrolls"] == []


def test_dashboard_exact_report_completion_does_not_render_in_inactive_account_view() -> None:
    output = run_dashboard_js(r'''
const panel={innerHTML:"",setAttribute(){}};
elements["account-holdings"]={querySelector(){return panel;},querySelectorAll(){return [];}};
elements["visible-count"]={textContent:""};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:{available:true,broker:"tiger",counts:{},audit:{}}},
  trend_reviews:{tiger:{available:true,market_label:"美股",strategy_snapshot:{parameter_rows:[]},metrics:{}}}};
state.brokerFilter="tiger";
state.trendSimulatePositions.tiger={available:true,positions:[]};
const scrolls=[];
globalThis.window={scrollY:333,scrollTo(_x,y){scrolls.push(y);},location:{search:""}};
let resolveExact;
globalThis.fetch=()=>new Promise((resolve)=>{resolveExact=resolve;});
const renderPanel=renderAccountViewPanelOnly;
let panelRenders=0;
renderAccountViewPanelOnly=(broker)=>{panelRenders+=1;return renderPanel(broker);};
const results=[];
for(const [view,ok] of [["real",true],["simulate",false],["review",true]]){
  state.accountViews.tiger="report";
  state.trendReportHistories.tiger={open:true,scrollY:100};
  delete state.trendHistoricalReports.tiger;
  const request=loadHistoricalTrendReport("tiger",`${view}.json`);
  await setAccountView("tiger",view);
  panelRenders=0;
  scrolls.length=0;
  resolveExact({ok,status:500,json:async()=>({available:true,artifact:`${view}.json`})});
  await request;
  results.push({view:state.accountViews.tiger,exact:state.trendHistoricalReports.tiger,panelRenders,scrolls:[...scrolls]});
}
console.log(JSON.stringify(results));
''')
    rendered = json.loads(output)
    assert [entry["view"] for entry in rendered] == ["real", "simulate", "review"]
    assert rendered[0]["exact"]["report"]["artifact"] == "real.json"
    assert rendered[1]["exact"]["error"] == "historical report 500"
    assert rendered[2]["exact"]["report"]["artifact"] == "review.json"
    for entry in rendered:
        assert entry["panelRenders"] == 0
        assert entry["scrolls"] == []


def test_dashboard_direct_exact_report_refreshes_scroll_and_ignores_stale_artifact() -> None:
    output = run_dashboard_js(r'''
const panel={innerHTML:"",setAttribute(){}};
elements["account-holdings"]={querySelector(){return panel;},querySelectorAll(){return [];}};
elements["visible-count"]={textContent:""};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:{available:true,broker:"tiger",counts:{},audit:{}}},trend_reviews:{}};
state.brokerFilter="tiger";
state.accountViews.tiger="simulate";
state.trendReportHistories.tiger={open:false,rows:[],scrollY:12};
const scrolls=[];
globalThis.window={scrollY:444,scrollTo(_x,y){scrolls.push(y);},location:{search:""}};
const pending={};
globalThis.fetch=(url)=>new Promise((resolve)=>{pending[url]=resolve;});
const renderPanel=renderAccountViewPanelOnly;
let panelRenders=0;
renderAccountViewPanelOnly=(broker)=>{panelRenders+=1;return renderPanel(broker);};
const firstRequest=loadHistoricalTrendReport("tiger","first.json");
const firstScroll=state.trendReportHistories.tiger.scrollY;
window.scrollY=555;
const secondRequest=loadHistoricalTrendReport("tiger","second.json");
const secondScroll=state.trendReportHistories.tiger.scrollY;
panelRenders=0;
scrolls.length=0;
pending["/api/trend-reports/tiger/history/first.json"]({ok:true,json:async()=>({artifact:"first.json"})});
await firstRequest;
const afterFirst={exact:{...state.trendHistoricalReports.tiger},panelRenders,scrolls:[...scrolls]};
panelRenders=0;
scrolls.length=0;
pending["/api/trend-reports/tiger/history/second.json"]({ok:true,json:async()=>({artifact:"second.json"})});
await secondRequest;
console.log(JSON.stringify({firstScroll,secondScroll,history:state.trendReportHistories.tiger,afterFirst,
  exact:state.trendHistoricalReports.tiger,panelRenders,scrolls}));
''')
    rendered = json.loads(output)
    assert rendered["firstScroll"] == 444
    assert rendered["secondScroll"] == 555
    assert rendered["history"]["open"] is True
    assert rendered["afterFirst"] == {
        "exact": {"artifact": "second.json", "loading": True},
        "panelRenders": 0,
        "scrolls": [],
    }
    assert rendered["exact"]["artifact"] == "second.json"
    assert rendered["exact"]["report"]["artifact"] == "second.json"
    assert rendered["panelRenders"] == 1
    assert rendered["scrolls"] == [555]


def test_dashboard_simulate_completion_does_not_render_after_view_switch() -> None:
    output = run_dashboard_js(r'''
const panel={innerHTML:"",setAttribute(){}};
elements["account-holdings"]={querySelector(){return panel;},querySelectorAll(){return [];}};
elements["visible-count"]={textContent:""};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:{available:true,broker:"tiger",counts:{},audit:{}}},
  trend_reviews:{tiger:{available:true,market_label:"美股",strategy_snapshot:{parameter_rows:[]},metrics:{}}}};
state.brokerFilter="tiger";
globalThis.window={location:{search:""}};
let resolveSimulate;
globalThis.fetch=()=>new Promise((resolve)=>{resolveSimulate=resolve;});
const renderPanel=renderAccountViewPanelOnly;
let panelRenders=0;
renderAccountViewPanelOnly=(broker)=>{panelRenders+=1;return renderPanel(broker);};
const results=[];
for(const [view,ok] of [["real",true],["review",false]]){
  state.accountViews.tiger="real";
  delete state.trendSimulatePositions.tiger;
  const request=setAccountView("tiger","simulate");
  await setAccountView("tiger",view);
  panelRenders=0;
  resolveSimulate({ok,status:500,json:async()=>({available:true,positions:[{symbol:"AAPL"}]})});
  await request;
  results.push({view:state.accountViews.tiger,payload:state.trendSimulatePositions.tiger,panelRenders});
}
console.log(JSON.stringify(results));
''')
    rendered = json.loads(output)
    assert rendered[0] == {
        "view": "real",
        "payload": {"available": True, "positions": [{"symbol": "AAPL"}]},
        "panelRenders": 0,
    }
    assert rendered[1]["view"] == "review"
    assert rendered[1]["payload"]["available"] is False
    assert rendered[1]["payload"]["positions"] == []
    assert rendered[1]["payload"]["error"] == "simulate positions 500"
    assert rendered[1]["panelRenders"] == 0


def test_dashboard_inactive_account_requests_do_not_render_or_restore_scroll() -> None:
    output = run_dashboard_js(r'''
const panel={innerHTML:"",setAttribute(){}};
elements["account-holdings"]={querySelector(){return panel;},querySelectorAll(){return [];}};
elements["visible-count"]={textContent:""};
state.dashboard={summary:{portfolio_value_hkd:"1000"},broker_summaries:[{broker:"tiger",portfolio_value_hkd:"1000"}],
  cash_rows:[],holdings:[],trend_reports:{tiger:{available:true,broker:"tiger",counts:{},audit:{}}},trend_reviews:{}};
state.brokerFilter="tiger";
state.accountViews.tiger="report";
const scrolls=[];
globalThis.window={scrollY:111,scrollTo(_x,y){scrolls.push(y);},location:{search:""}};
const pending=[];
globalThis.fetch=(url)=>new Promise((resolve)=>pending.push((payload)=>resolve({ok:true,json:async()=>payload})));
let fullRenders=0;
renderAccountHoldings=()=>{fullRenders+=1;};
const historyRequest=openTrendReportHistory("tiger");
state.brokerFilter="phillips";
pending.shift()([{available:true,artifact:"2026-07-16.json"}]);
await historyRequest;
state.brokerFilter="tiger";
delete state.trendReportHistories.tiger;
const exactRequest=loadHistoricalTrendReport("tiger","2026-07-16.json");
state.brokerFilter="phillips";
window.scrollY=999;
pending.shift()({available:true,broker:"tiger",counts:{},audit:{}});
await exactRequest;
console.log(JSON.stringify({fullRenders,scrolls,broker:state.brokerFilter}));
''')
    assert json.loads(output) == {
        "fullRenders": 0,
        "scrolls": [],
        "broker": "phillips",
    }


def test_dashboard_embedded_account_views_do_not_nest_main_landmarks() -> None:
    output = run_dashboard_js(r'''
const report={available:true,broker:"tiger",broker_label:"老虎",market:"US",market_label:"美股",counts:{},audit:{}};
const review={available:true,broker:"tiger",broker_label:"老虎",market_label:"美股",strategy_snapshot:{parameter_rows:[]},metrics:{}};
console.log(JSON.stringify({
  standaloneReport:renderTrendReportWorkspace(report),
  embeddedReport:renderTrendReportWorkspace(report,true),
  standaloneReview:renderTrendReviewWorkspace(review),
  embeddedReview:renderTrendReviewWorkspace(review,true),
}));
''')
    rendered = json.loads(output)
    assert rendered["standaloneReport"].startswith('<main class="cn-trend-report">')
    assert rendered["standaloneReview"].startswith('<main class="trend-review">')
    assert rendered["embeddedReport"].startswith('<div class="cn-trend-report">')
    assert rendered["embeddedReview"].startswith('<div class="trend-review">')
    assert "<main" not in rendered["embeddedReport"]
    assert "<main" not in rendered["embeddedReview"]


def test_dashboard_account_view_dom_at_375px() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    report = {
        "available": True, "broker": "tiger", "broker_label": "老虎",
        "market": "US", "market_label": "美股", "report_date": "2026-07-20",
        "data_date": "2026-07-17", "generated_at": "2026-07-18T09:00:00+08:00",
        "account_status": "已更新", "counts": {}, "audit": {},
        "artifact": "current.json", "report_sha256": "c" * 64,
        "strategy_version": "v-current",
    }
    review = {
        "available": True, "broker": "tiger", "broker_label": "老虎",
        "market": "US", "market_label": "美股",
        "strategy_snapshot": {"strategy_name": "美股趋势", "strategy_version": "v1", "parameter_rows": []},
        "metrics": {},
    }
    dashboard = {
        "summary": {"portfolio_value_hkd": "1000", "holding_value_hkd": "700", "cash_like_value_hkd": "300"},
        "broker_summaries": [
            {"broker": broker, "portfolio_value_hkd": "1000", "holding_value_hkd": "700", "cash_like_value_hkd": "300", "holding_count": "0",
             **({"cash_components": [{"label": "现金", "value_hkd": "300"}]} if broker == "tiger" else {})}
            for broker in ("futu", "tiger", "phillips", "eastmoney")
        ],
        "cash_rows": [], "holdings": [], "source_statuses": [], "poll_seconds": 0,
        "trend_reports": {
            "futu": {"available": False, "status_text": "今日暂无趋势报告"},
            "tiger": report,
            "phillips": {**report, "broker": "phillips", "broker_label": "辉立", "market": "HK", "market_label": "港股"},
            "eastmoney": {**report, "broker": "eastmoney", "broker_label": "东方财富", "market": "CN", "market_label": "A股"},
        },
        "trend_reviews": {
            "tiger": review,
            "phillips": {**review, "broker": "phillips", "market": "HK", "market_label": "港股"},
            "eastmoney": {**review, "broker": "eastmoney", "market": "CN", "market_label": "A股"},
        },
    }
    simulated = {
        "available": True, "broker": "tiger", "positions": [{
            "broker": "tiger", "market": "US", "symbol": "AAPL", "name": "Apple",
            "currency": "USD", "quantity": "2", "cost_price": "180", "last_price": "190",
            "market_value": "380", "market_value_hkd": "2964", "account_weight": "38%",
            "portfolio_weight": "38%", "unrealized_pnl_pct": "5.56%",
            "attribution_status": "linked",
            "report": {"artifact": "2026-07-16.json", "execution_date": "2026-07-20", "strategy_version": "v1", "report_sha256": "a" * 64},
        }],
    }
    bootstrap = f'''<script>
window.__requests=[];
window.__resolveSimulate=null;
const dashboardPayload={json.dumps(dashboard, ensure_ascii=False)};
const simulatedPayload={json.dumps(simulated, ensure_ascii=False)};
window.fetch=async (input)=>{{
  const url=String(input); window.__requests.push(url);
  if(url==="/api/trend-simulate-positions/tiger") return new Promise((resolve)=>{{
    window.__resolveSimulate=()=>resolve({{ok:true,status:200,json:async()=>structuredClone(simulatedPayload)}});
  }});
  const payload=url==="/api/dashboard"?dashboardPayload
    :url==="/api/quotes"?{{status:"ok",quotes:{{}},account_sync:{{status:"skipped"}}}}
    :url==="/api/trend-reports/tiger/history"?[{{available:true,artifact:"2026-07-16.json",execution_date:"2026-07-17",data_date:"2026-07-16",generated_at:"2026-07-18T09:30:00+08:00",strategy_version:"v1",execution_counts:{{sell:1,buy:2,hold:3,review:4}}}}]
    :url.endsWith("/2026-07-16.json")?{{...dashboardPayload.trend_reports.tiger,artifact:"2026-07-16.json",report_sha256:"{'a' * 64}",strategy_version:"v1",report_date:"2026-07-20",buy_actions:[{{symbol:"AAPL",execution:{{status:"missed"}}}}]}}
    :{{available:false}};
  return {{ok:true,status:200,json:async()=>structuredClone(payload)}};
}};
</script>'''
    page_html = html.replace(
        '<link rel="stylesheet" href="/static/dashboard.css">', f"<style>{css}</style>",
    ).replace(
        '<script src="/static/dashboard.js" defer></script>', f"{bootstrap}<script>{js}</script>",
    )
    errors: list[str] = []
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - local browser availability
            pytest.skip(f"Chrome is required for dashboard DOM checks: {exc}")
        page = browser.new_page(viewport={"width": 375, "height": 844})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(
            "http://dashboard.test/",
            lambda route: route.fulfill(status=200, content_type="text/html", body=page_html),
        )
        page.goto("http://dashboard.test/", wait_until="load")
        page.locator("#account-tab-tiger").click()
        assert errors == []
        section = page.locator("#account-tiger")
        dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        tabs = section.locator('[role="tab"][data-account-view]')
        tabs.first.wait_for(timeout=5000)

        original_label = tabs.first.inner_text()
        try:
            tabs.first.evaluate("node => { node.textContent = '错误标签'; }")
            with pytest.raises(AssertionError, match="Tab 顺序"):
                dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        finally:
            tabs.first.evaluate(
                "(node, label) => { node.textContent = label; }", original_label
            )

        tabs.nth(1).evaluate("node => { node.parentElement.prepend(node); }")
        try:
            with pytest.raises(AssertionError, match="Tab 顺序"):
                dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        finally:
            page.evaluate("renderAccountHoldings()")
            section = page.locator("#account-tiger")
            tabs = section.locator('[role="tab"][data-account-view]')

        tabs.first.evaluate("node => node.setAttribute('aria-selected', 'false')")
        tabs.nth(1).evaluate("node => node.setAttribute('aria-selected', 'true')")
        try:
            with pytest.raises(AssertionError, match="默认视图"):
                dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        finally:
            tabs.first.evaluate("node => node.setAttribute('aria-selected', 'true')")
            tabs.nth(1).evaluate("node => node.setAttribute('aria-selected', 'false')")

        original_tab_style = tabs.first.get_attribute("style")
        try:
            tabs.first.evaluate("node => { node.style.border = '1px solid red'; }")
            with pytest.raises(AssertionError, match="描边"):
                dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        finally:
            tabs.first.evaluate(
                "(node, style) => style === null ? node.removeAttribute('style') : node.setAttribute('style', style)",
                original_tab_style,
            )

        page.locator("head").evaluate("""head => {
          const style = document.createElement('style');
          style.id = 'acceptance-broken-tab-indicator';
          style.textContent = '[role="tab"][data-account-view][aria-selected="true"]::after { content: none !important; height: 0 !important; background: transparent !important; }';
          head.appendChild(style);
        }""")
        try:
            with pytest.raises(AssertionError, match="下划线"):
                dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        finally:
            page.locator("#acceptance-broken-tab-indicator").evaluate(
                "node => node.remove()"
            )

        original_document_style = page.locator("html").get_attribute("style")
        try:
            page.locator("html").evaluate(
                "node => { node.style.minWidth = '2000px'; }"
            )
            with pytest.raises(AssertionError, match="横向滚动"):
                dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        finally:
            page.locator("html").evaluate(
                "(node, style) => style === null ? node.removeAttribute('style') : node.setAttribute('style', style)",
                original_document_style,
            )

        dashboard_acceptance._check_account_view_contract(page, section, "tiger")
        assert [label.strip() for label in tabs.all_text_contents()] == [
            "真实持仓", "模拟盘持仓", "趋势报告", "美股复盘",
        ]
        assert all((tab.bounding_box() or {})["height"] >= 44 for tab in tabs.all())
        assert section.locator('[aria-selected="true"]').inner_text().strip() == "真实持仓"
        header = section.locator(".account-section-header")
        header.evaluate("node => { node.dataset.viewStable = 'yes'; }")
        tabs.first.focus()
        tabs.first.press("End")
        assert section.locator('[aria-selected="true"]').inner_text().strip() == "美股复盘"
        assert header.get_attribute("data-view-stable") == "yes"
        assert page.evaluate("document.activeElement.dataset.accountView") == "review"
        assert "卡玛比率" in section.inner_text()
        assert "夏普比率" in section.inner_text()
        assert page.locator(".workspace-grid").is_visible()
        section.locator('[data-account-view="simulate"]').click()
        section.get_by_text("模拟盘持仓加载中", exact=True).wait_for()
        assert header.get_attribute("data-view-stable") == "yes"
        assert page.evaluate("document.activeElement.dataset.accountView") == "simulate"
        page.evaluate("window.__resolveSimulate()")
        assert section.locator(".report-attribution-link").inner_text().strip() == "报告 2026-07-20 · v1"
        assert header.get_attribute("data-view-stable") == "yes"
        assert page.evaluate("document.activeElement.dataset.accountView") == "simulate"
        page.evaluate("renderHoldings()")
        assert page.evaluate("document.activeElement.dataset.accountView") == "simulate"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        section.locator(".report-attribution-link").click()
        return_current = section.locator("[data-current-trend-report]")
        return_current.wait_for()
        assert page.evaluate("window.__requests.at(-1)") == (
            "/api/trend-reports/tiger/history/2026-07-16.json"
        )
        dashboard_acceptance._check_loaded_report_identity(
            section, simulated["positions"][0]["report"], "tiger"
        )
        report_root = section.locator(".cn-trend-report")
        report_root.evaluate(
            "node => { node.dataset.reportArtifact = 'same-date-wrong.json'; }"
        )
        with pytest.raises(AssertionError, match="报告身份"):
            dashboard_acceptance._check_loaded_report_identity(
                section, simulated["positions"][0]["report"], "tiger"
            )
        report_root.evaluate(
            "node => { node.dataset.reportArtifact = '2026-07-16.json'; }"
        )
        section.locator(".cn-trend-report").evaluate(
            "node => { node.dataset.reportSha256 = 'wrong'; }"
        )
        with pytest.raises(AssertionError, match="报告身份"):
            dashboard_acceptance._check_loaded_report_identity(
                section, simulated["positions"][0]["report"], "tiger"
            )
        section.locator(".cn-trend-report").evaluate(
            f"node => {{ node.dataset.reportSha256 = '{'a' * 64}'; }}"
        )
        report_root.evaluate(
            "node => { node.dataset.strategyVersion = 'wrong-version'; }"
        )
        with pytest.raises(AssertionError, match="报告身份"):
            dashboard_acceptance._check_loaded_report_identity(
                section, simulated["positions"][0]["report"], "tiger"
            )
        report_root.evaluate(
            "node => { node.dataset.strategyVersion = 'v1'; }"
        )
        dashboard_acceptance._check_history_control_contract(
            return_current, "tiger 返回当前报告"
        )

        original_control_style = return_current.get_attribute("style")
        try:
            return_current.evaluate("node => { node.style.border = '1px solid red'; }")
            with pytest.raises(AssertionError, match="低强调"):
                dashboard_acceptance._check_history_control_contract(
                    return_current, "tiger 返回当前报告"
                )
        finally:
            return_current.evaluate(
                "(node, style) => style === null ? node.removeAttribute('style') : node.setAttribute('style', style)",
                original_control_style,
            )

        dashboard_styles = page.locator("head style").first
        try:
            dashboard_styles.evaluate("node => { node.sheet.disabled = true; }")
            assert return_current.evaluate(
                "node => getComputedStyle(node).backgroundColor"
            ) != "rgba(0, 0, 0, 0)"
            with pytest.raises(AssertionError, match="低强调"):
                dashboard_acceptance._check_history_control_contract(
                    return_current, "tiger 返回当前报告"
                )
        finally:
            dashboard_styles.evaluate("node => { node.sheet.disabled = false; }")
            page.wait_for_function(
                "getComputedStyle(document.querySelector('[data-current-trend-report]')).backgroundColor === 'rgba(0, 0, 0, 0)'",
                timeout=1000,
            )

        original_control_style = return_current.get_attribute("style")
        try:
            return_current.evaluate(
                "node => node.style.setProperty('font-weight', '700', 'important')"
            )
            with pytest.raises(AssertionError, match="低强调"):
                dashboard_acceptance._check_history_control_contract(
                    return_current, "tiger 返回当前报告"
                )
        finally:
            return_current.evaluate(
                "(node, style) => style === null ? node.removeAttribute('style') : node.setAttribute('style', style)",
                original_control_style,
            )

        dashboard_acceptance._check_history_control_contract(
            return_current, "tiger 返回当前报告"
        )
        assert "错过" in section.inner_text()
        return_current.click()
        history_button = section.locator("[data-report-history]")
        assert history_button.evaluate("node => node === document.activeElement")
        dashboard_acceptance._check_history_control_contract(
            history_button, "tiger 历史报告"
        )
        cash_details = section.locator(".account-cash-details")
        cash_details.evaluate("node => { node.open = true; node.dataset.historyStable = 'yes'; }")
        section.locator("[data-report-history]").click()
        history_row = section.locator('[data-history-artifact="2026-07-16.json"]')
        history_row.wait_for()
        for text in (
            "数据截至 2026-07-16",
            "生成时间 2026-07-18T09:30:00+08:00",
            "策略版本 v1",
            "执行摘要 卖出 1 · 买入 2 · 持有 3 · 复核 4",
        ):
            assert history_row.get_by_text(text, exact=True).count() == 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.evaluate(
            "window.__requests.filter(url => url === '/api/trend-reports/tiger/history').length",
        ) == 1
        assert cash_details.get_attribute("data-history-stable") == "yes"
        assert cash_details.evaluate("node => node.open") is True
        section.locator('[data-history-artifact="2026-07-16.json"]').click()
        section.locator("[data-current-trend-report]").wait_for()
        assert "错过" in section.inner_text()
        section.locator("[data-current-trend-report]").click()
        assert section.locator("[data-report-history]").evaluate(
            "node => node === document.activeElement"
        )
        assert cash_details.get_attribute("data-history-stable") == "yes"
        assert cash_details.evaluate("node => node.open") is True
        page.locator("#account-tab-futu").click()
        assert page.locator("#account-futu .trend-report-entry").count() == 1
        assert page.locator("#account-futu .account-view-tabs").count() == 0
        browser.close()
    assert errors == []


def test_dashboard_trend_review_is_compact_exact_and_account_scoped() -> None:
    output = run_dashboard_js(r'''
const review=(broker,brokerLabel,market,marketLabel)=>({
  available:true,broker,broker_label:brokerLabel,market,market_label:marketLabel,
  strategy_snapshot:{strategy_id:`trend/${market}/v1`,strategy_name:`${marketLabel}短线右侧趋势`,
    strategy_version:"v1",process_version:"abc1234",parameters:{position_limit:10},
    parameter_rows:[
      {group:"仓位执行",name:"持仓上限",value:"10 笔"},
      {group:"退出保护",name:"初始保护线",value:"成交均价减 2.0 倍 ATR14"},
    ]},
  metrics:{
    period_net_return:{discipline:{value:"12.6",reason:null},actual:{value:"9.4",reason:null},benchmark:{value:"7.8",reason:null}},
    market_excess_return:{discipline:{value:"4.8",reason:null},actual:{value:"1.6",reason:null},benchmark:{value:"0",reason:null}},
    max_drawdown:{discipline:{value:"-8.9",reason:null},actual:{value:"-10.2",reason:null},benchmark:{value:"-12.2",reason:null}},
    calmar:{discipline:{value:"1.42",reason:null},actual:{value:"0.92",reason:null},benchmark:{value:"0.64",reason:null}},
    sharpe:{discipline:{value:"1.07",reason:null},actual:{value:null,reason:"实际执行日终净值缺失"},benchmark:{value:"0.58",reason:null}},
  },
});
state.dashboard={trend_reports:{
  futu:{available:true,report_date:"2026-07-17",data_date:"2026-07-16"},
  tiger:{available:true,report_date:"2026-07-17",data_date:"2026-07-16"},
  phillips:{available:true,report_date:"2026-07-17",data_date:"2026-07-16"},
  eastmoney:{available:true,report_date:"2026-07-17",data_date:"2026-07-16"},
},trend_reviews:{
  tiger:review("tiger","老虎","US","美股"),
  phillips:review("phillips","辉立","HK","港股"),
  eastmoney:review("eastmoney","东方财富","CN","A股"),
}};
const group=(broker)=>({broker,profile:ACCOUNT_STRATEGY_PROFILES[broker],rows:[],summary:{broker,display_name:broker,portfolio_value_hkd:"1000",holding_value_hkd:"700",cash_like_value_hkd:"300",holding_count:"1"}});
for (const [broker,label] of [["tiger","美股复盘"],["phillips","港股复盘"],["eastmoney","A股复盘"]]) {
  const account=renderAccountSection(group(broker));
  if (!account.includes(`data-account-broker="${broker}" data-account-view="review"`) || !account.includes(label)) throw new Error(account);
  if (account.indexOf("趋势报告") > account.indexOf(label)) throw new Error("entry order");
}
if (renderAccountSection(group("futu")).includes("复盘")) throw new Error("futu review");
const html=renderTrendReviewWorkspace(state.dashboard.trend_reviews.eastmoney);
for (const text of ["东方财富｜A股","A股趋势复盘","A股短线右侧趋势","版本 v1","当前策略参数",
  "仓位执行","持仓上限","10 笔","退出保护","初始保护线","成交均价减 2.0 倍 ATR14",
  "收益与回撤","期间净收益率","相对市场超额收益","最大回撤",
  "风险调整收益","卡玛比率","夏普比率","纪律模拟","实际执行","市场基准",
  "12.6%","1.42","实际执行日终净值缺失"]) {
  if (!html.includes(text)) throw new Error(text+"\n"+html);
}
if ((html.match(/class="trend-review-chart"/g)||[]).length!==2) throw new Error(html);
for (const forbidden of ["复盘结论","Connected","创建回测","导出参数","Alpha","Beta","Sortino","胜率","盈亏比"]) {
  if (html.includes(forbidden)) throw new Error(forbidden+"\n"+html);
}
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_renders_action_first_trend_report_for_every_market() -> None:
    output = run_dashboard_js(r'''
const cn = renderTrendReportWorkspace({
  available:true,market:"CN",broker:"eastmoney",broker_label:"东方财富",
  market_label:"A股",report_date:"2026-07-16",data_date:"2026-07-15",
  generated_at:"2026-07-15T20:00:00+08:00",account_status:"已更新",
  buy_window:"09:30–10:00",counts:{sell:1,buy:1,hold:1,review:2},
  sell_actions:[{symbol:"601398",name:"工商银行",close:"7.2",
    temperature_prev:"温",temperature_curr:"温",strength:"91.3",
    reason:"left_trend_right_side",active_line:"5.457142857142857142857142857",
    entry_hints:["强度 91.3，低于入场线 95"]}],
  buy_actions:[{symbol:"688046",name:"药康生物",filter_price:"29.14",
    close:"28.81",temperature_prev:"温",temperature_curr:"热",phase:"立夏",
    strength:"99.9",industry:"医疗服务",industry_temperature:"热",
    market_cap:"110",amount:"6",target_weight:"0.04",
    target_amount:"27061.98",estimated_shares:900,
    estimated_initial_line:"24.54571428571428571428571429"}],
  hold_actions:[{symbol:"600900",name:"长江电力",close:"28.0",
    temperature_prev:"热",temperature_curr:"热",strength:"98.7",
    reason:"trend_intact",active_line:"27.52714285714285714285714286",
    entry_hints:["不是新的温转热或温转沸入场信号"]}],
  review_actions:[
    {symbol:"600036",name:"招商银行",close:"45.2",temperature_prev:"热",
     temperature_curr:"热",strength:"97",reason:"holding_kline_unavailable",
     active_line:"42.0",entry_hints:["筛选价数据不可用"]},
    {symbol:"600519",name:"贵州茅台",close:"1498",temperature_prev:"热",
     temperature_curr:"-",strength:"98",reason:"holding_signal_unknown",
     active_line:"1450",entry_hints:["行业温度数据不可用"]},
  ],audit:{candidates:[
    {symbol:"AUDIT-ONLY",name:"仅审计",eligible:false,rank:null,
     excluded_reasons:["strength_below_95"],filter_price:"9.8",close:"9.7",
     temperature_prev:"温",temperature_curr:"温",phase:"立夏",strength:"94",
     industry:"银行",industry_temperature:"热",market_cap:"120",amount:"3",atr:"0.4",danger:false},
  ],excluded:{"AUDIT-ONLY":["strength_below_95"]},industry_concentration:[],
    data_sources:["Trend Animals","Futu CN calendar/QFQ daily K-line"]},
});
for (const text of ["优先处理 · 卖出触发","09:30–10:00 · 正式买入计划",
  "需要确认 · 人工复核","盘中持续 · 已有持仓","筛选价（Trend Animals）","执行参考价（Futu 前复权）",
  "温 → 热","目标仓位 4%","全部卖出","正式买入","继续持有",
  "人工复核","600036","600519","日线数据不可用","筛选价数据不可用",
  "趋势信号不完整","行业温度数据不可用","买入纪律","卖出纪律","审计详情"]) {
  if (!cn.includes(text)) throw new Error(text + "\n" + cn);
}
const stageOrder=["优先处理 · 卖出触发","需要确认 · 人工复核",
  "09:30–10:00 · 正式买入计划","盘中持续 · 已有持仓"].map((text)=>cn.indexOf(`<h2>${text}</h2>`));
if(stageOrder.some((index)=>index<0)||!stageOrder.every((index,i)=>i===0||stageOrder[i-1]<index))throw new Error(cn);
if (!cn.includes('class="cn-trend-report"') ||
    !cn.includes('class="cn-trend-table"') ||
    !cn.includes('class="cn-trend-card"')) throw new Error(cn);
if ((cn.match(/<details class="trend-discipline" open>/g) || []).length !== 2) throw new Error(cn);
for (const price of ["5.46", "24.55", "27.53"]) {
  if (!cn.includes(`>${price}</td>`)) throw new Error(cn);
}
for (const raw of ["5.457142857142857142857142857", "24.54571428571428571428571429", "27.52714285714285714285714286"]) {
  if (cn.includes(raw)) throw new Error(cn);
}
const actionContent = cn.split('<details class="trend-audit"', 1)[0];
if (actionContent.includes("AUDIT-ONLY") || !cn.includes("AUDIT-ONLY")) throw new Error(cn);

const us = renderTrendReportWorkspace({
  market:"US",broker_label:"富途",market_label:"美股",
  report_date:"2026-07-16",data_date:"2026-07-15",generated_at:"now",
  account_status:"已更新",buy_window:"美股常规交易时段",
  counts:{sell:0,buy:1,hold:0,review:1},sell_actions:[],hold_actions:[],
  buy_actions:[{symbol:"EA",name:"艺电",close:"207.27",strength:"99.8",
    industry:"通讯服务",target_weight:"0.04",target_amount:"4941.49",
    estimated_shares:23,estimated_initial_line:"205.46930",
    execution:{status:"partially_filled",filled_qty:"13",target_qty:"23",
      avg_fill_price:"207.18",order_ids:["SIM-123"],
      updated_at:"2026-07-17T10:01:00-04:00",reason:""}}],
  review_actions:[{symbol:"BOTZ",name:"Global X Robotics ETF",
    reason:"holding_signal_unknown",close:null,strength:null,active_line:null}],
  audit:{account_exceptions:["现金类资产不参与趋势判断"]},
});
for (const text of ["优先处理 · 卖出触发","需要确认 · 人工复核",
  "美股常规交易时段 · 正式买入计划","盘中持续 · 已有持仓",
  "正式买入 1","全部卖出 0","继续持有 0","人工复核 1",
  "EA 艺电","207.27","99.8","通讯服务","4%","4,941.49","23 股",
  "205.47","BOTZ Global X Robotics ETF","趋势信号不完整",
  "部分成交","成交 13 / 23","均价 207.18","订单 SIM-123","2026-07-17T10:01:00-04:00",
  "账户不参与项","现金类资产不参与趋势判断","审计详情"]) {
  if (!us.includes(text)) throw new Error(text + "\n" + us);
}
const usOrder=["优先处理 · 卖出触发","需要确认 · 人工复核",
  "美股常规交易时段 · 正式买入计划","盘中持续 · 已有持仓"]
  .map((text)=>us.indexOf(`<h2>${text}</h2>`));
if (usOrder.some((index)=>index<0) ||
    !usOrder.every((index,i)=>i===0||usOrder[i-1]<index)) throw new Error(us);
if (!us.includes('class="cn-trend-report"') ||
    (us.match(/class="cn-trend-table"/g) || []).length !== 4 ||
    !us.includes('class="cn-trend-card"') ||
    us.includes("今日执行检查") || us.includes("筛选价（Trend Animals）") ||
    us.includes('class="trend-discipline"')) throw new Error(us);
if (!us.includes('class="cn-trend-execution"') ||
    us.includes("执行详情按钮") || us.includes("执行状态卡片")) throw new Error(us);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_renders_frozen_risk_summary_and_candidate_detail_rows() -> None:
    output = run_dashboard_js(r'''
const html = renderTrendReportWorkspace({
  available:true,market:"CN",broker:"eastmoney",broker_label:"富途模拟",market_label:"A股",
  report_date:"2026-07-16",data_date:"2026-07-15",generated_at:"now",
  account_status:"已更新",buy_window:"09:30–10:00",
  counts:{sell:1,buy:1,hold:0,review:0},
  risk_summary:{status:"active",status_label:"风险预算内",
    portfolio_planned_risk:"303",portfolio_planned_risk_pct:"0.00303",
    portfolio_risk_limit:"4000",portfolio_risk_limit_pct:"0.04",
    portfolio_remaining_risk:"3697",portfolio_remaining_risk_pct:"0.03697",
    single_entry_risk_limit:"400",single_entry_risk_limit_pct:"0.004",
    abnormal_loss_buffer:"1000",abnormal_loss_buffer_pct:"0.01",
    total_risk_budget_target_pct:"0.05",
    kelly_phase:"active_all_samples",kelly_eligible_sample_count:30,
    kelly_selected_sample_count:30,kelly_cap:"0.012626",kelly_reason:"",
    kelly_source:"合格的富途模拟闭环；实盘结果不参与计算",
    disclaimer:"5% 是风险预算目标，不是最大损失保证。",
    portfolio_remaining_risk_note:"组合剩余风险供本报告后续新仓共享，不等于单标的仓位上限。"},
  drawdown_summary:{status:"paused",status_label:"暂停新开仓",
    current_equity:"95000",high_water_mark:"100000",drawdown_pct:"0.05",
    drawdown_limit_pct:"0.05",pause_reason:"策略累计回撤已达到 5%，需人工解锁",
    bootstrap_event:{event_id:"automatic-bootstrap-audit",event_type:"automatic_bootstrap",
      baseline_equity:"100000",source_date:"2026-07-14",accepted_git_sha:"abc123",
      parameter_hash:"params456",actor:"acceptance",reason:"first_activation",
      occurred_at:"2026-07-16T08:00:00+08:00",entry_eligible_from:"2026-07-15"},
    recovery_event:{event_id:"snapshot-recovery-audit",event_type:"snapshot_recovery",
      snapshot:"state-snapshot.json",state_sha256:"statehash789",actor:"acceptance",
      occurred_at:"2026-07-16T08:30:00+08:00"}},
  sell_actions:[{symbol:"600000",name:"浦发银行",reason:"danger_signal"}],
  buy_actions:[{symbol:"600001",name:"测试",filter_price:"10",close:"10",
    temperature_prev:"温",temperature_curr:"热",phase:"立夏",strength:"96",
    industry:"电力",industry_temperature:"热",market_cap:"100",amount:"2",
    target_weight:"0.04",target_amount:"4000",estimated_shares:300,
    estimated_initial_line:"9",planned_stop_risk:"303",
    planned_stop_risk_pct:"0.00303",normal_cost:"3",
    decisive_constraint:"单笔风险上限"}],
  risk_skips:[{symbol:"600002",name:"第二候选",filter_price:"10",close:"10",
    temperature_prev:"温",temperature_curr:"热",phase:"立夏",strength:"96",
    industry:"电力",industry_temperature:"热",market_cap:"100",amount:"2",
    target_weight:"0.04",target_amount:"4000",estimated_shares:0,
    reason:"最小交易单位 100 股超过组合剩余风险",
    decisive_constraint:"组合剩余风险"}],
  hold_actions:[],review_actions:[],audit:{},
});
for (const text of ["组合计划风险","风险预算内",
  "Kelly 阶段","全样本启用 · 30 个合格模拟闭环","当前 Kelly 上限","1.26%",
  "合格的富途模拟闭环；实盘结果不参与计算",
  "策略累计回撤",
  "暂停新开仓","策略累计回撤已达到 5%，需人工解锁",
  "基准已自动建立","基准净值 100,000","快照日期 2026-07-14",
  "automatic-bootstrap-audit","abc123","params456","acceptance",
  "状态恢复审计详情","snapshot-recovery-audit","state-snapshot.json","statehash789",
  "组合剩余风险","单笔风险上限","异常损失缓冲","不得用于开仓",
  "5% 是风险预算目标，不是最大损失保证。","目标仓位（占净值）",
  "组合剩余风险供本报告后续新仓共享，不等于单标的仓位上限。",
  "允许 · 建议 300 股","计划止损风险 303","正常成本 3",
  "决定性约束 单笔风险上限","跳过 · 建议 0 股","第二候选",
  "最小交易单位 100 股超过组合剩余风险"]) {
  if (!html.includes(text)) throw new Error(text + "\n" + html);
}
if (html.includes("本次可用风险") || html.includes("<th scope=\"col\">目标仓位</th>")) {
  throw new Error(html);
}
if (html.includes("组合正常计划风险")) throw new Error(html);
const counts = html.indexOf('class="trend-report-metrics cn-trend-counts"');
const risk = html.indexOf('class="trend-risk-summary"');
const sell = html.indexOf("优先处理 · 卖出触发");
if (!(counts >= 0 && counts < risk && risk < sell)) throw new Error(html);
if ((html.match(/class="cn-trend-card"/g) || []).length < 3 ||
    (html.match(/class="cn-trend-execution cn-trend-risk-detail"/g) || []).length !== 2) {
  throw new Error(html);
}
const historical = renderTrendRiskSummary(null, {
  status:"active",status_label:"纪律内",current_equity:"100000",
  high_water_mark:"100000",drawdown_pct:"0",drawdown_limit_pct:"0.05",
  bootstrap_event:{event_id:"automatic-bootstrap-audit",event_type:"automatic_bootstrap",
    baseline_equity:"100000",source_date:"2026-07-14",accepted_git_sha:"abc123",
    parameter_hash:"params456",actor:"acceptance",
    occurred_at:"2026-07-16T08:00:00+08:00",entry_eligible_from:"2026-07-15"}
}, null, "2026-07-17");
if (historical.includes("基准已自动建立") ||
    !historical.includes("回撤基准审计详情") ||
    !historical.includes("100,000") ||
    !historical.includes("2026-07-14")) throw new Error(historical);
const drawdownOnly = renderTrendRiskSummary(null, {
  status:"paused",status_label:"暂停新开仓",drawdown_pct:null,
  drawdown_limit_pct:"0.05",current_equity:"95000",high_water_mark:null,
  pause_reason:"策略累计回撤状态缺失，暂停新开仓"
});
if (!drawdownOnly.includes("策略累计回撤") ||
    !drawdownOnly.includes('data-risk-status="paused"')) throw new Error(drawdownOnly);
console.log("ok");
''')
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    mobile = css.split("@media (max-width: 760px) {", 1)[1]

    assert "ok" in output
    assert ".trend-risk-summary" in css
    assert ".trend-risk-summary" in mobile
    assert ".cn-trend-buy {\n    overflow-x: hidden;\n  }" in mobile


def test_dashboard_formats_bootstrap_audit_equity_without_touching_identity() -> None:
    output = run_dashboard_js(r'''
const html = renderTrendRiskSummary(null, {
  status:"active",status_label:"纪律内",current_equity:"995953.447",
  high_water_mark:"1000000",drawdown_pct:"0.004046553",
  drawdown_limit_pct:"0.05",
  bootstrap_event:{event_id:"audit-00001234",event_type:"automatic_bootstrap",
    baseline_equity:"995953.447",source_date:"2026-07-21",accepted_git_sha:"abc123",
    parameter_hash:"params456",actor:"acceptance",
    occurred_at:"2026-07-22T08:00:00+08:00",entry_eligible_from:"2026-07-23"}
}, null, "2026-07-22");
console.log(JSON.stringify(html));
''')
    rendered = json.loads(output)

    assert "基准净值 995,953.45" in rendered
    assert "995953.447" not in rendered
    assert "audit-00001234" in rendered
    assert "2026-07-21" in rendered


def test_dashboard_renders_api_trade_stats_inside_risk_summary() -> None:
    output = run_dashboard_js(r'''
const base={status:"active",status_label:"风险预算内",
  portfolio_planned_risk:"303",portfolio_planned_risk_pct:"0.00303",
  portfolio_risk_limit_pct:"0.04",portfolio_remaining_risk:"3697",
  portfolio_remaining_risk_pct:"0.03697",single_entry_risk_limit:"400",
  single_entry_risk_limit_pct:"0.004",abnormal_loss_buffer:"1000",
  abnormal_loss_buffer_pct:"0.01",disclaimer:"风险提示",portfolio_remaining_risk_note:"说明"};
const available=renderTrendRiskSummary({...base,trade_stats:{available:true,
  statistics_cutoff_at:"2026-07-20T11:59:59+08:00",
  actual_broker_label:"东方财富",
  simulation:{win_rate:"0.5",payoff_ratio:"1.25",payoff_ratio_status:"available",eligible_sample_count:4},
  actual:{win_rate:null,payoff_ratio:null,payoff_ratio_status:"no_wins",eligible_sample_count:0}}});
for (const text of ["富途模拟盘交易统计","胜率 50% · 盈亏比 1.25 · 样本 4",
  "东方财富实盘交易统计","胜率 — · 盈亏比 无盈利样本 · 样本 0",
  "统计截至 2026-07-20T11:59:59+08:00"]) {
  if (!available.includes(text)) throw new Error(text + "\n" + available);
}
const unavailable=renderTrendRiskSummary({...base,trade_stats:{available:false,status_text:"交易统计暂不可用"}});
if (!unavailable.includes("交易统计暂不可用")) throw new Error(unavailable);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_renders_read_only_actual_execution_overlay() -> None:
    output = run_dashboard_js(r'''
const html = renderTrendReportWorkspace({
  available:true,market:"CN",broker:"eastmoney",broker_label:"东方财富",market_label:"A股",
  report_date:"2026-07-16",data_date:"2026-07-15",generated_at:"now",
  account_status:"已更新",buy_window:"09:30–10:00",counts:{},
  risk_summary:{status:"active",status_label:"风险预算内",
    portfolio_planned_risk:"303",portfolio_planned_risk_pct:"0.00303",
    portfolio_risk_limit_pct:"0.04",portfolio_remaining_risk:"3697",
    portfolio_remaining_risk_pct:"0.03697",single_entry_risk_limit:"400",
    single_entry_risk_limit_pct:"0.004",abnormal_loss_buffer:"1000",
    abnormal_loss_buffer_pct:"0.01",disclaimer:"风险提示",portfolio_remaining_risk_note:"说明"},
  actual_overlay:{available:true,broker_label:"东方财富",account_nav_hkd:"108000.00",
    status_text:"结单数据，非实时",
    notice:"只读执行辅助；实盘变化不会改写模拟建议、Kelly、模拟统计或报告哈希；系统不会自动交易真实账户。",
    items:[
      {symbol:"600001",name:"测试",frozen_action_label:"正式买入",target_weight:"0.04",
       simulation_quantity:"300",actual_reference_quantity:"400",actual_quantity:"200",
       actual_market_value:"2000",currency:"CNY",deviation:"underbought",deviation_label:"少买",frozen_reference_price:"10",protection_line:"9",
       risk_note:"若按策略保护线退出，预计损失 CNY 200.00（按冻结参考价估算，不代表实时风险上限）"},
      {symbol:"600002",name:"跳过",frozen_action_label:"正式买入",target_weight:"0.04",
       simulation_quantity:"300",actual_reference_quantity:"400",actual_quantity:"0",
       actual_market_value:"0",currency:"CNY",deviation:"skipped",deviation_label:"跳过",protection_line:"9",
       risk_note:"暂无策略保护线，风险未纳入估算"},
      {symbol:"600003",name:"待卖",frozen_action_label:"全部卖出",target_weight:"",
       simulation_quantity:"",actual_reference_quantity:"0",actual_quantity:"50",
       actual_market_value:"1000",currency:"CNY",deviation:"missed_sell",deviation_label:"漏卖",protection_line:"18",
       risk_note:"若按策略保护线退出，预计损失 CNY 100.00"},
      {symbol:"600004",name:"追买",frozen_action_label:"跳过",target_weight:"0.04",
       simulation_quantity:"0",actual_reference_quantity:"0",actual_quantity:"100",
       actual_market_value:"1000",currency:"CNY",deviation:"chased",deviation_label:"追买",protection_line:"",
       risk_note:"暂无策略保护线，风险未纳入估算"},
      {symbol:"600005",name:"超买",frozen_action_label:"正式买入",target_weight:"0.04",
       simulation_quantity:"300",actual_reference_quantity:"400",actual_quantity:"500",
       actual_market_value:"5000",currency:"CNY",deviation:"overbought",deviation_label:"超买",protection_line:"9",
       risk_note:"若按策略保护线退出，预计损失 CNY 500.00"}
    ],
    outside_positions:[{symbol:"600099",name:"报告外",actual_quantity:"10",
      actual_market_value:"500",currency:"CNY",deviation:"outside_report_addition",deviation_label:"报告外加仓",
      attribution_status:"unconfirmed",risk_note:"风险未纳入估算"}]},
  sell_actions:[],buy_actions:[],risk_skips:[],hold_actions:[],review_actions:[],audit:{},
});
for (const text of ["实盘执行辅助","东方财富","偏差 6","真实账户净值 HKD 108,000",
  "结单数据，非实时","模拟数量 300","实盘参考数量 400","真实持仓 200",
  "冻结参考价 CNY 10","按冻结参考价估算，不代表实时风险上限",
  "少买","跳过","漏卖","追买","超买","报告外加仓",
  "若按策略保护线退出，预计损失 CNY 200.00","风险未纳入估算",
  "不会改写模拟建议、Kelly、模拟统计或报告哈希","不会自动交易真实账户"]) {
  if (!html.includes(text)) throw new Error(text + "\n" + html);
}
for (const forbidden of ["真实最大风险","券商端已挂止损","已挂止损"]) {
  if (html.includes(forbidden)) throw new Error(forbidden + "\n" + html);
}
if ((html.match(/class="trend-actual-row"/g) || []).length !== 6 ||
    !html.includes('class="trend-actual-overlay" open')) throw new Error(html);
const followed = renderTrendActualOverlay({available:true,broker_label:"老虎",status_text:"账户实时同步",account_nav_hkd:"100",
  notice:"只读",items:[{symbol:"AAPL",name:"Apple",deviation:"followed",deviation_label:"已跟随",
    frozen_action_label:"继续持有",actual_quantity:"1",actual_market_value:"10",currency:"USD",
    risk_note:"若按策略保护线退出，预计损失 USD 1.00"}],outside_positions:[]});
if (!followed.includes("偏差 0") || followed.includes('class="trend-actual-overlay" open') ||
    !followed.includes('data-deviation="followed"')) throw new Error(followed);
const missingFx = renderTrendActualOverlay({available:true,broker_label:"老虎",status_text:"账户实时同步",account_nav_hkd:"780000",
  notice:"只读",items:[{symbol:"AAPL",name:"Apple",deviation:"reference_unavailable",deviation_label:"暂无法换算",
    frozen_action_label:"正式买入",actual_reference_quantity:"",actual_quantity:"1",actual_market_value:"",currency:"USD",
    reference_note:"实盘汇率缺失，暂无法换算",risk_note:"暂无策略保护线，风险未纳入估算"}],outside_positions:[]});
if (!missingFx.includes("实盘汇率缺失，暂无法换算")) throw new Error(missingFx);
console.log("ok");
''')

    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert "ok" in output
    assert '.trend-actual-row header span[data-deviation="followed"]' in css
    assert 'color: var(--ok);' in css
    assert '.trend-actual-row header span[data-deviation="overbought"]' in css


def test_dashboard_risk_summary_and_candidate_cards_fit_375px() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    rendered = json.loads(run_dashboard_js(r'''
console.log(JSON.stringify(renderTrendReportWorkspace({
  available:true,market:"CN",broker:"eastmoney",broker_label:"富途模拟",market_label:"A股",
  report_date:"2026-07-16",data_date:"2026-07-15",generated_at:"now",
  account_status:"已更新",buy_window:"09:30–10:00",counts:{sell:0,buy:1,hold:0,review:0},
  risk_summary:{status:"active",status_label:"风险预算内",
    portfolio_planned_risk:"303",portfolio_planned_risk_pct:"0.00303",
    portfolio_risk_limit_pct:"0.04",portfolio_remaining_risk:"3697",
    portfolio_remaining_risk_pct:"0.03697",single_entry_risk_limit:"400",
    single_entry_risk_limit_pct:"0.004",abnormal_loss_buffer:"1000",
    abnormal_loss_buffer_pct:"0.01",disclaimer:"5% 是风险预算目标，不是最大损失保证。",
    portfolio_remaining_risk_note:"组合剩余风险供本报告后续新仓共享，不等于单标的仓位上限。",
    trade_stats:{available:true,statistics_cutoff_at:"2026-07-20T11:59:59+08:00",
      actual_broker_label:"东方财富",
      simulation:{win_rate:"0.5",payoff_ratio:"1.25",payoff_ratio_status:"available",eligible_sample_count:4},
      actual:{win_rate:null,payoff_ratio:null,payoff_ratio_status:"no_wins",eligible_sample_count:0}}},
  actual_overlay:{available:true,broker_label:"东方财富",account_nav_hkd:"108000",
    status_text:"结单数据，非实时",notice:"只读执行辅助；系统不会自动交易真实账户。",
    items:[{symbol:"600001",name:"一个名称很长但仍然必须在三百七十五像素宽度内换行的标的",
      frozen_action_label:"正式买入",target_weight:"0.04",simulation_quantity:"300",
      actual_reference_quantity:"400",actual_quantity:"200",actual_market_value:"2000",
      currency:"CNY",deviation:"underbought",deviation_label:"少买",frozen_reference_price:"10",protection_line:"9",
      risk_note:"若按策略保护线退出，预计损失 CNY 200.00（按冻结参考价估算，不代表实时风险上限）"}],
    outside_positions:[{symbol:"600099",name:"报告外加仓",actual_quantity:"10",
      actual_market_value:"500",currency:"CNY",deviation_label:"报告外加仓",
      deviation:"outside_report_addition",
      risk_note:"风险未纳入估算"}]},
  sell_actions:[],buy_actions:[{symbol:"600001",name:"测试",filter_price:"10",close:"10",
    temperature_prev:"温",temperature_curr:"热",phase:"立夏",strength:"96",industry:"电力",
    industry_temperature:"热",market_cap:"100",amount:"2",target_weight:"0.04",
    target_amount:"4000",estimated_shares:300,estimated_initial_line:"9",
    planned_stop_risk:"303",planned_stop_risk_pct:"0.00303",normal_cost:"3",
    decisive_constraint:"单笔风险上限"}],
  risk_skips:[{symbol:"600002",name:"第二候选",close:"10",target_weight:"0.04",
    target_amount:"4000",estimated_shares:0,reason:"最小交易单位 100 股超过组合剩余风险",
    decisive_constraint:"组合剩余风险"}],hold_actions:[],review_actions:[],audit:{},
})));
'''))
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    errors: list[str] = []
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - local browser availability
            pytest.skip(f"Chrome is required for dashboard DOM checks: {exc}")
        page = browser.new_page(viewport={"width": 375, "height": 844})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(f"<style>{css}</style>{rendered}")

        assert errors == []
        risk_text = page.locator(".trend-risk-summary").inner_text()
        assert "组合计划风险" in risk_text
        assert "风险预算内" in risk_text
        assert "富途模拟盘交易统计" in risk_text
        assert "东方财富实盘交易统计" in risk_text
        assert "实盘执行辅助" in risk_text
        assert "冻结参考价 CNY 10" in risk_text
        assert page.locator(".trend-actual-overlay").get_attribute("open") is not None
        assert page.locator(".cn-trend-card").count() == 2
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        assert page.locator(".cn-trend-buy").evaluate(
            "node => node.scrollWidth <= node.clientWidth"
        )
        assert page.locator(".trend-actual-overlay").evaluate(
            "node => node.scrollWidth <= node.clientWidth"
        )
        assert page.locator(".trend-actual-row").evaluate_all(
            "nodes => nodes.every(node => node.scrollWidth <= node.clientWidth)"
        )
        browser.close()


def test_dashboard_cn_trend_report_escapes_every_rendered_fact() -> None:
    output = run_dashboard_js(r'''
const attack='<img src=x onerror=alert(1)>';
const html=renderTrendReportWorkspace({
  market:"CN",broker_label:attack,market_label:attack,report_date:attack,
  data_date:attack,generated_at:attack,account_status:attack,buy_window:attack,
  counts:{sell:attack},sell_actions:[{symbol:attack,name:attack,close:attack,
    temperature_prev:attack,temperature_curr:attack,strength:attack,
    reason:"unknown",active_line:attack,entry_hints:[attack]}],
  buy_actions:[{symbol:attack,name:attack,filter_price:attack,close:attack,
    temperature_prev:attack,temperature_curr:attack,phase:attack,strength:attack,
    industry:attack,industry_temperature:attack,market_cap:attack,amount:attack,
    target_weight:attack,target_amount:attack,estimated_shares:attack,
    estimated_initial_line:attack}],
  hold_actions:[],review_actions:[{symbol:attack,name:attack,close:attack,
    temperature_prev:attack,temperature_curr:attack,strength:attack,
    reason:"holding_kline_unavailable",active_line:attack,entry_hints:[attack]}],
  actual_overlay:{available:true,broker_label:attack,account_nav_hkd:attack,
    status_text:attack,notice:attack,items:[{symbol:attack,name:attack,
      frozen_action_label:attack,target_weight:attack,simulation_quantity:attack,
      actual_reference_quantity:attack,actual_quantity:attack,actual_market_value:attack,
      currency:attack,deviation_label:attack,frozen_reference_price:attack,
      protection_line:attack,risk_note:attack}],outside_positions:[]},
  audit:{candidates:[{symbol:attack,name:attack,
    excluded_reasons:[attack],filter_price:attack,close:attack}],excluded:{[attack]:[attack]},
    industry_concentration:[[attack]],data_sources:[attack],actual_api_cost:attack},
});
if (html.includes(attack) || !html.includes("&lt;img") ||
    !html.includes('class="cn-trend-report"') ||
    !html.includes("筛选价（Trend Animals）") ||
    !html.includes("执行参考价（Futu 前复权）")) throw new Error(html);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_cn_disciplines_default_closed_only_on_mobile() -> None:
    output = run_dashboard_js(r'''
const report={market:"CN",counts:{},sell_actions:[],buy_actions:[],hold_actions:[],audit:{}};
const deterministic=renderTrendReportWorkspace(report);
if ((deterministic.match(/<details class="trend-discipline" open>/g) || []).length !== 2) {
  throw new Error(deterministic);
}
window={matchMedia:(query)=>({matches:query==="(max-width: 760px)"})};
const mobile=renderTrendReportWorkspace(report);
if (mobile.includes('<details class="trend-discipline" open>') ||
    (mobile.match(/<details class="trend-discipline">/g) || []).length !== 2) {
  throw new Error(mobile);
}
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_cn_buy_scroller_is_keyboard_reachable_only_on_desktop() -> None:
    output = run_dashboard_js(r'''
const report={market:"CN",counts:{},sell_actions:[],buy_actions:[],hold_actions:[],audit:{}};
const desktop=renderTrendReportWorkspace(report);
if (!desktop.includes('class="trend-stage cn-trend-stage cn-trend-buy" tabindex="0" aria-label="正式买入计划，可横向滚动"')) {
  throw new Error(desktop);
}
window={matchMedia:(query)=>({matches:query==="(max-width: 760px)"})};
const mobile=renderTrendReportWorkspace(report);
if (!mobile.includes('class="trend-stage cn-trend-stage cn-trend-buy" tabindex="-1" aria-label="正式买入计划"')) {
  throw new Error(mobile);
}
console.log("ok");
''')
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert "ok" in output
    assert "\n.cn-trend-buy:focus {" in css
    focus = css.split("\n.cn-trend-buy:focus {", 1)[1].split("}", 1)[0]
    assert "outline: 3px solid var(--accent);" in focus
    assert "outline-offset: 2px;" in focus


def test_dashboard_cn_buy_scroller_semantics_sync_across_breakpoint_changes() -> None:
    output = run_dashboard_js(r'''
const attributes={};
const stage={tabIndex:99,setAttribute(name,value){attributes[name]=value;}};
elements["trend-report-workspace"]={querySelector(selector){
  if (selector !== ".cn-trend-buy") throw new Error("unknown selector " + selector);
  return stage;
}};
window={matchMedia(){return {matches:true};}};
syncCnTrendBuyAccessibility();
const mobile={tabIndex:stage.tabIndex,label:attributes["aria-label"]};
window={matchMedia(){return {matches:false};}};
syncCnTrendBuyAccessibility();
const desktop={tabIndex:stage.tabIndex,label:attributes["aria-label"]};
console.log(JSON.stringify({mobile,desktop}));
''')

    assert json.loads(output) == {
        "mobile": {"tabIndex": -1, "label": "正式买入计划"},
        "desktop": {"tabIndex": 0, "label": "正式买入计划，可横向滚动"},
    }


def test_dashboard_cn_empty_stages_keep_tables_and_price_source_labels() -> None:
    output = run_dashboard_js(r'''
const html=renderTrendReportWorkspace({
  market:"CN",buy_window:"09:30–10:00",counts:{},sell_actions:[],buy_actions:[],
  hold_actions:[],audit:{},
});
if ((html.match(/class="cn-trend-table"/g) || []).length !== 4 ||
    !html.includes("筛选价（Trend Animals）") ||
    !html.includes("执行参考价（Futu 前复权）") ||
    (html.match(/<p>无<\/p>/g) || []).length !== 4) throw new Error(html);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_trend_report_mobile_layout_css() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    mobile = css.split("@media (max-width: 760px) {", 1)[1]

    assert "grid-template-columns: minmax(0, 1fr) 280px;" in css
    assert ".trend-stage li,\n.trend-audit p {\n  overflow-wrap: anywhere;\n}" in css
    assert ".trend-report-body { grid-template-columns: minmax(0, 1fr); }" in mobile
    assert ".trend-checklist { position: static; order: 2; }" in mobile
    assert (
        ".trend-report-entry button,\n  .trend-report-header button,\n"
        "  .option-attention-header button { min-height: 44px; }"
    ) in mobile
    assert ".cn-trend-table {" in css
    table_css = css.split("\n.cn-trend-table {", 1)[1].split("}", 1)[0]
    assert "table-layout: fixed;" in table_css
    assert "width: 100%;" in table_css
    assert "min-width:" not in table_css
    assert ".cn-trend-table thead" in mobile
    assert ".cn-trend-card" in mobile
    assert "content: attr(data-label);" in mobile
    assert "overflow-x: hidden;" in mobile
    assert ".trend-discipline summary" in mobile
    assert "min-height: 44px;" in mobile


def test_dashboard_renders_fixed_order_futu_option_attention_list() -> None:
    output = run_dashboard_js(r'''
const report = {
  broker: "futu",
  attention_markets: [
    {
      market: "US", market_label: "美股", data_status: "current", data_date: "2026-07-16",
      items: [{
        market: "US", symbol: "QQQ", name: null, category: "strengthened",
        right_side: {previous: false, current: true, changed: true},
        temperature: {previous: "温", current: "热", changed: true},
        phase: {previous: "谷雨", current: "立夏", changed: true},
        local_strength: "95", global_strength: null,
        strength_prev_week: "91", strength_prev_month: "",
        strength_change: {previous: "→", current: "↑", changed: true},
        days: 2, gain_since_entry: "0.02",
        danger: {previous: false, current: false, changed: false},
        boiling: {previous: false, current: true, changed: true},
        champagne: {previous: null, current: false, changed: false},
        source_broker: "老虎", source_action: "BUY",
        headline: "首次进入关注范围", summary: "危险信号首次出现",
      }],
    },
    {
      market: "HK", market_label: "港股", data_status: "stale", data_date: "2026-07-15",
      items: [{
        market: "HK", symbol: "00700", name: "腾讯", category: "watch",
        right_side: {previous: true, current: true, changed: false},
        temperature: {previous: "热", current: "热", changed: false},
        phase: {previous: "立夏", current: "小满", changed: true},
        local_strength: "90", global_strength: "88",
        strength_prev_week: "89", strength_prev_month: "84",
        strength_change: {previous: "↑", current: "→", changed: true},
        days: 8, gain_since_entry: "0.08",
        danger: {previous: false, current: false, changed: false},
        boiling: {previous: false, current: false, changed: false},
        champagne: {previous: false, current: false, changed: false},
        source_broker: "辉立", source_action: "SELL_ALL",
      }],
    },
  ],
};
const html = renderTrendReportWorkspace(report);
const headings = [
  "标的", "分类", "右侧状态", "趋势温度", "趋势节气",
  "本地 / 全球强度", "上周 / 上月", "右侧天数 / 累计涨幅",
  "危险 / 沸腾 / 开香槟", "来源动作",
];
const renderedHeadings = [...html.matchAll(/<th scope="col">([^<]+)<\/th>/g)].map((match) => match[1]);
if (JSON.stringify(renderedHeadings) !== JSON.stringify(headings)) throw new Error(html);
const separators = [...html.matchAll(/<th colspan="10" scope="rowgroup"><div class="option-attention-market-content">([\s\S]*?)<\/div><\/th>/g)];
if (separators.length !== 2 || separators.some((match) => (match[1].match(/<span>/g) || []).length !== 2)) throw new Error(html);
const rowgroups = [...html.matchAll(/<tbody>([\s\S]*?)<\/tbody>/g)].map((match) => match[1]);
if (rowgroups.length !== 2 || !rowgroups[0].includes("美股") || !rowgroups[0].includes("QQQ") || rowgroups[0].includes("00700") ||
    !rowgroups[1].includes("港股") || !rowgroups[1].includes("00700") || rowgroups[1].includes("QQQ")) throw new Error(html);
for (const row of html.matchAll(/<tr class="option-attention-row">([\s\S]*?)<\/tr>/g)) {
  const labels = [...row[1].matchAll(/data-label="([^"]+)"/g)].map((match) => match[1]);
  if (JSON.stringify(labels) !== JSON.stringify(headings)) throw new Error(row[0]);
}
if ((html.match(/class="option-attention-row"/g) || []).length !== 2) throw new Error(html);
if (!html.includes('<td data-label="标的">QQQ 未提供</td>') || html.indexOf("美股") >= html.indexOf("港股")) throw new Error(html);
if (!html.includes("数据截至 2026-07-15；今日未更新")) throw new Error(html);
if (!html.includes("允许买入") || !html.includes("卖出复核")) throw new Error(html);
if (optionAttentionAction("HOLD") !== "继续持有" || optionAttentionAction("WATCH") !== "观察" || optionAttentionAction("constructor") !== "观察") throw new Error(html);
if (renderOptionAttentionTransition({previous:false,current:false,changed:false}).includes("option-attention-changed")) throw new Error(html);
if (!renderOptionAttentionTransition({previous:false,current:true,changed:true}).includes("option-attention-changed")) throw new Error(html);
for (const forbidden of ["首次进入关注范围", "危险信号首次出现", "headline", "summary"]) {
  if (html.includes(forbidden)) throw new Error(forbidden + ": " + html);
}
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_option_attention_uses_native_responsive_grid() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    desktop = css.split("@media (max-width: 760px) {", 1)[0]
    mobile = css.split("@media (max-width: 760px) {", 1)[1]

    separator_cell_rules = re.findall(
        r"\.option-attention-market th \{([^}]*)\}", desktop,
    )
    assert separator_cell_rules
    assert all("display:" not in rule for rule in separator_cell_rules)
    separator_content = desktop.split(
        ".option-attention-market-content {", 1,
    )[1].split("}", 1)[0]
    assert "display: flex;" in separator_content
    assert "justify-content: space-between;" in separator_content
    assert ".option-attention-table thead" in mobile
    assert ".option-attention-row" in mobile
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in mobile
    assert "content: attr(data-label);" in mobile
    assert ".option-attention-workspace" in mobile
    assert "overflow-x: hidden;" in mobile
    narrow = css.split("@media (max-width: 460px) {", 1)[1]
    assert ".option-attention-row" in narrow
    assert "grid-template-columns: minmax(0, 1fr);" in narrow


def test_dashboard_trend_report_defensively_handles_malformed_arrays() -> None:
    output = run_dashboard_js(r'''
const html=renderTrendReportWorkspace({
  broker_label:"富途",market_label:"美股",report_date:"2026-07-15",
  data_date:"2026-07-14",generated_at:"now",account_status:"已更新",
  buy_window:"美股常规交易时段",counts:{},
  sell_actions:{bad:true},buy_actions:null,hold_actions:"bad",review_actions:42,
  audit:{candidates:[null],excluded:{},industry_concentration:[null],data_sources:{bad:true}},
});
state.dashboard={trend_reports:{futu:{available:false,status_text:"今日趋势报告无效"}}};
const unavailable=renderTrendReportEntry("futu");
if((html.match(/<p>无<\/p>/g)||[]).length!==4)throw new Error(html);
if(!html.includes("数据来源：无"))throw new Error(html);
if(!unavailable.includes("今日趋势报告无效"))throw new Error(unavailable);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_futu_attention_entry_omits_aggregate_report_dates() -> None:
    output = run_dashboard_js(r'''
state.dashboard={trend_reports:{futu:{
  available:true,status_text:"期权关注",report_date:"-",data_date:"-",
}}};
const entry=renderTrendReportEntry("futu");
if(!entry.includes("期权关注"))throw new Error(entry);
if(entry.includes("报告日期")||entry.includes("数据截至"))throw new Error(entry);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_trend_report_escapes_report_strings() -> None:
    output = run_dashboard_js(r'''
const attack='<img src=x onerror=alert(1)>';
state.dashboard={trend_reports:{futu:{available:true,report_date:attack,data_date:attack}}};
const entry=renderTrendReportEntry("futu");
const workspace=renderTrendReportWorkspace({
  broker_label:attack,market_label:attack,report_date:attack,data_date:attack,
  generated_at:attack,account_status:attack,buy_window:attack,
  sell_actions:[{symbol:attack,name:attack,reason:"unknown",active_line:attack}],
  buy_actions:[{symbol:attack,name:attack,estimated_shares:attack,target_amount:attack,estimated_initial_line:attack}],
  hold_actions:[],review_actions:[],counts:{sell:attack},
  audit:{candidates:[{symbol:attack,name:attack,strength:attack}],excluded:{[attack]:["unknown"]},industry_concentration:[[attack]],data_sources:[attack],actual_api_cost:attack},
});
if((entry+workspace).includes(attack))throw new Error(entry+workspace);
if(!workspace.includes("&lt;img"))throw new Error(workspace);
console.log("ok");
''')

    assert "ok" in output


def test_dashboard_account_holdings_mobile_layout_css() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    mobile = css.split("@media (max-width: 760px) {", 1)[1]

    assert ".account-holdings-table thead" in mobile
    assert ".account-holding-row" in mobile
    assert 'grid-template-areas:\n      "symbol symbol market-value account-weight portfolio-weight pnl"\n      "market quantity price actions actions actions";' in mobile
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in mobile
    for area in (
        "symbol", "market-value", "account-weight", "portfolio-weight", "pnl",
        "market", "quantity", "price", "actions",
    ):
        assert f"grid-area: {area};" in mobile
    assert (
        ".account-holding-row .account-holding-cost,\n"
        "  .account-holding-row .account-holding-usd-value {"
    ) in mobile
    assert ".account-mobile-actions" not in mobile
    assert 'class="account-mobile-actions"' not in js
    assert "min-height: 44px;" in mobile
    assert ".account-mobile-label" in mobile
    assert "overflow-x: hidden;" in mobile


def test_dashboard_has_no_removed_header_broker_filter_references() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert "header-broker-filters" not in css
    assert "header-broker-filters" not in js


def test_dashboard_account_holdings_selects_only_one_broker_row_for_duplicate_symbol() -> None:
    run_dashboard_js(r'''
const mount = () => ({innerHTML: "", textContent: "", classList: {add() {}, remove() {}}});
elements["account-holdings"] = mount();
elements["visible-count"] = mount();
elements["workspace-grid"] = mount();
elements["symbol-detail-panel"] = mount();
elements["account-tabs"] = mount();
renderTSignalDetail = (holding) => `TDETAIL:${holding.symbol}`;
state.dashboard = {
  summary: {portfolio_value_hkd: "3000"},
  broker_summaries: [
    {broker: "futu", portfolio_value_hkd: "1000"},
    {broker: "tiger", portfolio_value_hkd: "2000"},
    {broker: "phillips", portfolio_value_hkd: "0"},
    {broker: "eastmoney", portfolio_value_hkd: "0"},
  ], source_statuses: [], cash_rows: [],
  holdings: [{market: "US", symbol: "QQQ", brokers: "futu;tiger", broker_details: [
    {broker: "futu", market: "US", symbol: "QQQ", quantity: "1", market_value_hkd: "700"},
    {broker: "tiger", market: "US", symbol: "QQQ", quantity: "2", market_value_hkd: "1600"},
  ]}],
};
state.brokerFilter = "tiger";
state.selectedHoldingKey = "tiger:US:QQQ:0";
renderAccountHoldings();
const html = elements["account-holdings"].innerHTML;
if ((html.match(/active-row/g) || []).length !== 1) throw new Error("expected one active broker row: " + html);
if ((html.match(/inline-symbol-detail/g) || []).length !== 1) throw new Error("expected one inline detail: " + html);
if (html.includes('id="account-futu"') || !html.includes('id="account-tiger"') || !html.includes("TDETAIL:QQQ")) {
  throw new Error("selected Tiger QQQ should not activate Futu QQQ: " + html);
}
''')


def test_dashboard_static_contains_account_holdings_mount() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="account-holdings"' in html
    assert 'id="tiger-long-term-panel"' not in html
    assert 'aria-live="polite"' in html


def test_dashboard_uses_new_account_roles_without_retired_strategy_summary() -> None:
    output = run_dashboard_js(r'''
state.dashboard={trend_reports:{tiger:{available:false,status_text:"暂时不可用"}}};
const group={broker:"tiger",profile:ACCOUNT_STRATEGY_PROFILES.tiger,rows:[],summary:{broker:"tiger"}};
console.log(renderAccountSection(group));
''')

    assert "趋势 · 美股趋势交易" in output
    for forbidden in ("SMA200", "影子验证", "夏普比率", "卡玛比率", "产物不存在"):
        assert forbidden not in output


def test_dashboard_css_does_not_contain_retired_tiger_strategy_selectors() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    for selector in (
        "account-strategy-summary",
        "tiger-long-term-panel",
        "tiger-panel-heading",
        "tiger-panel-status",
        "tiger-rule-strip",
        "tiger-metric-grid",
        "tiger-metric-card",
        "tiger-member-",
        "tiger-number",
        "tiger-symbol",
        "tiger-unavailable",
    ):
        assert selector not in css


def test_dashboard_js_renders_kelly_lab_panel() -> None:
    run_dashboard_js(
        """
state.dashboard = {
  kelly_lab: {
    available: true,
    experiment_count: 1,
    experiments: [{
      experiment_id: "trend_pullback_20d_exp_20260707",
      experiment_name: "趋势回调 20D 第一批",
      market: "US",
      status: "running",
      locked: true,
      experiment_budget: "30000",
      budget_currency: "USD",
      market_capital_pool: {currency: "USD", amount: "30000"},
      capital_utilization_pct: "50",
      order_sync: {
        status: "success",
        environment: "SIMULATE",
        last_synced_at: "2026-07-08 10:08",
        order_count: 7,
        fill_count: 5,
        message: "富途模拟盘订单已同步。",
        next_action: "可以继续扫描入场与退出信号。",
        orders: [
          {
            market: "US",
            symbol: "RAM",
            side: "buy",
            submitted_at: "2026-07-08 10:01",
            order_price: "12.34",
            order_qty: "800",
            filled_qty: "800",
            avg_fill_price: "12.34",
            status: "filled",
            order_id: "SIM-10001"
          },
          {
            market: "HK",
            symbol: "02840",
            side: "sell",
            submitted_at: "2026-07-08 10:03",
            order_price: "218.80",
            order_qty: "100",
            filled_qty: "0",
            avg_fill_price: "-",
            status: "submitted",
            order_id: "SIM-10002"
          }
        ]
      },
      order_execution: {
        status: "partial",
        environment: "DRY_RUN",
        source: "dry_run",
        last_executed_at: "2026-07-10 13:32",
        execution_count: 2,
        submitted_count: 0,
        dry_run_count: 1,
        skipped_count: 1,
        failed_count: 0,
        message: "Kelly 订单执行存在失败或跳过项。",
        executions: [
          {
            intent_id: "trend_pullback_20d_exp_20260707:US:RAM:entry",
            market: "US",
            symbol: "RAM",
            futu_code: "US.RAM",
            side: "buy",
            order_type: "NORMAL",
            price: "12.50",
            qty: "80",
            planned_notional: "400",
            budget_currency: "USD",
            execution_status: "dry_run",
            futu_order_id: "",
            executed_at: "2026-07-10 13:32",
            error: ""
          },
          {
            intent_id: "trend_pullback_20d_exp_20260707:HK:02840:exit",
            market: "HK",
            symbol: "02840",
            futu_code: "HK.02840",
            side: "sell",
            order_type: "NORMAL",
            price: "3000",
            qty: "",
            planned_notional: "",
            budget_currency: "USD",
            execution_status: "skipped",
            futu_order_id: "",
            executed_at: "2026-07-10 13:32",
            error: "missing order quantity"
          }
        ]
      },
      lifecycle_states: [
        {
          status: "watching",
          market: "US",
          symbol: "DRAM",
          reason: "价格距离 MA20 仍有 2.4%，入场规则未满足。",
          updated_at: "2026-07-08 10:00"
        },
        {
          status: "pending_entry_order",
          market: "US",
          symbol: "RAM",
          reason: "入场规则触发，仓位计算与风控检查待执行。",
          action: "等待仓位计算与风控检查",
          updated_at: "2026-07-08 10:01"
        },
        {
          status: "holding",
          market: "US",
          symbol: "SOXX",
          reason: "模拟盘买入已成交，当前监控退出规则。",
          action: "继续检查止盈、止损、移动止盈、时间退出",
          updated_at: "2026-07-08 10:02"
        },
        {
          status: "pending_exit_order",
          market: "HK",
          symbol: "02840",
          reason: "止盈触发，价格达到入场价 + 2R。",
          action: "准备卖出 50%",
          updated_at: "2026-07-08 10:03"
        }
      ],
      template: {
        strategy_id: "trend_pullback_20d",
        strategy_name: "趋势回调 20D",
        strategy_version: "v1",
        entry_rule_description: "结构化规则生成入场。",
        exit_rule_description: "目标价、止损或 20 个交易日到期。",
        rules: {
          entry: {
            type: "pullback_to_moving_average",
            ma_days: 20,
            tolerance_pct: 1,
            trend_filter: {type: "moving_average_slope", ma_days: 50, direction: "up"}
          },
          stop_loss: {
            type: "any_of",
            rules: [
              {type: "pct_below_moving_average", ma_days: 20, pct: 3},
              {type: "recent_swing_low_break", lookback_days: 20}
            ]
          },
          take_profit: {type: "risk_multiple", trigger_r: 2, sell_pct: 50},
          trailing_stop: {type: "close_below_moving_average", ma_days: 10, apply_to_remaining_position: true},
          time_exit: {type: "max_holding_days", days: 20, exit_if: "no_take_profit_or_stop_loss"}
        }
      },
      participants: [
        {market: "US", symbol: "DRAM", name: "Roundhill Memory ETF", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"},
        {market: "US", symbol: "RAM", name: "2倍做多DRAM ETF-T-REX", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"},
        {market: "US", symbol: "SOXX", name: "iShares费城交易所半导体ETF", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"},
        {market: "HK", symbol: "02840", name: "SPDR金", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"}
      ],
      stats: {
        completed_samples: 18,
        open_samples: 4,
        observed_win_rate: "56%",
        sample_stage: "insufficient",
        winning_samples: 10,
        losing_samples: 8,
        raw_win_rate: "56%",
        adjusted_win_rate: "52%",
        avg_net_win_pct: "4.8%",
        avg_net_loss_pct: "2.9%",
        payoff_ratio: "1.66",
        full_kelly_pct: "23.1%",
        fractional_kelly_pct: "5.8%",
        suggested_position_pct: "4%",
        sample_adjustment: "样本少于 200，向 50% 收缩",
        last_sample_closed_at: "2026-07-07 15:30",
        last_recomputed_at: "2026-07-07 15:31"
      }
    },
    {
      experiment_id: "breakout_10d_mock_20260707",
      experiment_name: "突破 10D Mock 第一批",
      market: "US",
      status: "running",
      locked: true,
      experiment_budget: "30000",
      budget_currency: "USD",
      market_capital_pool: {currency: "USD", amount: "30000"},
      capital_utilization_pct: "40",
      order_sync: {
        status: "failed",
        environment: "SIMULATE",
        last_synced_at: "2026-07-08 10:09",
        order_count: 3,
        fill_count: 2,
        message: "模拟盘订单同步失败：OpenD 不可用。",
        next_action: "本轮不下单，保留现有订单状态。",
        orders: [
          {
            market: "US",
            symbol: "MSFT",
            side: "buy",
            submitted_at: "2026-07-08 10:05",
            order_price: "505.10",
            order_qty: "20",
            filled_qty: "0",
            avg_fill_price: "-",
            status: "rejected",
            order_id: "SIM-20001"
          }
        ]
      },
      order_execution: {
        status: "failed",
        environment: "SIMULATE",
        source: "futu_simulate_order_execution_client",
        last_executed_at: "2026-07-10 13:35",
        execution_count: 1,
        submitted_count: 0,
        dry_run_count: 0,
        skipped_count: 0,
        failed_count: 1,
        message: "Kelly 订单执行存在失败或跳过项。",
        executions: [
          {
            intent_id: "breakout_10d_mock_20260707:US:MSFT:entry",
            market: "US",
            symbol: "MSFT",
            futu_code: "US.MSFT",
            side: "buy",
            order_type: "NORMAL",
            price: "505.10",
            qty: "1",
            planned_notional: "505.10",
            budget_currency: "USD",
            execution_status: "failed",
            futu_order_id: "",
            executed_at: "2026-07-10 13:35",
            error: "OpenD disconnected"
          }
        ]
      },
      template: {
        strategy_id: "breakout_10d",
        strategy_name: "突破 10D",
        strategy_version: "v1",
        entry_rule_description: "结构化规则生成入场。",
        exit_rule_description: "目标价、止损或 10 个交易日到期。",
        rules: {
          entry: {
            type: "volume_breakout_high",
            lookback_days: 10,
            volume_multiple: 1.5
          },
          stop_loss: {
            type: "any_of",
            rules: [
              {type: "pct_below_reference_price", reference: "breakout_price", pct: 2},
              {type: "atr_below_entry", atr_multiple: 1.5}
            ]
          },
          take_profit: {type: "risk_multiple", trigger_r: 2, sell_pct: 50},
          trailing_stop: {type: "close_below_recent_low", lookback_days: 5, apply_to_remaining_position: true},
          time_exit: {type: "max_holding_days", days: 10, exit_if: "minimum_unrealized_r_not_reached", min_unrealized_r: 1}
        }
      },
      participants: [
        {market: "US", symbol: "MSFT", name: "微软", source: "watchlist", per_symbol_budget: "15000", budget_currency: "USD"},
        {market: "US", symbol: "TSM", name: "台积电", source: "holding", per_symbol_budget: "15000", budget_currency: "USD"},
        {market: "HK", symbol: "06951", name: "三环集团", source: "holding", per_symbol_budget: "15000", budget_currency: "USD"}
      ],
      stats: {
        completed_samples: 42,
        open_samples: 3,
        observed_win_rate: "52%",
        sample_stage: "open",
        winning_samples: 22,
        losing_samples: 20,
        raw_win_rate: "52%",
        adjusted_win_rate: "51%",
        avg_net_win_pct: "6.1%",
        avg_net_loss_pct: "3.4%",
        payoff_ratio: "1.79",
        full_kelly_pct: "24.2%",
        fractional_kelly_pct: "6.1%",
        suggested_position_pct: "4%",
        sample_adjustment: "样本少于 200，向 50% 收缩",
        last_sample_closed_at: "2026-07-07 15:45",
        last_recomputed_at: "2026-07-07 15:46"
      }
    }]
  }
};
state.workspaceView = "portfolio";
const entryHtml = renderKellyLabPanel();
if (entryHtml !== "") {
  throw new Error("kelly lab homepage entry should be empty: " + entryHtml);
}
state.workspaceView = "kelly_lab";
const html = renderKellyLabPanel();
if (!html.includes("模拟盘策略实验室") || !html.includes("趋势回调 20D 第一批")) {
  throw new Error("kelly lab panel missing experiment identity: " + html);
}
if (!html.includes("role=\\\"tablist\\\"") || !html.includes("data-kelly-experiment=\\\"trend_pullback_20d_exp_20260707\\\"") || !html.includes("data-kelly-experiment=\\\"breakout_10d_mock_20260707\\\"")) {
  throw new Error("kelly lab strategy tabs missing: " + html);
}
const breakoutNameCount = html.split("突破 10D Mock 第一批").length - 1;
if (breakoutNameCount !== 1) {
  throw new Error("kelly lab should only render active strategy detail: " + html);
}
if (!html.includes("样本不足") || !html.includes("US.DRAM")) {
  throw new Error("kelly lab panel missing sample stage or participant: " + html);
}
function expectMetric(html, label, value, description) {
  const pattern = new RegExp("<div>\\\\s*<dt>" + label + "</dt>\\\\s*<dd>" + value + "</dd>\\\\s*</div>");
  if (!pattern.test(html)) {
    throw new Error(description + ": " + html);
  }
}
expectMetric(html, "市场", "US", "kelly lab panel missing market metric");
expectMetric(html, "模拟资金池", "USD 30,000", "kelly lab panel missing capital pool metric");
for (const forbidden of ["US.MSFT", "US.TSM", "HK.06951"]) {
  if (html.includes(forbidden)) {
    throw new Error("kelly first tab leaked another strategy symbol " + forbidden + ": " + html);
  }
}
if (html.includes("实验参与标的") || html.includes("kelly-participant-row")) {
  throw new Error("kelly lab should use symbol states as the only symbol list: " + html);
}
for (const required of [
  "标的状态",
  "订单执行",
  "部分执行",
  "Kelly 订单执行存在失败或跳过项。",
  "DRY_RUN",
  "2026-07-10 13:32",
  "执行",
  "2",
  "预演",
  "1",
  "提交",
  "0",
  "跳过",
  "1",
  "计划金额",
  "富途订单",
  "错误",
  "400",
  "预演",
  "已跳过",
  "missing order quantity",
  "订单同步",
  "同步成功",
  "富途模拟盘订单已同步。",
  "SIMULATE",
  "2026-07-08 10:08",
  "订单",
  "7",
  "成交",
  "5",
  "可以继续扫描入场与退出信号。",
  "标的",
  "方向",
  "下单时间",
  "订单价",
  "订单数量",
  "成交数量",
  "成交均价",
  "状态",
  "US.RAM",
  "SIM-10001",
  "买入",
  "2026-07-08 10:01",
  "12.34",
  "800",
  "已成交",
  "HK.02840",
  "SIM-10002",
  "卖出",
  "218.8",
  "100",
  "0",
  "待成交",
  "观察中 → 待下单 → 持仓中 → 待退出 → 已完成",
  "观察中",
  "该标的在策略监控范围内，但当前没有入场信号，也没有持仓。",
  "待下单",
  "入场规则触发，仓位计算与风控检查待执行。",
  "持仓中",
  "模拟盘买入已成交，这笔策略样本正在进行中。",
  "待退出",
  "这笔持仓已经触发退出规则，但卖出还没有完成。",
  "US.SOXX",
  "HK.02840",
  "US.RAM",
  "US.DRAM",
  "策略详情",
  "入场",
  "价格回调到 20 日均线 ±1% 内，且 50 日均线斜率向上。",
  "止损",
  "跌破 20 日均线 3% 或跌破最近波段低点。",
  "止盈",
  "价格达到入场价 + 2R 时卖出 50%。",
  "移动止盈",
  "剩余仓位收盘跌破 10 日均线时退出。",
  "时间退出",
  "持有满 20 个交易日仍未触发止盈或止损则退出。",
  "参数推导",
  "原始胜率",
  "10 赢 / 8 亏",
  "修正胜率",
  "52%",
  "盈亏比 b",
  "1.66",
  "Full Kelly",
  "23.1%",
  "建议仓位",
  "4%",
  "样本少于 200，向 50% 收缩",
  "2026-07-07 15:31"
]) {
  if (!html.includes(required)) {
    throw new Error("kelly derivation missing " + required + ": " + html);
  }
}
if (html.includes("Mock 状态样本") || html.includes("状态说明")) {
  throw new Error("kelly lifecycle should be scoped inside strategy card, not global: " + html);
}
if (html.includes("风控通过") || html.includes("Kelly 建议单标的仓位 4%")) {
  throw new Error("pending entry narrative claims pre-risk approval: " + html);
}
if (html.includes("第一目标") || html.includes("延续")) {
  throw new Error("kelly strategy rules contain vague terms: " + html);
}
if (html.includes("data-workspace-view=\\\"portfolio\\\"") || html.includes("返回主页")) {
  throw new Error("kelly lab panel has a workspace-local return button: " + html);
}
const fallbackHtml = renderKellyExperimentCard({
  experiment_name: "无状态样本策略",
  market: "US",
  status: "running",
  experiment_budget: "25000",
  budget_currency: "USD",
  order_sync: {
    status: "success",
    environment: "SIMULATE",
    last_synced_at: "2026-07-08 10:10",
    order_count: 0,
    fill_count: 0,
    message: "富途模拟盘订单已同步。",
    next_action: "等待下一次信号。"
  },
  participants: [{market: "US", symbol: "IBM", name: "IBM", source: "watchlist"}],
  template: {strategy_id: "fallback_strategy", strategy_name: "Fallback"},
  stats: {}
});
if (!fallbackHtml.includes("标的状态") || !fallbackHtml.includes("US.IBM") || !fallbackHtml.includes("等待该策略下一次入场信号。")) {
  throw new Error("kelly participant fallback lifecycle missing: " + fallbackHtml);
}
expectMetric(fallbackHtml, "市场", "US", "kelly fallback market metric missing");
expectMetric(fallbackHtml, "模拟资金池", "USD 25,000", "kelly fallback capital pool missing");
const disabledPoolHtml = renderKellyExperimentCard({
  experiment_name: "禁用市场资金池策略",
  market: "CN",
  status: "running",
  experiment_budget: "150000",
  budget_currency: "CNY",
  market_capital_pool: {market: "CN", currency: "CNY", amount: "150000", enabled: false},
  participants: [{market: "CN", symbol: "600000", name: "浦发银行", source: "watchlist"}],
  template: {strategy_id: "disabled_pool_strategy", strategy_name: "Disabled Pool"},
  stats: {}
});
expectMetric(disabledPoolHtml, "市场", "CN", "kelly disabled pool market metric missing");
expectMetric(disabledPoolHtml, "模拟资金池", "未启用", "kelly disabled pool should show unavailable metric");
if (/<div>\\s*<dt>模拟资金池<\\/dt>\\s*<dd>CNY 150000<\\/dd>\\s*<\\/div>/.test(disabledPoolHtml)) {
  throw new Error("kelly disabled pool rendered active capital amount: " + disabledPoolHtml);
}
if (fallbackHtml.includes("实验参与标的") || fallbackHtml.includes("kelly-participant-row")) {
  throw new Error("kelly fallback should not render duplicate participant chips: " + fallbackHtml);
}
if (!fallbackHtml.includes("暂无同步订单明细。")) {
  throw new Error("kelly order sync empty detail missing: " + fallbackHtml);
}
state.selectedKellyExperimentId = "breakout_10d_mock_20260707";
const secondHtml = renderKellyLabPanel();
const trendNameCount = secondHtml.split("趋势回调 20D 第一批").length - 1;
if (!secondHtml.includes("突破 10D Mock 第一批") || trendNameCount !== 1) {
  throw new Error("kelly lab tab selection did not isolate active strategy: " + secondHtml);
}
if (!secondHtml.includes("价格放量突破近 10 个交易日高点，成交量不低于 1.5 倍均量。") || !secondHtml.includes("US.MSFT") || !secondHtml.includes("US.TSM") || !secondHtml.includes("HK.06951")) {
  throw new Error("kelly lab second tab content missing: " + secondHtml);
}
for (const required of ["订单同步", "同步失败", "模拟盘订单同步失败：OpenD 不可用。", "本轮不下单，保留现有订单状态。", "US.MSFT", "SIM-20001", "买入", "505.1", "20", "拒单"]) {
  if (!secondHtml.includes(required)) {
    throw new Error("kelly second tab order sync missing " + required + ": " + secondHtml);
  }
}
for (const required of ["订单执行", "执行失败", "Kelly 订单执行存在失败或跳过项。", "SIMULATE", "2026-07-10 13:35", "OpenD disconnected", "执行失败"]) {
  if (!secondHtml.includes(required)) {
    throw new Error("kelly second tab order execution missing " + required + ": " + secondHtml);
  }
}
for (const forbidden of ["US.DRAM", "US.RAM", "US.SOXX", "HK.02840"]) {
  if (secondHtml.includes(forbidden)) {
    throw new Error("kelly second tab leaked another strategy symbol " + forbidden + ": " + secondHtml);
  }
}
"""
    )


def test_dashboard_js_renders_kelly_parameter_source() -> None:
    html = run_dashboard_js(
        """
const html = renderKellyParameterDerivation({
  completed_samples: 2,
  open_samples: 1,
  observed_win_rate: "50%",
  sample_stage: "insufficient",
  raw_win_rate: "50%",
  adjusted_win_rate: "50%",
  avg_net_win_pct: "10%",
  avg_net_loss_pct: "5%",
  payoff_ratio: "2",
  full_kelly_pct: "25%",
  fractional_kelly_pct: "6.25%",
  suggested_position_pct: "4%",
  sample_adjustment: "样本少于 200，向 50% 收缩",
  source_trade_samples_generated_at: "2026-07-12 09:59",
  last_sample_closed_at: "2026-07-12 10:00",
  last_recomputed_at: "2026-07-12 10:01",
  parameter_source: "futu_paper_order_samples",
  skipped_order_count: 3
});
console.log(html);
"""
    )

    assert "样本状态" in html
    assert "样本不足" in html
    assert "已完成样本" in html
    assert "2" in html
    assert "进行中样本" in html
    assert "1" in html
    assert "参数来源" in html
    assert "富途模拟盘订单样本" in html
    assert "跳过订单" in html
    assert "3" in html
    assert "来源样本时间" in html
    assert "2026-07-12 09:59" in html
    assert "最近完成样本" in html
    assert "2026-07-12 10:00" in html
    assert "最近计算" in html
    assert "2026-07-12 10:01" in html


def test_dashboard_js_renders_kelly_unavailable_strategy_stats_error() -> None:
    html = run_dashboard_js(
        """
state.workspaceView = "kelly_lab";
state.dashboard = {
  kelly_lab: {
    available: false,
    error: "kelly_strategy_stats.json stale: source trade sample timestamp does not match"
  }
};
console.log(renderKellyLabPanel());
"""
    )

    assert "不可用" in html
    assert "kelly_strategy_stats.json" in html


def test_dashboard_renders_kelly_strategy_capital_panel() -> None:
    output = run_dashboard_js(
        """
state.dashboard = {
  kelly_lab: {
    available: true,
    experiments: [{
      experiment_id: "trend_pullback_20d_us_mock_20260707",
      experiment_name: "趋势回调 20D Mock US 第一批",
      market: "US",
      experiment_budget: "30000",
      budget_currency: "USD",
      status: "running",
      template: {
        strategy_id: "trend_pullback_20d",
        strategy_name: "趋势回调 20D",
        entry_rule_description: "价格回调到 20 日均线附近。"
      },
      stats: {},
      capital: {
        currency: "USD",
        budget: 30000,
        occupied_notional: 8460,
        position_notional: 6200,
        reserved_order_notional: 2260,
        available_notional: 21540,
        utilization_pct: 28.2,
        open_buy_order_count: 2,
        realized_pnl: 420,
        updated_at: "2026-07-10 13:45",
        symbol_occupancy: [
          {symbol: "US.RAM", occupied_notional: 8460}
        ],
        next_order_impact: {
          symbol: "US.RAM",
          estimated_notional: 1500,
          available_after_order: 20040,
          risk_status: "approved",
          reason: "订单提交后仍保留充足可用资金。"
        }
      }
    }]
  }
};
state.workspaceView = "kelly_lab";
const html = renderKellyLabPanel();
for (const required of [
  "策略资金",
  "总资金",
  "USD 30,000",
  "可用资金",
  "USD 21,540",
  "已占用",
  "USD 8,460",
  "下一笔下单影响",
  "US.RAM",
  "资金足够"
]) {
  if (!html.includes(required)) {
    throw new Error("kelly capital panel missing " + required + ": " + html);
  }
}
"""
    )
    assert output == ""


def test_dashboard_renders_kelly_strategy_capital_unavailable_fallback() -> None:
    output = run_dashboard_js(
        """
const baseExperiment = {
  experiment_name: "资金缺失策略",
  market: "US",
  experiment_budget: "30000",
  budget_currency: "USD",
  status: "running",
  template: {
    strategy_id: "trend_pullback_20d",
    strategy_name: "趋势回调 20D",
    entry_rule_description: "价格回调到 20 日均线附近。"
  },
  stats: {}
};
const missingHtml = renderKellyExperimentCard(baseExperiment);
const disabledHtml = renderKellyExperimentCard({
  ...baseExperiment,
  capital: {available: false}
});
for (const html of [missingHtml, disabledHtml]) {
  for (const required of ["策略资金", "策略资金数据暂不可用。"]) {
    if (!html.includes(required)) {
      throw new Error("kelly capital fallback missing " + required + ": " + html);
    }
  }
}
"""
    )
    assert output == ""


def test_dashboard_bounds_kelly_strategy_capital_utilization_widths() -> None:
    output = run_dashboard_js(
        """
const overflowingHtml = renderKellyExperimentCard({
  experiment_name: "资金超限策略",
  market: "US",
  budget_currency: "USD",
  status: "running",
  template: {strategy_id: "overflow", strategy_name: "Overflow"},
  stats: {},
  capital: {
    currency: "USD",
    budget: 100,
    occupied_notional: 250,
    position_notional: 140,
    reserved_order_notional: 90,
    available_notional: 0,
    utilization_pct: 250,
    open_buy_order_count: 1,
    realized_pnl: 0
  }
});
const invalidHtml = renderKellyExperimentCard({
  experiment_name: "资金异常策略",
  market: "US",
  budget_currency: "USD",
  status: "running",
  template: {strategy_id: "invalid", strategy_name: "Invalid"},
  stats: {},
  capital: {
    currency: "USD",
    budget: "not-a-number",
    occupied_notional: "",
    position_notional: "bad",
    reserved_order_notional: -25,
    available_notional: 0,
    utilization_pct: "bad",
    open_buy_order_count: 0,
    realized_pnl: 0
  }
});
for (const html of [overflowingHtml, invalidHtml]) {
  if (html.includes("NaN%") || /width:\\s*-/.test(html)) {
    throw new Error("kelly capital utilization emitted invalid width: " + html);
  }
  const widths = [...html.matchAll(/width:\\s*([0-9.]+)%/g)].map((match) => Number.parseFloat(match[1]));
  if (widths.length !== 2) {
    throw new Error("kelly capital utilization width count mismatch: " + html);
  }
  for (const width of widths) {
    if (!Number.isFinite(width) || width < 0 || width > 100) {
      throw new Error("kelly capital utilization width out of bounds " + width + ": " + html);
    }
  }
}
if (!overflowingHtml.includes('style="width: 100%"></span>') || !overflowingHtml.includes('style="width: 0%"></span>')) {
  throw new Error("kelly capital overflowing widths should clamp to 100 and 0: " + overflowingHtml);
}
"""
    )
    assert output == ""


def test_dashboard_renders_kelly_capital_producer_symbol_shape() -> None:
    output = run_dashboard_js(
        """
const html = renderKellyExperimentCard({
  experiment_name: "真实资金形状策略",
  market: "US",
  budget_currency: "USD",
  status: "running",
  template: {strategy_id: "producer", strategy_name: "Producer"},
  stats: {},
  capital: {
    currency: "USD",
    budget: 10000,
    occupied_notional: 3720,
    position_notional: 3720,
    reserved_order_notional: 0,
    available_notional: 6280,
    utilization_pct: 37.2,
    open_buy_order_count: 0,
    realized_pnl: 0,
    symbol_occupancy: [
      {market: "US", symbol: "RAM", notional: "3720"},
      {market: "US", symbol: "US.DRAM", notional: "500"}
    ],
    next_order_impact: {
      market: "US",
      symbol: "US.RAM",
      estimated_notional: 500,
      available_after_order: 5780,
      risk_status: "approved"
    }
  }
});
if (!html.includes("US.RAM") || !html.includes("USD 3,720")) {
  throw new Error("kelly producer symbol shape missing rendered symbol: " + html);
}
if (html.includes("US.US.RAM") || html.includes("US.US.DRAM")) {
  throw new Error("kelly producer symbol duplicated market prefix: " + html);
}
"""
    )
    assert output == ""
def obsolete_dashboard_backtest_filter_limits_holdings_and_ignores_cash_view() -> None:
    output = run_dashboard_js(
        r"""
state.dashboard = {
  holdings: [
    {
      market: "US",
      symbol: "READY",
      name: "Ready",
      brokers: "futu",
      backtest_readiness: { status: "ready", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOPRICE",
      name: "No Price",
      brokers: "futu",
      backtest_readiness: { status: "missing_prices", prices_missing: true, missing_fields: [] },
    },
    {
      market: "HK",
      symbol: "NOFIELD",
      name: "No Field",
      brokers: "phillips",
      backtest_readiness: { status: "missing_fields", prices_missing: false, missing_fields: ["target_1"] },
    },
    {
      market: "US",
      symbol: "UNSUPPORTED",
      name: "Unsupported",
      brokers: "tiger",
      backtest_readiness: { status: "unsupported_strategy", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOREADINESS",
      name: "No Readiness",
      brokers: "futu",
    },
  ],
  cash_rows: [
    { market: "CASH", symbol: "HKD_CASH", brokers: "futu", market_value_hkd: "100" },
  ],
};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.backtestFilter = "READY";
let symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "READY") {
  throw new Error("READY filter mismatch: " + symbols);
}
state.backtestFilter = "MISSING_PRICES";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "NOPRICE") {
  throw new Error("MISSING_PRICES filter mismatch: " + symbols);
}
state.backtestFilter = "MISSING_FIELDS";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "NOFIELD") {
  throw new Error("MISSING_FIELDS filter mismatch: " + symbols);
}
state.backtestFilter = "UNSUPPORTED";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "UNSUPPORTED") {
  throw new Error("UNSUPPORTED filter mismatch: " + symbols);
}
state.backtestFilter = "ALL";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "READY,NOPRICE,NOFIELD,UNSUPPORTED,NOREADINESS") {
  throw new Error("ALL filter mismatch: " + symbols);
}
state.marketFilter = "US";
state.backtestFilter = "READY";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "READY") {
  throw new Error("combined market/backtest filter mismatch: " + symbols);
}
state.marketFilter = "CASH";
state.brokerFilter = "futu";
state.backtestFilter = "READY";
const cashRows = filteredCashRows();
if (cashRows.length !== 1 || cashRows[0].symbol !== "HKD_CASH") {
  throw new Error("backtest filter should not affect cash view: " + JSON.stringify(cashRows));
}
console.log("ok");
"""
    )

    assert "ok" in output


def obsolete_dashboard_backtest_filter_buttons_show_current_scope_counts() -> None:
    output = run_dashboard_js(
        r"""
state.dashboard = {
  holdings: [
    {
      market: "US",
      symbol: "READY",
      brokers: "futu",
      backtest_readiness: { status: "ready", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOPRICE",
      brokers: "futu",
      backtest_readiness: { status: "missing_prices", prices_missing: true, missing_fields: [] },
    },
    {
      market: "HK",
      symbol: "NOFIELD",
      brokers: "phillips",
      backtest_readiness: { status: "missing_fields", prices_missing: false, missing_fields: ["target_1"] },
    },
    {
      market: "US",
      symbol: "UNSUPPORTED",
      brokers: "tiger",
      backtest_readiness: { status: "unsupported_strategy", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOREADINESS",
      brokers: "futu",
    },
  ],
};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.backtestFilter = "READY";
let html = renderBacktestFilterButtons();
for (const expected of ["全部回测 5", "可运行 1", "缺价格 1", "缺字段 1", "暂不支持 1"]) {
  if (!html.includes(expected)) {
    throw new Error("missing global count " + expected + ": " + html);
  }
}
if (!html.includes('data-backtest="READY"') || !html.includes("active")) {
  throw new Error("active backtest filter should remain selected: " + html);
}
state.marketFilter = "US";
state.brokerFilter = "futu";
state.backtestFilter = "ALL";
html = renderBacktestFilterButtons();
for (const expected of ["全部回测 3", "可运行 1", "缺价格 1", "缺字段 0", "暂不支持 0"]) {
  if (!html.includes(expected)) {
    throw new Error("missing scoped count " + expected + ": " + html);
  }
}
console.log("ok");
"""
    )

    assert "ok" in output


def obsolete_dashboard_renders_backtest_price_auto_sync_status() -> None:
    output = run_dashboard_js(
        r"""
let rendered = "";
elements["backtest-price-sync-status"] = {
  textContent: "",
  className: "",
};
state.dashboard = {
  backtest_price_sync: {
    status: "ok",
    attempted: 2,
    succeeded: 2,
    failed: 0,
    errors: [],
  },
};
renderBacktestPriceSyncStatus();
rendered = elements["backtest-price-sync-status"].textContent;
if (rendered !== "已自动补齐 2 个回测价格文件") {
  throw new Error("success sync status mismatch: " + rendered);
}
if (!elements["backtest-price-sync-status"].className.includes("status-ok")) {
  throw new Error("success sync status should use ok tone: " + elements["backtest-price-sync-status"].className);
}
state.dashboard = {
  backtest_price_sync: {
    status: "failed",
    attempted: 1,
    succeeded: 0,
    failed: 1,
    errors: [{ market: "US", symbol: "VIXY", message: "kline unavailable" }],
  },
};
renderBacktestPriceSyncStatus();
rendered = elements["backtest-price-sync-status"].textContent;
if (rendered !== "自动补齐失败 1 个：US.VIXY") {
  throw new Error("failed sync status mismatch: " + rendered);
}
if (!elements["backtest-price-sync-status"].className.includes("status-warning")) {
  throw new Error("failed sync status should use warning tone: " + elements["backtest-price-sync-status"].className);
}
state.dashboard = { backtest_price_sync: { status: "skipped", attempted: 0, succeeded: 0, failed: 0, errors: [] } };
renderBacktestPriceSyncStatus();
if (elements["backtest-price-sync-status"].textContent !== "") {
  throw new Error("skipped sync status should stay empty: " + elements["backtest-price-sync-status"].textContent);
}
console.log("ok");
"""
    )

    assert "ok" in output


def test_dashboard_renders_futu_anomaly_signal_card_in_chinese() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  name: "英伟达",
  portfolio_weight_hkd: "8.2%",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: true,
      status: "ok",
      signal: "supportive",
      confidence: "medium",
      suggested_constraint: "",
      window_days: 7,
      summary: "技术信号支持趋势。",
      categories: [
        {name: "MACD", state: "anomaly", direction: "bullish", detail: "金叉后继续放大。", evidence_date: "2026-07-01"},
        {name: "RSI", state: "anomaly", direction: "risk_up", detail: "接近超买区。", evidence_date: "2026-07-02"},
        {name: "K线形态", state: "none", direction: "", detail: "窗口内无异常。", evidence_date: ""}
      ]
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "mixed",
      confidence: "medium",
      suggested_constraint: "no_add",
      window_days: 7,
      summary: "资金流向与加仓动作存在分歧。",
      categories: [
        {name: "资金流向", state: "anomaly", direction: "bearish", detail: "主力资金连续净流出。", evidence_date: "2026-07-02"},
        {name: "卖空情况", state: "none", direction: "", detail: "窗口内无异常。", evidence_date: ""}
      ]
    },
    derivatives_anomaly: {
      available: true,
      status: "partial",
      signal: "risk_up",
      confidence: "low",
      suggested_constraint: "no_add",
      window_days: 7,
      summary: "期权波动率偏高。",
      categories: [
        {name: "期权波动率", state: "anomaly", direction: "risk_up", detail: "IV 位于高位。", evidence_date: "2026-07-02"},
        {name: "期权大单", state: "anomaly", direction: "bullish", detail: "出现看涨大单。", evidence_date: "2026-07-01"}
      ]
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
const end = html.length;
if (start < 0 || start >= end) {
  throw new Error("Futu signal card boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    for required in [
        "市场信号 · 富途异动信号",
        "技术异动",
        "资金异动",
        "衍生品异动",
        "支持",
        "不加仓",
        "部分可用",
        "偏多",
        "偏空",
        "风险上升",
        "无异常",
    ]:
        assert required in output

    for forbidden in [
        "supportive",
        "no_add",
        "partial",
        "risk_up",
        "bullish",
        "bearish",
        "schema",
    ]:
        assert forbidden not in output


def test_dashboard_futu_anomaly_opposing_signal_affects_overall() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: true,
      status: "ok",
      signal: "opposing",
      confidence: "medium",
      suggested_constraint: "",
      summary: "技术信号反对追高。",
      categories: [
        {name: "MACD", state: "anomaly", direction: "bearish", detail: "动能转弱。", evidence_date: "2026-07-02"}
      ]
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "资金无明显方向。",
      categories: []
    },
    derivatives_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "衍生品无明显方向。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf('<div class="futu-signal-overall">');
const end = html.indexOf('<div class="futu-signal-module-grid">');
if (start < 0 || end < 0 || start >= end) {
  throw new Error("Futu signal overall boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    assert "反对" in output
    assert "市场信号反对当前交易方向" in output
    assert "中性" not in output


def test_dashboard_futu_anomaly_missing_modules_do_not_render_neutral_direction() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: false,
      status: "missing",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "缺少富途技术异动数据。",
      categories: []
    },
    capital_anomaly: {
      available: false,
      status: "error",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途资金异动查询失败。",
      categories: []
    },
    derivatives_anomaly: {
      available: false,
      status: "stale",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途衍生品异动数据已过期。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf('<div class="futu-signal-module-grid">');
const end = html.indexOf('<p class="condition-box">');
if (start < 0 || end < 0 || start >= end) {
  throw new Error("Futu signal module boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    for required in ["<strong>缺失</strong>", "<strong>错误</strong>", "<strong>已过期</strong>"]:
        assert required in output
    assert "<strong>中性</strong>" not in output


def test_dashboard_futu_anomaly_unavailable_modules_do_not_render_neutral_overall() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: false,
      status: "missing",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "缺少富途技术异动数据。",
      categories: []
    },
    capital_anomaly: {
      available: false,
      status: "error",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途资金异动查询失败。",
      categories: []
    },
    derivatives_anomaly: {
      available: false,
      status: "stale",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途衍生品异动数据已过期。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf('<div class="futu-signal-overall">');
const end = html.indexOf('<div class="futu-signal-module-grid">');
if (start < 0 || end < 0 || start >= end) {
  throw new Error("Futu signal overall boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    assert "需复核" in output
    assert "市场信号数据不可用" in output
    assert "窗口内未发现明显异动" not in output
    assert "<strong>中性</strong>" not in output


def test_dashboard_futu_anomaly_stale_run_date_blocks_supportive_overall() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: false,
      status: "stale_run_date",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "Futu facts run date does not match latest advice",
      categories: []
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "supportive",
      confidence: "low",
      suggested_constraint: "",
      summary: "资金信号支持当前方向。",
      categories: []
    },
    derivatives_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "窗口内无异常。",
      categories: []
    }
  }
};
console.log(futuAnomalySignalsPlugin(holding));
"""
    )

    assert "已过期" in output
    assert "status-stale" in output
    assert "status-warn" in output
    assert "需复核" in output
    assert "市场信号数据不可用" in output
    assert "窗口内未发现明显异动" not in output


def test_dashboard_futu_anomaly_unknown_enums_render_safe_chinese_fallback() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: true,
      status: "schema",
      signal: "schema_break",
      confidence: "very_high",
      suggested_constraint: "unsafe_add",
      summary: "异常字段测试。",
      categories: [
        {name: "MACD", state: "invalid_state", direction: "strange_direction", detail: "未知枚举测试。", evidence_date: "2026-07-02"}
      ]
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "正常模块。",
      categories: []
    },
    derivatives_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "正常模块。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
const end = html.length;
if (start < 0 || start >= end) {
  throw new Error("Futu signal card boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    assert "未知" in output
    assert "MACD" in output
    for forbidden in [
        "schema",
        "schema_break",
        "very_high",
        "unsafe_add",
        "invalid_state",
        "strange_direction",
    ]:
        assert forbidden not in output


def test_dashboard_holdings_table_uses_compact_asset_columns() -> None:
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert "function renderAccountTable(group, rows)" not in js
    for label in (
        "明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值",
        "港元市值", "账户权重", "组合权重", "盈亏",
    ):
        assert f'"{label}"' in js
    table_renderer = js.split("function renderAccountTable", 1)[1].split("function holdingKey", 1)[0]
    assert '"券商"' not in table_renderer
    assert '"策略"' not in table_renderer


def test_dashboard_display_helpers_keep_raw_english_out_of_chinese_ui() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
const holding = {
  strategy: {
    agent_reason: "trim into strength",
    plan_text: "Wait for pullback",
  },
  trade_action: {
    action: "TRIM",
    status: "review",
    trigger_status: "target_1_hit",
    reason: "trim into strength",
    watch_trigger: "wait for confirmation",
  },
};
const report = {
  rating: "reduce",
  status: "ok",
  run_date: "2026-06-19",
  agent_reason: "Risk is elevated.",
};
const summary = renderChineseAgentSummary(report, holding);
if (summary.includes("trim into strength") || summary.includes("Risk is elevated")) {
  throw new Error("raw English leaked into Chinese summary: " + summary);
}
const trigger = nextTriggerText(holding.trade_action, holding);
if (trigger.includes("wait for confirmation") || trigger.includes("Wait for pullback")) {
  throw new Error("raw English leaked into next trigger: " + trigger);
}
const translatedTrigger = nextTriggerText(
  { watch_trigger: "wait for confirmation" },
  { strategy: { plan_text_zh: "重新站回均线后复评", plan_text: "Wait for pullback" } },
);
if (!translatedTrigger.includes("重新站回均线后复评")) {
  throw new Error("Chinese fallback was not used: " + translatedTrigger);
}
if (chineseDisplayText("Risk is elevated.") !== "") {
  throw new Error("short English prose should be suppressed");
}
if (chineseDisplayText("YoY 增速稳定，OpenAI 影响有限。") === "") {
  throw new Error("Chinese text with business tokens should remain visible");
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_report_readability_helpers_build_decision_first_sections() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
state.detailLanguage = "zh";
const holding = {
  market: "US",
  symbol: "DRAM",
  name: "DRAM Test",
  total_quantity: "100",
  strategy: {
    available: true,
    rating: "Underweight",
    target_1: "51",
    target_2: "53",
    stop_loss: "60",
    catalyst: "6 月 24 日财报后复评",
    time_horizon: "1-3 个月",
    plan_text_zh: "财报前先锁定收益，财报后重新评估。",
    agent_reason_zh: "MACD 背离，仓位风险上升。财报是下一判断点。因此先减半而非清仓。",
  },
  agent_report: {
    available: true,
    rating: "Underweight",
    status: "ok",
    run_date: "2026-06-19",
    source_status: "fallback",
    summary_zh: "评级低配。趋势派认为 MACD 背离。组合结论是减仓而非清仓。",
    raw_decision: "The bull case remains possible, but risk is elevated.",
  },
  trade_action: {
    available: true,
    action: "TRIM",
    status: "ready",
    trigger_status: "target_1_hit",
    limit_price: "51",
    suggested_quantity: "50",
    suggested_notional: "2550",
    notional_currency: "USD",
    stop_price: "60",
    trigger_reason_zh: "达到第一目标价，先锁定部分收益。",
    agent_reason_zh: "MACD 背离，仓位风险上升。财报是下一判断点。因此先减半而非清仓。",
  },
  premarket_action: { available: false },
};
const action = currentDecisionAction(holding);
if (action.action !== "TRIM") {
  throw new Error("trade_action should lead the decision row");
}
const desired = desiredActionText(holding);
if (!desired.includes("减仓") || !desired.includes("DRAM")) {
  throw new Error("desired action should be Chinese and symbol-specific: " + desired);
}
const watch = watchPointText(holding);
if (!watch.includes("达到第一目标价") && !watch.includes("财报")) {
  throw new Error("watch point should use trigger or catalyst: " + watch);
}
const metricMap = Object.fromEntries(decisionMetricCells(holding));
if (!String(metricMap["目标价"] || "").includes("51") || !String(metricMap["触发状态"] || "").includes("达到第一目标价")) {
  throw new Error("metrics missing decision values: " + JSON.stringify(metricMap));
}
const conclusionText = JSON.stringify(finalConclusionItems(holding));
if (!conclusionText.includes("低配") || !conclusionText.includes("减仓") || !conclusionText.includes("60")) {
  throw new Error("conclusion missing decision text: " + conclusionText);
}
const html = renderAnalysisStrategySection(holding);
for (const required of ["分析与交易策略", "当前希望你做什么", "操作指令", "今天重点关注", "分析师对话", "最终结论", "查看英文原文", "正常", "使用历史报告回退"]) {
  if (!html.includes(required)) {
    throw new Error("missing rendered label " + required + " in " + html);
  }
}
const conclusionSection = html.includes("research-conclusion-grid")
  ? html.slice(html.indexOf("research-conclusion-grid"), html.indexOf("source-review") === -1 ? undefined : html.indexOf("source-review"))
  : "";
for (const required of ["低配", "减仓", "60"]) {
  if (!conclusionSection.includes(required)) {
    throw new Error("fallback conclusion missing " + required + ": " + conclusionSection);
  }
}
for (const placeholder of ["-", "暂无明确结论。"]) {
  const placeholderHolding = {
    ...holding,
    research_view: {
      available: true,
      tradingagents_conclusion: {status: "present", content: placeholder},
      user_llm_conclusion: {status: "missing", content: ""},
    },
  };
  const placeholderHtml = renderAnalysisStrategySection(placeholderHolding);
  const placeholderSection = placeholderHtml.includes("research-conclusion-grid")
    ? placeholderHtml.slice(placeholderHtml.indexOf("research-conclusion-grid"), placeholderHtml.indexOf("source-review") === -1 ? undefined : placeholderHtml.indexOf("source-review"))
    : "";
  for (const required of ["低配", "减仓", "60"]) {
    if (!placeholderSection.includes(required)) {
      throw new Error("placeholder research conclusion blocked fallback " + required + ": " + placeholderSection);
    }
  }
}
const primaryHtml = html.split("source-review", 1)[0];
if (primaryHtml.includes("risk is elevated") || primaryHtml.includes("The bull case")) {
  throw new Error("raw English leaked into primary Chinese UI: " + primaryHtml);
}
const sourceSection = html.includes("source-review") ? html.slice(html.indexOf("source-review")) : "";
if (!sourceSection.includes("english-source") || !sourceSection.includes("hidden") || !sourceSection.includes("The bull case")) {
  throw new Error("English source should remain collapsed and preserved: " + sourceSection);
}
const sourceOnlyHolding = {
  market: "US",
  symbol: "SRC",
  strategy: { available: true, plan_text: "Wait for earnings confirmation before adding." },
  agent_report: { available: false },
  trade_action: {
    available: true,
    action: "HOLD",
    status: "manual_review",
    agent_reason: "Risk remains elevated until earnings.",
  },
  premarket_action: { available: false },
};
const sourceOnlyHtml = renderAnalysisStrategySection(sourceOnlyHolding);
const sourceOnlyPrimary = sourceOnlyHtml.split("source-review", 1)[0];
const sourceOnlySource = sourceOnlyHtml.includes("source-review") ? sourceOnlyHtml.slice(sourceOnlyHtml.indexOf("source-review")) : "";
if (sourceOnlyPrimary.includes("Risk remains elevated") || sourceOnlyPrimary.includes("Wait for earnings")) {
  throw new Error("English-only rationale leaked into primary Chinese UI: " + sourceOnlyPrimary);
}
if (!sourceOnlyPrimary.includes("需复核") || !sourceOnlySource.includes("Risk remains elevated")) {
  throw new Error("manual_review/source preservation failed: " + sourceOnlyHtml);
}
const uppercaseLeakHolding = {
  market: "US",
  symbol: "CAPS",
  strategy: { available: false },
  agent_report: { available: false },
  trade_action: { available: false },
  premarket_action: {
    available: true,
    suggested_action: "reduce",
    watch_trigger_zh: "OPEN BELOW PRIOR CLOSE 后复评",
  },
};
const uppercaseOutputs = [
  decisionTriggerText(currentDecisionAction(uppercaseLeakHolding)),
  watchPointText(uppercaseLeakHolding),
  nextReviewText(uppercaseLeakHolding),
  finalConditionText(uppercaseLeakHolding),
  renderAnalysisStrategySection(uppercaseLeakHolding).split("source-review", 1)[0],
].join(" ");
if (uppercaseOutputs.includes("OPEN BELOW PRIOR CLOSE") || safePrimaryValue("BULLISH") || safePrimaryValue("BREAKOUT")) {
  throw new Error("all-caps English trading prose leaked into primary UI: " + uppercaseOutputs);
}
if (primaryChineseText("TSLA 财报后复评") !== "TSLA 财报后复评" || safePrimaryValue("AAPL 财报后复评") !== "AAPL 财报后复评") {
  throw new Error("normal ticker tokens should remain visible in Chinese helper text");
}
const noActionHtml = renderAnalysisStrategySection({
  market: "US",
  symbol: "CASH",
  strategy: { available: false },
  agent_report: { available: false },
  trade_action: { available: false },
  premarket_action: { available: false },
});
if (!noActionHtml.includes("今天暂无触发中的交易动作")) {
  throw new Error("missing explicit no-action state: " + noActionHtml);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_renders_fixed_decision_fact_cards_in_chinese() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
function fixedDecisionFactCards(html) {
  const klineStart = html.indexOf("<h4>趋势 / K 线</h4>");
  const newsStart = html.indexOf("<h4>新闻 / 舆论</h4>");
  const nextStart = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
  if (klineStart < 0 || newsStart < 0 || nextStart < 0 || !(klineStart < newsStart && newsStart < nextStart)) {
    throw new Error("fixed decision fact card boundaries missing: " + html);
  }
  return html.slice(klineStart, nextStart);
}
function renderDecisionFactCards(holding) {
  return renderDecisionPluginCard(klineDecisionFactsPlugin(holding))
    + renderDecisionPluginCard(newsSentimentPlugin(holding))
    + futuAnomalySignalsPlugin(holding);
}
function cardBefore(cards, nextTitle) {
  const end = cards.indexOf(nextTitle);
  if (end < 0) {
    throw new Error("card boundary missing before " + nextTitle + ": " + cards);
  }
  return cards.slice(0, end);
}
function cardFrom(cards, title) {
  const start = cards.indexOf(title);
  if (start < 0) {
    throw new Error("card boundary missing for " + title + ": " + cards);
  }
  return cards.slice(start);
}
function assertOrdered(card, labels) {
  let cursor = -1;
  for (const label of labels) {
    const next = card.indexOf("<span>" + label + "</span>", cursor + 1);
    if (next <= cursor) {
      throw new Error("label order mismatch for " + label + ": " + card);
    }
    cursor = next;
  }
}
const holding = {
  market: "US",
  symbol: "SOXX",
  name: "iShares Semiconductor ETF",
  agent_report: {available: true},
  strategy: {available: false},
  trade_action: {available: false},
  decision_facts: {
    kline: {
      available: true,
      fields: {
        trend: "过热拉升",
        position: "显著高于均线",
        momentum: "RSI 高位",
        key_levels: "支撑 580",
        risk: "超买风险"
      }
    },
    news_sentiment: {
      available: true,
      fields: {
        direction: "偏多",
        change: "较上次转强",
        catalyst: "AI 基建需求",
        risk: "估值过高",
        attention: "关注度升高"
      }
    }
  },
  futu_skill_facts: {
    news_sentiment: {
      available: true,
      domestic_discussion: {
        status: "ok",
        keyword_counts: [
          { keyword: "震荡", count: 3 },
          { keyword: "看空", count: 2 },
          { keyword: "损耗", count: 1 }
        ],
        summary: "富途社区相关讨论较少，少量用户关注 DRAM ETF 与成分股走势联动。",
        focus: "ETF 夜盘可能受韩股存储链影响，盘中更受美光、闪迪等美股成分影响。",
        divergence_risk: "社区样本少且噪声高，不能代表稳定共识。",
        credibility: "低",
        trading_constraint: "仅作为国内讨论温度和 ETF 结构风险提示，不支持单独加仓或减仓。",
        post_count: 8,
        relevant_post_count: 2
      }
    }
  }
};
const cards = fixedDecisionFactCards(renderDecisionFactCards(holding));
const klineCard = cardBefore(cards, "<h4>新闻 / 舆论</h4>");
const newsCard = cardFrom(cards, "<h4>新闻 / 舆论</h4>");
assertOrdered(klineCard, ["趋势", "位置", "动能", "关键位", "风险"]);
assertOrdered(newsCard, ["方向", "变化", "催化", "风险", "热度"]);
assertOrdered(newsCard, ["讨论关键词", "国内讨论结论", "主要关注点", "分歧 / 风险", "可信度", "交易约束"]);
for (const required of [
  "趋势 / K 线",
  "新闻 / 舆论",
  "趋势",
  "位置",
  "动能",
  "关键位",
  "风险",
  "方向",
  "变化",
  "催化",
  "热度",
  "过热拉升",
  "偏多",
  "AI 基建需求",
  "富途社区 / 国内讨论",
  "讨论关键词",
  "震荡",
  "3",
  "看空",
  "2",
  "损耗",
  "1",
  "国内讨论结论",
  "主要关注点",
  "分歧 / 风险",
  "可信度",
  "交易约束",
  "富途社区相关讨论较少，少量用户关注 DRAM ETF 与成分股走势联动。",
  "ETF 夜盘可能受韩股存储链影响，盘中更受美光、闪迪等美股成分影响。",
  "社区样本少且噪声高，不能代表稳定共识。",
  "仅作为国内讨论温度和 ETF 结构风险提示，不支持单独加仓或减仓。"
]) {
  if (!cards.includes(required)) {
    throw new Error("missing fixed decision fact content " + required + ": " + cards);
  }
}
for (const forbidden of ["Bullish", "condition-box", "Futu Skill 证据", "https://news.futunn.com", "代表观点", "国内风险点", "数据约束"]) {
  if (cards.includes(forbidden)) {
    throw new Error("unexpected fixed decision fact content " + forbidden + ": " + cards);
  }
}
if (!klineCard.includes("status-pill status-ok") || !klineCard.includes(">可用</span>")) {
  throw new Error("complete K-line card should be usable: " + klineCard);
}
if (!newsCard.includes("status-pill status-ok") || !newsCard.includes(">可用</span>")) {
  throw new Error("complete news card should be usable: " + newsCard);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_missing_decision_facts_show_only_missing_values() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
function fixedDecisionFactCards(html) {
  const klineStart = html.indexOf("<h4>趋势 / K 线</h4>");
  const newsStart = html.indexOf("<h4>新闻 / 舆论</h4>");
  const nextStart = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
  if (klineStart < 0 || newsStart < 0 || nextStart < 0 || !(klineStart < newsStart && newsStart < nextStart)) {
    throw new Error("fixed decision fact card boundaries missing: " + html);
  }
  return html.slice(klineStart, nextStart);
}
function renderDecisionFactCards(holding) {
  return renderDecisionPluginCard(klineDecisionFactsPlugin(holding))
    + renderDecisionPluginCard(newsSentimentPlugin(holding))
    + futuAnomalySignalsPlugin(holding);
}
function cardBefore(cards, nextTitle) {
  const end = cards.indexOf(nextTitle);
  if (end < 0) {
    throw new Error("card boundary missing before " + nextTitle + ": " + cards);
  }
  return cards.slice(0, end);
}
function cardFrom(cards, title) {
  const start = cards.indexOf(title);
  if (start < 0) {
    throw new Error("card boundary missing for " + title + ": " + cards);
  }
  return cards.slice(start);
}
function assertOrdered(card, labels) {
  let cursor = -1;
  for (const label of labels) {
    const next = card.indexOf("<span>" + label + "</span>", cursor + 1);
    if (next <= cursor) {
      throw new Error("label order mismatch for " + label + ": " + card);
    }
    cursor = next;
  }
}
function assertStatus(card, status, tone) {
  if (!card.includes("status-pill status-" + tone) || !card.includes(">" + status + "</span>")) {
    throw new Error("expected " + status + "/" + tone + " status: " + card);
  }
}
const baseHolding = {
  market: "US",
  symbol: "SOXX",
  name: "iShares Semiconductor ETF",
  agent_report: {available: false},
  strategy: {available: false},
  trade_action: {available: false},
  technical_facts: {
    available: true,
    status: "usable",
    facts: {
      timeframes: [
        {timeframe_label: "日线", rsi: {value: "66.66"}, trend_summary: "不应显示"}
      ]
    }
  },
};
const completeCards = fixedDecisionFactCards(renderDecisionFactCards({
  ...baseHolding,
  decision_facts: {
    kline: {available: true, fields: {trend: "过热拉升", position: "显著高于均线", momentum: "RSI 高位", key_levels: "支撑 580", risk: "超买风险"}},
    news_sentiment: {available: true, fields: {direction: "偏多", change: "较上次转强", catalyst: "AI 基建需求", risk: "估值过高", attention: "关注度升高"}}
  }
}));
assertStatus(cardBefore(completeCards, "<h4>新闻 / 舆论</h4>"), "可用", "ok");
assertStatus(cardFrom(completeCards, "<h4>新闻 / 舆论</h4>"), "可用", "ok");
const partialCards = fixedDecisionFactCards(renderDecisionFactCards({
  ...baseHolding,
  decision_facts: {
    kline: {available: true, fields: {trend: "过热拉升", position: "", momentum: "缺失"}},
    news_sentiment: {available: true, fields: {direction: "偏多", change: "较上次转强", catalyst: "AI 基建需求", risk: "估值过高", attention: "关注度升高"}}
  }
}));
const partialKlineCard = cardBefore(partialCards, "<h4>新闻 / 舆论</h4>");
assertStatus(partialKlineCard, "不完整", "partial");
assertOrdered(partialKlineCard, ["趋势", "位置", "动能", "关键位", "风险"]);
for (const required of ["过热拉升", "<strong>缺失</strong>"]) {
  if (!partialKlineCard.includes(required)) {
    throw new Error("partial K-line card missing fixed field value " + required + ": " + partialKlineCard);
  }
}
const missingCards = fixedDecisionFactCards(renderDecisionFactCards({
  ...baseHolding,
  decision_facts: {
    kline: {available: false, fields: {trend: "缺失", position: "缺失", momentum: "缺失", key_levels: "缺失", risk: "缺失"}},
    news_sentiment: {}
  }
}));
const missingKlineCard = cardBefore(missingCards, "<h4>新闻 / 舆论</h4>");
const missingNewsCard = cardFrom(missingCards, "<h4>新闻 / 舆论</h4>");
assertStatus(missingKlineCard, "缺失", "partial");
assertStatus(missingNewsCard, "缺失", "partial");
assertOrdered(missingKlineCard, ["趋势", "位置", "动能", "关键位", "风险"]);
assertOrdered(missingNewsCard, ["方向", "变化", "催化", "风险", "热度"]);
const cards = partialCards + missingCards;
for (const required of ["<strong>缺失</strong>", "<b>缺失</b>"]) {
  if (!cards.includes(required)) {
    throw new Error("missing fixed fields should render 缺失 values: " + cards);
  }
}
for (const forbidden of ["待接入", "未来确认", "暂无可用 K 线技术事实", "日线 RSI", "66.66", "不应显示", "condition-box"]) {
  if (cards.includes(forbidden)) {
    throw new Error("placeholder or old technical fact content leaked into fixed cards: " + forbidden + ": " + cards);
  }
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_tradingagents_card_renders_fixed_summary_fields_only() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
function tradingAgentsCard(html) {
  const start = html.indexOf("<h4>TradingAgents</h4>");
  const end = html.indexOf("<h4>财报</h4>");
  if (start < 0) {
    throw new Error("TradingAgents card boundaries missing: " + html);
  }
  return html.slice(start, end < 0 ? html.length : end);
}
function rowLabels(card) {
  return card
    .split("<span>")
    .slice(1)
    .filter((part) => part.includes("</span>") && part.split("</span>", 2)[1].includes("<strong>"))
    .map((part) => part.split("</span>", 1)[0]);
}
function assertOrderedValues(card, pairs) {
  let cursor = -1;
  for (const [label, value] of pairs) {
    const fragment = "<span>" + label + "</span>\\n          <strong>" + value + "</strong>";
    const next = card.indexOf(fragment, cursor + 1);
    if (next <= cursor) {
      throw new Error("missing or out-of-order row " + label + "=" + value + ": " + card);
    }
    cursor = next;
  }
}
const html = renderTradingAgentsSummaryCard({
  market: "US",
  symbol: "DRAM",
  portfolio_weight_hkd: "7.11%",
  agent_report: {
    available: true,
    rating: "Underweight",
    source_status: "fallback",
    raw_decision: "FINAL TRANSACTION PROPOSAL: REDUCE",
  },
  strategy: {
    available: true,
    rating: "Underweight",
    agent_reason: "price target hit",
  },
  trade_action: {
    available: true,
    action: "TRIM",
    reason: "target_1_hit",
    trigger_status: "target_1_hit",
  },
  tradingagents_summary: {
    available: true,
    ta_view: "低配",
    current_action: "减仓",
    core_reason: "内存超级周期仍在，但价格极度延伸、MACD 背离且财报前情绪拥挤，所以 TA 建议降低仓位而非清仓。",
    ta_report_date: "2026-06-22",
    latest_run_date: "2026-06-23",
    reason_fields: {
      main_judgment: "不应渲染",
    },
    source_hash: "sha256:debug",
    error: "debug only",
    history: ["2026-06-20"],
    artifact_path: "data/latest/US/tradingagents_summary.json",
    source_status: "fallback",
  },
});
const card = tradingAgentsCard(html);
const expectedLabels = ["TA 观点", "当前动作", "核心理由", "TA 报告日期", "当前 latest"];
const labels = rowLabels(card);
if (JSON.stringify(labels) !== JSON.stringify(expectedLabels)) {
  throw new Error("unexpected TradingAgents labels " + JSON.stringify(labels) + ": " + card);
}
assertOrderedValues(card, [
  ["TA 观点", "低配"],
  ["当前动作", "减仓"],
  ["核心理由", "内存超级周期仍在，但价格极度延伸、MACD 背离且财报前情绪拥挤，所以 TA 建议降低仓位而非清仓。"],
  ["TA 报告日期", "2026-06-22"],
  ["当前 latest", "2026-06-23"],
]);
for (const forbidden of [
  "status-pill",
  "已接入",
  "<strong>TA</strong>",
  "decision-plugin-output",
  "<b>",
  "来源状态",
  "history",
  "历史",
  "reason_fields",
  "main_judgment",
  "source_hash",
  "artifact_path",
  "data/latest",
  "FINAL TRANSACTION PROPOSAL",
  "Underweight",
  "target_1_hit",
  "条件：",
  "condition-box",
  "price target hit",
]) {
  if (card.includes(forbidden)) {
    throw new Error("forbidden TradingAgents content leaked " + forbidden + ": " + card);
  }
}
const missingCard = tradingAgentsCard(renderTradingAgentsSummaryCard({
  market: "US",
  symbol: "MISSING",
  agent_report: {available: false},
  strategy: {available: false},
  trade_action: {available: false},
  tradingagents_summary: {available: false},
}));
const missingLabels = rowLabels(missingCard);
if (JSON.stringify(missingLabels) !== JSON.stringify(expectedLabels)) {
  throw new Error("missing summary should still render all labels: " + missingCard);
}
assertOrderedValues(missingCard, [
  ["TA 观点", "缺失"],
  ["当前动作", "缺失"],
  ["核心理由", "缺失"],
  ["TA 报告日期", "缺失"],
  ["当前 latest", "缺失"],
]);
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_renders_kline_technical_card_without_duplicate_fact_grid() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
const holding = {
  market: "HK",
  symbol: "02476",
  portfolio_weight_hkd: "8.97%",
  agent_report: {available: false},
  strategy: {available: false},
  trade_action: {available: false},
  premarket_action: {available: false},
  technical_facts: {
    available: true,
    status: "usable",
    run_date: "2026-06-19",
    data_date: "2026-06-18",
    error: "",
    freshness: {status: "fresh", message: "日线数据截至 2026-06-18"},
    facts: {
      status: "present",
      market_data_as_of: "2026-06-18",
      timeframes: [
        {
          timeframe: "daily",
          timeframe_label: "日线",
          current_price: "411.60",
          trend_summary: "价格高于主要均线。",
          bollinger: {
            upper: "430.00",
            middle: "405.00",
            lower: "380.00",
            position: "middle_range",
            status: "neutral",
            reference_band: "",
            distance_pct: "",
            summary_zh: "当前价格位于日线布林带区间内",
            detail_zh: "价格未贴近上轨或下轨，布林带事实仅作背景展示。",
          },
          rsi: {value: "56.88"},
          macd: {macd: "0.22", signal: "0.15", histogram: "0.07", crossover: "bullish crossover / 金叉"},
          atr: {value: "33.17", percent_of_price: "8.1%"},
          support_resistance: {
            support_levels: ["398.15", "368.24"],
            resistance_levels: ["430.00", "445.50"]
          }
        },
        {
          timeframe: "weekly",
          timeframe_label: "周线",
          current_price: "409.20",
          trend_summary: "周线仍在上行通道。",
          macd: {crossover: "形成金叉"},
          atr: "41.10",
          support_resistance: {
            support_levels: ["380.00"],
            resistance_levels: ["455.00"]
          }
        },
        {
          timeframe: "monthly",
          timeframe_label: "月线",
          rsi: "61.20"
        }
      ]
    }
  }
};
const card = renderDecisionPluginCard(klineTechnicalFactsPlugin(holding));
for (const required of [
  "可用",
  "数据日 2026-06-18",
  "运行 2026-06-19",
  "日线布林带",
  "中性区间",
  "当前价格位于日线布林带区间内",
  "下轨 380",
  "中轨 405",
  "上轨 430"
]) {
  if (!card.includes(required)) {
    throw new Error("missing K-line bollinger fact " + required + ": " + card);
  }
}
for (const duplicate of ["日线 当前价", "日线 RSI", "日线 MACD", "周线 当前价", "条件："]) {
  if (card.includes(duplicate)) {
    throw new Error("duplicate K-line fact grid rendered " + duplicate + ": " + card);
  }
}
if (card.includes("待接入") || card.includes("占位") || card.includes("rsi:")) {
  throw new Error("usable technical facts rendered as placeholder/raw field: " + card);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_renders_fixed_bollinger_card_without_internal_enums() -> None:
    script = r'''
const holding = {
  technical_facts: {
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {message: "日线数据截至 2026-07-03"},
    facts: {
      timeframes: [{
        timeframe: "daily",
        timeframe_label: "日线",
        current_price: "466.20",
        bollinger: {
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "above_upper",
          status: "upper_risk",
          reference_band: "upper",
          reference_value: "459.13",
          distance_pct: "1.5%",
          summary_zh: "当前价格已超过日线布林带上轨",
          detail_zh: "价格处在布林带上沿之外，说明短线偏热。",
        },
        rsi: {value: "56.88"},
        macd: {crossover: "金叉后延续"},
        moving_averages: {summary: "价格在主要均线上方"},
      }],
    },
  },
};
const html = renderDecisionPluginCard(klineTechnicalFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "布林带" in html
    assert "回调风险升高" in html
    assert "当前价格已超过日线布林带上轨" in html
    assert "当前价" in html
    assert "上轨" in html
    assert "偏离幅度" in html
    assert "technical-bollinger-card upper-risk" in html
    assert "upper_risk" not in html
    assert "above_upper" not in html


def test_dashboard_renders_bollinger_card_in_current_kline_plugin_path() -> None:
    script = r'''
const holding = {
  market: "US",
  symbol: "MSFT",
  last_price: "710.55",
  portfolio_weight_hkd: "10.00%",
  decision_facts: {
    kline: {available: false, fields: {}},
    news_sentiment: {available: false, fields: {}},
  },
  technical_facts: {
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {message: "日线数据截至 2026-07-03"},
    facts: {
      timeframes: [{
        timeframe: "daily",
        timeframe_label: "日线",
        bollinger: {
          current_price: "47.00",
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "above_upper",
          status: "upper_risk",
          reference_band: "upper",
          reference_value: "459.13",
          distance_pct: "1.5%",
          summary_zh: "当前价格已超过日线布林带上轨",
          detail_zh: "价格处在布林带上沿之外，说明短线偏热。",
        },
      }],
    },
  },
};
const html = renderDecisionPluginCard(klineDecisionFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "<h4>趋势 / K 线</h4>" in html
    assert "technical-bollinger-card upper-risk" in html
    assert "回调风险升高" in html
    assert "当前价格已超过日线布林带上轨" in html
    assert "当前价</span>\n          <strong>710.55</strong>" in html
    assert "当前价</span>\n          <strong>缺失</strong>" not in html
    assert "status-pill status-ok\">可用" in html
    assert "趋势</span>" not in html
    assert "upper_risk" not in html
    assert "above_upper" not in html


def test_dashboard_renders_kline_extraction_error_without_decision_field_noise() -> None:
    script = r'''
const holding = {
  market: "US",
  symbol: "RAM",
  portfolio_weight_hkd: "2.95%",
  decision_facts: {
    kline: {available: false, fields: {}},
    news_sentiment: {available: false, fields: {}},
  },
  technical_facts: {
    available: false,
    status: "extraction_error",
    data_date: "2026-07-02",
    run_date: "2026-07-04",
    error: "日线不足 20 根，无法计算布林带",
    freshness: {message: "指标周期缺失，需复核"},
    facts: {},
  },
};
const html = renderDecisionPluginCard(klineDecisionFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "<h4>趋势 / K 线</h4>" in html
    assert "不可用" in html
    assert "抽取失败" in html
    assert "日线不足 20 根，无法计算布林带" in html
    assert "趋势</span>" not in html
    assert "undefined" not in html


@pytest.mark.parametrize(
    ("status", "expected_label", "expected_class"),
    [
        ("lower_opportunity", "低位机会区域", "lower-opportunity"),
        ("neutral", "中性区间", "middle-range"),
        ("unknown", "布林带数据缺失", "missing"),
    ],
)
def test_dashboard_renders_bollinger_status_variants(
    status: str,
    expected_label: str,
    expected_class: str,
) -> None:
    script = f'''
const holding = {{
  technical_facts: {{
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {{message: "日线数据截至 2026-07-03"}},
    facts: {{
      timeframes: [{{
        timeframe: "daily",
        timeframe_label: "日线",
        current_price: "388.20",
        bollinger: {{
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "middle_range",
          status: "{status}",
          reference_band: "",
          reference_value: "",
          distance_pct: "",
          summary_zh: "",
          detail_zh: "",
        }},
      }}],
    }},
  }},
}};
const html = renderDecisionPluginCard(klineTechnicalFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert expected_label in html
    assert f"technical-bollinger-card {expected_class}" in html
    assert status not in html


def test_dashboard_omits_bollinger_when_technical_facts_unusable() -> None:
    script = r'''
const holding = {
  market: "US",
  symbol: "MSFT",
  portfolio_weight_hkd: "10.00%",
  decision_facts: {
    kline: {available: true, fields: {trend: "长期看涨，短期动能减弱"}},
    news_sentiment: {available: false, fields: {}},
  },
  technical_facts: {
    available: false,
    status: "extraction_error",
    error: "technical facts status is missing",
  },
};
const html = renderDecisionPluginCard(klineDecisionFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "长期看涨，短期动能减弱" in html
    assert "technical-bollinger-card" not in html
    assert "布林带数据缺失" not in html
    assert "undefined" not in html
    assert "参考轨道" not in html
    assert "缺失" in html


def test_dashboard_renders_kline_technical_fact_unavailable_states() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
const cases = [
  [{available: false, status: "missing_file", error: "technical_facts.json not found"}, "缺少文件"],
  [{available: false, status: "missing_record", error: "technical facts record not found"}, "缺少记录"],
  [{available: false, status: "stale_source_hash", run_date: "2026-06-19", data_date: "2026-06-18", error: "technical facts source hash does not match latest advice"}, "来源已过期"],
  [{available: false, status: "extraction_error", run_date: "2026-06-19", data_date: "2026-06-18", error: "llm unavailable"}, "抽取失败"],
  [{available: false, status: "missing_timeframe", run_date: "2026-06-19", data_date: "2026-06-18", error: "technical facts timeframe missing"}, "缺少周期"],
];
for (const [technicalFacts, label] of cases) {
  const card = renderDecisionPluginCard(klineTechnicalFactsPlugin({
    market: "US",
    symbol: "VIXY",
    portfolio_weight_hkd: "7.11%",
    agent_report: {available: false},
    strategy: {available: false},
    trade_action: {available: false},
    premarket_action: {available: false},
    technical_facts: technicalFacts,
  }));
  if (!card.includes(label) || !card.includes("不可用")) {
    throw new Error("missing unavailable state " + label + ": " + card);
  }
  if (technicalFacts.run_date && (!card.includes("运行 2026-06-19") || !card.includes("数据日 2026-06-18"))) {
    throw new Error("unavailable state should preserve dates: " + card);
  }
  if (card.includes("日线 RSI") || card.includes("当前可用")) {
    throw new Error("unavailable facts presented as current: " + card);
  }
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_research_conclusions_render_missing_and_present_states() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    portfolio_weight_hkd: "7.11%",
    risk_flag: "normal",
    broker_details: [],
    agent_report: {available: false},
    strategy: {available: false},
    premarket_action: {available: false},
    trade_action: {available: false},
    research_view: {
      available: true,
      research_date: "2026-06-19",
      tradingagents_conclusion: {
        status: "present",
        content: "低配，当前动作为减仓。",
        reason: "达到第一目标价。",
        condition: "财报后复评。"
      },
      user_llm_conclusion: {status: "missing", content: ""}
    }
  }]
};
const html = renderResearchConclusions(state.dashboard.holdings[0]);
if (!html.includes("投研给出的结论") || !html.includes("我和 LLM 探讨后的结论")) {
  throw new Error("research conclusion labels missing: " + html);
}
if (!html.includes("低配，当前动作为减仓。") || !html.includes("缺失")) {
  throw new Error("research conclusion content missing: " + html);
}
if (!html.includes("开始讨论")) {
  throw new Error("missing start chat button: " + html);
}
state.dashboard.holdings[0].research_view.user_llm_conclusion = {
  status: "present",
  content: "确认减仓 100 股。",
};
const finalizedHtml = renderResearchConclusions(state.dashboard.holdings[0]);
if (!finalizedHtml.includes("确认减仓 100 股。") || finalizedHtml.includes("<strong>缺失</strong>")) {
  throw new Error("finalized user conclusion did not render: " + finalizedHtml);
}
if (!finalizedHtml.includes("继续讨论")) {
  throw new Error("missing continue chat button: " + finalizedHtml);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_research_chat_ignores_stale_session_response() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
(async () => {
const calls = [];
let resolveA;
let resolveB;
postDashboardJson = (url, payload) => {
  calls.push(payload.symbol);
  return new Promise((resolve) => {
    if (payload.symbol === "AAA") resolveA = resolve;
    if (payload.symbol === "BBB") resolveB = resolve;
  });
};
elements["research-chat-send"] = { disabled: false };
elements["research-chat-finalize"] = { disabled: false };
elements["research-chat-status"] = { textContent: "" };
elements["research-chat-messages"] = { innerHTML: "" };
state.researchChat.holdingKey = "US|AAA";
const first = createResearchChatSession({ market: "US", symbol: "AAA" });
state.researchChat.holdingKey = "US|BBB";
const second = createResearchChatSession({ market: "US", symbol: "BBB" });
resolveB({ session_id: "session-b", messages: [{role: "user", content: "b"}, {role: "assistant", content: "reply b"}] });
await second;
if (state.researchChat.sessionId !== "session-b") {
  throw new Error("active session did not use latest response: " + state.researchChat.sessionId);
}
resolveA({ session_id: "session-a", messages: [{role: "user", content: "a"}, {role: "assistant", content: "reply a"}] });
await first;
if (state.researchChat.sessionId !== "session-b") {
  throw new Error("stale session overwrote active session: " + state.researchChat.sessionId);
}
if (calls.join(",") !== "AAA,BBB") {
  throw new Error("unexpected call order: " + calls.join(","));
}
const classes = new Set();
elements["research-chat-layer"] = {
  hidden: true,
  classList: {
    add(name) { classes.add(name); },
    remove(name) { classes.delete(name); },
  },
};
elements["research-chat-title"] = { textContent: "" };
elements["research-chat-context-note"] = { textContent: "" };
elements["research-chat-context-list"] = { innerHTML: "" };
elements["research-chat-input"] = { value: "", focus() {} };
state.dashboard = {
  holdings: [
    {
      market: "US",
      symbol: "AAA",
      name: "Available",
      research_view: {
        available: true,
        tradingagents_conclusion: {status: "present", content: "有上下文"},
        user_llm_conclusion: {status: "missing", content: ""},
      },
    },
    {
      market: "US",
      symbol: "CCC",
      name: "Missing",
      research_view: {available: false},
    },
  ],
};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
postDashboardJson = () => new Promise(() => {});
openResearchChat(holdingKey(state.dashboard.holdings[0]));
if (!state.researchChat.busy) {
  throw new Error("available chat should be busy while context request is pending");
}
await openResearchChat(holdingKey(state.dashboard.holdings[1]));
if (state.researchChat.busy) {
  throw new Error("missing context chat should clear busy state");
}
if (!elements["research-chat-send"].disabled) {
  throw new Error("missing context chat should disable send button");
}
if (!String(elements["research-chat-context-note"].textContent).includes("暂无投研上下文")) {
  throw new Error("missing context note should not claim loaded context: " + elements["research-chat-context-note"].textContent);
}
if (!String(elements["research-chat-messages"].innerHTML).includes("暂无投研上下文")) {
  throw new Error("missing context message should explain unavailable context: " + elements["research-chat-messages"].innerHTML);
}
if (state.researchChat.sessionId) {
  throw new Error("missing context chat should clear stale session id: " + state.researchChat.sessionId);
}
if (!String(elements["research-chat-status"].textContent).includes("暂无投研上下文")) {
  throw new Error("missing context status not shown: " + elements["research-chat-status"].textContent);
}
})()
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_research_chat_renders_user_message_before_reply() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
(async () => {
let resolveMessage;
postDashboardJson = () => new Promise((resolve) => { resolveMessage = resolve; });
elements["research-chat-send"] = { disabled: false };
elements["research-chat-finalize"] = { disabled: false };
elements["research-chat-status"] = { textContent: "" };
elements["research-chat-messages"] = { innerHTML: "" };
elements["research-chat-input"] = { value: "为什么要减仓？" };
state.researchChat.sessionId = "session-1";
state.researchChat.busy = false;
state.researchChat.messages = [
  {role: "user", content: "结合我的仓位，我已经做什么动作？"},
  {role: "assistant", content: "建议先减仓。"},
];
state.researchChat.messageCount = 2;

const pending = sendResearchChatMessage();
if (elements["research-chat-input"].value !== "") {
  throw new Error("input should clear immediately");
}
const htmlWhilePending = elements["research-chat-messages"].innerHTML;
if (!htmlWhilePending.includes("为什么要减仓？")) {
  throw new Error("user message did not render before reply: " + htmlWhilePending);
}
if (!htmlWhilePending.includes("LLM 正在处理")) {
  throw new Error("pending assistant message missing: " + htmlWhilePending);
}
if (!elements["research-chat-send"].disabled) {
  throw new Error("send button should be disabled while request is pending");
}
resolveMessage({
  session_id: "session-1",
  messages: [
    {role: "user", content: "结合我的仓位，我已经做什么动作？"},
    {role: "assistant", content: "建议先减仓。"},
    {role: "user", content: "为什么要减仓？"},
    {role: "assistant", content: "因为已达到第一目标价。"},
  ],
});
await pending;
const htmlAfterReply = elements["research-chat-messages"].innerHTML;
if (!htmlAfterReply.includes("因为已达到第一目标价。")) {
  throw new Error("assistant reply did not render after response: " + htmlAfterReply);
}
if (htmlAfterReply.includes("LLM 正在处理")) {
  throw new Error("pending message should be replaced after response: " + htmlAfterReply);
}
if (state.researchChat.messageCount !== 4) {
  throw new Error("persisted message count should update after response: " + state.researchChat.messageCount);
}
})()
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_header_account_tabs_and_summary_helpers() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
state.dashboard = {
  summary: {
    portfolio_value_hkd: "123456.78",
    holding_value_hkd: "200000.00",
    cash_like_value_hkd: "-76543.22",
    holding_weight_hkd: "162.00%",
    holding_count: 5,
  },
  holdings: [
    {
      market: "HK",
      symbol: "00700",
      name: "Tencent",
      brokers: "phillips",
      currency: "HKD",
      total_quantity: "100",
      avg_cost_price: "150.00",
      market_value: "15982.00",
      market_value_hkd: "15982.00",
      portfolio_weight_hkd: "3.25%",
      unrealized_pnl_pct: "2.00%",
    },
    {
      market: "US",
      symbol: "VIXY",
      name: "ProShares VIX Short-Term Futures ETF",
      brokers: "futu;tiger",
      currency: "USD",
      total_quantity: "10",
      avg_cost_price: "12.34",
      market_value: "6250.00",
      market_value_hkd: "49062.50",
      portfolio_weight_hkd: "7.50%",
      unrealized_pnl_pct: "5.00%",
      broker_details: [
        {
          broker: "futu",
          market: "US",
          symbol: "VIXY",
          currency: "USD",
          market_value: "1940.00",
          market_value_hkd: "15132.00",
        },
        {
          broker: "tiger",
          market: "US",
          symbol: "VIXY",
          currency: "USD",
          market_value: "2910.00",
          market_value_hkd: "22698.00",
        },
      ],
      t_signal: {
        schema_version: "open_trader.t_signal.v1",
        run_date: "2026-07-02",
        market: "US",
        symbol: "VIXY",
        futu_symbol: "US.VIXY",
        name: "ProShares VIX Short-Term Futures ETF",
        session_phase: "regular",
        updated_at: "2026-07-02T22:32:00+08:00",
        action: "BUY_T",
        suggested_ratio: "15",
        current_status: "BUY_T 条件满足，等待执行确认。",
        signal_summary_zh: "低吸做T信号成立，确定比例 15%。",
        price: {
          last_price: "48.50",
          day_change_pct: "-1.20",
          vwap: "49.10",
          ma_1m: "48.55",
          ma_5m: "48.85",
          day_low: "48.00",
          day_high: "50.20",
        },
        liquidity: {
          bid: "48.49",
          ask: "48.50",
          spread_pct: "0.02",
          bid_depth: "5000",
          ask_depth: "4700",
          depth_status: "pass",
        },
        technical: {
          rsi_5m: "34",
          volume_ratio_5m: "1.30",
          price_position: "below_vwap_reclaim",
          trend_state: "range_rebound",
        },
        hard_gates: [
          {
            name: "session_phase",
            status: "pass",
            message_zh: "当前处于盘中交易时段。",
          },
        ],
        evidence: [
          {
            name: "vwap_reclaim",
            direction: "buy",
            strength: "medium",
            message_zh: "价格低于 VWAP 后回收，出现低吸做T信号。",
          },
          {
            name: "rsi_low",
            direction: "buy",
            strength: "medium",
            message_zh: "5分钟 RSI 偏低。",
          },
        ],
        timeline: [
          {
            event_at: "2026-07-02T22:32:00+08:00",
            event_type: "signal_created",
            action: "BUY_T",
            suggested_ratio: "15",
            message_zh: "生成 BUY_T 信号，建议比例 15%。",
          },
          {
            event_at: "2026-07-02T22:32:00+08:00",
            event_type: "notification_sent",
            action: "BUY_T",
            suggested_ratio: "15",
            message_zh: "已发送 BUY_T 通知。",
          },
        ],
        notification: {
          should_notify: false,
          notified: true,
          dedupe_key: "2026-07-02|US.VIXY|BUY_T|15",
          last_notified_at: "2026-07-02T22:32:00+08:00",
          last_notified_dedupe_key: "2026-07-02|US.VIXY|BUY_T|15",
          last_attempted_dedupe_key: "2026-07-02|US.VIXY|BUY_T|15",
        },
        status: "ok",
        error: "",
      },
    },
    {
      market: "US",
      symbol: "BND",
      name: "Vanguard Total Bond Market ETF",
      brokers: "tiger",
      currency: "HKD",
      total_quantity: "2",
      avg_cost_price: "50.00",
      market_value: "100.00",
      market_value_hkd: "100.00",
      portfolio_weight_hkd: "2.50%",
      unrealized_pnl_pct: "-1.00%",
    },
    {
      market: "US",
      symbol: "VIXY260821C22000",
      name: "VIXY 260821 22.00C",
      brokers: "futu",
      currency: "USD",
      total_quantity: "1",
      avg_cost_price: "2.10",
      market_value: "168.00",
      market_value_hkd: "300.00",
      portfolio_weight_hkd: "0.50%",
      unrealized_pnl_pct: "-20.00%",
    },
    {
      market: "HK",
      symbol: "HKOPT",
      name: "腾讯 260730 400.00C",
      asset_class: "option",
      brokers: "futu",
      currency: "HKD",
      total_quantity: "1",
      avg_cost_price: "1.00",
      market_value: "200.00",
      market_value_hkd: "200.00",
      portfolio_weight_hkd: "0.40%",
      unrealized_pnl_pct: "1.00%",
    },
  ],
  cash_rows: [
    {
      market: "CASH",
      symbol: "HKD_CASH",
      name: "HKD Cash",
      brokers: "futu;phillips;tiger",
      currency: "HKD",
      market_value_hkd: "90061.99",
    },
    {
      market: "CASH",
      symbol: "USD_CASH",
      name: "USD Cash",
      brokers: "futu;phillips;tiger",
      currency: "USD",
      market_value_hkd: "-200205.54",
    },
  ],
  cash_details: [
    {
      broker: "futu",
      currency: "HKD",
      cash_balance: "-125409.59",
      market_value_hkd: "-125409.59",
    },
    {
      broker: "futu",
      currency: "USD",
      cash_balance: "1435.80",
      market_value_hkd: "11206.24",
    },
    {
      broker: "phillips",
      currency: "HKD",
      cash_balance: "8000.00",
      market_value_hkd: "8000.00",
    },
  ],
  broker_summaries: [
    {
      broker: "futu",
      display_name: "富途",
      holding_value_hkd: "15132.00",
      cash_like_value_hkd: "-114203.35",
      portfolio_value_hkd: "-99071.35",
      holding_count: 1,
      source_status: "real_time",
    },
    {
      broker: "phillips",
      display_name: "辉立",
      portfolio_value_hkd: "8000.00",
      holding_count: 1,
      source_status: "statement",
    },
    {
      broker: "tiger",
      display_name: "老虎",
      portfolio_value_hkd: "22698.00",
      holding_count: 1,
      source_status: "real_time",
    },
  ],
  source_statuses: [
    {
      broker: "futu",
      display_name: "富途",
      status: "real_time",
      updated_at: "2026-06-19T09:30:00+08:00",
    },
    {
      broker: "tiger",
      display_name: "老虎",
      status: "ok",
      display_text: "账户实时同步，行情走富途",
      updated_at: "2026-06-19T09:30:00+08:00",
    },
    {
      broker: "phillips",
      display_name: "辉立",
      status: "statement",
      value: "非实时",
      updated_at: "2026-05",
    },
  ],
};
state.marketFilter = "US";
state.brokerFilter = "futu";
for (const id of ["current-view-value", "current-view-holding-value", "current-view-holding-weight", "current-view-cash-note", "current-view-label"]) {
  elements[id] = {textContent: ""};
}
renderHeaderSummary();
if (elements["current-view-value"].textContent !== "HKD 123,456.78") {
  throw new Error("header total should use the unfiltered payload summary");
}
if (!elements["current-view-label"].textContent.includes("富途") || !elements["current-view-label"].textContent.includes("2 条")) {
  throw new Error("header label should describe the selected broker and market: " + elements["current-view-label"].textContent);
}
const brokerCards = renderBrokerSummaryCards();
if (!brokerCards.includes("富途") || !brokerCards.includes("HKD -99,071.35")) {
  throw new Error("broker card missing expected text: " + brokerCards);
}
if (!brokerCards.includes("老虎") || !brokerCards.includes("账户实时同步，行情走富途")) {
  throw new Error("broker card should distinguish Tiger account data from Futu quotes: " + brokerCards);
}
let sourceList = renderSourceStatusList();
if (!sourceList.includes("辉立") || !sourceList.includes("非实时")) {
  throw new Error("source list missing statement status: " + sourceList);
}
state.quotePayload = {
  status: "failed",
  stale: true,
  diagnostic: { message: "网络中断" },
};
sourceList = renderSourceStatusList();
if (!sourceList.includes("富途") || !sourceList.includes("网络中断")) {
  throw new Error("source list missing quote diagnostic: " + sourceList);
}
state.quotePayload = {
  status: "partial",
  stale: false,
  diagnostic: { message: "缺失 1 个标的行情。" },
};
sourceList = renderSourceStatusList();
if (!sourceList.includes("富途") || !sourceList.includes("缺失 1 个标的行情。")) {
  throw new Error("source list missing partial quote diagnostic: " + sourceList);
}
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) {
        names.forEach((name) => classes.add(name));
      },
      remove(...names) {
        names.forEach((name) => classes.delete(name));
      },
      contains(name) {
        return classes.has(name);
      },
      toggle(name, force) {
        if (force === undefined) {
          classes.has(name) ? classes.delete(name) : classes.add(name);
        } else if (force) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
        return classes.has(name);
      },
    },
    querySelectorAll() {
      return [];
    },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["account-tabs"] = makeElement();
elements["holdings-body"] = makeElement();
state.selectedHoldingKey = "";
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "futu";
state.selectedHoldingKey = accountHoldingKey("futu", state.dashboard.holdings[1], 1);
renderHoldings();
if (!elements["symbol-detail-panel"].classList.contains("hidden")) {
  throw new Error("trading decision should keep bottom symbol detail panel hidden");
}
const initialHoldingHtml = elements["holdings-body"].innerHTML;
if (!initialHoldingHtml.includes(">做T<") || initialHoldingHtml.includes(">凯利<") || initialHoldingHtml.includes(">详情<")) {
  throw new Error("holdings row should expose only the T-signal entry: " + initialHoldingHtml);
}
for (const retired of ['data-detail-mode="decision"', "TradingAgents", "交易决策"]) {
  if (initialHoldingHtml.includes(retired)) {
    throw new Error("retired AI decision UI remains " + retired + ": " + initialHoldingHtml);
  }
}
if (!elements["holdings-body"].innerHTML.includes("t-signal-button-active")) {
  throw new Error("active BUY_T/SELL_T signals should pulse the t signal button: " + elements["holdings-body"].innerHTML);
}
state.dashboard.holdings[1].t_signal.session_phase = "closed";
renderHoldings();
if (elements["holdings-body"].innerHTML.includes("t-signal-button-active")) {
  throw new Error("non-regular t signals should not pulse the t signal button: " + elements["holdings-body"].innerHTML);
}
state.dashboard.holdings[1].t_signal.session_phase = "regular";
renderHoldings();
const renderedHoldings = elements["holdings-body"].innerHTML;
let renderedRowCount = 0;
for (const broker of ["futu", "tiger", "phillips", "eastmoney"]) {
  selectBroker(broker);
  const accountHtml = elements["holdings-body"].innerHTML;
  if (!accountHtml.includes('id="account-' + broker + '"')) throw new Error("missing selected account section " + broker);
  for (const other of ACCOUNT_BROKERS.filter((item) => item !== broker)) {
    if (accountHtml.includes('id="account-' + other + '"')) throw new Error("rendered unselected account section " + other);
  }
  renderedRowCount += (accountHtml.match(/account-holding-row/g) || []).length;
}
state.brokerFilter = "futu";
state.selectedHoldingKey = accountHoldingKey("futu", state.dashboard.holdings[1], 1);
renderHoldings();
if (renderedHoldings.includes("美股正股") || renderedHoldings.includes("美股期权")) {
  throw new Error("account tables should not contain nested market sections: " + renderedHoldings);
}
for (const required of ["成本价", "美元市值", "港元市值", "账户权重", "组合权重", "USD 1,940", "HKD 15,132", "期权关注", "今日暂无趋势报告"]) {
  if (!renderedHoldings.includes(required)) {
    throw new Error("account holdings missing " + required + ": " + renderedHoldings);
  }
}
if (renderedHoldings.includes("<th>策略</th>")) {
  throw new Error("account holdings should not render row strategy column: " + renderedHoldings);
}
if (renderedRowCount !== 6) throw new Error("account tabs should expose six broker rows in total: " + renderedRowCount);
for (const unexpected of ["<td>futu;tiger</td>", "<td>phillips</td>", "<td>futu</td>", "<td>tiger</td>", "<span class=\\"badge\\">"]) {
  if (renderedHoldings.includes(unexpected)) {
    throw new Error("main holdings table should not render broker/action cell " + unexpected + ": " + renderedHoldings);
  }
}
if (renderedHoldings.includes("观察 ·") || renderedHoldings.includes("人工复核 ·")) {
  throw new Error("main holdings table should not render action badges: " + renderedHoldings);
}
for (const required of ["做T信号 ·", "买入做T", "确定比例", "15%", "信号依据", "价格低于 VWAP 后回收", "前置条件", "t-signal-checkmark", "交易时段", "详细信息", "消息 timeline", "已发送 BUY_T 通知。", "已发起提醒 · 2026-07-02T22:32:00+08:00"]) {
  if (!elements["holdings-body"].innerHTML.includes(required)) {
    throw new Error("t signal detail missing " + required + ": " + elements["holdings-body"].innerHTML);
  }
}
for (const unexpected of ["小T", "大T", "状态机", ">session_phase<", "已提醒 ·"]) {
  if (elements["holdings-body"].innerHTML.includes(unexpected)) {
    throw new Error("t signal detail should not render ambiguous wording " + unexpected);
  }
}
state.dashboard.holdings.push({
  market: "JP",
  symbol: "7203",
  name: "Toyota",
  brokers: "phillips",
  currency: "JPY",
  total_quantity: "1",
  avg_cost_price: "3000",
  market_value: "300.00",
  market_value_hkd: "300.00",
  portfolio_weight_hkd: "1.50%",
  unrealized_pnl_pct: "0.00%",
});
state.selectedHoldingKey = "";
selectBroker("phillips");
const renderedWithOther = elements["holdings-body"].innerHTML;
if (!renderedWithOther.includes(">JP<") || !renderedWithOther.includes(">Toyota<") || !renderedWithOther.includes("HKD 300")) {
  throw new Error("non-standard markets should remain ordinary account rows: " + renderedWithOther);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def obsolete_dashboard_renders_backtest_entry_and_detail_only_after_selection() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force === undefined) {
          classes.has(name) ? classes.delete(name) : classes.add(name);
        } else if (force) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
        return classes.has(name);
      },
    },
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingKey = "";
state.selectedHoldingDetail = "decision";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: {
      available: true,
      run_id: "2026-06-18-US-VIXY-trading-plan",
      run_date: "2026-06-18",
      market: "US",
      symbol: "VIXY",
      strategy: "trading_plan",
      adapter: "backtrader",
      metrics: {
        total_return_pct: "1.17",
        win_rate_pct: "50.00",
        max_drawdown_pct: "-3.40",
        trade_count: "2",
      },
      trades: [
        {
          date: "2026-06-19",
          side: "BUY",
          price: "40.2000",
          quantity: "621",
          fees: "24.96",
          cash_after: "75010.84",
          reason: "entry_zone",
        },
        {
          date: "2026-06-20",
          side: "SELL",
          price: "47.9760",
          quantity: "621",
          fees: "29.79",
          cash_after: "104774.15",
          reason: "target_1",
        },
      ],
      equity_curve: [
        { date: "2026-06-18", close: "45.0000", equity: "100000.00", drawdown_pct: "0.00" },
        { date: "2026-06-19", close: "42.0000", equity: "101092.84", drawdown_pct: "0.00" },
        { date: "2026-06-20", close: "48.0000", equity: "104774.15", drawdown_pct: "0.00" },
      ],
      report_path: "reports/backtests/2026-06-18-US-VIXY-trading-plan.md",
      trades_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/trades.csv",
      equity_curve_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/equity_curve.csv",
      status: "ok",
      error: "",
    },
  }],
};
renderHoldings();
let html = elements["holdings-body"].innerHTML;
if (!html.includes(">查看回测<") || !html.includes('data-detail-mode="backtest"')) {
  throw new Error("holding row should expose backtest entry: " + html);
}
if (html.includes("总收益") || html.includes("1.17%") || html.includes("回测详情 ·")) {
  throw new Error("main holdings table should not show backtest metrics before selection: " + html);
}
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
state.selectedHoldingDetail = "backtest";
renderHoldings();
html = elements["holdings-body"].innerHTML;
for (const required of ["回测详情 · US.VIXY", "Backtrader", "总收益", "1.17%", "胜率", "50.00%", "最大回撤", "-3.40%", "交易次数", "2", "权益曲线", "价格走势与买卖点", "交易明细", "<svg", "BUY", "SELL", "entry_zone", "target_1", "reports/backtests/2026-06-18-US-VIXY-trading-plan.md"]) {
  if (!html.includes(required)) {
    throw new Error("backtest detail missing " + required + ": " + html);
  }
}
if ((html.match(/回测准备/g) || []).length !== 1) {
  throw new Error("backtest readiness should render once: " + html);
}
console.log(html);
"""
    )

    assert "回测详情 · US.VIXY" in html


def obsolete_dashboard_backtest_detail_runs_from_button_and_refreshes() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    disabled: false,
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force === undefined) {
          classes.has(name) ? classes.delete(name) : classes.add(name);
        } else if (force) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
        return classes.has(name);
      },
    },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
}
(async () => {
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
let html = elements["holdings-body"].innerHTML;
if (!html.includes(">运行回测<") || !html.includes('data-run-backtest="US:VIXY:ProShares VIX Short-Term Futures ETF:0"')) {
  throw new Error("backtest detail should expose run button: " + html);
}
let posted = null;
let loadCount = 0;
globalThis.fetch = async (url, options) => {
  posted = { url, body: JSON.parse(options.body) };
  return {
    ok: true,
    json: async () => ({
      status: "ok",
      backtest: {
        available: true,
        run_id: "2026-06-18-US-VIXY-trading-plan",
        metrics: { total_return_pct: "1.17" },
      },
    }),
  };
};
loadDashboard = async () => {
  loadCount += 1;
  state.dashboard.holdings[0].backtest = {
    available: true,
    run_id: "2026-06-18-US-VIXY-trading-plan",
    run_date: "2026-06-18",
    market: "US",
    symbol: "VIXY",
    strategy: "trading_plan",
    adapter: "backtrader",
    metrics: {
      total_return_pct: "1.17",
      win_rate_pct: "50.00",
      max_drawdown_pct: "-3.40",
      trade_count: "2",
    },
    report_path: "reports/backtests/2026-06-18-US-VIXY-trading-plan.md",
    trades_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/trades.csv",
    equity_curve_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/equity_curve.csv",
    metrics_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/metrics.json",
  };
};
await runBacktestForHolding(state.selectedHoldingKey);
if (!posted || posted.url !== "/api/backtests/run") {
  throw new Error("backtest run should post to API: " + JSON.stringify(posted));
}
if (posted.body.market !== "US" || posted.body.symbol !== "VIXY" || posted.body.initial_position_quantity !== "10") {
  throw new Error("backtest run body should identify holding: " + JSON.stringify(posted.body));
}
if (loadCount !== 1) {
  throw new Error("backtest run should reload dashboard once: " + loadCount);
}
html = elements["holdings-body"].innerHTML;
if (!html.includes("回测详情 · US.VIXY") || !html.includes("1.17%")) {
  throw new Error("backtest detail should refresh after run: " + html);
}
console.log(html);
})();
"""
    )

    assert "回测详情 · US.VIXY" in html


def obsolete_dashboard_backtest_detail_renders_readiness_gaps() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force === undefined) {
          classes.has(name) ? classes.delete(name) : classes.add(name);
        } else if (force) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
        return classes.has(name);
      },
    },
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
    backtest_readiness: {
      available: false,
      status: "missing_fields",
      run_date: "2026-06-18",
      plan_path: "data/latest/US/trading_plan.csv",
      prices_path: "data/prices/US/VIXY.csv",
      prices_missing: true,
      missing_fields: ["entry_zone_high", "max_weight"],
      error: "missing backtest field(s): entry_zone_high, max_weight",
    },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
const html = elements["holdings-body"].innerHTML;
for (const required of ["回测准备", "缺少计划字段", "entry_zone_high", "max_weight", "data/latest/US/trading_plan.csv", "data/prices/US/VIXY.csv"]) {
  if (!html.includes(required)) {
    throw new Error("backtest readiness missing " + required + ": " + html);
  }
}
console.log(html);
"""
    )

    assert "缺少计划字段" in html


def obsolete_dashboard_backtest_detail_renders_unsupported_strategy() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force === undefined) {
          classes.has(name) ? classes.delete(name) : classes.add(name);
        } else if (force) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
        return classes.has(name);
      },
    },
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
    backtest_readiness: {
      available: false,
      status: "unsupported_strategy",
      run_date: "2026-06-18",
      plan_path: "data/latest/US/trading_plan.csv",
      prices_path: "data/prices/US/VIXY.csv",
      prices_missing: false,
      missing_fields: [],
      error: "unsupported backtest strategy rating",
    },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
const html = elements["holdings-body"].innerHTML;
for (const required of ["回测准备", "暂不支持该策略", "第一版回测支持买入、加仓和减仓类交易计划；其他策略暂不支持。"]) {
  if (!html.includes(required)) {
    throw new Error("unsupported strategy readiness missing " + required + ": " + html);
  }
}
if (html.includes(">运行回测<")) {
  throw new Error("unsupported strategy should not expose run button: " + html);
}
console.log(html);
"""
    )

    assert "暂不支持该策略" in html


def obsolete_dashboard_backtest_detail_hides_manual_missing_price_fetch_button() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force === undefined) {
          classes.has(name) ? classes.delete(name) : classes.add(name);
        } else if (force) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
        return classes.has(name);
      },
    },
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
    backtest_readiness: {
      available: false,
      status: "missing_fields",
      run_date: "2026-06-18",
      plan_path: "data/latest/US/trading_plan.csv",
      prices_path: "data/prices/US/VIXY.csv",
      prices_missing: true,
      missing_fields: ["max_weight"],
      error: "missing backtest field(s): max_weight",
    },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
const html = elements["holdings-body"].innerHTML;
if (!html.includes("缺少计划字段") || !html.includes("missing backtest field(s): max_weight")) {
  throw new Error("missing price readiness should still show diagnostic state: " + html);
}
if (html.includes(">拉取价格数据<") || html.includes("data-fetch-backtest-prices")) {
  throw new Error("missing price readiness should not expose manual fetch button: " + html);
}
console.log(html);
"""
    )

    assert "缺少计划字段" in html


def test_build_dashboard_payload_returns_json_safe_state(tmp_path) -> None:
    from open_trader.dashboard_web import build_dashboard_payload

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    payload = build_dashboard_payload(config)

    json.dumps(payload)
    assert payload["summary"]["holding_count"] == 1
    assert len(payload["holdings"]) == 1
    assert payload["holdings"][0]["symbol"] == "VIXY"


def test_build_quotes_payload_returns_service_refresh() -> None:
    from open_trader.dashboard_web import build_quotes_payload

    service = FakeQuoteService(quote_result())
    account_sync = FakeAccountSyncService({"status": "ok", "interval_seconds": 60})

    payload = build_quotes_payload(service, account_sync_service=account_sync)

    json.dumps(payload)
    assert service.refresh_count == 1
    assert account_sync.refresh_count == 1
    assert payload["status"] == "ok"
    assert payload["account_sync"]["status"] == "ok"
    assert payload["account_sync"]["interval_seconds"] == 60
    assert list(payload["quotes"]) == ["US.MSFT"]
    assert payload["quotes"]["US.MSFT"]["last_price"] == "500"


def test_dashboard_server_runs_backtest_api_and_refreshes_payload(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "target_1": "48",
            "stop_loss": "36",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    write_csv(
        config.data_dir / "prices" / "US" / "VIXY.csv",
        ["date", "open", "high", "low", "close"],
        [
            {"date": "2026-06-18", "open": "45", "high": "46", "low": "44", "close": "45"},
            {"date": "2026-06-19", "open": "41", "high": "43", "low": "40", "close": "42"},
            {"date": "2026-06-20", "open": "47", "high": "49", "low": "46", "close": "48"},
        ],
    )
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        payload = post_json(
            f"http://{host}:{port}/api/backtests/run",
            {"market": "US", "symbol": "VIXY"},
        )
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert payload["status"] == "ok"
    assert payload["backtest"]["run_id"] == "2026-06-18-US-VIXY-trading-plan"
    assert payload["backtest"]["adapter"] == "backtrader"
    assert payload["backtest"]["metrics"]["trade_count"] == "2"
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert "backtest" not in vixy
    assert "backtest_readiness" not in vixy


def test_dashboard_server_runs_sell_side_backtest_from_current_position(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Underweight",
            "entry_zone_low": "",
            "entry_zone_high": "",
            "target_1": "40",
            "stop_loss": "",
            "max_weight": "",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    write_csv(
        config.data_dir / "prices" / "US" / "VIXY.csv",
        ["date", "open", "high", "low", "close"],
        [
            {"date": "2026-06-18", "open": "45", "high": "46", "low": "44", "close": "45"},
            {"date": "2026-06-19", "open": "41", "high": "43", "low": "39", "close": "40"},
        ],
    )
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        payload = post_json(
            f"http://{host}:{port}/api/backtests/run",
            {"market": "US", "symbol": "VIXY", "initial_position_quantity": "10"},
        )
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert payload["status"] == "ok"
    assert payload["backtest"]["metrics"]["trade_count"] == "1"
    assert payload["backtest"]["trades"][0]["side"] == "SELL"
    assert payload["backtest"]["trades"][0]["reason"] == "target_1"
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert "backtest" not in vixy
    assert "backtest_readiness" not in vixy


def obsolete_dashboard_server_fetches_backtest_prices_api(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    provider = FakeBacktestPriceProvider()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        backtest_price_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        payload = post_json(
            f"http://{host}:{port}/api/backtests/prices",
            {"market": "US", "symbol": "VIXY", "end": "2026-07-10"},
        )
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert payload["status"] == "ok"
    assert payload["records"] == 1
    assert payload["prices_path"] == str(config.data_dir / "prices" / "US" / "VIXY.csv")
    assert payload["backtest_readiness"]["status"] == "ready"
    assert provider.requests == [
        {
            "futu_symbol": "US.VIXY",
            "start": "2026-06-18",
            "end": "2026-07-10",
        }
    ]
    assert (config.data_dir / "prices" / "US" / "VIXY.csv").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "date,open,high,low,close",
        "2026-06-19,41.0,43.0,40.0,42.0",
    ]
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"]["status"] == "ready"


def obsolete_dashboard_server_auto_fetches_missing_backtest_prices_on_dashboard_load(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    provider = FakeBacktestPriceProvider()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        backtest_price_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert provider.requests == [
        {
            "futu_symbol": "US.VIXY",
            "start": "2026-06-18",
            "end": date.today().isoformat(),
        }
    ]
    assert (config.data_dir / "prices" / "US" / "VIXY.csv").is_file()
    assert dashboard_payload["backtest_price_sync"] == {
        "status": "ok",
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "errors": [],
    }
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"]["status"] == "ready"


def obsolete_dashboard_server_keeps_payload_when_auto_backtest_price_fetch_fails(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    provider = RaisingBacktestPriceProvider()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        backtest_price_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert provider.requests == [
        {
            "futu_symbol": "US.VIXY",
            "start": "2026-06-18",
            "end": date.today().isoformat(),
        }
    ]
    assert not (config.data_dir / "prices" / "US" / "VIXY.csv").exists()
    assert dashboard_payload["backtest_price_sync"] == {
        "status": "failed",
        "attempted": 1,
        "succeeded": 0,
        "failed": 1,
        "errors": [
            {
                "market": "US",
                "symbol": "VIXY",
                "message": "kline unavailable",
            }
        ],
    }
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"]["status"] == "missing_prices"


def test_dashboard_server_serves_dashboard_and_quotes_api(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    quote_service = FakeQuoteService(quote_result())
    account_sync = FakeAccountSyncService({"status": "skipped", "interval_seconds": 60})
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=quote_service,
        account_sync_service=account_sync,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
        quotes_payload = read_json(f"http://{host}:{port}/api/quotes")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert dashboard_payload["summary"]["holding_count"] == 1
    assert dashboard_payload["holdings"][0]["symbol"] == "VIXY"
    assert quotes_payload["quotes"]["US.MSFT"]["last_price"] == "500"
    assert quotes_payload["account_sync"]["status"] == "skipped"
    assert quote_service.refresh_count == 1
    assert account_sync.refresh_count == 1


def test_dashboard_http_loads_only_requested_simulated_account(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    calls: list[str] = []
    server = create_dashboard_server(
        config,
        "127.0.0.1",
        0,
        quote_service=FakeQuoteService(quote_result()),
        trend_simulate_position_service=FakeTrendSimulatePositionService(calls),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        read_json(f"http://{host}:{port}/api/dashboard")
        read_json(f"http://{host}:{port}/api/quotes")
        assert calls == []
        status, _, _ = read_text_error(
            f"http://{host}:{port}/api/trend-simulate-positions/tiger/positions"
        )
        assert status == 404
        assert calls == []

        payload = read_json(
            f"http://{host}:{port}/api/trend-simulate-positions/tiger"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert calls == ["tiger"]
    assert payload["broker"] == "tiger"


def test_dashboard_http_serves_report_history_and_exact_artifact(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server
    from open_trader.trend_review import _report_hash

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    payload = write_trend_history_report(
        config.reports_dir,
        "2026-07-16.json",
        execution_date="2026-07-17",
        generated_at="2026-07-17T09:00:00+08:00",
    )
    event = (
        config.data_dir
        / "trend_review/ledgers/US/actions/2026-07-17/action-key/event.json"
    )
    event.parent.mkdir(parents=True)
    event.write_text(
        json.dumps({
            "report_sha256": _report_hash(payload),
            "symbol": "VIXY",
            "side": "buy",
            "status": "missed",
        }),
        encoding="utf-8",
    )
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        history = read_json(
            f"http://{host}:{port}/api/trend-reports/tiger/history"
        )
        report = read_json(
            f"http://{host}:{port}/api/trend-reports/tiger/history/2026-07-16.json"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert [row["artifact"] for row in history] == ["2026-07-16.json"]
    assert report["report_date"] == "2026-07-17"
    assert report["buy_actions"][0]["execution"]["status"] == "missed"


def test_dashboard_http_report_history_enforces_read_only_route_errors(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_trend_history_report(
        config.reports_dir,
        "wrong-market.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
        market="HK",
    )
    (config.reports_dir / "trend_us_tiger" / "broken.json").write_text(
        "{broken", encoding="utf-8"
    )
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    root = f"http://{host}:{port}/api/trend-reports"
    try:
        assert read_error_json(f"{root}/unknown/history")[0] == 400
        assert read_error_json(
            f"{root}/tiger/history/..%2Fsecret.json"
        )[0] == 400
        assert read_error_json(f"{root}/tiger/history/missing.json")[0] == 404
        assert read_error_json(
            f"{root}/tiger/history/wrong-market.json"
        )[0] == 400
        history = read_json(f"{root}/tiger/history")
        method_statuses = []
        for method in ("POST", "PUT", "DELETE"):
            request = urllib.request.Request(
                f"{root}/tiger/history", data=b"{}", method=method
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            method_statuses.append(error.value.code)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert history == [
        {
            "available": False,
            "artifact": "wrong-market.json",
            "status_text": "报告不可读取",
        },
        {
            "available": False,
            "artifact": "broken.json",
            "status_text": "报告不可读取",
        },
    ]
    assert all(status != 200 for status in method_statuses)


def test_dashboard_http_rejects_unknown_simulated_broker(tmp_path) -> None:
    from open_trader.dashboard import DETAIL_FX_TO_HKD
    from open_trader.dashboard_web import create_dashboard_server
    from open_trader.trend_simulate_positions import TrendSimulatePositionService

    config = dashboard_config(tmp_path)
    server = create_dashboard_server(
        config,
        "127.0.0.1",
        0,
        quote_service=FakeQuoteService(quote_result()),
        trend_simulate_position_service=TrendSimulatePositionService(
            host=config.futu_host,
            port=config.futu_port,
            account_ids={},
            fx_to_hkd=DETAIL_FX_TO_HKD,
            data_dir=config.data_dir,
            reports_dir=config.reports_dir,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        status, _, payload = read_error_json(
            f"http://{host}:{port}/api/trend-simulate-positions/futu"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 400
    assert payload["message"] == "unsupported trend simulate broker: futu"


def test_serve_dashboard_configures_simulate_accounts_once(
    tmp_path, monkeypatch, capsys
) -> None:
    import open_trader.dashboard_web as dashboard_web
    from open_trader.dashboard import DETAIL_FX_TO_HKD

    created: list[dict[str, object]] = []
    server_kwargs: dict[str, object] = {}

    class FakeTrendSimulatePositionServiceFactory:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

    class FakeServer:
        server_address = ("127.0.0.1", 8765)

        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    def fake_create_dashboard_server(**kwargs: object) -> FakeServer:
        server_kwargs.update(kwargs)
        return FakeServer()

    monkeypatch.setattr(
        dashboard_web,
        "TrendSimulatePositionService",
        FakeTrendSimulatePositionServiceFactory,
        raising=False,
    )
    monkeypatch.setattr(
        dashboard_web,
        "DashboardAccountSyncService",
        lambda **_: type("FakeAccountSync", (), {"interval_seconds": 60})(),
    )
    monkeypatch.setattr(
        dashboard_web, "create_dashboard_server", fake_create_dashboard_server
    )
    config = dashboard_config(
        tmp_path,
        trend_review_cn_simulate_acc_id=101,
        trend_review_us_simulate_acc_id=102,
        trend_review_hk_simulate_acc_id=103,
    )

    dashboard_web.serve_dashboard(config, host="127.0.0.1", port=0)

    sha = subprocess.check_output(
        ["git", "-C", str(Path.cwd()), "rev-parse", "HEAD"], text=True
    ).strip()
    output = capsys.readouterr().out
    assert f'"pid": {os.getpid()}' in output
    assert f'"git_sha": "{sha}"' in output
    assert len(created) == 1
    assert created[0]["account_ids"] == {
        "eastmoney": 101,
        "tiger": 102,
        "phillips": 103,
    }
    assert created[0]["fx_to_hkd"] == DETAIL_FX_TO_HKD
    assert server_kwargs["trend_simulate_position_service"].__class__ is (
        FakeTrendSimulatePositionServiceFactory
    )


def test_dashboard_server_imports_loopback_pdf_statement(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    importer = FakeStatementImportService()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        statement_import_service=importer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        payload = post_pdf(
            f"http://{host}:{port}/api/statements/phillips",
            b"%PDF-1.7\nstatement",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert payload["statement_date"] == "2026-07-10"
    assert importer.calls == [("phillips", b"%PDF-1.7\nstatement")]


def test_dashboard_server_returns_statement_parse_failure_reason(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    class FailingStatementImportService:
        def import_pdf(self, broker: str, body: bytes) -> dict[str, Any]:
            raise ValueError("辉立成交表格式无法识别")

    server = create_dashboard_server(
        config=dashboard_config(tmp_path),
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        statement_import_service=FailingStatementImportService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        status, payload = post_pdf_error(
            f"http://{host}:{port}/api/statements/phillips",
            b"%PDF-1.7\nbroken",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 400
    assert payload["message"] == "辉立成交表格式无法识别"


@pytest.mark.parametrize(
    ("content_type", "body", "message"),
    [
        ("application/json", b"%PDF-1.7", "请求正文必须是 PDF"),
        ("application/pdf", b"not a pdf", "请求正文必须是有效的 PDF"),
    ],
)
def test_dashboard_server_rejects_invalid_statement_body(
    tmp_path,
    content_type: str,
    body: bytes,
    message: str,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    server = create_dashboard_server(
        config=dashboard_config(tmp_path),
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        statement_import_service=FakeStatementImportService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        status, payload = post_pdf_error(
            f"http://{host}:{port}/api/statements/phillips",
            body,
            content_type=content_type,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 400
    assert payload["message"] == message


def test_dashboard_server_rejects_statement_larger_than_twenty_mib(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    server = create_dashboard_server(
        config=dashboard_config(tmp_path),
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        statement_import_service=FakeStatementImportService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.putrequest("POST", "/api/statements/phillips")
        connection.putheader("Content-Type", "application/pdf")
        connection.putheader("Content-Length", str(20 * 1024 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 413
    assert payload["message"] == "PDF 不能超过 20 MiB"


@pytest.mark.parametrize(
    ("address", "allowed"),
    [("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True), ("192.0.2.1", False)],
)
def test_statement_upload_loopback_policy(address: str, allowed: bool) -> None:
    from open_trader.dashboard_web import _is_loopback_address

    assert _is_loopback_address(address) is allowed


def test_dashboard_server_serves_research_chat_apis(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    chat_service = FakeResearchChatService()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        research_chat_service=chat_service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"
        session = post_json(
            f"{base}/api/research-chat/sessions",
            {"market": "US", "symbol": "VIXY"},
        )
        loaded = read_json(f"{base}/api/research-chat/sessions/{session['session_id']}")
        message_payload = post_json(
            f"{base}/api/research-chat/sessions/{session['session_id']}/messages",
            {"content": "请解释风险。"},
        )
        finalize_payload = post_json(
            f"{base}/api/research-chat/sessions/{session['session_id']}/finalize",
            {},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert session["session_id"] == "20260620T103000-US-VIXY"
    assert loaded["session_id"] == "20260620T103000-US-VIXY"
    assert message_payload["messages"][1]["content"] == "assistant reply"
    assert finalize_payload["conclusion"]["content"] == "确认减仓 100 股。"
    assert chat_service.created == [{"market": "US", "symbol": "VIXY"}]
    assert chat_service.messages == [
        {"session_id": "20260620T103000-US-VIXY", "content": "请解释风险。"}
    ]
    assert chat_service.finalized == ["20260620T103000-US-VIXY"]


@pytest.mark.parametrize(
    ("body", "error_type", "expected_status", "expected_message"),
    [
        (b"", "ResearchChatError", 500, "market and symbol are required"),
        (b"{bad json", "ValueError", 400, "请求正文必须是有效的 JSON 对象"),
        (b'["not", "object"]', "ValueError", 400, "请求正文必须是有效的 JSON 对象"),
        (b'"not object"', "ValueError", 400, "请求正文必须是有效的 JSON 对象"),
    ],
)
def test_dashboard_server_returns_json_error_for_bad_research_chat_create_body(
    tmp_path,
    body: bytes,
    error_type: str,
    expected_status: int,
    expected_message: str,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        research_chat_service=FakeResearchChatService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"
        status, content_type, payload = post_error_json(
            f"{base}/api/research-chat/sessions",
            body,
        )
        dashboard_payload = read_json(f"{base}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == expected_status
    assert content_type == "application/json; charset=utf-8"
    assert payload["status"] == "error"
    assert payload["error_type"] == error_type
    assert payload["message"] == expected_message
    assert dashboard_payload["summary"]["holding_count"] == 1


def test_dashboard_server_returns_404_for_invalid_research_chat_get_subroute(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        research_chat_service=FakeResearchChatService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, body = read_text_error(
            f"http://{host}:{port}/api/research-chat/sessions/id/messages"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 404
    assert content_type == "text/plain; charset=utf-8"
    assert body == "not found"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/research-chat/sessions//messages", b'{"content": "hello"}'),
        ("/api/research-chat/sessions//finalize", b"{}"),
    ],
)
def test_dashboard_server_returns_404_for_empty_session_research_chat_post_routes(
    tmp_path,
    path: str,
    body: bytes,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    chat_service = FakeResearchChatService()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        research_chat_service=chat_service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, response_body = post_text_error(
            f"http://{host}:{port}{path}",
            body,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 404
    assert content_type == "text/plain; charset=utf-8"
    assert response_body == "not found"
    assert chat_service.messages == []
    assert chat_service.finalized == []


def test_dashboard_server_returns_json_500_when_research_chat_service_raises(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        research_chat_service=RaisingResearchChatService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, payload = read_error_json(
            f"http://{host}:{port}/api/research-chat/sessions/boom"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 500
    assert content_type == "application/json; charset=utf-8"
    assert payload == {
        "status": "error",
        "error_type": "RuntimeError",
        "message": "chat boom: boom",
    }


def test_dashboard_server_returns_json_500_when_quotes_refresh_raises(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=RaisingQuoteService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, payload = read_error_json(
            f"http://{host}:{port}/api/quotes"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 500
    assert content_type == "application/json; charset=utf-8"
    assert payload == {
        "status": "error",
        "error_type": "RuntimeError",
        "message": "boom",
    }


def test_dashboard_server_returns_json_500_when_dashboard_payload_raises(
    tmp_path,
    monkeypatch,
) -> None:
    import open_trader.dashboard_web as dashboard_web

    def raise_runtime_error(config, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("dashboard boom")

    monkeypatch.setattr(
        dashboard_web,
        "build_dashboard_payload",
        raise_runtime_error,
    )
    config = dashboard_config(tmp_path)
    server = dashboard_web.create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, payload = read_error_json(
            f"http://{host}:{port}/api/dashboard"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 500
    assert content_type == "application/json; charset=utf-8"
    assert payload == {
        "status": "error",
        "error_type": "RuntimeError",
        "message": "dashboard boom",
    }


def test_dashboard_server_keeps_unrelated_file_not_found_as_json_500(
    tmp_path, monkeypatch,
) -> None:
    import open_trader.dashboard_web as dashboard_web

    def raise_file_not_found(config, **kwargs: Any) -> dict[str, Any]:
        raise FileNotFoundError("dashboard source missing")

    monkeypatch.setattr(
        dashboard_web,
        "build_dashboard_payload",
        raise_file_not_found,
    )
    config = dashboard_config(tmp_path)
    server = dashboard_web.create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, payload = read_error_json(
            f"http://{host}:{port}/api/dashboard"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 500
    assert content_type == "application/json; charset=utf-8"
    assert payload == {
        "status": "error",
        "error_type": "FileNotFoundError",
        "message": "dashboard source missing",
    }


def test_dashboard_server_serves_static_routes_when_files_exist(
    tmp_path,
    monkeypatch,
) -> None:
    import open_trader.dashboard_web as dashboard_web

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<main>dashboard</main>", encoding="utf-8")
    (static_dir / "dashboard.css").write_text("body{}", encoding="utf-8")
    (static_dir / "dashboard.js").write_text("console.log('ok');", encoding="utf-8")
    monkeypatch.setattr(dashboard_web, "STATIC_DIR", static_dir)

    config = dashboard_config(tmp_path)
    server = dashboard_web.create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/html; charset=utf-8"
            assert response.read().decode("utf-8") == "<main>dashboard</main>"
        with urllib.request.urlopen(
            f"http://{host}:{port}/static/dashboard.css",
            timeout=5,
        ) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/css; charset=utf-8"
            assert response.read().decode("utf-8") == "body{}"
        with urllib.request.urlopen(
            f"http://{host}:{port}/static/dashboard.js",
            timeout=5,
        ) as response:
            assert response.status == 200
            assert (
                response.headers["Content-Type"]
                == "application/javascript; charset=utf-8"
            )
            assert response.read().decode("utf-8") == "console.log('ok');"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
