from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .a_share_trend import valid_frozen_report_contract
from .account_api import check_account_api_parity
from .account_sync_state import DASHBOARD_POSITION_FIELDS
from .dashboard import (
    SHANGHAI,
    _is_dashboard_holding,
    _project_trend_actions,
    _project_trend_money_fields,
    _project_trend_strength_fields,
    _read_csv_rows,
    _valid_partial_trend_action,
)
from .daily_premarket import _optional_positive_tm_id, _read_env_file
from .futu_symbols import to_futu_symbol
from .kelly_order_execution import FutuSimulateOrderExecutionClient
from .parsers.phillips import PhillipsStatementParser
from .trend_simulate_positions import (
    TREND_SIMULATE_BROKERS,
    _action_events,
    _reports_by_hash,
)
from .trend_review import _protection_event_identity, _report_hash
from .strategy_drawdown import valid_strategy_parameter_audit_identity


SESSION_LABELS = ("夜盘", "盘前", "盘中", "盘后")
SESSION_KEYS = {"overnight", "pre_market", "regular", "after_hours"}
CONTROLLER_DOM_FIELDS = {
    "quantity": "data-quantity",
    "cost_price": "data-cost-price",
    "last_price": "data-last-price",
    "price_kind": "data-price-kind",
    "market_value_usd": "data-market-value-usd",
    "market_value_hkd": "data-market-value-hkd",
    "account_weight_hkd": "data-account-weight-hkd",
    "portfolio_weight_hkd": "data-portfolio-weight-hkd",
    "unrealized_pnl": "data-unrealized-pnl",
    "unrealized_pnl_pct": "data-unrealized-pnl-pct",
}

ACCOUNT_BROKERS = ("futu", "tiger", "phillips", "eastmoney")
TREND_REPORT_BROKERS = ("tiger", "phillips", "eastmoney")
TREND_REPORT_DIRECTORIES = {
    "tiger": "trend_us_tiger",
    "phillips": "trend_hk_phillips",
    "eastmoney": "trend_a_share",
}
TREND_SIMULATE_MARKETS = {
    broker: market for broker, (market, _currency) in TREND_SIMULATE_BROKERS.items()
}
TREND_ACCEPTED_STRATEGY_VERSIONS = {
    "CN": frozenset({"v4", "v6", "v7", "v8", "v9", "v10", "v11", "v12"}),
    "US": frozenset({"v4", "v5", "v6", "v7", "v8", "v9", "v10"}),
    "HK": frozenset({"v4", "v5", "v6", "v7", "v8", "v9", "v10"}),
}
ACCOUNT_VIEW_LABELS = {
    broker: ("真实持仓", "模拟盘持仓", "趋势报告")
    for broker in ("tiger", "phillips", "eastmoney")
}
SIMULATE_POSITIONS_READY_EXPRESSION = """
({broker, expected}) => {
  const panel = document.querySelector(`#account-${broker}-view-panel`);
  const tab = document.querySelector(`#account-${broker}-view-simulate`);
  if (!panel || tab?.getAttribute("aria-selected") !== "true") return false;
  if (document.activeElement !== tab) return false;
  if (panel.textContent.includes("模拟盘持仓加载中")) return false;
  return expected === null
    || panel.querySelectorAll(".account-holding-row").length === expected;
}
"""
SIMULATE_POSITIONS_READY_TIMEOUT_MS = 30_000
DASHBOARD_API_TIMEOUT_SECONDS = 30
ACCOUNT_SNAPSHOT_PATH = "/api/v1/account/snapshot"
ACCOUNT_POLL_PROOF_WAIT_MS = 10_100
WARM_LEDGER_TOKENS = {
    "--bg": "#F7F5F1",
    "--surface": "#FFFEFA",
    "--surface-soft": "#F2EEE7",
    "--text": "#201D18",
    "--muted": "#746E64",
    "--accent": "#8B5E34",
    "--line": "#D8D2C8",
    "--primary": "#24211D",
    "--danger": "#B42318",
    "--success": "#2F855A",
}
ACCEPTANCE_SCREENSHOT_DIR = Path("/tmp/open_trader_dashboard_acceptance")
ACCEPTANCE_BROWSER_VIEWPORTS = (
    ("wide_desktop", {"width": 1920, "height": 1080}),
    ("desktop", {"width": 1440, "height": 1000}),
    ("tablet", {"width": 760, "height": 1000}),
    ("mobile", {"width": 375, "height": 844}),
)
ACCEPTANCE_SCREENSHOT_NAMES = (
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
TREND_REVIEW_METRIC_SPECS = (
    ("period_net_return", "期间净收益率", True),
    ("market_excess_return", "相对市场超额收益", True),
    ("max_drawdown", "最大回撤", True),
    ("calmar", "卡玛比率", False),
    ("sharpe", "夏普比率", False),
)
TREND_REVIEW_COMPARISONS = (
    ("discipline", "纪律模拟", "纪律模拟与市场"),
    ("actual", "实际执行", "实际执行与市场"),
)
TREND_REASON_LABELS = {
    "protection_line_already_triggered": "活动保护线已触发",
    "danger_signal": "危险信号触发",
    "left_trend_right_side": "右侧趋势已结束",
    "holding_signal_unknown": "趋势信号不完整",
    "holding_trend_excluded": "已排除趋势查询",
    "holding_kline_unavailable": "持仓日线数据不可用",
    "holding_lot_size_unavailable": "持仓整手信息不可用",
    "trend_intact": "趋势保持完好",
    "temperature_changed_to_flat": "趋势温度转平",
    "overheat_take_profit": "沸腾/开香槟过热止盈",
    "a_share_only": "仅限 A 股股票",
    "temperature_missing": "个股趋势温度缺失",
    "temperature_transition_not_entry": "不是温转热或温转沸",
    "filter_price_missing": "筛选价缺失",
    "filter_price_above_200": "筛选价高于 200 元",
    "strength_missing": "趋势强度缺失",
    "strength_below_95": "趋势强度低于 95",
    "industry_id_missing": "行业 ID 缺失",
    "industry_temperature_missing": "行业温度缺失",
    "industry_temperature_not_hot": "行业温度未达到要求",
    "phase_missing": "趋势节气缺失",
    "phase_after_summer_solstice": "趋势节气晚于夏至",
    "market_cap_missing": "市值缺失",
    "market_cap_below_100": "市值低于 100 亿元",
    "amount_missing": "日成交额缺失",
    "amount_below_2": "日成交额不足 2 亿元",
    "right_side_days_missing": "右侧天数缺失",
    "right_side_not_true": "尚未进入右侧趋势",
    "strength_not_above_90": "趋势强度未超过 90",
    "right_side_days_not_below_10": "进入右侧趋势已满 10 天",
    "not_tradable": "当前不可交易",
    "amount_below_1": "日成交额不足 1 亿元",
    "danger_unknown": "危险信号未知",
    "name_missing": "标的名称缺失",
    "asset_missing": "资产类型缺失",
    "unsupported_asset": "不属于 A 股股票或境内 ETF",
    "already_held": "当前账户已经持有",
    "excluded_security": "北交所、ST 或退市标的",
    "unsupported_exchange": "不属于沪深市场",
    "atr_unavailable": "缺少 ATR 数据",
    "data_date_mismatch": "数据日期不一致",
}
REMOVED_TREND_EXECUTION_LABELS = (
    "待执行", "已提交", "部分成交", "全部成交", "失败", "受阻",
    "状态不确定，禁止自动重试", "订单事实冲突，禁止提交", "已错过策略窗口",
    "未完成", "早期版本已执行", "不足整手，未下单",
)
REMOVED_TREND_REPORT_POSITION_LABELS = (
    "允许 · 建议", "计划止损风险", "正常成本", "决定性约束",
    "待执行", "模拟盘执行状态", "实盘执行辅助",
)


def _latest_phillips_expectation(data_dir: Path) -> tuple[Decimal, str]:
    statements = list((data_dir / "statements/phillips").glob("*/*.pdf"))
    if not statements:
        raise FileNotFoundError("找不到项目内辉立结单 PDF")
    latest = max(statements, key=lambda path: (path.parent.name, path.name))
    period = latest.parent.name[:7]
    parsed = PhillipsStatementParser().parse(latest, period)
    assets = [
        *((position.currency, position.market_value) for position in parsed.positions),
        *((cash.currency, cash.cash_balance) for cash in parsed.cash_balances),
    ]
    if any(currency != "HKD" or value is None for currency, value in assets):
        raise ValueError("最新辉立结单包含无法直接核对的非港币或缺失资产")
    return sum((value for _, value in assets if value is not None), Decimal("0")), period


def _project_data_dir(root: Path) -> Path:
    common = Path(subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"], text=True
    ).strip())
    if not common.is_absolute():
        common = root / common
    return common.resolve().parent / "data"


def _configured_simulate_account_ids(expected_root: Path) -> dict[str, int]:
    path = _project_data_dir(expected_root).parent / "config/daily_premarket.env"
    values = _read_env_file(path)
    return {
        broker: _optional_positive_tm_id(
            values, f"OPEN_TRADER_TREND_REVIEW_{market}_SIMULATE_ACC_ID"
        )
        for broker, market in TREND_SIMULATE_MARKETS.items()
    }


def _expected_cn_holdings(expected_root: Path) -> int:
    rows = _read_csv_rows(_project_data_dir(expected_root) / "latest/portfolio.csv")
    return sum(
        row.get("market", "").strip().upper() == "CN" and _is_dashboard_holding(row)
        for row in rows
    )


def _trend_execution_batch_errors(payload: Mapping[str, Any]) -> list[str]:
    reports = payload.get("trend_reports")
    if not isinstance(reports, Mapping):
        return []
    errors: list[str] = []
    for broker in TREND_SIMULATE_MARKETS:
        report = reports.get(broker)
        if not isinstance(report, Mapping) or report.get(
            "execution_batch_blocking"
        ) is not True:
            continue
        reason = str(
            report.get("execution_batch_error")
            or report.get("status_text")
            or "执行批次状态未知"
        )
        errors.append(f"{broker} 当前趋势报告执行批次阻断：{reason}")
    return errors


def validate_dashboard_payload(
    payload: dict[str, Any], *, expected_cn: int,
    expected_eastmoney_cny: Decimal | None = None,
    expected_rows: int | None = None,
    expected_phillips_total: Decimal | None = None,
    expected_phillips_period: str | None = None,
) -> list[str]:
    errors: list[str] = []
    holdings = payload.get("holdings") or []
    cash_rows = payload.get("cash_rows") or []
    rows = [*holdings, *cash_rows]
    if expected_rows is not None and len(rows) != expected_rows:
        errors.append(f"组合总行数不是 {expected_rows}：{len(rows)}")
    if expected_phillips_total is not None:
        phillips_summary = next(
            (
                row
                for row in payload.get("broker_summaries") or []
                if row.get("broker") == "phillips"
            ),
            {},
        )
        try:
            phillips_value = Decimal(
                str(phillips_summary.get("portfolio_value_hkd", ""))
            )
        except (InvalidOperation, TypeError, ValueError):
            phillips_value = Decimal("0")
        if not phillips_summary.get("detail_available") or phillips_value <= 0:
            errors.append("辉立账户卡没有可用月结单资产")
        elif phillips_value != expected_phillips_total:
            errors.append(
                f"辉立总资产不匹配：{phillips_value} != "
                f"{expected_phillips_total} HKD"
            )
    if expected_phillips_period is not None:
        account_sync = payload.get("account_sync")
        brokers = account_sync.get("brokers") if isinstance(account_sync, Mapping) else {}
        phillips_source = brokers.get("phillips") if isinstance(brokers, Mapping) else {}
        data_as_of = str(
            phillips_source.get("data_as_of", "")
            if isinstance(phillips_source, Mapping) else ""
        )
        if not data_as_of.startswith(expected_phillips_period):
            errors.append(f"辉立未使用最新结单：{expected_phillips_period}")
    cn_rows = [row for row in holdings if row.get("market") == "CN"]
    if len(cn_rows) != expected_cn:
        errors.append(f"A 股持仓数量不是 {expected_cn}：{len(cn_rows)}")

    universe = (payload.get("backtest_universe") or {}).get("holdings") or []
    cn_universe = [row for row in universe if row.get("market") == "CN"]
    if len(cn_universe) != expected_cn:
        errors.append(f"A 股回测标的数量不是 {expected_cn}：{len(cn_universe)}")

    try:
        total = sum(
            (
                Decimal(str(row["portfolio_weight_hkd"]).rstrip("%"))
                for row in [*holdings, *cash_rows]
            ),
            Decimal("0"),
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        errors.append("组合权重包含无效值")
    else:
        if total != Decimal("100.00"):
            errors.append(f"组合权重合计不是 100.00%：{total}%")
    if expected_eastmoney_cny is not None:
        try:
            eastmoney_total = sum(
                (
                    Decimal(str(row["market_value"]))
                    for row in [*holdings, *cash_rows]
                    if row.get("currency") == "CNY"
                    and "eastmoney" in str(row.get("brokers", "")).split(";")
                ),
                Decimal("0"),
            )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            errors.append("东方财富总资产包含无效值")
        else:
            if eastmoney_total != expected_eastmoney_cny:
                errors.append(
                    "东方财富总资产不匹配："
                    f"{eastmoney_total} != {expected_eastmoney_cny} CNY"
                )
    if "tiger_" + "long_term_strategy" in payload:
        errors.append("Dashboard API 仍包含已退役策略")
    errors.extend(_account_sync_errors(payload))
    errors.extend(_dashboard_position_field_errors(payload))
    errors.extend(_trend_execution_batch_errors(payload))
    return errors


def _dashboard_position_field_errors(payload: Mapping[str, Any]) -> list[str]:
    positions = payload.get("broker_positions")
    if positions is None:
        return []
    if not isinstance(positions, list):
        return ["Dashboard 缺少控制器持仓字段"]
    errors: list[str] = []
    for index, row in enumerate(positions):
        if not isinstance(row, Mapping):
            errors.append(f"控制器持仓第 {index + 1} 行不是对象")
            continue
        missing = [
            field for field in DASHBOARD_POSITION_FIELDS
            if not isinstance(row.get(field), str)
        ]
        if missing:
            errors.append(
                f"控制器持仓第 {index + 1} 行缺少字段：{', '.join(missing)}"
            )
    return errors


def _account_sync_errors(payload: Mapping[str, Any]) -> list[str]:
    account_sync = payload.get("account_sync")
    if not isinstance(account_sync, Mapping):
        return ["Dashboard 缺少账户同步状态"]
    errors: list[str] = []
    if account_sync.get("status") != "ok":
        errors.append("账户同步状态异常")
    controller = account_sync.get("controller")
    if not isinstance(controller, Mapping) or controller.get("status") != "ok":
        errors.append("账户同步 Worker 不可用")
    brokers = account_sync.get("brokers")
    if not isinstance(brokers, Mapping):
        return [*errors, "账户同步券商状态缺失"]
    for broker in ACCOUNT_BROKERS:
        source = brokers.get(broker)
        if not isinstance(source, Mapping) or source.get("status") != "ok":
            errors.append(f"{broker} 账户同步状态不是正常")

    positions = payload.get("broker_positions")
    summaries = payload.get("broker_summaries")
    if isinstance(positions, list) and isinstance(summaries, list):
        for summary in summaries:
            if not isinstance(summary, Mapping):
                continue
            broker = str(summary.get("broker") or "")
            expected = summary.get("holding_count")
            if broker not in ACCOUNT_BROKERS or not isinstance(expected, int):
                continue
            actual = sum(
                1 for row in positions
                if (
                    isinstance(row, Mapping)
                    and str(row.get("broker") or "") == broker
                    and _is_accepted_dashboard_holding(row)
                )
            )
            if actual != expected:
                errors.append(f"{broker} 已接受持仓数量不匹配：{actual} != {expected}")
    return errors


def _is_accepted_dashboard_holding(row: Mapping[str, Any]) -> bool:
    normalized = {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
    }
    quantity = row.get("total_quantity")
    if quantity is None or not str(quantity).strip():
        quantity = row.get("quantity", "")
    normalized["total_quantity"] = "" if quantity is None else str(quantity)
    return _is_dashboard_holding(normalized)


def _account_sync_worker_errors(
    root: Path, *, expected_root: Path, expected_sha: str, now: datetime | None = None,
) -> list[str]:
    path = _project_data_dir(root) / "account_sync/controller_status.json"
    try:
        controller = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["账户同步 Worker 状态缺失"]
    if not isinstance(controller, Mapping):
        return ["账户同步 Worker 状态缺失"]

    errors: list[str] = []
    pid = controller.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        errors.append("账户同步 Worker PID 无效")
    else:
        try:
            os.kill(pid, 0)
        except OSError as exc:
            errors.append(f"账户同步 Worker PID 不存活：{pid}（{exc}）")
    working_directory = controller.get("working_directory")
    if not isinstance(working_directory, str) or Path(working_directory).resolve() != expected_root.resolve():
        errors.append("账户同步 Worker 工作目录不匹配")
    if controller.get("git_sha") != expected_sha:
        errors.append("账户同步 Worker Git SHA 不匹配")
    try:
        heartbeat = datetime.fromisoformat(str(controller.get("heartbeat_at") or ""))
        if heartbeat.tzinfo is None or heartbeat.utcoffset() is None:
            raise ValueError
        if abs((now or datetime.now().astimezone()) - heartbeat) > timedelta(minutes=2):
            errors.append("账户同步 Worker 心跳不新鲜")
    except (TypeError, ValueError):
        errors.append("账户同步 Worker 心跳无效")
    return errors


def validate_quotes_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not payload.get("fetched_at"):
        errors.append("行情 API 缺少全局获取时间")
    if payload.get("us_session_status") not in {"active", "closed", "mixed"}:
        errors.append("行情 API 缺少有效的美股时段状态")
    us_quotes = [
        quote for quote in (payload.get("quotes") or {}).values()
        if quote.get("market") == "US"
    ]
    if not us_quotes:
        errors.append("行情 API 没有美股报价")
    for quote in us_quotes:
        symbol = str(quote.get("symbol", ""))
        try:
            price = Decimal(str(quote.get("last_price", "")))
        except (InvalidOperation, ValueError):
            price = Decimal("0")
        if not price.is_finite() or price <= 0:
            errors.append(f"US.{symbol} 价格无效")
        if quote.get("price_session") not in SESSION_KEYS:
            errors.append(f"US.{symbol} 时段缺失")
        if not quote.get("market_state"):
            errors.append(f"US.{symbol} 市场状态缺失")
        if quote.get("current_session_quote") is True and not quote.get("price_time"):
            errors.append(f"US.{symbol} 当前时段行情时间缺失")
    return errors


def validate_prediction_payload(payload: Mapping[str, Any]) -> list[str]:
    """Validate the non-mutating prediction Dashboard contract."""

    errors: list[str] = []
    status = str(payload.get("status") or "")
    if status not in {"healthy", "loading", "degraded", "unavailable", "error", "executing", "success", "incident", "completed"}:
        errors.append(f"预测市场状态无效：{status or 'missing'}")
    health = payload.get("health")
    if not isinstance(health, Mapping):
        errors.append("预测市场 health 不是对象")
        health_status = ""
    else:
        health_status = str(health.get("status") or "")
        if health_status not in {"healthy", "loading", "degraded", "unavailable", "error"}:
            errors.append(f"预测市场 health 状态无效：{health_status or 'missing'}")
    events = payload.get("events")
    if not isinstance(events, list):
        errors.append("预测市场 events 不是数组")
        events = []
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            errors.append(f"预测市场事件 {index} 不是对象")
            continue
        if not str(event.get("title") or event.get("question") or "").strip():
            errors.append(f"预测市场事件 {index} 缺少标题")
        if "volume_24h" not in event:
            errors.append(f"预测市场事件 {index} 缺少 24h 成交量")
    event_count = payload.get("event_count")
    if event_count is not None and (not isinstance(event_count, int) or event_count < len(events)):
        errors.append("预测市场 event_count 小于当前事件数量")
    opportunities = payload.get("opportunities")
    if not isinstance(opportunities, list):
        errors.append("预测市场 opportunities 不是数组")
        opportunities = []
    required_actionable_fields = (
        "opportunity_id", "title", "market_type", "fee_status", "yes_price",
        "no_price", "quantity", "max_cost", "profit",
    )
    for index, opportunity in enumerate(opportunities):
        if not isinstance(opportunity, Mapping) or opportunity.get("actionable") is not True:
            continue
        if any(
            opportunity.get(key) is None or str(opportunity.get(key)).strip() == ""
            for key in required_actionable_fields
        ):
            errors.append(f"预测市场 actionable opportunity 字段不完整：{index}")
    stale = (
        bool(payload.get("stale"))
        or status in {"degraded", "unavailable", "error"}
        or health_status != "healthy"
    )
    breaker = payload.get("breaker")
    breaker_open = isinstance(breaker, Mapping) and breaker.get("open") is True
    execution = payload.get("current_execution")
    execution_state = str(execution.get("status") or execution.get("state") or "").lower() if isinstance(execution, Mapping) else ""
    execution_locked = any(value in execution_state for value in ("running", "executing", "validating", "submitting", "reconciling", "merging"))
    if stale or breaker_open or execution_locked:
        if any(isinstance(item, Mapping) and item.get("actionable") is True for item in opportunities):
            errors.append("预测市场异常/执行锁定时仍暴露 actionable opportunity")
    return errors


def classify_result(
    errors: list[str],
    *,
    browser_blocker: str | None,
    external_blocker: str | None = None,
) -> str:
    if errors:
        return "FAIL"
    return "BLOCKED" if browser_blocker or external_blocker else "PASS"


def dashboard_signature(payload: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    fields = ("market", "symbol", "brokers")
    rows = [*(payload.get("holdings") or []), *(payload.get("cash_rows") or [])]
    return tuple(sorted(tuple(str(row.get(field, "")) for field in fields) for row in rows))


def trend_advice_signature(payload: Mapping[str, Any]) -> tuple[str, ...]:
    reports = payload.get("trend_reports")
    reports = reports if isinstance(reports, Mapping) else {}
    signature: list[str] = []
    for broker in TREND_SIMULATE_MARKETS:
        report = reports.get(broker)
        report = report if isinstance(report, Mapping) else {}
        summary = report.get("risk_summary")
        summary = dict(summary) if isinstance(summary, Mapping) else {}

        def frozen_actions(key: str) -> list[dict[str, Any]]:
            actions = report.get(key)
            if not isinstance(actions, list):
                return []
            return [
                {field: value for field, value in action.items() if field != "execution"}
                for action in actions
                if isinstance(action, Mapping)
            ]

        signature.append(json.dumps({
            "broker": broker,
            "report_sha256": report.get("report_sha256"),
            "strategy_version": report.get("strategy_version"),
            "sell_actions": frozen_actions("sell_actions"),
            "buy_actions": frozen_actions("buy_actions"),
            "hold_actions": frozen_actions("hold_actions"),
            "review_actions": frozen_actions("review_actions"),
            "risk_skips": frozen_actions("risk_skips"),
            "risk_summary": summary,
            "drawdown_summary": report.get("drawdown_summary"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return tuple(signature)


def _trend_report_is_current_or_recent_weekend_snapshot(
    report: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Keep strict current-report checks while allowing the latest weekend snapshot.

    The dashboard is routinely reviewed after the Friday close, before the next
    market session has produced a new report.  In that window a recent frozen
    report is intentionally marked ``stale`` by the product; rejecting it here
    would make the acceptance gate depend on the wall-clock crossing midnight.
    Weekdays still require ``data_status=current``.
    """
    if report.get("data_status") == "current":
        return True
    if report.get("data_status") != "stale":
        return False
    now = now or datetime.now(SHANGHAI)
    operator_date = now.astimezone(SHANGHAI).date()
    if report.get("report_date") == operator_date.isoformat():
        return True
    if operator_date.weekday() < 5:
        return False
    try:
        generated_at = datetime.fromisoformat(str(report.get("generated_at") or ""))
    except (TypeError, ValueError):
        return False
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        return False
    age = (operator_date - generated_at.astimezone(SHANGHAI).date()).days
    return 0 <= age <= 3


def validate_integrated_candidate(
    payload: Mapping[str, Any],
    *,
    expected_root: Path,
    expected_sha: str,
    reports_dir: Path,
    account_ids: Mapping[str, int],
) -> list[str]:
    errors: list[str] = []
    try:
        templates_payload = json.loads(
            (expected_root / "data/latest/kelly_strategy_templates.json").read_text(
                encoding="utf-8"
            )
        )
        assert isinstance(templates_payload, Mapping), "Kelly 模板文件不是对象"
        expected_templates = templates_payload["templates"]
        lab = payload.get("kelly_lab")
        assert isinstance(lab, Mapping) and lab.get("available") is True, (
            "Kelly 模板未从干净候选加载"
        )
        assert (
            isinstance(expected_templates, list)
            and expected_templates
            and lab.get("template_count") == len(expected_templates)
            and lab.get("templates") == expected_templates
        ), "Kelly 模板与候选 SHA 不一致"
    except (AssertionError, KeyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(str(exc) or f"Kelly 模板检查失败：{type(exc).__name__}")

    source_cutoffs: dict[str, str] = {}
    try:
        data_dir_value = payload.get("data_dir")
        assert isinstance(data_dir_value, str) and data_dir_value.strip(), (
            "Dashboard 缺少交易统计数据目录"
        )
        data_dir = Path(data_dir_value)
        if not data_dir.is_absolute():
            data_dir = expected_root / data_dir
        stats_payload = json.loads(
            (data_dir / "latest/trend_api_stats.json").read_text(encoding="utf-8")
        )
        assert isinstance(stats_payload, Mapping), "交易统计来源文件不是对象"
        sources = stats_payload.get("sources")
        assert isinstance(sources, list), "交易统计来源清单无效"
        for broker, market in TREND_SIMULATE_MARKETS.items():
            matching = [
                source for source in sources
                if isinstance(source, Mapping)
                and source.get("source") == "actual"
                and source.get("broker") == broker
                and source.get("market") == market
            ]
            assert len(matching) == 1 and matching[0].get("statistics_cutoff_at"), (
                f"{broker} 实盘统计来源截止时间不可用"
            )
            source_cutoffs[broker] = str(matching[0]["statistics_cutoff_at"])
    except (AssertionError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(str(exc) or f"交易统计来源检查失败：{type(exc).__name__}")

    reports = payload.get("trend_reports")
    if not isinstance(reports, Mapping):
        return [*errors, "Dashboard 缺少三市场趋势报告"]
    labels = {"tiger": "老虎", "phillips": "辉立", "eastmoney": "东方财富"}
    for broker, market in TREND_SIMULATE_MARKETS.items():
        try:
            report = reports.get(broker)
            assert isinstance(report, Mapping) and report.get("available") is True, (
                f"{broker} {market} 趋势报告不可用"
            )
            expected_version = str(report.get("strategy_version") or "")
            assert expected_version in TREND_ACCEPTED_STRATEGY_VERSIONS[market], (
                f"{broker} 趋势策略版本不在兼容白名单"
            )
            assert report.get("broker") == broker and report.get("market") == market, (
                f"{broker} 三市场报告身份不匹配"
            )
            assert _trend_report_is_current_or_recent_weekend_snapshot(report), (
                f"{broker} 未加载当前真实数据报告"
            )
            assert report.get("account_fresh") is True, (
                f"{broker} Futu 模拟账户快照不是最新"
            )
            artifact = report.get("artifact") or (
                report.get("audit") or {}
            ).get("artifact")
            assert (
                isinstance(artifact, str)
                and artifact.endswith(".json")
                and Path(artifact).name == artifact
            ), f"{broker} 冻结报告文件名无效"
            path = reports_dir / TREND_REPORT_DIRECTORIES[broker] / artifact
            frozen = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(frozen, Mapping), f"{broker} 冻结报告不是对象"
            assert valid_frozen_report_contract(frozen), (
                f"{broker} 冻结报告契约无效"
            )
            assert report.get("report_sha256") == _report_hash(frozen), (
                f"{broker} 报告哈希与冻结产物不一致"
            )
            metadata = frozen.get("metadata")
            assert (
                isinstance(metadata, Mapping)
                and metadata.get("market") == market
                and metadata.get("broker") == broker
                and metadata.get("simulate_acc_id") == account_ids.get(broker)
            ), f"{broker} 未使用对应 Futu 模拟账户作为策略基线"
            assert f"Futu {market} SIMULATE account" in frozen.get(
                "data_sources", []
            ), f"{broker} 冻结报告缺少 Futu 模拟账户数据源"
            account = frozen.get("account")
            assert isinstance(account, Mapping) and account.get("fresh") is True, (
                f"{broker} 冻结报告的 Futu 模拟账户快照不是最新"
            )

            snapshot = frozen.get("strategy_snapshot")
            parameters = (
                snapshot.get("parameters") if isinstance(snapshot, Mapping) else None
            )
            assert (
                isinstance(parameters, Mapping)
                and snapshot.get("strategy_version") == expected_version
                and re.fullmatch(
                    r"[0-9a-f]{40}", str(snapshot.get("process_version") or "")
                )
                and report.get("strategy_version") == expected_version
            ), f"{broker} 冻结 Kelly/回撤策略身份无效"
            for key, expected, label in (
                ("single_entry_risk_limit", Decimal("0.004"), "单笔风险"),
                ("portfolio_risk_limit", Decimal("0.04"), "组合风险"),
                ("abnormal_loss_buffer", Decimal("0.01"), "异常损失缓冲"),
                ("drawdown_limit", Decimal("0.05"), "回撤阈值"),
            ):
                assert _position_decimal(parameters.get(key), label) == expected, (
                    f"{broker} {label}参数不正确"
                )
            target = parameters.get("target_weight")
            target_values = (
                target.values() if isinstance(target, Mapping) else (target,)
            )
            expected_target_weight = Decimal("0.04")
            allocation = frozen.get("allocation")
            if isinstance(allocation, Mapping):
                markets = allocation.get("markets")
                allocation_market = (
                    markets.get(market) if isinstance(markets, Mapping) else None
                )
                assert isinstance(allocation_market, Mapping), (
                    f"{broker} 资源排名市场缺失"
                )
                expected_target_weight = _position_decimal(
                    allocation_market.get("entry_weight"), "资源排名仓位"
                )
            assert target_values and max(
                _position_decimal(value, "名义仓位上限") for value in target_values
            ) == expected_target_weight, f"{broker} 目标仓位与资源排名不一致"

            summary = report.get("risk_summary")
            assert isinstance(summary, Mapping), f"{broker} 缺少风险摘要"
            for key, expected, label in (
                ("single_entry_risk_limit_pct", Decimal("0.004"), "单笔风险"),
                ("portfolio_risk_limit_pct", Decimal("0.04"), "组合风险"),
                ("abnormal_loss_buffer_pct", Decimal("0.01"), "异常损失缓冲"),
                ("total_risk_budget_target_pct", Decimal("0.05"), "总风险预算"),
            ):
                assert _position_decimal(summary.get(key), label) == expected, (
                    f"{broker} {label}摘要不正确"
                )
            assert summary.get("disclaimer") == (
                "5% 是风险预算目标，不是最大损失保证。"
            ), f"{broker} 风险预算免责声明不正确"
            frozen_summary = frozen.get("risk_summary")
            projected_summary = dict(summary)
            stats = projected_summary.pop("trade_stats", None)
            assert projected_summary == frozen_summary, (
                f"{broker} 冻结风险摘要被实盘数据改写"
            )
            assert (
                isinstance(stats, Mapping)
                and stats.get("available") is True
                and isinstance(stats.get("simulation"), Mapping)
                and isinstance(stats.get("actual"), Mapping)
                and stats.get("actual_broker") == broker
                and stats.get("actual_broker_label") == labels[broker]
            ), f"{broker} 实盘统计券商或来源截止时间不正确"
            assert stats.get("statistics_cutoff_at") == source_cutoffs.get(broker), (
                f"{broker} 实盘统计来源截止时间与源数据不一致"
            )
            assert (
                summary.get("kelly_phase") in {
                    "cold_start", "active_all_samples", "active_rolling_200",
                    "unavailable",
                }
                and summary.get("kelly_source")
                == "合格的富途模拟闭环；实盘结果不参与计算"
            ), f"{broker} Kelly 统计来源不正确"

            judgments = frozen.get("strategy_judgments")
            assert isinstance(judgments, Mapping), f"{broker} 冻结策略动作缺失"
            expected_risk_skips = [
                _project_trend_money_fields(
                    dict(item), payload=dict(frozen), market=market
                )
                for item in judgments.get("risk_skips", [])
                if isinstance(item, Mapping)
            ]
            assert report.get("risk_skips") == expected_risk_skips, (
                f"{broker} 风险跳过动作与冻结报告不一致"
            )
            buys = report.get("buy_actions")
            assert isinstance(buys, list), f"{broker} 正式买入动作无效"
            for action in buys:
                assert isinstance(action, Mapping), f"{broker} 正式买入动作无效"
                quantity = _position_decimal(action.get("estimated_shares"), "买入数量")
                lot = _position_decimal(action.get("lot_size"), "整手数量")
                weight = _position_decimal(action.get("target_weight"), "目标仓位")
                assert (
                    quantity == quantity.to_integral_value()
                    and lot > 0
                    and lot == lot.to_integral_value()
                    and quantity % lot == 0
                ), f"{broker} 买入数量未按整手向下取整"
                assert Decimal("0") < weight <= expected_target_weight, (
                    f"{broker} 买入目标超过资源排名仓位上限"
                )

            drawdown = report.get("drawdown_summary")
            assert (
                isinstance(drawdown, Mapping)
                and drawdown.get("state_status") == "ok"
            ), f"{broker} 回撤状态缺失或损坏"
            bootstrap = drawdown.get("bootstrap_event")
            assert (
                isinstance(bootstrap, Mapping)
                and re.fullmatch(
                    r"[0-9a-f]{40}", str(bootstrap.get("accepted_git_sha") or "")
                )
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(bootstrap.get("parameter_hash") or "")
                )
                and bootstrap.get("baseline_equity")
                and bootstrap.get("source_date")
                and bootstrap.get("event_id")
                and bootstrap.get("actor")
            ), f"{broker} 自动回撤基准审计不完整"
            assert valid_strategy_parameter_audit_identity(
                market=market,
                strategy_id=str(snapshot.get("strategy_id") or ""),
                strategy_version=expected_version,
                parameters=parameters,
                bootstrap_event=bootstrap,
                parameter_compatibility_event=drawdown.get(
                    "parameter_compatibility_event"
                ),
            ), f"{broker} 冻结策略参数与回撤审计身份不一致"
            assert (
                drawdown.get("entry_allowed") is True or not buys
            ), f"{broker} 回撤阻断状态仍包含正式买入"
            assert not any(
                isinstance(item, Mapping)
                and item.get("decisive_constraint") == "策略累计回撤"
                and "状态" in str(item.get("reason") or "")
                for item in report.get("risk_skips", [])
            ), f"{broker} 仍因回撤状态缺失跳过买入"
            assert (
                drawdown == frozen.get("drawdown_summary")
                and drawdown.get("status") in {"active", "pending", "paused"}
                and bool(drawdown.get("status_label"))
                and _position_decimal(
                    drawdown.get("drawdown_limit_pct"), "回撤阈值"
                ) == Decimal("0.05")
            ), f"{broker} 5% 策略回撤状态不正确"
            overlay = report.get("actual_overlay")
            assert (
                isinstance(overlay, Mapping)
                and overlay.get("available") is True
                and overlay.get("broker") == broker
                and overlay.get("broker_label") == labels[broker]
                and overlay.get("market") == market
                and "不会改写模拟建议、Kelly、模拟统计或报告哈希"
                in str(overlay.get("notice") or "")
            ), f"{broker} 只读实盘辅助与对应账户不一致"
        except (
            AssertionError, InvalidOperation, KeyError, OSError, TypeError,
            UnicodeError, ValueError, json.JSONDecodeError,
        ) as exc:
            errors.append(str(exc) or f"{broker} 集成报告检查失败：{type(exc).__name__}")
    return errors


def _fetch_payload(url: str) -> dict[str, Any]:
    with urlopen(
        f"{url.rstrip('/')}/api/dashboard", timeout=DASHBOARD_API_TIMEOUT_SECONDS
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Dashboard API HTTP {response.status}")
        return json.load(response)


def _fetch_quotes_payload(url: str) -> dict[str, Any]:
    with urlopen(
        f"{url.rstrip('/')}/api/quotes", timeout=DASHBOARD_API_TIMEOUT_SECONDS
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Quotes API HTTP {response.status}")
        return json.load(response)


def _fetch_account_snapshot(
    url: str, *, etag: str | None = None,
) -> tuple[int, dict[str, Any] | None, str | None]:
    headers = {"If-None-Match": etag} if etag else {}
    request = Request(f"{url.rstrip('/')}{ACCOUNT_SNAPSHOT_PATH}", headers=headers)
    try:
        with urlopen(request, timeout=DASHBOARD_API_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RuntimeError(f"Account snapshot HTTP {response.status}")
            payload = json.load(response)
            if not isinstance(payload, dict):
                raise RuntimeError("Account snapshot 不是对象")
            return response.status, payload, response.headers.get("ETag")
    except HTTPError as error:
        if error.code == 304:
            return 304, None, error.headers.get("ETag")
        raise


def _account_snapshot_errors(
    payload: object, *, expected_sha: str,
) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["Account snapshot 不是对象"]
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("Account snapshot schema 不匹配")
    if payload.get("status") not in {"healthy", "stale"}:
        errors.append("Account snapshot 状态不可用")
    if not isinstance(payload.get("stale"), bool):
        errors.append("Account snapshot stale 状态无效")
    for field in (
        "snapshot_generation", "account_generation", "generated_at", "quote_as_of",
    ):
        if not isinstance(payload.get(field), str) or not payload[field]:
            errors.append(f"Account snapshot 缺少 {field}")
    for field in ("summary", "sources", "release"):
        if not isinstance(payload.get(field), Mapping):
            errors.append(f"Account snapshot 缺少 {field}")
    for field in ("broker_summaries", "positions", "cash_balances", "errors"):
        if not isinstance(payload.get(field), list):
            errors.append(f"Account snapshot {field} 不是列表")
    release = payload.get("release")
    if isinstance(release, Mapping) and (
        release.get("api_git_sha") != expected_sha
        or release.get("worker_git_sha") != expected_sha
    ):
        errors.append("Account snapshot release Git SHA 不匹配")
    return errors


def _fetch_json_path(url: str, path: str) -> Any:
    with urlopen(
        f"{url.rstrip('/')}{path}", timeout=DASHBOARD_API_TIMEOUT_SECONDS
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Dashboard API HTTP {response.status}: {path}")
        return json.load(response)


def _position_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise AssertionError(f"{field} 不是有效数字") from None
    assert result.is_finite(), f"{field} 不是有限数字"
    return result


def _direct_simulate_facts(
    snapshot: Mapping[str, Any], market: str,
) -> tuple[tuple[str, str, Decimal, Decimal], ...]:
    positions = snapshot.get("positions")
    assert isinstance(positions, list), "Futu 模拟盘持仓不可用"
    facts: list[tuple[str, str, Decimal, Decimal]] = []
    for position in positions:
        assert isinstance(position, Mapping), "Futu 模拟盘持仓格式无效"
        quantity = _position_decimal(
            position.get("qty", position.get("quantity")), "Futu 持仓数量"
        )
        if quantity <= 0:
            continue
        code = str(position.get("code") or position.get("futu_code") or "").upper()
        assert to_futu_symbol(market, code) == code, f"Futu 持仓代码无效：{code}"
        facts.append((
            market,
            code.split(".", 1)[1],
            quantity,
            _position_decimal(
                position.get("cost_price", position.get("average_cost")),
                "Futu 持仓成本价",
            ),
        ))
    return tuple(sorted(facts))


def _api_simulate_facts(
    payload: Mapping[str, Any], market: str,
) -> tuple[tuple[str, str, Decimal, Decimal], ...]:
    positions = payload.get("positions")
    assert isinstance(positions, list), "Dashboard 模拟盘持仓格式无效"
    facts: list[tuple[str, str, Decimal, Decimal]] = []
    for position in positions:
        assert isinstance(position, Mapping), "Dashboard 模拟盘持仓行无效"
        assert position.get("market") == market, "Dashboard 模拟盘持仓市场不匹配"
        symbol = str(position.get("symbol") or "").strip().upper()
        assert symbol, "Dashboard 模拟盘持仓代码缺失"
        quantity = _position_decimal(position.get("quantity"), "Dashboard 持仓数量")
        assert quantity > 0, "Dashboard 模拟盘持仓数量必须为正数"
        facts.append((
            market,
            symbol,
            quantity,
            _position_decimal(position.get("cost_price"), "Dashboard 持仓成本价"),
        ))
    return tuple(sorted(facts))


def _current_simulate_attributions(
    data_dir: Path, reports_dir: Path, *, broker: str, market: str,
) -> dict[str, tuple[str, dict[str, str] | None]]:
    reports = {
        (report_hash, report["strategy_version"]): report
        for report_hash, report in _reports_by_hash(
            reports_dir / TREND_REPORT_DIRECTORIES[broker],
            broker=broker,
            market=market,
        ).items()
    }

    active: dict[str, set[tuple[str, str] | None]] = {}
    for _event_date, _recorded_at, _path, event in _action_events(data_dir, market):
        symbol = str(event.get("symbol") or "").strip().upper()
        side = str(event.get("side") or "").strip().lower()
        status = str(event.get("status") or "").strip().lower()
        if not symbol:
            continue
        if side == "sell" and (
            status == "filled"
            or (
                status == "incomplete"
                and event.get("reason") == "position_zero_confirmed"
            )
        ):
            active.pop(symbol, None)
            continue
        if side != "buy" or status not in {"partially_filled", "filled"}:
            continue
        if _position_decimal(event.get("filled_qty"), "账本成交数量") <= 0:
            continue
        report_sha256 = str(event.get("report_sha256") or "").strip().lower()
        strategy_version = str(event.get("strategy_version") or "").strip()
        identity = (
            (report_sha256, strategy_version)
            if len(report_sha256) == 64
            and all(character in "0123456789abcdef" for character in report_sha256)
            and strategy_version
            else None
        )
        active.setdefault(symbol, set()).add(identity)

    result: dict[str, tuple[str, dict[str, str] | None]] = {}
    for symbol, identities in active.items():
        valid = {identity for identity in identities if identity in reports}
        if len(valid) > 1:
            result[symbol] = ("conflict", None)
        elif identities - valid or not valid:
            result[symbol] = ("unlinked", None)
        else:
            result[symbol] = ("linked", reports[next(iter(valid))])
    return result


def _validate_simulated_positions(
    broker: str,
    direct_snapshot: Mapping[str, Any],
    payload: Mapping[str, Any],
    data_dir: Path,
    reports_dir: Path,
) -> None:
    market = TREND_SIMULATE_MARKETS[broker]
    positions = payload.get("positions")
    if payload.get("available") is not True:
        if positions:
            raise AssertionError(f"{broker} 模拟盘不可用时显示了替代持仓")
        raise AssertionError(f"{broker} Dashboard 模拟盘不可用：{payload.get('error', '')}")
    assert payload.get("broker") == broker and payload.get("market") == market, (
        f"{broker} Dashboard 模拟盘账户身份不匹配"
    )
    assert _api_simulate_facts(payload, market) == _direct_simulate_facts(
        direct_snapshot, market
    ), f"{broker} 模拟盘持仓与 Futu 不匹配"

    expected_attributions = _current_simulate_attributions(
        data_dir, reports_dir, broker=broker, market=market
    )
    assert isinstance(positions, list)
    for position in positions:
        assert isinstance(position, Mapping)
        symbol = str(position.get("symbol") or "").strip().upper()
        expected_status, expected_report = expected_attributions.get(
            symbol, ("unlinked", None)
        )
        assert expected_status != "conflict", (
            f"{broker} {symbol} 模拟盘报告归因冲突"
        )
        status = position.get("attribution_status")
        assert status == expected_status, (
            f"{broker} {symbol} 模拟盘报告归因不匹配"
        )
        if status == "unlinked":
            assert position.get("report") is None, (
                f"{broker} 未关联持仓错误携带报告"
            )
            continue
        report = position.get("report")
        assert isinstance(report, Mapping), f"{broker} 已关联持仓缺少报告身份"
        assert expected_report is not None and all(
            report.get(key) == expected_report[key]
            for key in (
                "artifact", "execution_date", "strategy_version", "report_sha256"
            )
        ), f"{broker} {position.get('symbol', '')} 模拟盘报告身份不匹配"


def _check_simulated_accounts(
    url: str,
    dashboard_payload: Mapping[str, Any],
    account_ids: Mapping[str, int],
    data_dir: Path,
    reports_dir: Path,
) -> tuple[dict[str, dict[str, Any]], list[str], str | None]:
    host = dashboard_payload.get("futu_host")
    port = dashboard_payload.get("futu_port")
    if not isinstance(host, str) or not host or not isinstance(port, int) or port <= 0:
        return {}, ["Dashboard 缺少有效 Futu OpenD 配置"], None
    payloads: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for broker, market in TREND_SIMULATE_MARKETS.items():
        account_id = account_ids.get(broker, 0)
        if not isinstance(account_id, int) or account_id <= 0:
            errors.append(f"{broker} 配置的 Futu 模拟账户不可用")
            continue
        client = None
        try:
            client = FutuSimulateOrderExecutionClient(
                host=host,
                port=port,
                simulate_acc_id=account_id,
                trd_market=market,
            )
            snapshot = client.account_snapshot()
        except Exception as exc:
            return payloads, errors, f"{broker} Futu 模拟账户不可用：{exc}"
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    return payloads, errors, f"{broker} Futu 模拟账户关闭失败：{exc}"
        try:
            payload = _fetch_json_path(url, f"/api/trend-simulate-positions/{broker}")
            assert isinstance(payload, dict), f"{broker} 模拟盘 API 不是对象"
            _validate_simulated_positions(
                broker, snapshot, payload, data_dir, reports_dir
            )
        except Exception as exc:
            errors.append(f"{broker} 模拟盘检查失败：{type(exc).__name__}: {exc}")
            continue
        payloads[broker] = payload
    return payloads, errors, None


def _validate_history_projection(
    data_dir: Path,
    reports_dir: Path,
    broker: str,
    history: object,
    exact_by_artifact: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    market = TREND_SIMULATE_MARKETS[broker]
    reports = _reports_by_hash(
        reports_dir / TREND_REPORT_DIRECTORIES[broker],
        broker=broker,
        market=market,
    )
    assert isinstance(history, list), f"{broker} 历史报告 API 不是列表"
    history_rows = {
        str(row.get("artifact")): row
        for row in history
        if isinstance(row, Mapping) and row.get("available") is True
    }
    latest_events: dict[tuple[str, str, str], Mapping[str, object]] = {}
    events_by_action: dict[
        tuple[str, str, str], list[Mapping[str, object]]
    ] = {}
    protection_actions: set[tuple[str, str, str]] = set()
    for event_date, _, event_path, event in _action_events(data_dir, market):
        report_hash = str(event.get("report_sha256") or "").strip().lower()
        if len(report_hash) == 64:
            action = (
                report_hash,
                str(event.get("symbol") or "").strip().upper(),
                str(event.get("side") or "").strip().lower(),
            )
            latest_events[action] = event
            events_by_action.setdefault(action, []).append(event)
            if _protection_event_identity(
                event,
                market=market,
                execution_date=event_date,
                action_key=Path(event_path).parent.name,
            ) is not None:
                protection_actions.add(action)

    expectations: list[dict[str, Any]] = []
    for (report_hash, symbol, side), event in latest_events.items():
        report = reports.get(report_hash)
        if report is None and (report_hash, symbol, side) in protection_actions:
            continue
        assert report is not None, f"{broker} 账本引用的冻结报告不存在：{report_hash}"
        artifact = report["artifact"]
        summary = history_rows.get(artifact)
        assert summary is not None, f"{artifact} 从 Dashboard 历史报告中消失"
        assert (
            summary.get("execution_date") == report["execution_date"]
            and summary.get("strategy_version") == report["strategy_version"]
        ), f"{artifact} 历史报告身份不匹配"
        exact = exact_by_artifact.get(artifact)
        assert isinstance(exact, Mapping), f"{artifact} 精确历史报告缺失"
        audit = exact.get("audit")
        assert (
            exact.get("report_date") == report["execution_date"]
            and exact.get("artifact") == artifact
            and exact.get("report_sha256") == report_hash
            and exact.get("strategy_version") == report["strategy_version"]
            and isinstance(audit, Mapping)
            and audit.get("artifact") == artifact
        ), f"{artifact} 精确历史报告身份不匹配"
        action_key = {"buy": "buy_actions", "sell": "sell_actions"}.get(side)
        assert action_key is not None, f"{artifact} 账本动作方向无效：{side}"
        actions = exact.get(action_key)
        assert isinstance(actions, list), f"{artifact} 精确历史报告动作缺失"
        projected = next(
            (
                item for item in actions
                if isinstance(item, Mapping)
                and str(item.get("symbol") or "").strip().upper() == symbol
            ),
            None,
        )
        execution = projected.get("execution") if isinstance(projected, Mapping) else None
        assert (
            isinstance(execution, Mapping)
            and execution.get("status") == event.get("status")
            and any(
                execution.get("status") == observed.get("status")
                and execution.get("updated_at") == observed.get("recorded_at")
                for observed in events_by_action[(report_hash, symbol, side)]
            )
        ), f"{artifact} 历史报告动作 {symbol} 消失或执行状态不匹配"
        expectations.append({
            **exact,
            "symbol": symbol,
            "side": side,
            "event": event,
        })
    return expectations


def _check_account_view_contract(page: Any, section: Any, broker: str) -> None:
    tabs = section.locator('[role="tab"][data-account-view]')
    assert tabs.count() == 3, f"{broker} 账户视图 Tab 数量不是 3"
    actual_labels = tuple(tabs.nth(index).inner_text().strip() for index in range(3))
    assert actual_labels == ACCOUNT_VIEW_LABELS[broker], f"{broker} 账户视图 Tab 顺序不正确"
    assert tuple(
        tabs.nth(index).get_attribute("data-account-view") for index in range(3)
    ) == ("real", "simulate", "report"), (
        f"{broker} 账户视图 Tab 身份不正确"
    )
    assert tabs.nth(0).get_attribute("aria-selected") == "true" and all(
        tabs.nth(index).get_attribute("aria-selected") == "false"
        for index in range(1, 3)
    ), f"{broker} 默认视图不是真实持仓"
    expression = (
        "element => { const style = getComputedStyle(element); return {"
        "borderTopWidth: style.borderTopWidth, borderLeftWidth: style.borderLeftWidth, "
        "borderRightWidth: style.borderRightWidth, "
        "borderBottomWidth: style.borderBottomWidth, "
        "backgroundColor: style.backgroundColor, "
        "borderRadius: style.borderRadius, "
        "indicatorHeight: getComputedStyle(element, '::after').height, "
        "indicatorBackground: getComputedStyle(element, '::after').backgroundColor, "
        "indicatorContent: getComputedStyle(element, '::after').content}; }"
    )
    for index in range(3):
        style = tabs.nth(index).evaluate(expression)
        common = {
            "borderTopWidth": "0px",
            "borderLeftWidth": "0px",
            "borderRightWidth": "0px",
            "borderBottomWidth": "0px",
            "backgroundColor": "rgba(0, 0, 0, 0)",
            "borderRadius": "0px",
        }
        assert {key: style.get(key) for key in common} == common, (
            f"{broker} 账户视图使用了描边或按钮背景：{style}"
        )
        if index == 0:
            assert (
                style.get("indicatorHeight") == "2px"
                and style.get("indicatorBackground") != "rgba(0, 0, 0, 0)"
                and style.get("indicatorContent") == '""'
            ), f"{broker} 选中 Tab 缺少 2px 下划线：{style}"
        else:
            assert style.get("indicatorContent") == "none", (
                f"{broker} 未选中 Tab 错误显示下划线：{style}"
            )
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), f"{broker} 账户视图出现横向滚动"


def _wait_for_simulate_positions(
    page: Any, broker: str, expected: int | None,
) -> None:
    page.wait_for_function(
        SIMULATE_POSITIONS_READY_EXPRESSION,
        arg={"broker": broker, "expected": expected},
        timeout=SIMULATE_POSITIONS_READY_TIMEOUT_MS,
    )


def _check_history_control_contract(control: Any, context: str) -> None:
    style = control.evaluate(
        """element => { const style = getComputedStyle(element); return {
          borderTopWidth: style.borderTopWidth,
          borderLeftWidth: style.borderLeftWidth,
          borderRightWidth: style.borderRightWidth,
          borderBottomWidth: style.borderBottomWidth,
          backgroundColor: style.backgroundColor,
          borderRadius: style.borderRadius,
          fontWeight: style.fontWeight,
          color: style.color,
          textDecorationLine: style.textDecorationLine,
        }; }"""
    )
    assert style == {
        "borderTopWidth": "0px",
        "borderLeftWidth": "0px",
        "borderRightWidth": "0px",
        "borderBottomWidth": "0px",
        "backgroundColor": "rgba(0, 0, 0, 0)",
        "borderRadius": "0px",
        "fontWeight": "400",
        "color": "rgb(116, 110, 100)",
        "textDecorationLine": "underline",
    }, f"{context} 不是低强调文字控件：{style}"


def _check_loaded_report_identity(
    panel: Any, expected: Mapping[str, Any], broker: str,
) -> None:
    report_root = panel.locator(".cn-trend-report")
    actual = {
        "artifact": report_root.get_attribute("data-report-artifact"),
        "report_sha256": report_root.get_attribute("data-report-sha256"),
        "strategy_version": report_root.get_attribute("data-strategy-version"),
    }
    _check_report_identity(actual, expected, broker)
    assert str(expected.get("strategy_version") or "") in report_root.inner_text(), (
        f"{broker} 精确历史报告未显示策略版本"
    )


def _trend_context_percent(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if abs(number) <= 1:
        number *= 100
    return f"{_display_number(str(number))}%"


def _trend_ratio_transition(current: Any, prior: Any) -> str:
    current_text = _trend_context_percent(current)
    if current_text is None:
        return "未提供"
    prior_text = _trend_context_percent(prior)
    return f"{prior_text} → {current_text}" if prior_text else f"{current_text} · 基准建立中"


def _trend_context_display_value(key: str, value: Any) -> str:
    if key in {"strength", "warm_to_hot_count"}:
        return _display_number(value)
    return str(value)


def _check_frozen_trend_disciplines(
    report_root: Any, report: Mapping[str, Any], broker: str,
    *, page: Any | None = None,
) -> None:
    current_rows = report.get("current_strategy_parameter_rows")
    raw_rows = (
        current_rows
        if isinstance(current_rows, list)
        else report.get("strategy_parameter_rows")
    )
    rows = raw_rows if isinstance(raw_rows, list) else []
    has_rows = bool(rows)
    workspace = report_root.locator(".trend-discipline-workspace")
    assert workspace.count() == 1, f"{broker} 纪律区块数量不是 1"
    assert workspace.get_attribute("open") is None, (
        f"{broker} 纪律未默认收起"
    )
    workspace_summary = workspace.locator(":scope > summary")
    assert workspace_summary.count() == 1, f"{broker} 纪律区块缺少摘要"
    assert workspace_summary.inner_text().startswith("纪律"), (
        f"{broker} 纪律摘要标题不正确"
    )
    workspace_summary.click()
    cards = workspace.locator(".trend-discipline-category")
    assert cards.count() == 6, f"{broker} 纪律类别数量不是 6"
    summaries = cards.locator("summary")
    titles = summaries.all_inner_texts()
    for title in ("入场门槛", "候选排序", "仓位与执行", "持有管理", "退出规则", "其他设置"):
        assert any(title in value for value in titles), (
            f"{broker} 纪律缺少 {title}"
        )
    for index in range(cards.count()):
        card = cards.nth(index)
        summary = card.locator("summary")
        summary_text = summary.inner_text()
        assert re.search(r"\d+\s+项", summary_text), (
            f"{broker} 纪律类别缺少参数计数：{summary_text}"
        )
        if card.get_attribute("open") is None:
            summary.click()
        assert card.locator(".trend-discipline-category-body").count() == 1, (
            f"{broker} 纪律类别缺少参数详情"
        )
        summary.focus()
        assert summary.evaluate("element => element === document.activeElement"), (
            f"{broker} 纪律摘要不可键盘聚焦"
        )
    evaluate_all = getattr(summaries, "evaluate_all", None)
    if callable(evaluate_all):
        boxes = evaluate_all(
            "nodes => nodes.map(node => ({height: node.getBoundingClientRect().height}))"
        )
        assert all(box.get("height", 0) >= 44 for box in boxes), (
            f"{broker} 纪律摘要不足 44px：{boxes}"
        )
    workspace_text = report_root.inner_text()
    if has_rows:
        for row in rows:
            assert isinstance(row, Mapping), f"{broker} 策略参数行格式无效"
            for key in ("group", "name", "value"):
                value = row.get(key)
                assert value is not None and str(value) in workspace_text, (
                    f"{broker} 纪律缺少 {key}：{value}"
                )
    else:
        assert "本报告未提供该类纪律参数" in workspace_text, (
            f"{broker} 无纪律参数时缺少明确空状态"
        )
        assert "趋势强度不低于 95" not in workspace_text, (
            f"{broker} 无纪律参数时泄漏当前入场规则"
        )
    cost = report.get("api_cost")
    if isinstance(cost, Mapping) and cost.get("label"):
        assert str(cost["label"]) in workspace_text, (
            f"{broker} 未显示冻结 API 成本标签"
        )
    contexts = report.get("industry_contexts")
    status = report.get("industry_context_status")
    context_section = report_root.locator(".trend-industry-context")
    assert context_section.count() == 1, f"{broker} 缺少行业上下文区"
    context_text = context_section.inner_text()
    context_rows = contexts if isinstance(contexts, list) else []
    if not context_rows and not has_rows:
        assert "当前行业上下文未提供，无法确认排序" in context_text, (
            f"{broker} 无行业上下文时缺少明确回退提示"
        )
    for context in context_rows:
        assert isinstance(context, Mapping), f"{broker} 行业上下文格式无效"
        for key in ("industry", "temperature", "strength", "warm_to_hot_count"):
            value = context.get(key)
            if value is not None:
                expected = _trend_context_display_value(key, value)
                assert expected in context_text, (
                    f"{broker} 行业上下文缺少 {key}：{value}"
                )
        count_text = _trend_ratio_transition(
            context.get("aggregate_right_count_ratio"),
            context.get("prior_aggregate_right_count_ratio"),
        )
        market_cap_text = _trend_ratio_transition(
            context.get("aggregate_right_market_cap_ratio"),
            context.get("prior_aggregate_right_market_cap_ratio"),
        )
        assert count_text in context_text, f"{broker} 行业右侧个数占比未显示：{count_text}"
        assert market_cap_text in context_text, f"{broker} 行业右侧市值占比未显示：{market_cap_text}"
    if isinstance(status, Mapping) and (
        str(status.get("ordering_mode", "")).startswith("legacy")
        or status.get("current_complete") is False
        or any(
            isinstance(context, Mapping) and context.get("valid") is False
            for context in context_rows
        )
    ):
        assert "当前行业上下文无效" in context_text, (
            f"{broker} 行业上下文无效时缺少回退提示"
        )
    if page is not None and (
        getattr(page, "viewport_size", None) or {}
    ).get("width", 0) <= 760:
        ordered_selectors = (
            ".cn-trend-sell", ".cn-trend-buy", ".cn-trend-review",
            ".cn-trend-hold", ".trend-industry-context",
            ".trend-discipline-workspace", ".trend-risk-summary",
            ".trend-controller-status", ".trend-audit",
        )
        boxes: list[dict[str, object]] = []
        try:
            for selector in ordered_selectors:
                locator = report_root.locator(selector)
                if locator.count() != 1:
                    continue
                box = locator.bounding_box()
                if not isinstance(box, Mapping) or "y" not in box:
                    boxes = []
                    break
                boxes.append(dict(box))
        except (AssertionError, AttributeError):
            boxes = []
        if boxes:
            assert all(
                float(boxes[index]["y"]) <= float(boxes[index + 1]["y"])
                for index in range(len(boxes) - 1)
            ), f"{broker} 移动端趋势报告顺序不符合行动优先布局：{boxes}"
    if page is not None:
        metrics = context_section.locator("tbody .trend-industry-metric")
        if metrics.count():
            market_metric = context_section.locator(
                'tbody [data-trend-industry-help*="不是账户仓位或上涨概率"]'
            )
            if market_metric.count() == 0:
                market_metric = metrics.nth(0)
            else:
                market_metric = market_metric.first
            tooltip = context_section.locator(".trend-industry-tooltip")
            market_metric.hover()
            visible = getattr(tooltip, "is_visible", None)
            assert visible() if callable(visible) else tooltip.get_attribute("hidden") is None, (
                f"{broker} 行业指标 hover 未显示解释"
            )
            if callable(getattr(tooltip, "inner_text", None)):
                tooltip_text = tooltip.inner_text()
                if market_metric.get_attribute("data-trend-industry-help") and "不是账户仓位或上涨概率" in market_metric.get_attribute("data-trend-industry-help"):
                    assert "不是账户仓位或上涨概率" in tooltip_text, (
                        f"{broker} 右侧市值占比 tooltip 缺少口径说明"
                    )
            focus = getattr(market_metric, "focus", None)
            if callable(focus):
                focus()
            click = getattr(market_metric, "click", None)
            if callable(click):
                click()
            press = getattr(market_metric, "press", None)
            if callable(press):
                press("Escape")
            if callable(visible):
                assert not visible(), f"{broker} 行业指标 Escape 未关闭解释"
        if getattr(page, "viewport_size", None) and page.viewport_size.get("width", 0) <= 760:
            cells = context_section.locator(
                'td[data-label="右侧个数占比"], td[data-label="右侧市值占比"]'
            )
            evaluate_all = getattr(cells, "evaluate_all", None)
            if callable(evaluate_all):
                boxes = evaluate_all(
                    "nodes => nodes.map(node => ({height: node.getBoundingClientRect().height, width: node.getBoundingClientRect().width}))"
                )
                assert all(
                    box.get("height", 0) >= 44 and box.get("width", 0) > 0
                    for box in boxes
                ), f"{broker} 移动端行业指标点击区域不足 44px：{boxes}"
    if workspace.get_attribute("open") is not None:
        workspace_summary.click()


def _check_report_identity(
    actual: Mapping[str, Any], expected: Mapping[str, Any], broker: str,
) -> None:
    keys = ("artifact", "report_sha256", "strategy_version")
    actual_identity = {key: str(actual.get(key) or "") for key in keys}
    wanted = {key: str(expected.get(key) or "") for key in keys}
    assert actual_identity == wanted, (
        f"{broker} 精确历史报告身份不匹配：{actual_identity} != {wanted}"
    )


def _check_trend_account_views(
    page: Any,
    payload: Mapping[str, Any],
    simulate_payloads: Mapping[str, Mapping[str, Any]],
    history_expectations: Mapping[str, list[Mapping[str, Any]]],
    *,
    screenshot_dir: Path | None = None,
) -> None:
    reports = payload.get("trend_reports")
    reviews = payload.get("trend_reviews")
    controllers = payload.get("trend_controllers")
    assert (
        isinstance(reports, Mapping)
        and isinstance(reviews, Mapping)
        and isinstance(controllers, Mapping)
    )
    batch_errors = _trend_execution_batch_errors(payload)
    assert not batch_errors, "；".join(batch_errors)
    for broker in TREND_SIMULATE_MARKETS:
        section = _select_account_tab(page, broker)
        _check_account_view_contract(page, section, broker)
        panel = section.locator(f"#account-{broker}-view-panel")
        simulate_tab = section.locator('[data-account-view="simulate"]')
        simulated = simulate_payloads.get(broker)
        positions = simulated.get("positions") if simulated is not None else []
        if simulated is not None:
            assert isinstance(positions, list)
        simulate_tab.click()
        _wait_for_simulate_positions(
            page, broker, len(positions) if simulated is not None else None
        )
        rows = panel.locator(".account-holding-row")
        if simulated is None:
            assert rows.count() == 0, f"{broker} Futu 不可用时显示了替代持仓"
        else:
            assert rows.count() == len(positions), f"{broker} 模拟盘持仓行数不匹配"
            for index, position in enumerate(positions):
                assert isinstance(position, Mapping)
                row = rows.nth(index)
                assert row.locator(".account-holding-symbol strong").inner_text().strip() == str(
                    position.get("symbol")
                ), f"{broker} 模拟盘持仓代码未显示"
                assert row.locator(".account-holding-quantity").inner_text().strip().endswith(
                    _display_number(position.get("quantity"))
                ), f"{broker} 模拟盘持仓数量未显示"
                assert row.locator(".account-holding-cost").inner_text().strip().endswith(
                    _display_number(position.get("cost_price"))
                ), f"{broker} 模拟盘持仓成本价未显示"
                if position.get("attribution_status") == "unlinked":
                    assert "未关联历史报告" in row.inner_text(), (
                        f"{broker} 未关联模拟持仓被隐藏或缺少标记"
                    )
            linked = [
                position for position in positions
                if isinstance(position, Mapping)
                and position.get("attribution_status") == "linked"
            ]
            links = panel.locator(".report-attribution-link")
            assert links.count() == len(linked), f"{broker} 模拟持仓报告入口数量不匹配"
            if linked:
                report = linked[0].get("report")
                assert isinstance(report, Mapping)
                artifact = str(report.get("artifact") or "")
                exact_path = f"/api/trend-reports/{broker}/history/{artifact}"
                _check_history_control_contract(links.first, f"{broker} 模拟持仓报告入口")
                with page.expect_response(
                    lambda response: response.url.endswith(exact_path)
                ) as response_info:
                    links.first.click()
                response = response_info.value
                assert response.ok, f"{broker} 精确历史报告请求失败：{response.status}"
                loaded = response.json()
                assert isinstance(loaded, Mapping)
                _check_report_identity(loaded, report, broker)
                panel.locator("[data-current-trend-report]").wait_for()
                _check_loaded_report_identity(panel, report, broker)
                _check_frozen_trend_disciplines(
                    panel.locator(".cn-trend-report"), loaded, broker, page=page
                )
                current = panel.locator("[data-current-trend-report]")
                _check_history_control_contract(current, f"{broker} 返回当前报告")
                current.click()
                history = panel.locator("[data-report-history]")
                history.wait_for()
                assert history.evaluate("node => node === document.activeElement"), (
                    f"{broker} 模拟持仓报告返回后焦点未恢复"
                )
                simulate_tab.click()
                _wait_for_simulate_positions(page, broker, len(positions))
        assert simulate_tab.get_attribute("aria-selected") == "true", (
            f"{broker} 模拟盘加载后 Tab 状态丢失"
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 模拟盘视图出现横向滚动"

        report_tab = section.locator('[data-account-view="report"]')
        report_tab.click()
        report_root = panel.locator(".cn-trend-report")
        report_root.wait_for()
        report = reports.get(broker)
        assert isinstance(report, Mapping) and report.get("available") is True, (
            f"{broker} 当前趋势报告不可用"
        )
        _check_trend_controller_status(
            page, panel, broker, controllers.get(broker)
        )
        _check_integrated_trend_ui(report_root, report, broker)
        _check_frozen_trend_disciplines(report_root, report, broker, page=page)
        _check_trend_rotation_visibility(report_root, report, broker)
        assert _plain(report.get("report_date")) in report_root.inner_text(), (
            f"{broker} 当前趋势报告日期未显示"
        )
        history_button = panel.locator("[data-report-history]")
        assert history_button.count() == 1, f"{broker} 当前报告缺少历史入口"
        _check_history_control_contract(history_button, f"{broker} 历史报告入口")
        expectations = history_expectations.get(broker) or []
        if expectations:
            history_button.click()
            expectation = expectations[0]
            artifact = str(expectation["artifact"])
            exact = panel.locator(f'[data-history-artifact="{artifact}"]')
            exact.wait_for()
            exact.click()
            current = panel.locator("[data-current-trend-report]")
            current.wait_for()
            _check_loaded_report_identity(panel, expectation, broker)
            assert panel.locator("details.trend-review-disclosure").count() == 0, (
                f"{broker} 历史趋势报告混入当前趋势复盘"
            )
            _check_frozen_trend_disciplines(
                panel.locator(".cn-trend-report"), expectation, broker, page=page
            )
            _check_history_control_contract(current, f"{broker} 历史报告返回")
            historical_text = panel.inner_text()
            assert panel.locator(".cn-trend-execution").count() == 0, (
                f"{broker} 精确历史报告仍包含已删除的执行状态行"
            )
            assert not any(
                label in historical_text for label in REMOVED_TREND_EXECUTION_LABELS
            ), f"{broker} 精确历史报告仍包含已删除的执行状态文案"
            current.click()
            history_button = panel.locator("[data-report-history]")
            history_button.wait_for()
            assert history_button.evaluate("node => node === document.activeElement"), (
                f"{broker} 历史报告返回后焦点未恢复"
            )
        assert report_tab.get_attribute("aria-selected") == "true", (
            f"{broker} 历史报告返回后趋势报告 Tab 丢失"
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势报告视图出现横向滚动"

        assert section.locator('[data-account-view="review"]').count() == 0, (
            f"{broker} 仍存在独立复盘 Tab"
        )
        review_disclosure = panel.locator("details.trend-review-disclosure")
        assert review_disclosure.count() == 1, f"{broker} 趋势复盘折叠栏目数量不是 1"
        assert review_disclosure.get_attribute("open") is None, (
            f"{broker} 趋势复盘默认未折叠"
        )
        review_disclosure.locator(":scope > summary").click()
        review_root = review_disclosure.locator(".trend-review")
        review_root.wait_for()
        review = reviews.get(broker)
        assert isinstance(review, Mapping) and review.get("available") is True, (
            f"{broker} 趋势复盘不可用"
        )
        text = review_root.inner_text()
        assert "卡玛比率" in text and "夏普比率" in text, (
            f"{broker} 趋势复盘指标不完整"
        )
        assert report_tab.get_attribute("aria-selected") == "true", (
            f"{broker} 复盘未合入趋势报告 Tab"
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势复盘视图出现横向滚动"
        _capture_trend_review_screenshot(page, broker, screenshot_dir)
        section.locator('[data-account-view="real"]').click()


def _check_separated_trend_report_views(
    page: Any,
    payload: Mapping[str, Any],
    *,
    screenshot_dir: Path | None = None,
) -> None:
    reports = payload.get("trend_reports")
    reviews = payload.get("trend_reviews")
    assert isinstance(reports, Mapping), "API 缺少趋势报告"
    assert isinstance(reviews, Mapping), "API 缺少趋势复盘"
    for broker in TREND_SIMULATE_MARKETS:
        section = _select_account_tab(page, broker)
        panel = section.locator(f"#account-{broker}-view-panel")
        report_tab = section.locator('[data-account-view="report"]')
        report_tab.click()
        report_root = panel.locator(".cn-trend-report")
        report_root.wait_for()
        report = reports.get(broker)
        assert isinstance(report, Mapping) and report.get("available") is True, (
            f"{broker} 当前趋势报告不可用"
        )
        _check_integrated_trend_ui(report_root, report, broker)
        visible_text = report_root.inner_text()
        assert report_root.locator(".cn-trend-execution").count() == 0, (
            f"{broker} 趋势报告仍包含已删除的执行状态行"
        )
        assert not any(
            label in visible_text
            for label in REMOVED_TREND_REPORT_POSITION_LABELS
        ), f"{broker} 趋势报告仍混入持仓或执行信息"
        review = reviews.get(broker)
        assert isinstance(review, Mapping) and review.get("available") is True, (
            f"{broker} 当前趋势复盘不可用"
        )
        review_disclosure = panel.locator("details.trend-review-disclosure")
        assert review_disclosure.count() == 1, f"{broker} 趋势复盘折叠栏目数量不是 1"
        assert review_disclosure.get_attribute("open") is None, (
            f"{broker} 趋势复盘默认未折叠"
        )
        review_disclosure.locator(":scope > summary").click()
        review_root = review_disclosure.locator(".trend-review")
        assert review_root.count() == 1, f"{broker} 趋势复盘未合入趋势报告"
        assert f"{_plain(review.get('market_label'))}趋势复盘" in review_root.inner_text()
        if broker == "eastmoney" and screenshot_dir is not None:
            width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
            page.screenshot(
                path=str(screenshot_dir / f"{width}-trend-report.png"),
                full_page=True,
            )
        section.locator('[data-account-view="real"]').click()


def _check_history_endpoints(
    url: str,
    data_dir: Path,
    reports_dir: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    expected_by_broker: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for broker, market in TREND_SIMULATE_MARKETS.items():
        try:
            reports = _reports_by_hash(
                reports_dir / TREND_REPORT_DIRECTORIES[broker],
                broker=broker,
                market=market,
            )
            history = _fetch_json_path(url, f"/api/trend-reports/{broker}/history")
            artifacts = {
                reports[report_hash]["artifact"]
                for _, _, _, event in _action_events(data_dir, market)
                if (
                    len(report_hash := str(event.get("report_sha256") or "").lower())
                    == 64
                    and report_hash in reports
                )
            }
            latest_artifact = ""
            if isinstance(history, list):
                latest_artifact = next(
                    (
                        str(row.get("artifact"))
                        for row in history
                        if isinstance(row, Mapping)
                        and row.get("available") is True
                        and row.get("artifact")
                    ),
                    "",
                )
                if latest_artifact:
                    artifacts.add(latest_artifact)
            exact = {
                artifact: _fetch_json_path(
                    url, f"/api/trend-reports/{broker}/history/{artifact}"
                )
                for artifact in artifacts
            }
            expectations = _validate_history_projection(
                data_dir, reports_dir, broker, history, exact
            )
            if latest_artifact and not any(
                item.get("artifact") == latest_artifact for item in expectations
            ):
                latest = exact.get(latest_artifact)
                assert isinstance(latest, Mapping), (
                    f"{latest_artifact} 精确历史报告缺失"
                )
                local = next(
                    (
                        report for report in reports.values()
                        if report.get("artifact") == latest_artifact
                    ),
                    None,
                )
                assert isinstance(local, Mapping), (
                    f"{latest_artifact} 本地冻结报告缺失"
                )
                _check_report_identity(latest, local, broker)
                expectations.append(dict(latest))
            expected_by_broker[broker] = expectations
        except Exception as exc:
            errors.append(f"{broker} 历史报告检查失败：{type(exc).__name__}: {exc}")
    return expected_by_broker, errors


def _effective_reports_dir(
    payload: Mapping[str, Any], *, process_cwd: Path
) -> Path:
    value = payload.get("reports_dir")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Dashboard reports_dir 缺失或不是非空字符串")
    try:
        configured = Path(value)
        if configured.is_absolute():
            resolved = configured.resolve()
        else:
            root = process_cwd.resolve()
            resolved = (root / configured).resolve()
            resolved.relative_to(root)
        if not resolved.is_dir():
            raise ValueError("目录不存在或不是目录")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Dashboard reports_dir 无效：{value!r}（{exc}）") from exc
    return resolved


def _listener(url: str) -> tuple[int, Path]:
    port = url.rsplit(":", 1)[-1].rstrip("/")
    pid_text = subprocess.check_output(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], text=True
    ).strip().splitlines()
    if len(pid_text) != 1:
        raise RuntimeError(f"端口 {port} 没有唯一监听进程")
    pid = int(pid_text[0])
    return pid, _process_cwd(pid)


def _process_cwd(pid: int) -> Path:
    output = subprocess.check_output(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True
    )
    cwd_line = next((line for line in output.splitlines() if line.startswith("n")), "")
    if not cwd_line:
        raise RuntimeError("无法读取 Dashboard 进程工作目录")
    return Path(cwd_line[1:]).resolve()


def _process_started_at(pid: int) -> datetime:
    return datetime.strptime(
        subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="], text=True
        ).strip(),
        "%a %b %d %H:%M:%S %Y",
    ).astimezone()


def _source_changes(cwd: Path) -> list[str]:
    output = subprocess.check_output(
        [
            "git", "-C", str(cwd), "status", "--porcelain",
            "--untracked-files=all",
        ],
        text=True,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def _runtime_evidence(
    name: str,
    *,
    url: str,
    expected_schema: str,
    expected_module: str,
    expected_root: Path,
    expected_sha: str,
    expected_upstream_status: str | None = None,
    expected_account_upstream_status: str | None = None,
    account_api: bool = False,
) -> tuple[int | None, Path, datetime | None, list[str]]:
    expected_cwd = expected_root.resolve()
    try:
        pid, cwd = _listener(url)
        process_started_at = _process_started_at(pid)
        errors: list[str] = []
        if cwd != expected_cwd:
            errors.append(f"{name} 运行目录不匹配（运行工作目录）：{cwd}")
        running_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
        if running_sha != expected_sha:
            errors.append(
                f"{name} 运行 Git SHA 不匹配："
                f"{running_sha[:7]} != {expected_sha[:7]}"
            )
        source_changes = _source_changes(cwd)
        if source_changes:
            errors.append(f"{name} 源码未提交：{'；'.join(source_changes)}")
        health = _fetch_json_path(url, "/healthz")
        if account_api:
            errors.extend(_account_runtime_health_errors(
                health,
                pid=pid,
                expected_sha=expected_sha,
                process_started_at=process_started_at,
            ))
        else:
            errors.extend(_runtime_health_errors(
                health,
                name=name,
                expected_schema=expected_schema,
                expected_module=expected_module,
                pid=pid,
                expected_sha=expected_sha,
                expected_cwd=expected_cwd,
                process_started_at=process_started_at,
                expected_upstream_status=expected_upstream_status,
                expected_account_upstream_status=expected_account_upstream_status,
            ))
        return pid, cwd, process_started_at, errors
    except Exception as exc:
        return (
            None,
            expected_cwd,
            None,
            [f"{name} 运行检查失败：{type(exc).__name__}: {exc}"],
        )


def _is_actionable_console_error(message: str) -> bool:
    # Chrome can emit an unattributed favicon 404 without exposing a response.
    # HTTP failures for actual page resources and APIs are checked separately.
    return not (
        message.startswith("Failed to load resource:")
        and "status of 404" in message
    )


def _first_in_scope_holding(payload: dict[str, Any]) -> tuple[str, str, str]:
    for holding in payload.get("holdings") or []:
        brokers = {
            "phillips" if value == "phillip" else value
            for value in [
                *(str(holding.get("brokers") or "").lower().split(";")),
                str(holding.get("broker") or "").lower(),
                *(
                    str(detail.get("broker") or "").lower()
                    for detail in holding.get("broker_details") or []
                    if isinstance(detail, Mapping)
                ),
            ]
            if value
        }
        broker = next((item for item in ACCOUNT_BROKERS if item in brokers), "")
        if broker:
            return str(holding.get("market", "")), str(holding.get("symbol", "")), broker
    raise AssertionError("no account holding exists in Dashboard payload")


def _dashboard_holding_key(
    payload: Mapping[str, Any], market: str, symbol: str,
) -> str:
    for index, holding in enumerate(payload.get("holdings") or []):
        if (
            isinstance(holding, Mapping)
            and str(holding.get("market", "")) == market
            and str(holding.get("symbol", "")) == symbol
        ):
            return ":".join((market, symbol, str(holding.get("name", "")), str(index)))
    raise AssertionError(f"{market}.{symbol} is missing from Dashboard payload")


def _check_mobile_targets(page: Any, selector: str) -> None:
    targets = page.locator(selector)
    assert targets.count() >= 1, f"移动端缺少交互控件：{selector}"
    boxes = targets.evaluate_all(
        "nodes => nodes.map(node => ({"
        "height: node.getBoundingClientRect().height, "
        "label: node.getAttribute('aria-label') || node.textContent.trim() || node.tagName"
        "}))"
    )
    for box in boxes:
        assert box["height"] >= 44, f"{box['label']} 高度不足 44px"


def _check_tool_workspaces(page: Any, detail_key: str) -> None:
    mobile = (getattr(page, "viewport_size", None) or {}).get("width", 0) <= 760
    if mobile:
        _check_mobile_targets(
            page,
            '#account-tabs [role="tab"]:visible, #header-market-filters button:visible, '
            ".strategy-tools button:visible, "
            ".broker-summary-card:visible, .account-holding-actions button:visible",
        )
        t_signal_button = page.locator(
            '.account-holding-actions button[data-detail-mode="t_signal"]:visible'
        )
        if t_signal_button.count():
            t_signal_button.first.click()
            _check_mobile_targets(
                page,
                ".symbol-detail-panel.inline-symbol-detail:visible button:visible, "
                ".symbol-detail-panel.inline-symbol-detail:visible input:visible, "
                ".symbol-detail-panel.inline-symbol-detail:visible select:visible",
            )
            back_button = page.locator("[data-back-to-holdings]:visible")
            assert back_button.count() >= 1, "做T详情缺少返回入口"
            back_button.first.click()
            assert page.locator(".holdings-panel:visible").count() == 1, (
                "做T详情返回后持仓未恢复"
            )
        else:
            assert page.locator(".account-review-action:visible").count() >= 1, (
                "移动端既无做T详情入口，也未显示人工复核"
            )

    page.locator('#main-navigation [data-workspace="kelly_lab"]').click()
    assert page.locator(".kelly-lab-panel:visible").count() == 1, (
        "Kelly Lab 工作区未显示"
    )
    if mobile:
        _check_mobile_targets(
            page, "#return-to-portfolio:visible, .kelly-lab-panel button:visible"
        )
    page.locator("#return-to-portfolio:visible").click()
    assert page.locator(".holdings-panel:visible").count() == 1, (
        "Kelly Lab 返回后持仓未恢复"
    )

    page.locator('#main-navigation [data-workspace="standard_backtest"]').click()
    assert page.locator("#standard-backtest-workspace:visible").count() == 1, (
        "标准回测工作区未显示"
    )
    if mobile:
        _check_mobile_targets(
            page,
            "#standard-backtest-workspace button:visible, "
            "#standard-backtest-workspace input:visible, "
            "#standard-backtest-workspace select:visible",
        )
    page.locator("#return-to-portfolio:visible").click()
    assert page.locator(".holdings-panel:visible").count() == 1, (
        "标准回测返回后持仓未恢复"
    )

    trigger = page.locator("[data-research-chat]:visible")
    if trigger.count():
        trigger.first.click()
    else:
        page.evaluate("detailKey => openResearchChat(detailKey)", detail_key)
    try:
        assert page.locator(".research-chat-modal:visible").count() == 1, (
            "投研讨论弹窗未显示"
        )
        if mobile:
            _check_mobile_targets(
                page,
                ".research-chat-modal button:visible, "
                ".research-chat-modal input:visible",
            )
    finally:
        close = page.locator("#research-chat-close:visible")
        if close.count():
            close.click()
    assert page.locator(".research-chat-modal:visible").count() == 0, (
        "投研讨论弹窗关闭失败"
    )


def _plain(value: Any) -> str:
    return "-" if value is None or str(value).strip() == "" else str(value)


def _trend_review_strategy_version(value: Any) -> str:
    version = _plain(value)
    match = re.fullmatch(r"v(.+)", version, flags=re.IGNORECASE)
    return f"第 {match.group(1)} 版" if match else version
def _trend_audit_reason_label(reason: Any) -> str:
    code = _plain(reason)
    mapped = TREND_REASON_LABELS.get(code)
    if mapped is None:
        return code
    for prefix, label in (
        ("filter_price_", "筛选价"),
        ("strength_", "趋势强度"),
        ("industry_temperature_", "行业温度"),
        ("market_cap_", "总市值"),
        ("amount_", "日成交额"),
        ("right_side_days_", "右侧天数"),
    ):
        if code.startswith(prefix):
            return label
    return {
        "a_share_only": "资产类型",
        "temperature_missing": "个股温度",
        "temperature_transition_not_entry": "温度变化",
        "industry_id_missing": "行业 ID",
        "phase_missing": "趋势节气",
        "phase_after_summer_solstice": "趋势节气",
        "right_side_not_true": "右侧趋势",
        "not_tradable": "交易状态",
        "danger_signal": "危险信号",
        "danger_unknown": "危险信号",
        "name_missing": "标的名称",
        "asset_missing": "资产类型",
        "unsupported_asset": "资产类型",
        "already_held": "账户状态",
        "excluded_security": "证券范围",
        "unsupported_exchange": "交易所",
        "atr_unavailable": "ATR14",
        "data_date_mismatch": "数据日期",
    }.get(code, mapped)


def _check_visible_decimal_precision(text: str, label: str) -> None:
    offenders = re.findall(
        r"(?<![\w.-])[+-]?\d[\d,]*\.\d{3,}(?![\w.-])", text
    )
    assert not offenders, f"{label} 数值超过两位小数：{offenders[:3]}"


def _check_integrated_trend_ui(
    report_root: Any, report: Mapping[str, Any], broker: str,
) -> None:
    summary = report.get("risk_summary")
    drawdown = report.get("drawdown_summary")
    assert (
        isinstance(summary, Mapping)
        and isinstance(drawdown, Mapping)
    ), f"{broker} 趋势报告缺少集成风险视图数据"
    risk = report_root.locator(".trend-risk-summary")
    assert risk.count() == 1, f"{broker} 趋势报告缺少风险摘要"
    assert risk.get_attribute("open") is None, f"{broker} 风险摘要未默认收起"
    risk.locator(":scope > summary").click()
    assert risk.get_attribute("data-risk-status") == summary.get("status"), (
        f"{broker} 风险状态未同时提供文字状态"
    )
    assert report_root.locator(".trend-drawdown-summary").count() == 1, (
        f"{broker} 趋势报告缺少回撤状态"
    )
    for selector in (".trend-simulation-overlay", ".trend-actual-overlay"):
        assert report_root.locator(selector).count() == 0, (
            f"{broker} 趋势报告仍包含已删除的 {selector}"
        )
    bootstrap = drawdown.get("bootstrap_event")
    if isinstance(bootstrap, Mapping):
        audit = risk.locator(".trend-drawdown-bootstrap-audit")
        assert audit.count() == 1, f"{broker} 缺少回撤基准审计详情"
        audit.locator("summary").click()
    recovery = drawdown.get("recovery_event")
    if isinstance(recovery, Mapping):
        audit = risk.locator(".trend-drawdown-recovery-audit")
        assert audit.count() == 1, f"{broker} 缺少状态恢复审计详情"
        audit.locator("summary").click()
    text = risk.inner_text()
    _check_visible_decimal_precision(text, f"{broker} 风险摘要")
    for stage_text in report_root.locator(".trend-stage:visible").all_inner_texts():
        _check_visible_decimal_precision(stage_text, f"{broker} 趋势报告")
    stats = summary.get("trade_stats")
    actual_label = (
        stats.get("actual_broker_label") if isinstance(stats, Mapping) else ""
    )
    required = (
        "组合计划风险", "组合剩余风险", "单笔风险上限", "异常损失缓冲",
        "不得用于开仓", "Kelly 阶段", "当前 Kelly 上限",
        "富途模拟盘交易统计", f"{_plain(actual_label)}实盘交易统计",
        "策略累计回撤", _plain(summary.get("status_label")),
        _plain(drawdown.get("status_label")),
        "5% 是风险预算目标，不是最大损失保证。",
    )
    for value in required:
        assert value != "-" and value in text, f"{broker} 集成风险视图缺少 {value}"
    if isinstance(bootstrap, Mapping):
        baseline_equity = _display_number(bootstrap.get("baseline_equity"))
        assert baseline_equity in text, (
            f"{broker} 回撤基准审计未显示 {baseline_equity}"
        )
        for value in (
            "回撤基准审计详情",
            bootstrap.get("source_date"),
            bootstrap.get("event_id"),
            bootstrap.get("accepted_git_sha"),
            bootstrap.get("parameter_hash"),
            bootstrap.get("actor"),
            bootstrap.get("occurred_at"),
            bootstrap.get("entry_eligible_from"),
        ):
            assert _plain(value) in text, f"{broker} 回撤基准审计未显示 {_plain(value)}"
        if str(bootstrap.get("occurred_at") or "")[:10] == str(
            report.get("report_date") or ""
        ):
            assert "基准已自动建立" in text, f"{broker} 当日自动建基准提示未显示"
    if isinstance(recovery, Mapping):
        for value in (
            "状态恢复审计详情",
            recovery.get("event_id"),
            recovery.get("snapshot"),
            recovery.get("state_sha256"),
            recovery.get("actor"),
            recovery.get("occurred_at"),
        ):
            assert _plain(value) in text, f"{broker} 状态恢复审计未显示 {_plain(value)}"
    assert "本次可用风险" not in text, f"{broker} UI 仍包含 本次可用风险"
    if risk.get_attribute("open") is not None:
        risk.locator(":scope > summary").click()


def _check_trend_rotation_visibility(
    report_root: Any, report: Mapping[str, Any], broker: str,
) -> None:
    if not report.get("allocation"):
        assert report_root.locator(".trend-rotation-panel").count() == 0, (
            f"{broker} 无资源排名时仍显示轮换面板"
        )
        return
    panel = report_root.locator(".trend-rotation-panel")
    assert panel.count() == 1, f"{broker} 缺少相对强度轮换面板"
    for mode, key in (
        ("automatic", "simulate_rotation_comparisons"),
        ("manual", "real_rotation_comparisons"),
    ):
        comparisons = report.get(key)
        comparisons = comparisons if isinstance(comparisons, list) else []
        group = panel.locator(f'.trend-rotation-group[data-mode="{mode}"]')
        assert group.count() == 1, f"{broker} 缺少 {mode} 轮换组"
        text = group.inner_text()
        for comparison in comparisons:
            assert isinstance(comparison, Mapping), f"{broker} 轮换比较格式无效"
            for value in (
                comparison.get("sell_symbol"), comparison.get("sell_name"),
                comparison.get("buy_symbol"), comparison.get("buy_name"),
            ):
                if value:
                    assert str(value) in text, f"{broker} 轮换比较缺少 {value}"
            basis = comparison.get("strength_basis")
            basis_label = {
                "local": "大类内强度",
                "global": "全局强度",
            }.get(str(basis), "数据不可用")
            assert basis_label in text, f"{broker} 轮换比较缺少比较口径"
            assert f"强度差" in text, f"{broker} 轮换比较缺少强度差"
            outcome = str(comparison.get("outcome") or "")
            if outcome == "gap_below_threshold":
                assert "未触发 · 门槛" in text and "还差" in text, (
                    f"{broker} 轮换比较缺少门槛未触发原因"
                )
            elif outcome == "sizing_blocked":
                assert "未执行" in text, f"{broker} 轮换比较缺少仓位阻断原因"
            elif outcome == "data_unavailable":
                assert "数据不可用" in text or "未触发" in text, (
                    f"{broker} 轮换比较缺少数据不可用原因"
                )


def _display_number(value: Any) -> str:
    raw = _plain(value).strip()
    match = re.fullmatch(r"([+-]?)(\d+)(?:\.(\d+))?", raw)
    if match is None:
        return raw
    sign, integer, fraction = match.groups()
    fraction = fraction or ""
    digits = list(f"{integer}{fraction[:2].ljust(2, '0')}")
    if len(fraction) > 2 and fraction[2] >= "5":
        for index in range(len(digits) - 1, -1, -1):
            if digits[index] != "9":
                digits[index] = str(int(digits[index]) + 1)
                break
            digits[index] = "0"
        else:
            digits.insert(0, "1")
    rounded = "".join(digits)
    grouped = re.sub(r"\B(?=(\d{3})+(?!\d))", ",", rounded[:-2])
    decimals = rounded[-2:].rstrip("0")
    return f"{sign}{grouped}{f'.{decimals}' if decimals else ''}"


def _display_price(value: Any) -> str:
    raw = _plain(value).strip()
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return raw
    if not number.is_finite():
        return raw
    return _display_number(format(number, "f"))


def _check_displayed_protection_prices(values: list[str]) -> None:
    assert values, "A 股趋势报告缺少保护线价格"
    assert all(
        re.fullmatch(r"(?:-|[+-]?\d+(?:,\d{3})*(?:\.\d{1,2})?)", value.strip())
        for value in values
    ), "A 股趋势报告保护线超过两位小数"


def _trend_action_needs_review(item: Mapping[str, Any]) -> bool:
    action = item.get("action")
    reason = item.get("reason")
    known_reason = isinstance(reason, str) and reason in TREND_REASON_LABELS
    if action == "BUY":
        return reason not in (None, "") and not known_reason
    if action == "SELL_PARTIAL":
        return not _valid_partial_trend_action(dict(item))
    return (
        action == "MANUAL_REVIEW"
        or action not in {"SELL_ALL", "SELL_PARTIAL", "HOLD", "MANUAL_REVIEW"}
        or action in {"SELL_ALL", "HOLD"} and not known_reason
    )


def _finite_decimal(value: object) -> bool:
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, TypeError, ValueError):
        return False


def _valid_account_source_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    format_ = {7: "%Y-%m", 10: "%Y-%m-%d"}.get(len(value))
    if format_ is None:
        return False
    try:
        return datetime.strptime(value, format_).strftime(format_) == value
    except ValueError:
        return False


def _valid_trend_position(value: object) -> bool:
    if not isinstance(value, Mapping) or any(
        not isinstance(value.get(field), str) or not value[field].strip()
        for field in ("symbol", "name", "asset_class")
    ):
        return False
    average_cost = value.get("avg_cost_price")
    return (
        _finite_decimal(value.get("quantity"))
        and _finite_decimal(value.get("market_value"))
        and "avg_cost_price" in value
        and (average_cost is None or _finite_decimal(average_cost))
    )


def _valid_trend_account(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    positions = value.get("positions")
    exceptions = value.get("exceptions")
    return (
        _valid_account_source_date(value.get("source_date"))
        and _finite_decimal(value.get("net_value"))
        and _finite_decimal(value.get("available_cash"))
        and isinstance(positions, list)
        and all(_valid_trend_position(item) for item in positions)
        and isinstance(exceptions, list)
        and all(isinstance(item, str) for item in exceptions)
    )


def _check_trend_artifact_projection(
    reports_dir: Path, broker: str, report: Mapping[str, Any]
) -> None:
    audit = report.get("audit")
    audit = audit if isinstance(audit, Mapping) else {}
    artifact = audit.get("artifact")
    assert (
        isinstance(artifact, str)
        and artifact.endswith(".json")
        and Path(artifact).name == artifact
    ), f"{broker} 趋势报告产物文件名无效"
    directory = TREND_REPORT_DIRECTORIES[broker]
    path = reports_dir / directory / artifact
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"{broker} 冻结趋势报告无法读取：{exc}") from exc
    assert isinstance(payload, Mapping), f"{broker} 冻结趋势报告不是对象"
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    expected_market = {"tiger": "US", "phillips": "HK", "eastmoney": "CN"}[broker]
    assert (
        payload.get("execution_date") == report.get("report_date")
        and payload.get("as_of_date") == report.get("data_date")
        and payload.get("generated_at") == report.get("generated_at")
        and metadata.get("market") == expected_market
        and metadata.get("broker") == broker
    ), f"{broker} 冻结报告身份与 API 投影不一致"
    assert _valid_trend_account(payload.get("account")), (
        f"{broker} 冻结报告账户快照无效"
    )
    judgments = payload.get("strategy_judgments")
    assert isinstance(judgments, Mapping), f"{broker} 冻结报告缺少策略判断"
    formal = judgments.get("formal_actions")
    holdings = judgments.get("holding_decisions")
    assert isinstance(formal, list) and all(
        isinstance(item, Mapping) for item in formal
    ), f"{broker} 冻结报告正式动作无效"
    assert isinstance(holdings, list) and all(
        isinstance(item, Mapping) for item in holdings
    ), f"{broker} 冻结报告持仓动作无效"
    sells, buys, holds, reviews = _project_trend_actions(dict(payload), {})
    frozen_signals = payload.get("signal_snapshots")
    frozen_signals = frozen_signals if isinstance(frozen_signals, Mapping) else {}
    buys = _project_trend_strength_fields(buys, frozen_signals.get("candidates"))
    sells = _project_trend_strength_fields(sells, frozen_signals.get("holdings"))
    holds = _project_trend_strength_fields(holds, frozen_signals.get("holdings"))
    if broker == "eastmoney":
        for item in buys:
            for key, label in (
                ("industry", "行业"),
                ("filter_price", "筛选价（Trend Animals）"),
                ("close", "执行参考价（Futu 前复权）"),
            ):
                assert item.get(key) is not None and str(item[key]).strip() not in {
                    "", "-",
                }, f"A 股正式买入缺少 {label}"
    expected_actions = {
        "sell_actions": sells,
        "buy_actions": buys,
        "hold_actions": holds,
        "review_actions": reviews,
    }
    assert all(
        isinstance(projected := report.get(key), list)
        and all(isinstance(item, Mapping) for item in projected)
        and [
            {
                field: field_value
                for field, field_value in item.items()
                if field not in {"execution", "option_anomaly", "trend_report_state"}
            }
            for item in projected
        ] == value
        for key, value in expected_actions.items()
    ), f"{broker} 冻结报告动作与 API 投影不一致"
    for key in ("simulate_rotation_comparisons", "real_rotation_comparisons"):
        expected = judgments.get(key, [])
        assert isinstance(expected, list) and report.get(key) == expected, (
            f"{broker} 冻结报告轮换比较与 API 投影不一致：{key}"
        )
    assert report.get("counts") == {
        "sell": len(sells),
        "buy": len(buys),
        "hold": len(holds),
        "review": len(reviews),
    }, f"{broker} 冻结报告计数与 API 投影不一致"
    signal_snapshots = payload.get("signal_snapshots")
    expected_candidates = judgments.get("top10_candidates", [])
    if broker == "eastmoney" and isinstance(signal_snapshots, Mapping):
        expected_candidates = signal_snapshots.get("candidates", expected_candidates)
    assert audit.get("candidates") == expected_candidates, (
        f"{broker} 冻结报告候选榜与 API 投影不一致"
    )
    for key, default in (
        ("excluded", {}),
        ("industry_concentration", []),
        ("data_sources", []),
    ):
        assert audit.get(key) == payload.get(key, default), (
            f"{broker} 冻结报告审计字段 {key} 与 API 投影不一致"
        )


def _trend_table_text(value: Any) -> str:
    value = _plain(value)
    return "—" if value == "-" else value


def _trend_action_reason_label(
    item: Mapping[str, Any], report: Mapping[str, Any]
) -> str:
    reason = str(item.get("reason") or "")
    market = str(report.get("market") or "").upper()
    version = str(report.get("strategy_version") or "")
    if reason == "protection_line_already_triggered" and (
        market,
        version,
    ) in {
        ("CN", "v9"),
        ("CN", "v10"),
        ("CN", "v12"),
        ("US", "v6"),
        ("US", "v7"),
        ("US", "v10"),
        ("HK", "v6"),
        ("HK", "v7"),
        ("HK", "v10"),
    }:
        try:
            initial = Decimal(str(item.get("initial_line")))
            active = Decimal(str(item.get("active_line")))
        except (InvalidOperation, TypeError, ValueError):
            initial = active = None
        return (
            "2×ATR14 硬止损"
            if initial is not None and active == initial
            else "既有活动保护线触发"
        )
    return TREND_REASON_LABELS.get(reason, "未知动作或原因，需人工确认")


def _check_action_trend_stages(
    stage_texts: list[str], report: Mapping[str, Any], broker: str,
) -> None:
    expected = [
        ("优先处理 · 卖出触发", "sell_actions", None),
        (
            f"{_plain(report.get('buy_window'))} · 正式买入计划",
            "buy_actions",
            "正式买入",
        ),
    ]
    review_rows = report.get("review_actions")
    if isinstance(review_rows, list) and review_rows:
        expected.append(("需要确认 · 人工复核", "review_actions", "人工复核"))
    expected.append(("盘中持续 · 已有持仓", "hold_actions", "继续持有"))
    assert len(stage_texts) == len(expected), f"{broker} 趋势报告阶段数量不正确"
    for text, (title, key, action) in zip(stage_texts, expected, strict=True):
        assert title in text, f"{broker} 趋势报告缺少阶段 {title}"
        rows = report.get(key) if isinstance(report.get(key), list) else []
        if not rows:
            assert "无" in text, f"{broker} 的 {title} 空阶段未显示 无"
            continue
        for item in rows:
            assert isinstance(item, Mapping), f"{broker} 的 {title} 动作格式无效"
            action = (
                "止盈减仓 30%"
                if item.get("action") == "SELL_PARTIAL"
                else "全部卖出"
                if key == "sell_actions"
                else action
            )
            assert action in text, f"{broker} 的 {title} 缺少动作 {action}"
            for value in (item.get("symbol"), item.get("name")):
                if value:
                    assert str(value) in text, f"{broker} 的 {title} 缺少 {value}"
            if key == "buy_actions" and broker == "eastmoney":
                weight = Decimal(str(item.get("target_weight", "NaN"))) * 100
                facts = (
                    item.get("filter_price"), item.get("close"),
                    f"{_trend_table_text(item.get('temperature_prev'))} → {_trend_table_text(item.get('temperature_curr'))}",
                    item.get("phase"), item.get("strength"), item.get("industry"),
                    item.get("industry_temperature"), item.get("market_cap_cny_100m"),
                    item.get("amount_cny_100m"), f"{format(weight.normalize(), 'f')}%",
                    item.get("target_amount"), f"{_plain(item.get('estimated_shares'))} 股",
                    _display_price(item.get("estimated_initial_line")),
                )
            elif key == "buy_actions":
                weight = Decimal(str(item.get("target_weight", "NaN"))) * 100
                facts = (
                    item.get("close"), item.get("strength"), item.get("industry"),
                    f"{format(weight.normalize(), 'f')}%",
                    _display_number(item.get("target_amount")),
                    f"{_display_number(item.get('estimated_shares'))} 股",
                    _display_number(item.get("estimated_initial_line")),
                )
            elif broker != "eastmoney":
                facts = (
                    item.get("close"), item.get("strength"),
                    _trend_action_reason_label(item, report),
                    _display_number(item.get("active_line")),
                    *(
                        item.get("entry_hints")
                        if isinstance(item.get("entry_hints"), list) else []
                    ),
                )
            else:
                facts = (
                    item.get("close"),
                    f"{_trend_table_text(item.get('temperature_prev'))} → {_trend_table_text(item.get('temperature_curr'))}",
                    item.get("strength"),
                    _trend_action_reason_label(item, report),
                    _display_price(item.get("active_line")),
                    *(
                        item.get("entry_hints")
                        if isinstance(item.get("entry_hints"), list)
                        else ["数据不可用"]
                    ),
                )
            for fact in facts:
                expected_fact = _trend_table_text(fact)
                assert expected_fact in text, (
                    f"{broker} 的 {title} 缺少事实 {expected_fact}"
                )


def _check_cn_buy_rows(workspace: Any, report: Mapping[str, Any]) -> None:
    items = report.get("buy_actions")
    items = items if isinstance(items, list) else []
    rows = workspace.locator(".cn-trend-buy .cn-trend-card")
    assert rows.count() == len(items), "eastmoney 正式买入行数与 API 不一致"
    for index, item in enumerate(items):
        assert isinstance(item, Mapping), "eastmoney 正式买入动作格式无效"
        row = rows.nth(index)
        for label, key in (
            ("行业", "industry"),
            ("筛选价（Trend Animals）", "filter_price"),
            ("执行参考价（Futu 前复权）", "close"),
        ):
            expected = _plain(item.get(key))
            assert expected != "-", f"eastmoney 正式买入缺少 {label}"
            cell = row.locator(f'td[data-label="{label}"]')
            assert cell.count() == 1 and cell.inner_text().strip() == expected, (
                f"eastmoney 正式买入行 {index + 1} 的 {label} 与 API 不一致"
            )


def _check_trend_audit(
    audit: Any,
    report: Mapping[str, Any],
    broker: str,
    *,
    page: Any | None = None,
) -> None:
    assert audit.count() == 1 and audit.get_attribute("open") is None, (
        f"{broker} 趋势报告审计详情未保持收起"
    )
    summary = audit.locator("summary")
    assert summary.count() == 1, f"{broker} 趋势报告缺少审计摘要"
    summary.click()
    if broker == "eastmoney":
        data = report.get("audit") if isinstance(report.get("audit"), Mapping) else {}
        candidates = (
            data.get("candidates")
            if isinstance(data.get("candidates"), list) else []
        )
        table = audit.locator(".trend-audit-table")
        assert table.count() == 1, "eastmoney 缺少候选审计表"
        assert table.locator("thead th").all_inner_texts() == [
            "标的", "结论", "未通过项目", "已通过的关键事实", "审计",
        ], "eastmoney 候选审计表头不匹配"
        rows = table.locator(".trend-audit-row")
        assert rows.count() == len(candidates), "eastmoney 候选审计行数与 API 不一致"
        assert audit.locator(
            "section h3", has_text="排除项"
        ).count() == 0, "eastmoney 仍重复显示排除项"
        audit_text = audit.inner_text()
        assert "为什么没有进入买入名单" in audit_text, (
            "eastmoney 缺少审计解释标题"
        )
        passed = sum(
            isinstance(item, Mapping) and item.get("eligible") is True
            for item in candidates
        )
        excluded = sum(
            isinstance(item, Mapping) and item.get("eligible") is False
            for item in candidates
        )
        for label, value in (
            ("候选", len(candidates)),
            ("通过", passed),
            ("排除", excluded),
        ):
            assert f"{label} {value}" in audit_text, (
                f"eastmoney 审计摘要缺少 {label} {value}"
            )
        reason_counts: dict[str, int] = {}
        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            reasons = (
                item.get("excluded_reasons")
                if isinstance(item.get("excluded_reasons"), list) else []
            )
            for reason in reasons:
                label = _trend_audit_reason_label(reason)
                reason_counts[label] = reason_counts.get(label, 0) + 1
        for label, count in reason_counts.items():
            assert f"{label} {count}" in audit_text, (
                f"eastmoney 审计摘要缺少原因统计 {label} {count}"
            )
        for index, item in enumerate(candidates):
            assert isinstance(item, Mapping), "eastmoney 候选审计数据格式无效"
            row = rows.nth(index)
            identity = row.locator('td[data-label="标的"]')
            identity_text = identity.inner_text()
            for value in (item.get("symbol"), item.get("name")):
                if value:
                    assert str(value) in identity_text, (
                        f"eastmoney 候选审计行 {index + 1} 缺少 {value}"
                    )
            reasons = (
                item.get("excluded_reasons")
                if isinstance(item.get("excluded_reasons"), list) else []
            )
            expected_status = (
                "通过纪律"
                if item.get("eligible") is True
                else f"已排除 · {len(reasons)} 项未通过"
                if item.get("eligible") is False and reasons
                else "数据缺失"
                if item.get("eligible") is False
                else "待确认"
            )
            status = row.locator(
                'td[data-label="结论"] .trend-audit-status'
            )
            assert status.count() == 1 and status.inner_text().strip() == expected_status, (
                f"eastmoney 候选审计行 {index + 1} 结论不匹配"
            )
            reason_nodes = row.locator(".trend-audit-reason")
            assert reason_nodes.count() == len(reasons), (
                f"eastmoney 候选审计行 {index + 1} 原因数量不匹配"
            )
            reason_texts = reason_nodes.all_inner_texts()
            for reason, reason_text in zip(reasons, reason_texts):
                assert "→" in reason_text, (
                    f"eastmoney 候选审计行 {index + 1} 原因缺少实际值箭头"
                )
                assert "要求" in reason_text or "ATR" in reason_text, (
                    f"eastmoney 候选审计行 {index + 1} 原因缺少要求说明"
                )
                if str(reason) not in TREND_REASON_LABELS:
                    assert str(reason) in reason_text, (
                        f"eastmoney 候选审计行 {index + 1} 未显示未知原因 {reason}"
                    )
            details = row.locator(".trend-audit-more")
            assert details.count() == 1, (
                f"eastmoney 候选审计行 {index + 1} 缺少全部字段审计"
            )
            detail_summary = details.locator("summary")
            assert detail_summary.count() == 1, (
                f"eastmoney 候选审计行 {index + 1} 缺少字段展开控件"
            )
            detail_summary.click()
        industry_section = audit.locator(
            "section", has_text="行业集中度"
        )
        assert industry_section.count() == 1, "eastmoney 缺少行业集中度"
        industries = (
            data.get("industry_concentration")
            if isinstance(data.get("industry_concentration"), list) else []
        )
        industry_text = industry_section.inner_text()
        if not industries:
            assert "无" in industry_text, "eastmoney 空行业集中度未显示 无"
        for row in industries:
            for position, value in enumerate(row if isinstance(row, list) else []):
                expected = _plain(value) if position == 0 else _display_number(value)
                assert expected in industry_text, (
                    f"eastmoney 行业集中度缺少 {value}"
                )
        sources = (
            data.get("data_sources")
            if isinstance(data.get("data_sources"), list) else []
        )
        for source in sources:
            assert str(source) in audit_text, f"eastmoney 审计详情缺少数据来源 {source}"
        cost = data.get("actual_api_cost")
        if cost is None:
            cost = data.get("estimated_api_cost")
        if cost is None:
            cost = "未知"
        assert f"API 成本：{_plain(cost)}" in audit_text, (
            "eastmoney 审计详情缺少 API 成本"
        )
        target_page = page or getattr(audit, "page", None)
        viewport = (
            (getattr(target_page, "viewport_size", None) or {}).get("width")
            if target_page is not None else None
        )
        if viewport is not None and viewport <= 760:
            assert audit.evaluate(
                "node => node.scrollWidth <= node.clientWidth"
            ), "eastmoney 移动候选审计横向溢出"
            assert target_page is not None, (
                "eastmoney 移动审计检查缺少 page"
            )
            _check_mobile_targets(target_page, ".trend-audit-more summary")
        return
    sections = audit.locator("section").all_inner_texts()
    expected_sections = 4
    assert len(sections) == expected_sections, (
        f"{broker} 趋势报告审计区块数量不是 {expected_sections}"
    )
    data = report.get("audit") if isinstance(report.get("audit"), Mapping) else {}
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    if not candidates:
        assert "无" in sections[0], f"{broker} 空候选榜未显示 无"
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        for value in (item.get("symbol"), item.get("name")):
            if value:
                assert str(value) in sections[0], f"{broker} 候选榜缺少 {value}"
        assert f"强度 {_display_number(item.get('strength'))}" in sections[0], (
            f"{broker} 候选榜缺少强度"
        )
    excluded = data.get("excluded") if isinstance(data.get("excluded"), Mapping) else {}
    if not excluded:
        assert "无" in sections[1], f"{broker} 空排除项未显示 无"
    for symbol, reasons in excluded.items():
        assert str(symbol) in sections[1], f"{broker} 排除项缺少 {symbol}"
        for reason in reasons if isinstance(reasons, list) else []:
            label = TREND_REASON_LABELS.get(str(reason), "未知原因")
            assert label in sections[1], f"{broker} 排除项缺少原因 {label}"
    if broker != "eastmoney":
        account_exceptions = (
            data.get("account_exceptions")
            if isinstance(data.get("account_exceptions"), list) else []
        )
        if not account_exceptions:
            assert "无" in sections[2], f"{broker} 空账户不参与项未显示 无"
        for item in account_exceptions:
            assert str(item) in sections[2], f"{broker} 账户不参与项缺少 {item}"
    industries = (
        data.get("industry_concentration")
        if isinstance(data.get("industry_concentration"), list) else []
    )
    industry_section = sections[-1]
    if not industries:
        assert "无" in industry_section, f"{broker} 空行业集中度未显示 无"
    for row in industries:
        for index, value in enumerate(row if isinstance(row, list) else []):
            expected = _plain(value) if index == 0 else _display_number(value)
            assert expected in industry_section, f"{broker} 行业集中度缺少 {value}"
    audit_text = audit.inner_text()
    sources = data.get("data_sources") if isinstance(data.get("data_sources"), list) else []
    for source in sources:
        assert str(source) in audit_text, f"{broker} 审计详情缺少数据来源 {source}"
    cost = data.get("actual_api_cost")
    if cost is None:
        cost = data.get("estimated_api_cost")
    if cost is None:
        cost = "未知"
    assert f"API 成本：{_display_number(cost)}" in audit_text, f"{broker} 审计详情缺少 API 成本"


def _check_statement_upload(section: Any, broker: str, width: int) -> None:
    count = section.locator(
        f'[data-statement-upload="{broker}"]:visible'
    ).count()
    expected = int(width > 760 and broker in {"phillips", "eastmoney"})
    assert count == expected, (
        f"{broker} 结单上传入口数量不是 {expected}（视口宽度 {width}）"
    )


def _check_trend_option_buttons(
    page: Any, workspace: Any, report: Mapping[str, Any], broker: str,
) -> None:
    actions = [
        item
        for key in ("buy_actions", "real_position_actions", "hold_actions")
        for item in (report.get(key) if isinstance(report.get(key), list) else [])
        if isinstance(item, Mapping)
    ]
    available_actions = [
        item
        for item in actions
        if isinstance(item.get("option_anomaly"), Mapping)
        and item["option_anomaly"].get("available") is True
    ]
    buttons = workspace.locator(".trend-option-button")
    assert buttons.count() == len(available_actions), (
        f"{broker} 可用期权按钮数量与已提供期权异动的标的不匹配"
    )
    assert workspace.locator(".trend-option-button:disabled").count() == 0, (
        f"{broker} 仍显示不可用的期权按钮"
    )
    first_enabled: tuple[Any, Mapping[str, Any]] | None = None
    for index, action in enumerate(available_actions):
        button = buttons.nth(index)
        assert not button.is_disabled(), (
            f"{broker} {action.get('symbol')} 可用期权按钮错误置灰"
        )
        if first_enabled is None:
            first_enabled = (button, action)

    headings = workspace.locator(".cn-trend-table thead th").count()
    assert headings >= 3, f"{broker} 趋势表格表头缺失"
    if first_enabled is None:
        return
    button, action = first_enabled
    button.click()
    dialog = workspace.locator("dialog.trend-option-dialog:visible")
    assert dialog.count() == 1, f"{broker} 可用期权按钮未打开原生详情弹窗"
    assert "富途" in dialog.inner_text(), f"{broker} 期权详情未标明富途数据源"
    identity = " ".join(
        str(action.get(key)).strip()
        for key in ("symbol", "name")
        if action.get(key)
    )
    assert identity in str(dialog.get_attribute("aria-label") or ""), (
        f"{broker} 期权详情标的与按钮行不一致"
    )
    close = dialog.locator('button[data-option-anomaly-close]')
    assert close.count() >= 1, f"{broker} 期权详情缺少关闭按钮"
    close.first.click()
    assert workspace.locator("dialog.trend-option-dialog:visible").count() == 0, (
        f"{broker} 期权详情关闭失败"
    )
    assert workspace.locator(".cn-trend-table thead th").count() == headings, (
        f"{broker} 打开期权详情后趋势表头数量发生变化"
    )


def _check_trend_holding_tabs(
    workspace: Any, report: Mapping[str, Any], broker: str,
) -> None:
    """Check the read-only real-account tab without changing strategy facts."""
    section = workspace.locator("[data-trend-holding-section]")
    assert section.count() == 1, f"{broker} 趋势报告持仓区块数量不是 1"
    tabs = section.locator("[data-trend-holding-view]")
    assert tabs.count() == 2, f"{broker} 趋势报告持仓 Tab 数量不是 2"
    assert [tabs.nth(index).inner_text().strip() for index in range(2)] == [
        "真实持仓", "模拟盘持仓",
    ], f"{broker} 趋势报告持仓 Tab 文案或顺序不正确"
    assert tabs.nth(0).get_attribute("aria-selected") == "true", (
        f"{broker} 趋势报告默认未展示真实持仓"
    )
    assert tabs.nth(1).get_attribute("aria-selected") == "false", (
        f"{broker} 趋势报告模拟盘 Tab 默认状态不正确"
    )

    headings = (
        "标的", "动作", "执行参考价", "温度变化", "节气", "大类内强度",
        "全局强度", "行业", "当前判断", "活动保护线", "持仓提示",
    )
    real_panel = section.locator('[data-trend-holding-panel="real"]')
    simulate_panel = section.locator('[data-trend-holding-panel="simulate"]')
    assert real_panel.count() == 1 and simulate_panel.count() == 1, (
        f"{broker} 趋势报告持仓面板缺失"
    )
    status = report.get("real_position_status")
    real_table = real_panel.locator(".cn-trend-table")
    if status == "available":
        assert real_table.count() == 1, f"{broker} 真实持仓表格缺失"
        assert real_panel.locator(".cn-trend-table thead th").all_inner_texts() == list(headings), (
            f"{broker} 真实持仓列定义发生变化"
        )
        source = report.get("real_position_source")
        if isinstance(source, Mapping) and source:
            source_text = real_panel.inner_text()
            assert "只读" in source_text, f"{broker} 真实持仓未标明只读"
        real_items = report.get("real_position_actions")
        real_items = real_items if isinstance(real_items, list) else []
        real_rows = real_panel.locator(".cn-trend-card")
        assert real_rows.count() == len(real_items), (
            f"{broker} 真实持仓行数与 API 不一致"
        )
        real_text = real_panel.inner_text()
        for item in real_items:
            if not isinstance(item, Mapping):
                continue
            for value in (item.get("symbol"), item.get("name")):
                if value:
                    assert str(value) in real_text, f"{broker} 真实持仓缺少 {value}"
    elif status == "unavailable":
        assert real_table.count() == 0, f"{broker} 不可用真实持仓仍显示表格"
        reason = str(report.get("real_position_reason") or "数据未提供")
        assert reason in real_panel.inner_text(), f"{broker} 真实持仓缺少不可用原因"
    elif status == "legacy":
        assert real_table.count() == 0, f"{broker} 旧报告错误显示真实持仓表格"
        assert "当前报告未包含真实持仓判断" in real_panel.inner_text(), (
            f"{broker} 旧报告缺少真实持仓兼容提示"
        )
    elif status is not None:
        raise AssertionError(f"{broker} 真实持仓状态无效")

    assert simulate_panel.locator(".cn-trend-table").count() == 1, (
        f"{broker} 模拟盘持仓表格缺失"
    )
    assert simulate_panel.locator(".cn-trend-table thead th").all_inner_texts() == list(headings), (
        f"{broker} 模拟盘持仓列定义发生变化"
    )
    simulated_items = report.get("hold_actions")
    simulated_items = simulated_items if isinstance(simulated_items, list) else []
    assert simulate_panel.locator(".cn-trend-card").count() == len(simulated_items), (
        f"{broker} 模拟盘持仓行数与 API 不一致"
    )

    tabs.nth(1).click()
    assert tabs.nth(0).get_attribute("aria-selected") == "false", (
        f"{broker} 切换模拟盘后真实持仓仍被选中"
    )
    assert tabs.nth(1).get_attribute("aria-selected") == "true", (
        f"{broker} 模拟盘 Tab 未选中"
    )
    assert section.locator('[data-trend-holding-panel="simulate"]:visible').count() == 1, (
        f"{broker} 模拟盘持仓面板切换失败"
    )
    assert section.locator('[data-trend-holding-panel="real"]:visible').count() == 0, (
        f"{broker} 切换模拟盘后真实持仓面板仍可见"
    )
    tabs.nth(0).click()
    assert tabs.nth(0).get_attribute("aria-selected") == "true", (
        f"{broker} 无法切回真实持仓"
    )
    assert section.locator('[data-trend-holding-panel="real"]:visible').count() == 1, (
        f"{broker} 无法恢复真实持仓面板"
    )


def _check_controller_owned_rows(page: Any, section: Any, broker: str) -> None:
    positions = page.evaluate(
        "() => state.accountSnapshot?.positions ?? []"
    )
    assert isinstance(positions, list), "页面持仓状态无效"
    expected = [
        row for row in positions
        if isinstance(row, Mapping)
        and row.get("broker") == broker
        and _is_accepted_dashboard_holding(row)
    ]
    if not expected or any(
        not all(isinstance(row.get(field), str) for field in CONTROLLER_DOM_FIELDS)
        for row in expected
    ):
        return
    rows = section.locator(".account-holding-row:visible")
    assert rows.count() == len(expected), f"{broker} 控制器持仓行数与 DOM 不一致"
    unmatched = [rows.nth(index) for index in range(rows.count())]
    for expected_row in expected:
        symbol = str(expected_row.get("symbol", "")).upper()
        index = next(
            (
                index for index, row in enumerate(unmatched)
                if row.get_attribute("data-broker") == broker
                and row.get_attribute("data-symbol") == symbol
                and all(
                    row.get_attribute(attribute) == expected_row[field]
                    for field, attribute in CONTROLLER_DOM_FIELDS.items()
                )
            ),
            None,
        )
        if index is None:
            index = next(
                (
                    index for index, row in enumerate(unmatched)
                    if row.get_attribute("data-broker") == broker
                    and row.get_attribute("data-symbol") == symbol
                ),
                None,
            )
        assert index is not None, f"{broker} DOM 持仓标的不一致"
        row = unmatched.pop(index)
        assert row.get_attribute("data-broker") == broker, (
            f"{broker} DOM 持仓券商字段不一致"
        )
        for field, attribute in CONTROLLER_DOM_FIELDS.items():
            assert row.get_attribute(attribute) == expected_row[field], (
                f"{broker} {expected_row.get('symbol', '-')} DOM 字段 {field} 不一致"
            )


def _check_account_holdings(
    page: Any,
    payload: dict[str, Any],
    *,
    reports_dir: Path | None = None,
    screenshot_dir: Path | None = None,
) -> None:
    tabs = page.locator("#account-tabs [data-broker]")
    assert tabs.count() == 4, "券商账户 Tab 数量不是 4"
    assert tuple(
        tabs.nth(index).get_attribute("data-broker")
        for index in range(tabs.count())
    ) == ACCOUNT_BROKERS, "券商账户 Tab 顺序不正确"
    assert page.locator('[data-market="CASH"]').count() == 0, "页面仍包含现金筛选"
    assert page.locator("#cash-detail-panel").count() == 0, "页面仍包含现金明细挂载点"

    reports = payload.get("trend_reports") or {}
    reviews = payload.get("trend_reviews") or {}
    account_sync = payload.get("account_sync") or {}
    broker_sync = account_sync.get("brokers") if isinstance(account_sync, Mapping) else {}
    positions = payload.get("broker_positions") or []
    check_accepted_counts = isinstance(payload.get("broker_positions"), list) and bool(positions)
    profiles = {
        "futu": ("富途", "期权增强"),
        "tiger": ("老虎", "趋势", "美股趋势交易"),
        "phillips": ("辉立", "趋势", "港股趋势交易"),
        "eastmoney": ("东方财富", "偏短线", "趋势交易"),
    }
    for broker in ACCOUNT_BROKERS:
        section = _select_account_tab(page, broker)
        width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
        _check_statement_upload(section, broker, width)
        text = section.inner_text()
        for required in (*profiles[broker], "持仓资产", "现金", "持仓", "来源", "时间"):
            assert required in text, f"{broker} 账户区块缺少 {required}"
        for legacy in ("数据日", "账户源", "最近保护提醒", "策略指标待接入"):
            assert legacy not in text, f"账户持仓视图仍包含旧趋势摘要 {legacy}"
        for retired in ("SMA200 策略", "SMA200 " + "组合策略", "富途｜美股"):
            assert retired not in text, f"账户持仓视图仍包含已退役身份 {retired}"
        for forbidden in (
            "tiger-long-term-panel", "calibration_required", "provenance_incomplete",
        ):
            assert forbidden not in text, f"账户持仓视图泄漏内部代码 {forbidden}"
        rows = section.locator(".account-holding-row:visible")
        if check_accepted_counts:
            expected_rows = sum(
                1 for row in positions
                if (
                    isinstance(row, Mapping)
                    and row.get("broker") == broker
                    and _is_accepted_dashboard_holding(row)
                )
            )
            assert rows.count() == expected_rows, (
                f"{broker} 已接受持仓行数不匹配：{rows.count()} != {expected_rows}"
            )
        source = broker_sync.get(broker) if isinstance(broker_sync, Mapping) else {}
        source_status = source.get("status") if isinstance(source, Mapping) else "unknown"
        if source_status in {"failed", "stale", "unknown"}:
            banner = section.locator(".account-sync-alert:visible")
            assert banner.count() == 1, f"{broker} 异常账户缺少状态横幅"
            assert section.locator('[data-detail-mode="t_signal"]').count() == 0, (
                f"{broker} 异常账户仍暴露做T动作"
            )
            assert section.locator(".account-review-action").count() == rows.count(), (
                f"{broker} 异常账户缺少人工复核动作"
            )
        empty = section.locator(".account-empty:visible")
        if rows.count() == 0:
            assert empty.count() == 1 and empty.inner_text().strip() == "当前筛选下没有持仓", (
                f"{broker} 无持仓账户缺少中文空状态"
            )
        else:
            assert empty.count() == 0, f"{broker} 有持仓账户错误显示空状态"
        _check_controller_owned_rows(page, section, broker)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 账户区块出现横向滚动"
        if broker == "futu":
            assert section.locator(".trend-report-entry").count() == 0, (
                "futu 仍显示旧期权关注入口"
            )
            continue
        entry_label = "当天趋势报告"
        report = reports.get(broker) if isinstance(reports, Mapping) else None
        assert isinstance(report, Mapping), f"API 缺少 {broker} 趋势报告状态"
        if broker == "eastmoney":
            assert report.get("available") is True, "eastmoney 当前趋势报告不可用"
            if reports_dir is not None:
                _check_trend_artifact_projection(reports_dir, broker, report)
            continue
        entry = section.locator(".trend-report-entry")
        if entry.count() == 0:
            report_tab = section.locator('[data-account-view="report"]')
            assert report_tab.count() == 1, f"{broker} 账户缺少趋势报告视图"
            report_tab.click()
            panel = section.locator(f"#account-{broker}-view-panel:visible")
            assert panel.count() == 1, f"{broker} 趋势报告视图面板未显示"
            if report.get("available") is True:
                workspace = panel.locator(".cn-trend-report:visible")
                assert workspace.count() == 1, f"{broker} 趋势报告工作区未显示"
                workspace_text = workspace.inner_text()
                for label, key in (("报告", "report_date"), ("数据", "data_date")):
                    assert _plain(report.get(key)) in workspace_text, (
                        f"{broker} 趋势报告缺少 {label}日期"
                    )
                _check_trend_option_buttons(page, workspace, report, broker)
                _check_trend_holding_tabs(workspace, report, broker)
                if reports_dir is not None and broker in TREND_REPORT_BROKERS:
                    _check_trend_artifact_projection(reports_dir, broker, report)
                if (getattr(page, "viewport_size", None) or {}).get("width", 0) <= 760:
                    _check_mobile_targets(
                        page,
                        f"#account-{broker}-view-panel:visible .cn-trend-report button:visible, "
                        f"#account-{broker}-view-panel:visible .cn-trend-report summary:visible",
                    )
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    ), f"{broker} 趋势报告工作区出现横向滚动"
            else:
                status_text = _plain(report.get("status_text") or "今日暂无趋势报告")
                assert status_text in panel.inner_text(), (
                    f"{broker} 不可用趋势报告缺少状态文案"
                )
            review = reviews.get(broker) if isinstance(reviews, Mapping) else None
            assert isinstance(review, Mapping), f"API 缺少 {broker} 趋势复盘状态"
            review_disclosure = panel.locator("details.trend-review-disclosure")
            assert review_disclosure.count() == 1, f"{broker} 趋势复盘折叠栏目数量不是 1"
            review_disclosure.locator(":scope > summary").click()
            assert review.get("available") is True, f"{broker} 趋势复盘不可用"
            section.locator('[data-account-view="real"]').click()
            continue
        assert entry_label in text, f"{broker} 账户区块缺少 {entry_label}"
        assert entry.count() == 1, f"{broker} 趋势报告入口数量不是 1"
        trigger = entry.locator("[data-trend-report]")
        if report.get("available") is not True:
            assert trigger.count() == 0, f"{broker} 不可用报告仍可打开"
            button = entry.locator(f'button:has-text("{entry_label}")')
            assert button.count() == 1 and button.is_disabled(), (
                f"{broker} 不可用报告入口未禁用"
            )
            assert page.locator("#trend-report-workspace:visible").count() == 0, (
                f"{broker} 不可用报告错误打开工作区"
            )
            if broker in {"futu", "eastmoney"} and screenshot_dir is not None:
                raise AssertionError(f"{broker} 趋势报告不可用，无法生成验收截图")
            if broker in TREND_REPORT_BROKERS:
                review = reviews.get(broker) if isinstance(reviews, Mapping) else None
                assert isinstance(review, Mapping), f"API 缺少 {broker} 趋势复盘状态"
                _check_trend_review(
                    page, section, broker, review, screenshot_dir=screenshot_dir
                )
            continue
        assert trigger.count() == 1, f"{broker} 可用报告缺少入口"
        if reports_dir is not None and broker in TREND_REPORT_BROKERS:
            _check_trend_artifact_projection(reports_dir, broker, report)
        entry_text = entry.inner_text()
        for label, key in (("报告日期", "report_date"), ("数据截至", "data_date")):
            assert f"{label} {_plain(report.get(key))}" in entry_text, (
                f"{broker} 入口缺少 {label}"
            )
        trigger.click()
        workspace = page.locator("#trend-report-workspace:visible")
        assert workspace.count() == 1, f"{broker} 趋势报告工作区未显示"
        close = page.locator("#return-to-portfolio:visible")
        assert close.count() == 1, f"{broker} 趋势报告工作区缺少共享返回按钮"
        assert close.evaluate("element => element === document.activeElement"), (
            f"{broker} 趋势报告打开后焦点未进入工作区"
        )
        buy_actions = report.get("buy_actions")
        expected_buy_count = len(buy_actions) if isinstance(buy_actions, list) else 0
        _check_open_report_layout(
            page, workspace, broker, expected_buy_count=expected_buy_count
        )
        if broker in {"tiger", "phillips"}:
            _check_trend_option_buttons(page, workspace, report, broker)
        if (
            (getattr(page, "viewport_size", None) or {}).get("width", 0) <= 760
        ):
            _check_mobile_targets(
                page,
                "#return-to-portfolio:visible, "
                "#trend-report-workspace:visible button:visible, "
                "#trend-report-workspace:visible summary:visible",
            )
        if broker == "eastmoney" and screenshot_dir is not None:
            width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
            page.screenshot(
                path=str(screenshot_dir / f"{width}-trend-report.png"),
                full_page=True,
            )
        workspace_text = workspace.inner_text()
        identity = f"{_plain(report.get('broker_label'))}｜{_plain(report.get('market_label'))}"
        assert identity in workspace_text, f"{broker} 趋势报告身份不匹配"
        for required in ("报告", "数据", "生成", "账户"):
            assert required in workspace_text, f"{broker} 趋势报告工作区缺少 {required}"
        header_values = workspace.locator(".trend-report-header dd").all_inner_texts()
        assert header_values == [
            _plain(report.get(key)) for key in (
                "report_date", "data_date", "generated_at", "account_status",
            )
        ], f"{broker} 趋势报告头部内容与 API 不一致"
        counts = report.get("counts") if isinstance(report.get("counts"), Mapping) else {}
        count_labels = (
            ("买入", "buy"), ("卖出", "sell"),
            ("持有", "hold"), ("复核", "review"),
        )
        for label, key in count_labels:
            assert f"{label} {_display_number(counts.get(key) or 0)}" in workspace_text, (
                f"{broker} 趋势报告缺少 {label}计数"
            )
        required_stages = [
            "优先处理 · 卖出触发",
            f"{_plain(report.get('buy_window'))} · 正式买入计划",
            "盘中持续 · 已有持仓", "全部卖出", "正式买入", "继续持有",
        ]
        if isinstance(report.get("review_actions"), list) and report.get("review_actions"):
            required_stages.insert(2, "需要确认 · 人工复核")
            required_stages.append("人工复核")
        for required in required_stages:
            assert required in workspace_text, (
                f"{broker} 趋势报告工作区缺少 {required}"
            )
        assert workspace.locator(".cn-trend-report").count() == 1, (
            f"{broker} 趋势报告未使用动作优先结构"
        )
        stage_texts = workspace.locator(".cn-trend-stage").all_inner_texts()
        _check_action_trend_stages(stage_texts, report, broker)
        _check_trend_holding_tabs(workspace, report, broker)
        expected_stage_tables = 3 + int(
            isinstance(report.get("review_actions"), list)
            and bool(report.get("review_actions"))
        ) + int(report.get("real_position_status") == "available")
        assert workspace.locator(".cn-trend-table").count() == expected_stage_tables, (
            f"{broker} 趋势报告动作表数量与 API 不一致"
        )
        execution_rows = workspace.locator(".cn-trend-execution")
        assert execution_rows.count() == 0, (
            f"{broker} 趋势报告仍包含已删除的执行状态行"
        )
        assert not any(
            label in workspace_text for label in REMOVED_TREND_EXECUTION_LABELS
        ), f"{broker} 趋势报告仍包含已删除的执行状态文案"
        if broker == "eastmoney" and not (
            isinstance(report.get("strategy_parameter_rows"), list)
            and report.get("strategy_parameter_rows")
        ):
            for required in (
                "筛选价（Trend Animals）", "执行参考价（Futu 前复权）",
                "纪律", "行业上下文",
            ):
                assert required in workspace_text, (
                    f"eastmoney 趋势报告工作区缺少 {required}"
                )
            _check_cn_buy_rows(workspace, report)
            _check_displayed_protection_prices(
                workspace.locator(
                    'td[data-label="活动保护线"], td[data-label="预计保护线"]'
                ).all_inner_texts()
            )
            discipline = workspace.locator(".trend-discipline-workspace")
            assert discipline.count() == 1, "eastmoney 趋势报告纪律区块缺失"
            assert discipline.get_attribute("open") is None, (
                "eastmoney 趋势报告纪律默认展开"
            )
        viewport = getattr(page, "viewport_size", None)
        if viewport and viewport.get("width", 0) <= 760:
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            ), f"{broker} 趋势报告在 375px 产生横向滚动"
            cards = workspace.locator(".cn-trend-card:visible")
            assert all(
                box is not None and box["x"] + box["width"] <= width + 1
                for box in cards.evaluate_all(
                    "nodes => nodes.map(node => node.getBoundingClientRect()).map(r => ({x:r.x,width:r.width}))"
                )
            ), f"{broker} 趋势报告动作卡超出 {width}px 视口"
        audit = workspace.locator(".trend-audit")
        _check_trend_audit(audit, report, broker, page=page)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势报告工作区出现横向滚动"
        return_control = workspace.locator("[data-close-trend-report]")
        assert return_control.count() == 1, f"{broker} 趋势报告缺少可用返回按钮"
        return_control.click()
        assert page.locator("#trend-report-workspace:visible").count() == 0, (
            f"{broker} 返回后趋势报告工作区仍可见"
        )
        assert page.locator(".workspace-grid:visible").count() == 1, (
            f"{broker} 返回后持仓工作区未恢复"
        )
        assert trigger.evaluate("element => element === document.activeElement"), (
            f"{broker} 返回后焦点未恢复到报告入口"
        )
        review = reviews.get(broker) if isinstance(reviews, Mapping) else None
        assert isinstance(review, Mapping), f"API 缺少 {broker} 趋势复盘状态"
        _check_trend_review(
            page, section, broker, review, screenshot_dir=screenshot_dir
        )


def _trend_review_display(cell: object, *, percent: bool) -> str:
    if not isinstance(cell, Mapping) or cell.get("value") is None:
        return _plain(cell.get("reason") if isinstance(cell, Mapping) else None)
    try:
        value = Decimal(str(cell["value"]))
    except (InvalidOperation, TypeError, ValueError):
        return _plain(cell.get("value"))
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()
    rendered = _display_number(format(rounded, "f"))
    return f"{rendered}%" if percent else rendered


def _check_trend_review_visual_contract(page: Any, broker: str) -> None:
    styles = page.evaluate(
        r"""() => { // trend-review-style-contract
        const root = document.documentElement;
        const probe = document.createElement("span");
        root.appendChild(probe);
        const color = name => {
          probe.style.color = `var(${name})`;
          return getComputedStyle(probe).color;
        };
        const tokens = {
          bg: color("--bg"), surface: color("--surface"),
          surfaceSoft: color("--surface-soft"), text: color("--text"),
          muted: color("--muted"), accent: color("--accent"),
          line: color("--line"), primary: color("--primary"),
        };
        probe.style.boxShadow = "var(--shadow)";
        tokens.shadow = getComputedStyle(probe).boxShadow;
        probe.remove();
        const read = element => {
          const value = getComputedStyle(element);
          return {
            backgroundColor: value.backgroundColor,
            borderColor: value.borderColor,
            borderWidth: value.borderWidth,
            color: value.color,
            borderRadius: value.borderRadius,
            boxShadow: value.boxShadow,
            backgroundImage: value.backgroundImage,
          };
        };
        const workspace = document.querySelector("#trend-report-workspace");
        const panels = [...workspace.querySelectorAll(".trend-review-comparison")];
        const labels = [...workspace.querySelectorAll(
          ".trend-review-series > span:first-child"
        )];
        const headerSpans = [...workspace.querySelectorAll(
          ".trend-review-header-side > span"
        )];
        return {
          tokens, workspace: read(workspace), panels: panels.map(read),
          side: read(workspace.querySelector(".trend-review-header-side")),
          button: read(workspace.querySelector(".trend-review-header-side button")),
          headerSpans: headerSpans.map(read),
          labels: labels.map(element => ({text: element.textContent.trim(), ...read(element)})),
        };
        }"""
    )
    assert isinstance(styles, Mapping), f"{broker} 趋势复盘样式不可读"
    tokens = styles.get("tokens")
    assert isinstance(tokens, Mapping), f"{broker} 趋势复盘 token 不可读"
    workspace = styles.get("workspace")
    assert isinstance(workspace, Mapping), f"{broker} 趋势复盘工作区样式不可读"
    assert workspace == {
        "backgroundColor": tokens["surface"],
        "borderColor": tokens["line"],
        "borderWidth": "1px",
        "color": tokens["text"],
        "borderRadius": "8px",
        "boxShadow": tokens["shadow"],
        "backgroundImage": "none",
    }, f"{broker} 趋势复盘工作区未使用暖色 token：{workspace}"
    panels = styles.get("panels")
    assert isinstance(panels, list) and len(panels) == 2, (
        f"{broker} 趋势复盘 panel 样式数量不正确"
    )
    expected_panel = {
        "backgroundColor": tokens["surfaceSoft"],
        "borderColor": tokens["line"],
        "borderWidth": "1px",
        "color": tokens["text"],
        "borderRadius": "8px",
        "boxShadow": "none",
        "backgroundImage": "none",
    }
    assert all(panel == expected_panel for panel in panels), (
        f"{broker} 趋势复盘 panel token、圆角或阴影漂移：{panels}"
    )
    side = styles.get("side")
    assert isinstance(side, Mapping) and (
        side.get("backgroundColor") == "rgba(0, 0, 0, 0)"
        and side.get("borderWidth") == "0px"
        and side.get("boxShadow") == "none"
        and side.get("backgroundImage") == "none"
    ), f"{broker} 趋势复盘 header side 不得成为卡片：{side}"
    button = styles.get("button")
    assert isinstance(button, Mapping) and button == {
        "backgroundColor": tokens["surface"],
        "borderColor": tokens["accent"],
        "borderWidth": "1px",
        "color": tokens["accent"],
        "borderRadius": "7px",
        "boxShadow": "none",
        "backgroundImage": "none",
    }, f"{broker} 趋势复盘返回按钮未沿用暖色描边样式：{button}"
    expected_header_span = {
        "backgroundColor": "rgba(0, 0, 0, 0)",
        "borderColor": tokens["muted"],
        "borderWidth": "0px",
        "color": tokens["muted"],
        "borderRadius": "0px",
        "boxShadow": "none",
        "backgroundImage": "none",
    }
    header_spans = styles.get("headerSpans")
    assert isinstance(header_spans, list) and len(header_spans) == 3 and all(
        span == expected_header_span for span in header_spans
    ), f"{broker} 趋势复盘 header span 不得使用 badge 样式：{header_spans}"

    def expected_label(text: str, color: str) -> dict[str, object]:
        return {
            "text": text,
            "backgroundColor": "rgba(0, 0, 0, 0)",
            "borderColor": color,
            "borderWidth": "0px",
            "color": color,
            "borderRadius": "0px",
            "boxShadow": "none",
            "backgroundImage": "none",
        }

    expected_labels = [
        *([expected_label("纪律模拟", tokens["accent"]),
           expected_label("同期市场", tokens["primary"])] * 5),
        *([expected_label("实际执行", tokens["accent"]),
           expected_label("同期市场", tokens["primary"])] * 5),
    ]
    assert styles.get("labels") == expected_labels, (
        f"{broker} 趋势复盘 series 未同时使用文字与 token 颜色区分"
    )


def _check_trend_review_geometry(page: Any, broker: str) -> None:
    geometry = page.evaluate(
        r"""() => { // trend-review-geometry-contract
        const workspace = document.querySelector("#trend-report-workspace");
        const rect = element => {
          const value = element.getBoundingClientRect();
          return {x:value.x, y:value.y, width:value.width, height:value.height};
        };
        const side = workspace.querySelector(".trend-review-header-side");
        const textSelectors = [
          ".trend-review-header > div:first-child > *",
          ".trend-review-header-side > *",
          ".trend-review-comparison figcaption",
          ".trend-review-metric h3",
          ".trend-review-series > span:first-child",
          ".trend-review-series strong",
        ];
        return {
          documentWidth: document.documentElement.scrollWidth,
          side: rect(side), sideItems: [...side.children].map(rect),
          button: rect(side.querySelector("button")),
          panels: [...workspace.querySelectorAll(".trend-review-comparison")].map(rect),
          textGroups: textSelectors.map(selector => ({
            selector,
            layouts: [...workspace.querySelectorAll(selector)].map(element => {
              const style = getComputedStyle(element);
              return {
                clientWidth: element.clientWidth,
                scrollWidth: element.scrollWidth,
                clientHeight: element.clientHeight,
                scrollHeight: element.scrollHeight,
                whiteSpace: style.whiteSpace,
                textOverflow: style.textOverflow,
                overflow: style.overflow,
                overflowX: style.overflowX,
                overflowY: style.overflowY,
              };
            }),
          })),
        };
        }"""
    )
    assert isinstance(geometry, Mapping), f"{broker} 趋势复盘几何信息不可读"
    width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
    panels = geometry.get("panels")
    assert isinstance(panels, list) and len(panels) == 2, (
        f"{broker} 趋势复盘 panel 几何数量不正确"
    )
    assert all(
        panel["x"] >= -1 and panel["x"] + panel["width"] <= width + 1
        for panel in panels
    ), f"{broker} 趋势复盘 panel 超出 {width}px 视口"
    if width == 1440:
        assert abs(panels[0]["y"] - panels[1]["y"]) <= 1 and (
            panels[1]["x"] >= panels[0]["x"] + panels[0]["width"]
        ), f"{broker} 趋势复盘在 1440px 未并排显示"
    if width != 375:
        return
    assert geometry.get("documentWidth") == 375, (
        f"{broker} 趋势复盘 375px 横向滚动宽度为 {geometry.get('documentWidth')}"
    )
    side = geometry.get("side")
    button = geometry.get("button")
    assert isinstance(side, Mapping) and isinstance(button, Mapping)
    assert button["height"] >= 44 and abs(button["width"] - side["width"]) <= 1, (
        f"{broker} 趋势复盘 375px 返回按钮不是 100% 宽或不足 44px"
    )
    side_items = geometry.get("sideItems")
    assert isinstance(side_items, list) and len(side_items) == 4 and all(
        later["y"] >= earlier["y"] + earlier["height"]
        for earlier, later in zip(side_items, side_items[1:])
    ), f"{broker} 趋势复盘 375px header side 未单列显示"
    assert panels[1]["y"] >= panels[0]["y"] + panels[0]["height"], (
        f"{broker} 趋势复盘 375px panel 未纵向显示"
    )
    expected_text_counts = {
        ".trend-review-header > div:first-child > *": 3,
        ".trend-review-header-side > *": 4,
        ".trend-review-comparison figcaption": 2,
        ".trend-review-metric h3": 10,
        ".trend-review-series > span:first-child": 20,
        ".trend-review-series strong": 20,
    }
    text_groups = geometry.get("textGroups")
    assert isinstance(text_groups, list), f"{broker} 趋势复盘 375px 文本几何不可读"
    actual_text_counts = {
        group.get("selector"): len(group.get("layouts", []))
        for group in text_groups
        if isinstance(group, Mapping) and isinstance(group.get("layouts"), list)
    }
    assert actual_text_counts == expected_text_counts, (
        f"{broker} 趋势复盘 375px 文本元素数量不正确：{actual_text_counts}"
    )
    layouts = [
        layout
        for group in text_groups
        if isinstance(group, Mapping) and isinstance(group.get("layouts"), list)
        for layout in group["layouts"]
    ]
    assert layouts and all(isinstance(layout, Mapping) for layout in layouts), (
        f"{broker} 趋势复盘 375px 文本几何不可读"
    )
    assert all(
        layout.get("whiteSpace") != "nowrap"
        and layout.get("textOverflow") != "ellipsis"
        and all(layout.get(key) != "hidden" for key in ("overflow", "overflowX", "overflowY"))
        and layout.get("scrollWidth", 1) <= layout.get("clientWidth", 0)
        and layout.get("scrollHeight", 1) <= layout.get("clientHeight", 0)
        for layout in layouts
    ), f"{broker} 趋势复盘 375px 长文本被截断或未换行"


def _assert_no_trend_review_latin(
    texts: list[str],
    broker: str,
    context: str,
    *,
    allow_version: bool = False,
    allow_atr: bool = False,
) -> None:
    for text in texts:
        inspected = re.sub(r"v\d+", "", text) if allow_version else text
        inspected = re.sub(r"A\s*股", "", inspected)
        if allow_atr:
            inspected = inspected.replace("ATR14", "")
        assert re.search(r"[A-Za-z]", inspected) is None, (
            f"{broker} 趋势复盘 {context} 包含拉丁界面词：{text}"
        )


def _check_trend_controller_status(
    page: Any,
    workspace: Any,
    broker: str,
    controller: object,
) -> None:
    assert isinstance(controller, Mapping), f"API 缺少 {broker} 趋势控制器状态"
    baseline_controller = controller
    controller = page.evaluate(
        "broker => state.dashboard?.trend_controllers?.[broker] ?? null",
        broker,
    )
    assert isinstance(controller, Mapping), f"页面缺少 {broker} 趋势控制器状态"
    card = workspace.locator(".trend-controller-status")
    assert card.count() == 1, f"{broker} 趋势报告缺少控制器状态卡"
    assert card.get_attribute("open") is None, f"{broker} 控制器状态未默认收起"
    card.locator(":scope > summary").click()
    health = controller.get("health")
    assert card.get_attribute("data-health") == health, (
        f"{broker} 控制器状态卡健康标记与 API 不一致"
    )
    text = card.inner_text()
    rendered_facts: dict[str, str] = {}
    for row in card.locator("dl div").all_inner_texts():
        parts = row.splitlines()
        if len(parts) >= 2:
            rendered_facts[parts[0].strip()] = " ".join(parts[1:]).strip()
    for label, key in (
        ("执行模式", "effective_mode"),
        ("执行主机", "executor_host"),
        ("本地主机", "local_host"),
        ("PID", "pid"),
        ("Git SHA", "git_sha"),
        ("当前阶段", "phase"),
        ("心跳", "heartbeat_at"),
        ("最近成功", "last_success"),
        ("当前阻塞", "blocker"),
        ("下次检查", "next_check_at"),
    ):
        assert label in text, f"{broker} 控制器状态卡缺少 {label}"
        value = (
            baseline_controller.get(key)
            if key in {"heartbeat_at", "next_check_at"}
            else controller.get(key)
        )
        if key in {"heartbeat_at", "next_check_at"}:
            rendered = rendered_facts.get(label, "")
            if value in (None, ""):
                assert rendered == "—", f"{broker} 控制器状态卡 {label} 无效"
                continue
            try:
                baseline_time = datetime.fromisoformat(str(value))
                rendered_time = datetime.fromisoformat(rendered)
            except ValueError:
                raise AssertionError(
                    f"{broker} 控制器状态卡 {label} 不是有效时间"
                ) from None
            assert (
                baseline_time.tzinfo is not None
                and baseline_time.utcoffset() is not None
                and rendered_time.tzinfo is not None
                and rendered_time.utcoffset() is not None
            ), f"{broker} 控制器状态卡 {label} 不是带时区时间"
            assert rendered_time >= baseline_time, (
                f"{broker} 控制器状态卡 {label} 早于验收基线"
            )
            continue
        if key == "last_success" and isinstance(value, Mapping):
            assert "[object Object]" not in text, (
                f"{broker} 控制器最近成功不可读"
            )
            for fact_label, fact_key in (
                ("状态", "status"),
                ("市场", "market"),
                ("日期", "date"),
                ("提交数", "submitted_count"),
                ("产物", "artifact_paths"),
            ):
                if fact_key not in value:
                    continue
                assert fact_label in text, (
                    f"{broker} 控制器最近成功缺少 {fact_label}"
                )
                fact_value = value[fact_key]
                if isinstance(fact_value, list):
                    expected = [str(item) for item in fact_value] or ["无"]
                elif fact_value not in (None, ""):
                    expected = [str(fact_value)]
                else:
                    expected = []
                assert all(item in text for item in expected), (
                    f"{broker} 控制器最近成功 {fact_label} 与 API 不一致"
                )
            continue
        if key == "last_success" and value is None:
            assert rendered_facts.get(label) == "—", (
                f"{broker} 控制器尚无首次成功时展示无效"
            )
            continue
        if value not in (None, ""):
            assert str(value) in text, f"{broker} 控制器状态卡 {label} 与 API 不一致"
    mode = controller.get("effective_mode")
    if mode == "readonly":
        assert health == "readonly" and controller.get("blocking") is False, (
            f"{broker} 只读控制器状态无效"
        )
        assert "只读部署，不运行本机控制器" in text, (
            f"{broker} 只读控制器缺少说明"
        )
    else:
        assert mode == "execute", f"{broker} 控制器执行模式无效"
    width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
    if width <= 760:
        boxes = card.evaluate_all(
            "nodes => nodes.map(node => node.getBoundingClientRect())"
            ".map(r => ({x:r.x,width:r.width}))"
        )
        assert boxes and all(
            box["x"] >= -1 and box["x"] + box["width"] <= width + 1
            for box in boxes
        ), f"{broker} 控制器状态卡超出 {width}px 视口"
    if card.get_attribute("open") is not None:
        card.locator(":scope > summary").click()


def _check_trend_review(
    page: Any,
    section: Any,
    broker: str,
    review: Mapping[str, Any],
    *,
    screenshot_dir: Path | None = None,
) -> None:
    assert review.get("available") is True, f"{broker} 趋势复盘不可用"
    labels = {"tiger": "美股复盘", "phillips": "港股复盘", "eastmoney": "A股复盘"}
    assert labels[broker] in section.inner_text(), f"{broker} 账户区块缺少 {labels[broker]}"
    trigger = section.locator(f'[data-trend-review="{broker}"]')
    assert trigger.count() == 1, f"{broker} 趋势复盘入口数量不是 1"
    trigger.click()
    workspace = page.locator("#trend-report-workspace:visible")
    assert workspace.count() == 1, f"{broker} 趋势复盘工作区未显示"
    text = workspace.inner_text()
    market_label = _plain(review.get("market_label"))
    snapshot = review.get("strategy_snapshot")
    assert isinstance(snapshot, Mapping), f"{broker} 趋势复盘缺少策略快照"
    sample_counts = review.get("sample_counts")
    assert isinstance(sample_counts, Mapping), f"{broker} 趋势复盘缺少样本数"
    required_count = sample_counts.get("required")
    assert isinstance(required_count, int), f"{broker} 趋势复盘要求样本数无效"

    def sample_text(key: str, label: str) -> str:
        count = sample_counts.get(key)
        assert isinstance(count, int), f"{broker} 趋势复盘 {label}样本数无效"
        return (
            f"{label} {count} 笔"
            if count >= required_count
            else f"{label} {count} / {required_count}，数据不足"
        )

    cutoff = review.get("common_cutoff")
    header_left_items = [
        f"{_plain(review.get('broker_label'))}｜{market_label}",
        f"{market_label}趋势复盘",
        f"{_plain(snapshot.get('strategy_name'))}｜"
        f"{_trend_review_strategy_version(snapshot.get('strategy_version'))}",
    ]
    rendered_header_left = workspace.locator(
        ".trend-review-header > div:first-child > *"
    ).all_inner_texts()
    _assert_no_trend_review_latin(
        rendered_header_left, broker, "header 左侧", allow_version=True
    )
    assert rendered_header_left == header_left_items, (
        f"{broker} 趋势复盘 header 左侧顺序或文字错误"
    )
    header_items = [
        "返回持仓看板",
        sample_text("discipline", "纪律模拟"),
        sample_text("actual", "实际执行"),
        f"共同截止日 {_plain(cutoff) if cutoff is not None else '暂无'}",
    ]
    for required in (
        f"{market_label}趋势复盘",
        _plain(review.get("broker_label")),
        _plain(snapshot.get("strategy_name")),
        _trend_review_strategy_version(snapshot.get("strategy_version")),
        *header_items,
        *(title for _series, _label, title in TREND_REVIEW_COMPARISONS),
        "同期市场",
    ):
        assert required in text, f"{broker} 趋势复盘缺少 {required}"
    assert workspace.locator(".trend-review-header-side").count() == 1, (
        f"{broker} 趋势复盘 header side 数量不是 1"
    )
    rendered_header_side = workspace.locator(
        ".trend-review-header-side > *"
    ).all_inner_texts()
    _assert_no_trend_review_latin(rendered_header_side, broker, "header 右侧")
    assert rendered_header_side == header_items, (
        f"{broker} 趋势复盘 header side 顺序或文字错误"
    )
    assert workspace.locator(".trend-review-parameters").count() == 0, (
        f"{broker} 趋势复盘仍重复展示当前策略参数"
    )
    assert workspace.locator(".trend-review-comparison").count() == 2, (
        f"{broker} 趋势复盘比较 panel 数量不是 2"
    )
    comparison_titles = workspace.locator(
        ".trend-review-comparison figcaption"
    ).all_inner_texts()
    assert comparison_titles == [
        title for _series, _label, title in TREND_REVIEW_COMPARISONS
    ], f"{broker} 趋势复盘比较 panel 顺序不正确"
    metric_labels = [label for _key, label, _percent in TREND_REVIEW_METRIC_SPECS]
    benchmark_values: list[list[str]] = []
    series_labels: list[str] = []
    for series, label, _title in TREND_REVIEW_COMPARISONS:
        panel = workspace.locator(
            f'.trend-review-comparison[data-series="{series}"]'
        )
        assert panel.count() == 1, f"{broker} 趋势复盘缺少 {label}比较 panel"
        metrics = panel.locator(".trend-review-metric")
        assert metrics.count() == 5, f"{broker} {label}比较 panel 指标数量不是 5"
        assert panel.locator(
            ".trend-review-metric h3"
        ).all_inner_texts() == metric_labels, f"{broker} {label}指标不完整或顺序错误"
        assert panel.locator(".trend-review-series").count() == 10, (
            f"{broker} {label}比较 panel series 数量不是 10"
        )
        for index in range(metrics.count()):
            assert metrics.nth(index).locator(".trend-review-series").count() == 2, (
                f"{broker} {label}第 {index + 1} 个指标 series 数量不是 2"
            )
        rendered_series_labels = panel.locator(
            ".trend-review-series > span:first-child"
        ).all_inner_texts()
        assert rendered_series_labels == [
            item for _metric in metric_labels for item in (label, "同期市场")
        ], f"{broker} {label} series 文字标签不正确"
        series_labels.extend(rendered_series_labels)
        benchmark_values.append(panel.locator(
            '.trend-review-series[data-series="benchmark"] strong'
        ).all_inner_texts())
    assert benchmark_values[0] == benchmark_values[1], (
        f"{broker} 两个比较 panel 的市场基准显示值不一致"
    )
    metrics_payload = review.get("metrics")
    assert isinstance(metrics_payload, Mapping), f"{broker} 趋势复盘指标数据无效"
    expected_benchmark = [
        _trend_review_display(
            metrics_payload[key]["benchmark"],
            percent=percent,
        )
        for key, _label, percent in TREND_REVIEW_METRIC_SPECS
    ]
    assert benchmark_values[0] == expected_benchmark, (
        f"{broker} 比较 panel 的市场基准未使用同一 API cell"
    )
    metric_values = workspace.locator(
        ".trend-review-series strong"
    ).all_inner_texts()
    assert len(metric_values) == 20, f"{broker} 趋势复盘指标值数量不是 20"
    _assert_no_trend_review_latin(
        [*header_items, *comparison_titles, *metric_labels,
         *series_labels, *metric_values],
        broker,
        "可见界面",
    )
    for forbidden in (
        "复盘结论", "运行状态", "回测", "导出", "缺陷", "Connected",
        "Backtest", "Sharpe", "Calmar", "Alpha", "Beta", "Sortino",
    ):
        assert forbidden not in text, f"{broker} 趋势复盘包含未要求内容 {forbidden}"
    _check_trend_review_visual_contract(page, broker)
    _check_trend_review_geometry(page, broker)
    width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
    if width <= 760:
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势复盘在 375px 产生横向滚动"
        _check_mobile_targets(
            page,
            "#return-to-portfolio:visible, "
            "#trend-report-workspace:visible button:visible",
        )
    _capture_trend_review_screenshot(page, broker, screenshot_dir)
    close = workspace.locator("[data-close-trend-report]")
    assert close.count() == 1, f"{broker} 趋势复盘缺少返回按钮"
    close.click()
    assert page.locator("#trend-report-workspace:visible").count() == 0, (
        f"{broker} 返回后趋势复盘工作区仍可见"
    )
    assert trigger.evaluate("element => element === document.activeElement"), (
        f"{broker} 返回后焦点未恢复到复盘入口"
    )


def _capture_trend_review_screenshot(
    page: Any, broker: str, screenshot_dir: Path | None,
) -> None:
    width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
    if broker == "eastmoney" and screenshot_dir is not None and width in {1440, 375}:
        page.screenshot(
            path=str(screenshot_dir / f"{width}-trend-review.png"),
            full_page=True,
        )


def _select_account_tab(page: Any, broker: str) -> Any:
    tab = page.locator(f'#account-tabs [data-broker="{broker}"]')
    assert tab.count() == 1, f"缺少 {broker} 券商 Tab"
    tab.click()
    assert tab.get_attribute("aria-selected") == "true", f"{broker} Tab 未选中"
    section = page.locator(f"#account-{broker}:visible")
    assert section.count() == 1, f"{broker} 账户区块未显示"
    assert page.locator(".account-section:visible").count() == 1, "同时显示多个账户区块"
    return section


def _check_session_prices(page: Any) -> None:
    price_cells = page.locator(
        '.account-holding-row:visible:has('
        '.account-holding-market:has-text("US")) .account-holding-price'
    )
    assert price_cells.count() >= 1, "美股持仓没有价格单元格"
    for index in range(price_cells.count()):
        prices = price_cells.nth(index).locator(".session-quote")
        assert prices.count() == 1, "每个可见美股价格单元格必须恰好一个分时段价格"
        price = prices.nth(0)
        text = re.sub(r"\s+", " ", price.inner_text()).strip()
        assert sum(label in text for label in SESSION_LABELS) == 1, "单个标的展示了多个时段"
        assert "CST" not in text, "标的行重复展示全局获取时间"
        assert "ET" in text or "上一有效价" in text, "标的价格没有时间或回退说明"
        if page.viewport_size and page.viewport_size["width"] <= 500:
            box = price.bounding_box()
            assert box is not None, "无法读取标的价格位置"
            assert box["x"] + box["width"] <= page.viewport_size["width"] + 1, (
                "移动端标的价格超出视口"
            )


def _source_time_text(broker: str, source: Mapping[str, object]) -> str:
    raw = source.get("data_as_of")
    if broker in {"futu", "tiger"} and (
        not isinstance(raw, str) or not raw.strip()
    ):
        raw = source.get("last_success_at")
    raw = raw if isinstance(raw, str) else ""
    pattern = (
        r"(?:T|\s|^)(\d{2}:\d{2})(?::\d{2})?"
        if broker in {"futu", "tiger"}
        else r"\b\d{4}-(\d{2}-\d{2})\b"
    )
    match = re.search(pattern, raw)
    return match.group(1) if match else ""


def _expected_source_copy(broker: str, source: Mapping[str, object]) -> str:
    status = str(source.get("status") or "unknown").lower()
    live = broker in {"futu", "tiger"}
    time = _source_time_text(broker, source)
    if status in {"ok", "healthy"}:
        return (
            f"同步正常{f' · {time}' if time else ''}"
            if live
            else (f"数据截至 · {time}" if time else "同步正常")
        )
    if status == "failed":
        return (
            f"同步失败 · {'上次' if live else '数据截至'} {time}"
            if time
            else "同步失败"
        )
    if status == "stale":
        return f"数据已过期{f' · 截至 {time}' if time else ''}"
    return "同步状态未知 · 数据未验证"


def _check_source_status_panel(page: Any, payload: Mapping[str, object]) -> None:
    panel = page.locator("#source-status-list")
    assert panel.count() == 1, "缺少券商数据来源面板"
    panel_text = re.sub(r"\s+", " ", panel.inner_text()).strip()
    assert "实时账户" in panel_text and "券商结单" in panel_text, "券商来源未分组"
    for removed in ("控制器心跳", "控制器", "刷新于", "部分标的当前时段无报价"):
        assert removed not in panel_text, f"来源面板仍显示冗余信息：{removed}"
    account_sync = payload.get("account_sync")
    account_sync = account_sync if isinstance(account_sync, Mapping) else {}
    brokers = account_sync.get("brokers")
    brokers = brokers if isinstance(brokers, Mapping) else {}
    for broker in ACCOUNT_BROKERS:
        row = page.locator(f'#source-status-list [data-broker="{broker}"]')
        assert row.count() == 1, f"缺少 {broker} 券商来源行"
        source = brokers.get(broker)
        source = source if isinstance(source, Mapping) else {}
        assert _expected_source_copy(broker, source) in re.sub(
            r"\s+", " ", row.inner_text()
        ), f"{broker} 券商来源时间或状态不正确"


def _page_dashboard_payload(page: Any) -> Mapping[str, object]:
    payload = page.evaluate(
        """() => {
          const dashboard = state.dashboard;
          const live = state.accountSnapshot?.sources?.account?.brokers;
          if (!dashboard || !live) return dashboard;
          const brokers = {...(dashboard.account_sync?.brokers || {})};
          for (const broker of ["futu", "tiger"]) {
            if (live[broker]) brokers[broker] = {...brokers[broker], ...live[broker]};
          }
          return {
            ...dashboard,
            account_sync: {...(dashboard.account_sync || {}), brokers},
          };
        }"""
    )
    assert isinstance(payload, Mapping), "Dashboard 当前页面数据无效"
    return payload


def _check_visual_contract(page: Any) -> None:
    names = list(WARM_LEDGER_TOKENS)
    actual = page.evaluate(
        "names => { const styles = getComputedStyle(document.documentElement); "
        "return Object.fromEntries(names.map(name => "
        "[name, styles.getPropertyValue(name).trim().toUpperCase()])); }",
        names,
    )
    assert actual == WARM_LEDGER_TOKENS, f"Dashboard A 色板漂移：{actual}"

    expected = {
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
    }
    surface = {
        "backgroundColor": "rgb(255, 254, 250)",
        "borderTopColor": "rgb(216, 210, 200)",
    }
    for selector in (
        ".header-brand-panel", ".header-assets-panel", ".header-source-panel",
        ".holdings-panel", ".kelly-lab-panel", ".trend-report-workspace",
        ".backtest-workspace", ".symbol-detail-panel", ".research-chat-modal",
    ):
        expected[selector] = surface
    expression = (
        "element => { const styles = getComputedStyle(element); return {"
        "backgroundColor: styles.backgroundColor, "
        "borderTopColor: styles.borderTopColor, color: styles.color}; }"
    )
    for selector, required in expected.items():
        locator = page.locator(selector)
        assert locator.count() == 1, f"A 色板验收缺少表面 {selector}"
        actual_style = locator.evaluate(expression)
        assert all(
            actual_style.get(key) == value for key, value in required.items()
        ), f"{selector} 未使用 A 色板：{actual_style}"

    assert page.locator("#refresh-quotes").count() == 0, "页面仍包含账户刷新按钮"
    assert page.locator("text=刷新账户与行情").count() == 0, "页面仍包含旧账户刷新文案"
    assert page.locator("#source-status-list").count() == 1, "缺少券商数据来源面板"


def _check_open_report_layout(
    page: Any, workspace: Any, broker: str, *, expected_buy_count: int | None = None,
) -> None:
    viewport = getattr(page, "viewport_size", None) or {}
    width = viewport.get("width", 0)
    if width >= 1920:
        geometry = page.evaluate("""() => {
          const shell = document.querySelector('.dashboard-shell').getBoundingClientRect();
          const header = document.querySelector('.dashboard-header').getBoundingClientRect();
          const report = document.querySelector('#trend-report-workspace').getBoundingClientRect();
          const grid = document.querySelector('.workspace-grid');
          const holdings = document.querySelector('.holdings-panel');
          const gridHidden = grid.classList.contains('hidden');
          const holdingsHidden = holdings.classList.contains('hidden');
          grid.classList.remove('hidden');
          holdings.classList.remove('hidden');
          const holdingsRect = holdings.getBoundingClientRect();
          if (holdingsHidden) holdings.classList.add('hidden');
          if (gridHidden) grid.classList.add('hidden');
          return {shellWidth: shell.width, headerLeft: header.left, headerRight: header.right,
                  reportLeft: report.left, reportRight: report.right,
                  holdingsLeft: holdingsRect.left, holdingsRight: holdingsRect.right};
        }""")
        assert abs(geometry["shellWidth"] - 1600) <= 1, (
            "1920px 下 Dashboard shell 不是 1600px"
        )
        assert abs(geometry["headerLeft"] - geometry["reportLeft"]) <= 1, (
            "趋势报告左边线未与 Header 对齐"
        )
        assert abs(geometry["headerRight"] - geometry["reportRight"]) <= 1, (
            "趋势报告右边线未与 Header 对齐"
        )
        assert abs(geometry["holdingsLeft"] - geometry["reportLeft"]) <= 1, (
            "趋势报告左边线未与持仓面板左边线对齐"
        )
        assert abs(geometry["holdingsRight"] - geometry["reportRight"]) <= 1, (
            "趋势报告右边线未与持仓面板右边线对齐"
        )

    buy_stage = workspace.locator(".cn-trend-buy")
    assert buy_stage.count() == 1, f"{broker} 趋势报告缺少正式买入区"
    expected_buy_count = 1 if expected_buy_count is None else expected_buy_count
    cards = buy_stage.locator(".cn-trend-card:visible")
    if width <= 760:
        assert buy_stage.get_attribute("tabindex") == "-1", (
            f"{broker} 正式买入区在手机端产生多余 Tab 停靠点"
        )
        assert buy_stage.get_attribute("aria-label") == "正式买入计划", (
            f"{broker} 正式买入区手机端标签不正确"
        )
        assert cards.count() == expected_buy_count, (
            f"{broker} 趋势报告手机端买入卡数量与 API 不一致"
        )
        if expected_buy_count == 0:
            assert "无" in buy_stage.inner_text(), f"{broker} 零买入报告未显示 无"
        return
    assert buy_stage.get_attribute("tabindex") == "0", (
        f"{broker} 正式买入滚动区不可通过键盘聚焦"
    )
    assert buy_stage.get_attribute("aria-label") == "正式买入计划，可横向滚动", (
        f"{broker} 正式买入滚动区缺少无障碍标签"
    )
    buy_stage.focus()
    assert buy_stage.evaluate("element => element === document.activeElement"), (
        f"{broker} 正式买入滚动区无法获得焦点"
    )
    focus = buy_stage.evaluate(
        "element => { const styles = getComputedStyle(element); return {"
        "outlineColor: styles.outlineColor, outlineStyle: styles.outlineStyle, "
        "outlineWidth: styles.outlineWidth}; }"
    )
    assert focus == {
        "outlineColor": "rgb(139, 94, 52)",
        "outlineStyle": "solid", "outlineWidth": "3px",
    }, f"{broker} 正式买入滚动区焦点样式不正确：{focus}"
    if expected_buy_count == 0:
        return
    overflow = buy_stage.evaluate(
        "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, "
        "overflowX: getComputedStyle(element).overflowX})"
    )
    assert overflow["overflowX"] == "auto", f"{broker} 正式买入区未启用内部横向滚动"
    assert overflow["scrollWidth"] > overflow["clientWidth"], (
        f"{broker} 正式买入宽表没有可滚动内容"
    )


def _check_page_safety(page: Any) -> None:
    assert page.locator("#tiger-long-term-panel").count() == 0, "页面仍包含独立老虎长线面板"
    assert page.locator("#trade-actions").count() == 0, "页面仍包含交易动作面板"
    visible_text = page.locator("body").inner_text()
    for forbidden in (
        "TIGER · LONG TERM", "broad_us_growth", "semiconductor",
        "INELIGIBLE", "LONG", "CASH", "insufficient_sma200_history",
        "state_change", "provenance_incomplete", "calibration_required",
    ):
        assert forbidden not in visible_text, f"页面泄漏英文内部状态 {forbidden}"
    for label in page.locator("a:visible, button:visible").all_inner_texts():
        assert "下单" not in label, f"页面包含下单入口：{label}"


def _check_tiger_tab(page: Any) -> None:
    _select_account_tab(page, "tiger")


def _check_cn_filter(page: Any, expected_cn: int) -> None:
    page.locator('[data-market="CN"]').first.click()
    page.wait_for_timeout(500)
    total = 0
    for broker in ACCOUNT_BROKERS:
        section = _select_account_tab(page, broker)
        if broker in TREND_SIMULATE_MARKETS:
            real_tab = section.locator('[data-account-view="real"]')
            assert real_tab.count() == 1, f"{broker} 缺少真实持仓视图"
            real_tab.click()
            page.wait_for_function(
                "broker => document.querySelector("
                "`#account-${broker} [data-account-view=\"real\"]`)"
                "?.getAttribute('aria-selected') === 'true'",
                arg=broker,
                timeout=10_000,
            )
        rows = section.locator(".account-holding-row:visible")
        empty = section.locator(".account-empty:visible")
        count = rows.count()
        total += count
        assert page.locator("#visible-count").inner_text().strip() == f"{_display_number(count)} 条", (
            f"{broker} A 股筛选计数不是 {count} 条"
        )
        if count == 0:
            assert empty.count() == 1 and empty.inner_text().strip() == "当前筛选下没有持仓", (
                f"{broker} A 股筛选后缺少中文空状态"
            )
            continue
        assert empty.count() == 0, f"{broker} A 股筛选后错误显示空状态"
        markets = section.locator(
            ".account-holding-row:visible td:nth-child(2)"
        ).all_inner_texts()
        assert len(markets) == count, f"{broker} A 股筛选后市场列缺失"
        assert all(
            re.sub(r"\s+", " ", market).strip() in {"CN", "市场 CN"}
            for market in markets
        ), f"{broker} A 股筛选后包含非 CN 持仓"
    assert total == expected_cn, f"A 股筛选不是 {expected_cn} 条：{total}"


def _prepare_acceptance_screenshots() -> int:
    ACCEPTANCE_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ACCEPTANCE_SCREENSHOT_NAMES:
        (ACCEPTANCE_SCREENSHOT_DIR / name).unlink(missing_ok=True)
    return time.time_ns()


def _validate_acceptance_screenshots(started_at_ns: int) -> list[str]:
    errors: list[str] = []
    for name in ACCEPTANCE_SCREENSHOT_NAMES:
        path = ACCEPTANCE_SCREENSHOT_DIR / name
        try:
            stat = path.stat()
        except FileNotFoundError:
            errors.append(f"验收截图缺失：{name}")
            continue
        if stat.st_size == 0:
            errors.append(f"验收截图是空文件：{name}")
        if stat.st_mtime_ns < started_at_ns:
            errors.append(f"验收截图过期：{name}")
    return errors


def _refresh_simulate_payloads(
    url: str,
    payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    refreshed: dict[str, dict[str, Any]] = {}
    for broker in payloads:
        payload = _fetch_json_path(
            url, f"/api/trend-simulate-positions/{broker}"
        )
        assert isinstance(payload, Mapping), f"{broker} 模拟盘 API 不是对象"
        refreshed[broker] = dict(payload)
    return refreshed


def _browser_check(
    url: str,
    expected_cn: int,
    payload: dict[str, Any],
    reports_dir: Path | None = None,
    simulate_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    history_expectations: Mapping[str, list[Mapping[str, Any]]] | None = None,
    *,
    expected_sha: str | None = None,
) -> tuple[list[str], str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "Playwright 未安装"
    errors: list[str] = []
    _prepare_acceptance_screenshots()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                market, symbol, decision_broker = _first_in_scope_holding(payload)
                detail_key = _dashboard_holding_key(payload, market, symbol)
            except AssertionError as exc:
                browser.close()
                return [str(exc)], None
            for name, viewport in ACCEPTANCE_BROWSER_VIEWPORTS:
                page = None
                try:
                    page = browser.new_page(viewport=viewport)
                    browser_errors: list[str] = []
                    browser_requests: list[tuple[Any, str, str | None]] = []
                    browser_responses: list[Any] = []
                    page.on(
                        "console",
                        lambda message: browser_errors.append(message.text)
                        if message.type == "error"
                        and _is_actionable_console_error(message.text)
                        else None,
                    )
                    page.on("pageerror", lambda error: browser_errors.append(str(error)))
                    page.on(
                        "request",
                        lambda request: browser_requests.append((
                            request, request.url, None,
                        )),
                    )
                    page.on("response", lambda response: browser_errors.append(
                        f"HTTP {response.status} {response.url}"
                    ) if response.status >= 400 else None)
                    page.on(
                        "response",
                        lambda response: browser_responses.append(response),
                    )
                    page.goto(url, wait_until="networkidle")
                    page.wait_for_timeout(ACCOUNT_POLL_PROOF_WAIT_MS)
                    assert page.evaluate(
                        """() => {
                          const active = state.quoteIntervalId !== null
                            && state.accountIntervalId !== null;
                          clearInterval(state.quoteIntervalId);
                          clearInterval(state.accountIntervalId);
                          state.quoteIntervalId = null;
                          state.accountIntervalId = null;
                          return active;
                        }"""
                    ), "Dashboard 未启动数据轮询"
                    errors.extend(
                        f"{name}：{message}"
                        for message in _browser_account_network_errors(
                            [
                                (
                                    request,
                                    request_url,
                                    request.header_value("if-none-match"),
                                )
                                for request, request_url, _ in browser_requests
                            ],
                            [_browser_response_record(response) for response in browser_responses],
                            url,
                            expected_sha=expected_sha,
                        )
                    )
                    page_payload = _page_dashboard_payload(page)
                    _check_visual_contract(page)
                    _check_source_status_panel(page, page_payload)
                    page.screenshot(
                        path=str(
                            ACCEPTANCE_SCREENSHOT_DIR / f"{name}-portfolio.png"
                        ),
                        full_page=True,
                    )
                    if "看板数据加载失败" in page.locator("body").inner_text():
                        errors.append(f"{name}：页面显示看板数据加载失败")
                    try:
                        _check_page_safety(page)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_tool_workspaces(page, detail_key)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_account_holdings(
                            page,
                            payload,
                            reports_dir=reports_dir,
                            screenshot_dir=ACCEPTANCE_SCREENSHOT_DIR,
                        )
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_separated_trend_report_views(
                            page,
                            payload,
                            screenshot_dir=ACCEPTANCE_SCREENSHOT_DIR,
                        )
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    if simulate_payloads is not None and history_expectations is not None:
                        try:
                            _check_trend_account_views(
                                page,
                                payload,
                                _refresh_simulate_payloads(url, simulate_payloads),
                                history_expectations,
                                screenshot_dir=ACCEPTANCE_SCREENSHOT_DIR,
                            )
                        except Exception as exc:
                            errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _select_account_tab(page, "futu")
                        _check_session_prices(page)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_tiger_tab(page)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    phillips_card = page.locator(
                        '#broker-summary-cards [data-broker="phillips"]'
                    )
                    if phillips_card.locator("strong").inner_text().strip() in {"", "-"}:
                        errors.append(f"{name}：辉立账户卡没有显示资产")
                    try:
                        _check_cn_filter(page, expected_cn)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    errors.extend(
                        f"{name}：浏览器错误：{message}" for message in browser_errors
                    )
                    page.close()
                    page = None
                except Exception as exc:
                    errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    if page is not None:
                        try:
                            page.close()
                        except Exception as close_exc:
                            errors.append(
                                f"{name}：{type(close_exc).__name__}: {close_exc}"
                            )
            browser.close()
    except Exception as exc:
        return errors, f"浏览器不可用：{type(exc).__name__}: {exc}"
    return errors, None


def _browser_account_network_errors(
    requests: list[tuple[Any, str, str | None]],
    responses: list[tuple[Any, str, int, str | None, object | None]],
    gateway_url: str,
    *,
    expected_sha: str | None = None,
) -> list[str]:
    gateway = urlsplit(gateway_url)
    account_requests = [
        item for item in requests
        if urlsplit(item[1]).netloc == gateway.netloc
        and urlsplit(item[1]).path == ACCOUNT_SNAPSHOT_PATH
    ]
    if len(account_requests) < 3:
        return ["浏览器未等待两个 Account 五秒轮询机会"]
    if not any(etag for _request, _url, etag in account_requests[1:]):
        return ["浏览器后续 Account 请求缺少 If-None-Match"]
    if any(urlsplit(request_url).path == "/api/quotes" for _request, request_url, _etag in requests):
        return ["浏览器仍请求 Legacy /api/quotes"]
    if not any(
        urlsplit(request_url).netloc == gateway.netloc
        and urlsplit(request_url).path == "/api/dashboard"
        for _request, request_url, _etag in requests
    ):
        return ["浏览器未请求 Legacy /api/dashboard"]
    account_responses = [
        [response_request, response_url, status, response_etag, payload]
        for response_request, response_url, status, response_etag, payload in responses
        if urlsplit(response_url).netloc == gateway.netloc
        and urlsplit(response_url).path == ACCOUNT_SNAPSHOT_PATH
    ]
    for request, _request_url, _request_etag in account_requests[1:]:
        matched_index = next(
            (
                index for index, response in enumerate(account_responses)
                if response[0] is request
            ),
            None,
        )
        if matched_index is None:
            return ["浏览器后续 Account 请求没有对应的 304 或有效 200 响应"]
        _response_request, _response_url, status, _response_etag, payload = account_responses.pop(
            matched_index
        )
        if status == 304:
            continue
        if status != 200:
            return ["浏览器后续 Account 请求响应状态无效"]
        if expected_sha is not None:
            errors = _account_snapshot_errors(payload, expected_sha=expected_sha)
        else:
            errors = [] if isinstance(payload, Mapping) and payload.get(
                "schema_version"
            ) == 1 and payload.get("status") in {"healthy", "stale"} and isinstance(
                payload.get("stale"), bool
            ) else ["浏览器后续 Account 200 响应契约无效"]
        if errors:
            return ["浏览器后续 Account 请求响应契约无效"]
    return []


def _browser_response_record(
    response: Any,
) -> tuple[Any, str, int, str | None, object | None]:
    request = response.request
    etag = request.header_value("if-none-match")
    payload: object | None = None
    if response.status == 200 and urlsplit(response.url).path == ACCOUNT_SNAPSHOT_PATH:
        try:
            payload = response.json()
        except Exception:
            payload = None
    return request, response.url, response.status, etag, payload


def _runtime_health_errors(
    payload: object,
    *,
    name: str,
    expected_schema: str,
    expected_module: str,
    pid: int,
    expected_sha: str,
    expected_cwd: Path,
    process_started_at: datetime,
    expected_upstream_status: str | None = None,
    expected_account_upstream_status: str | None = None,
) -> list[str]:
    if not isinstance(payload, Mapping):
        return [f"{name} health 不是对象"]
    errors: list[str] = []
    if payload.get("schema_version") != expected_schema:
        errors.append(f"{name} health schema 不匹配")
    if payload.get("module") != expected_module:
        errors.append(f"{name} health 模块身份不匹配")
    health_pid = payload.get("pid")
    if (
        not isinstance(health_pid, int)
        or isinstance(health_pid, bool)
        or health_pid <= 0
        or health_pid != pid
    ):
        errors.append(f"{name} health PID 不匹配")
    cwd = payload.get("cwd")
    if (
        not isinstance(cwd, str)
        or not cwd.strip()
        or Path(cwd).resolve() != expected_cwd.resolve()
    ):
        errors.append(f"{name} health 工作目录不匹配")
    if payload.get("git_sha") != expected_sha:
        errors.append(f"{name} health Git SHA 不匹配")
    if payload.get("source_state") != "clean":
        errors.append(f"{name} health 源码状态不是 clean")
    if (
        expected_upstream_status is not None
        and payload.get("upstream_status") != expected_upstream_status
    ):
        errors.append(f"{name} health upstream 状态不匹配")
    if (
        expected_account_upstream_status is not None
        and payload.get("account_upstream_status") != expected_account_upstream_status
    ):
        errors.append(f"{name} health Account upstream 状态不匹配")
    try:
        started_at = datetime.fromisoformat(str(payload.get("started_at") or ""))
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        if started_at < process_started_at:
            errors.append(f"{name} health 启动时间早于候选进程")
    except (TypeError, ValueError):
        errors.append(f"{name} health 启动时间无效")
    return errors


def _account_runtime_health_errors(
    payload: object,
    *,
    pid: int,
    expected_sha: str,
    process_started_at: datetime,
) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["Account API health 不是对象"]
    errors: list[str] = []
    expected = {
        "schema_version": "open_trader.account_api.health.v1",
        "module": "account_api",
        "status": "ok",
        "mode": "production",
        "api_git_sha": expected_sha,
        "worker_git_sha": expected_sha,
        "release_match": True,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            label = "Worker Git SHA" if field == "worker_git_sha" else field
            errors.append(f"Account API health {label} 不匹配")
    health_pid = payload.get("pid")
    if type(health_pid) is not int or health_pid <= 0 or health_pid != pid:
        errors.append("Account API health PID 不匹配")
    try:
        started_at = datetime.fromisoformat(str(payload.get("started_at") or ""))
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        if started_at < process_started_at:
            errors.append("Account API health 启动时间早于候选进程")
    except (TypeError, ValueError):
        errors.append("Account API health 启动时间无效")
    return errors


def _log_errors(
    path: Path,
    *,
    pid: int,
    expected_sha: str,
    expected_cwd: Path,
    process_started_at: datetime,
    name: str = "Dashboard",
    prefix: str = "dashboard_runtime: ",
) -> list[str]:
    try:
        if not path.exists():
            return [f"日志不存在：{path}"]
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"日志读取失败：{type(exc).__name__}: {exc}"]
    records: list[tuple[int, Mapping[str, Any]]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            record = json.loads(line.removeprefix(prefix))
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            records.append((index, record))
    errors: list[str] = []
    matching = [
        item for item in records
        if (
            isinstance(item[1].get("pid"), int)
            and not isinstance(item[1].get("pid"), bool)
            and item[1].get("pid") > 0
            and item[1].get("pid") == pid
        )
    ]
    if not matching:
        errors.append(f"日志没有候选 {name} PID：{pid}")
        fresh_text = text
    else:
        index, record = matching[-1]
        if index != 0:
            errors.append(f"{name} 日志不是候选进程的新日志文件")
        try:
            if path.stat().st_mtime < process_started_at.timestamp():
                errors.append(f"{name} 日志修改时间早于候选进程")
        except OSError as exc:
            errors.append(f"日志状态读取失败：{type(exc).__name__}: {exc}")
        if record.get("git_sha") != expected_sha:
            errors.append(f"日志中的 {name} Git SHA 不匹配")
        record_cwd = record.get("cwd")
        if (
            not isinstance(record_cwd, str)
            or not record_cwd.strip()
            or Path(record_cwd).resolve() != expected_cwd.resolve()
        ):
            errors.append(f"日志中的 {name} 工作目录不匹配")
        if record.get("source_state") != "clean":
            errors.append(f"日志中的 {name} 源码状态不是 clean")
        try:
            recorded_start = datetime.fromisoformat(
                str(record.get("started_at") or "")
            )
            if recorded_start.tzinfo is None or recorded_start.utcoffset() is None:
                raise ValueError("timezone-aware timestamp required")
            if recorded_start < process_started_at:
                errors.append(f"日志中的 {name} 启动时间早于候选进程")
        except (TypeError, ValueError):
            errors.append(f"日志中的 {name} 启动时间无效")
        fresh_text = "\n".join(text.splitlines()[index:])
    markers = ("Traceback (most recent call last)", "看板数据加载失败")
    errors.extend(
        f"日志包含错误标记：{marker}" for marker in markers if marker in fresh_text
    )
    return errors


def _controller_log_errors(
    root: Path,
    *,
    market: str,
    pid: int,
    expected_sha: str,
    expected_cwd: Path,
    process_started_at: datetime,
) -> list[str]:
    stem = root / "logs/daily_premarket" / (
        f"launchd-trend-controller-{market.lower()}"
    )
    stdout_path = stem.with_suffix(".out.log")
    stderr_path = stem.with_suffix(".err.log")
    try:
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_bytes()
    except OSError as exc:
        return [f"{market} 控制器日志读取失败：{type(exc).__name__}: {exc}"]

    prefix = "controller_runtime: "
    records: list[Mapping[str, Any]] = []
    for line in stdout.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            record = json.loads(line.removeprefix(prefix))
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping) and record.get("pid") == pid:
            records.append(record)
    if not records:
        return [f"{market} 控制器日志没有当前 PID：{pid}"]

    record = records[-1]
    errors: list[str] = []
    if record.get("git_sha") != expected_sha:
        errors.append(f"{market} 控制器日志 Git SHA 不匹配")
    record_cwd = record.get("cwd")
    if (
        not isinstance(record_cwd, str)
        or not record_cwd.strip()
        or Path(record_cwd).resolve() != expected_cwd.resolve()
    ):
        errors.append(f"{market} 控制器日志工作目录不匹配")
    try:
        verified_at = datetime.fromisoformat(str(record.get("verified_at") or ""))
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise ValueError
        if verified_at < process_started_at:
            errors.append(f"{market} 控制器日志早于当前进程")
    except (TypeError, ValueError):
        errors.append(f"{market} 控制器日志验证时间无效")
    offset = record.get("stderr_offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(stderr):
        errors.append(f"{market} 控制器 stderr 起点无效")
    elif stderr[offset:].strip():
        errors.append(f"{market} 控制器 stderr 包含启动后输出")
    return errors


def _controller_allows_missing_first_success(
    controller: Mapping[str, Any],
) -> bool:
    return (
        controller.get("health") == "healthy"
        and controller.get("blocking") is False
        and controller.get("blocker") in (None, "")
        and controller.get("phase") in {"reconciling", "recovering_report"}
    )


def _trend_controller_errors(
    payload: Mapping[str, Any],
    *,
    expected_root: Path,
    expected_sha: str,
    now: datetime | None = None,
) -> list[str]:
    controllers = payload.get("trend_controllers")
    if not isinstance(controllers, Mapping):
        return ["Dashboard 缺少三市场趋势控制器状态"]

    errors: list[str] = []
    current = now or datetime.now().astimezone()
    expected_cwd = expected_root.resolve()
    for broker, market in TREND_SIMULATE_MARKETS.items():
        controller = controllers.get(broker)
        if not isinstance(controller, Mapping):
            errors.append(f"{broker} 控制器状态缺失")
            continue
        if (
            controller.get("effective_mode") != "execute"
            or controller.get("health") != "healthy"
            or controller.get("blocking") is not False
            or controller.get("blocker") not in (None, "")
        ):
            errors.append(f"{broker} 控制器不可用或阻塞")
        if (
            controller.get("last_success") is None
            and not _controller_allows_missing_first_success(controller)
        ):
            errors.append(f"{broker} 控制器尚无首次成功状态")

        pid = controller.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            errors.append(f"{broker} 控制器 PID 无效")
            continue
        try:
            os.kill(pid, 0)
        except OSError as exc:
            errors.append(f"{broker} 控制器 PID 不存活：{pid}（{exc}）")
            continue

        working_directory = controller.get("working_directory")
        if (
            not isinstance(working_directory, str)
            or not working_directory.strip()
            or Path(working_directory).resolve() != expected_cwd
        ):
            errors.append(f"{broker} 控制器工作目录不匹配")
        if controller.get("git_sha") != expected_sha:
            errors.append(f"{broker} 控制器 Git SHA 不匹配")
        try:
            heartbeat = datetime.fromisoformat(
                str(controller.get("heartbeat_at") or "")
            )
            if heartbeat.tzinfo is None or heartbeat.utcoffset() is None:
                raise ValueError
            if abs(current - heartbeat) > timedelta(minutes=2):
                errors.append(f"{broker} 控制器心跳不新鲜")
        except (TypeError, ValueError):
            errors.append(f"{broker} 控制器心跳无效")

        try:
            process_cwd = _process_cwd(pid)
            process_started_at = _process_started_at(pid)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            errors.append(
                f"{broker} 控制器进程事实读取失败：{type(exc).__name__}: {exc}"
            )
            continue
        if process_cwd != expected_cwd:
            errors.append(f"{broker} 控制器实际工作目录不匹配")
        errors.extend(_controller_log_errors(
            expected_root,
            market=market,
            pid=pid,
            expected_sha=expected_sha,
            expected_cwd=expected_cwd,
            process_started_at=process_started_at,
        ))
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--legacy-url", default="http://127.0.0.1:8767")
    parser.add_argument("--account-url", default="http://127.0.0.1:8768")
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument(
        "--expected-eastmoney-cny", type=Decimal
    )
    parser.add_argument("--expected-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-sha")
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("logs/frontend_gateway/launchd.out.log"),
    )
    parser.add_argument(
        "--legacy-log",
        type=Path,
        default=Path("logs/legacy_dashboard/launchd.out.log"),
    )
    parser.add_argument(
        "--account-log",
        type=Path,
        default=Path("logs/account_api/launchd.out.log"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors: list[str] = []
    expected_cn = 0
    expected_sha = ""
    gateway_pid: int | None = None
    gateway_cwd = args.expected_root.resolve()
    gateway_started_at: datetime | None = None
    legacy_pid: int | None = None
    legacy_cwd = args.expected_root.resolve()
    legacy_started_at: datetime | None = None
    account_pid: int | None = None
    account_cwd = args.expected_root.resolve()
    account_started_at: datetime | None = None
    browser_payload: dict[str, Any] = {}
    reports_dir: Path | None = None
    simulate_payloads: dict[str, dict[str, Any]] = {}
    history_expectations: dict[str, list[dict[str, Any]]] = {}
    account_ids: dict[str, int] = {}
    external_blocker: str | None = None
    project_data_dir: Path | None = None
    try:
        expected_sha = args.expected_sha or subprocess.check_output(
            ["git", "-C", str(args.expected_root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        (
            gateway_pid,
            gateway_cwd,
            gateway_started_at,
            gateway_errors,
        ) = _runtime_evidence(
            "Frontend Gateway",
            url=args.url,
            expected_schema="open_trader.frontend_gateway.health.v1",
            expected_module="frontend_gateway",
            expected_root=args.expected_root,
            expected_sha=expected_sha,
            expected_upstream_status="ok",
            expected_account_upstream_status="ok",
        )
        (
            legacy_pid,
            legacy_cwd,
            legacy_started_at,
            legacy_errors,
        ) = _runtime_evidence(
            "Legacy Dashboard",
            url=args.legacy_url,
            expected_schema="open_trader.legacy_dashboard.health.v1",
            expected_module="legacy_dashboard",
            expected_root=args.expected_root,
            expected_sha=expected_sha,
        )
        (
            account_pid,
            account_cwd,
            account_started_at,
            account_errors,
        ) = _runtime_evidence(
            "Account API",
            url=args.account_url,
            expected_schema="open_trader.account_api.health.v1",
            expected_module="account_api",
            expected_root=args.expected_root,
            expected_sha=expected_sha,
            account_api=True,
        )
        errors.extend(gateway_errors)
        errors.extend(legacy_errors)
        errors.extend(account_errors)
        if (
            gateway_pid is not None
            and legacy_pid is not None
            and gateway_pid == legacy_pid
        ):
            errors.append("Frontend Gateway 与 Legacy Dashboard 必须使用不同 PID")
        if account_pid is not None and account_pid in {gateway_pid, legacy_pid}:
            errors.append("Account API 必须使用独立 PID")
        project_data_dir = _project_data_dir(args.expected_root)
        expected_cn = _expected_cn_holdings(args.expected_root)
        phillips_total, phillips_period = _latest_phillips_expectation(
            project_data_dir
        )
        errors.extend(_account_sync_worker_errors(
            args.expected_root, expected_root=args.expected_root, expected_sha=expected_sha,
        ))
        first = _fetch_payload(args.url)
        first_reports_dir = _effective_reports_dir(first, process_cwd=legacy_cwd)
        errors.extend(validate_dashboard_payload(
            first, expected_cn=expected_cn,
            expected_eastmoney_cny=args.expected_eastmoney_cny,
            expected_rows=args.expected_rows,
            expected_phillips_total=phillips_total,
            expected_phillips_period=phillips_period,
        ))
        try:
            account_ids = _configured_simulate_account_ids(args.expected_root)
        except Exception as exc:
            errors.append(f"Futu 模拟账户配置不可用：{exc}")
        else:
            (
                simulate_payloads,
                simulate_errors,
                external_blocker,
            ) = _check_simulated_accounts(
                args.url,
                first,
                account_ids,
                project_data_dir,
                first_reports_dir,
            )
            errors.extend(simulate_errors)
            errors.extend(validate_integrated_candidate(
                first,
                expected_root=args.expected_root,
                expected_sha=expected_sha,
                reports_dir=first_reports_dir,
                account_ids=account_ids,
            ))
        history_expectations, history_errors = _check_history_endpoints(
            args.url,
            project_data_dir,
            first_reports_dir,
        )
        errors.extend(history_errors)
        snapshot_status, snapshot, snapshot_etag = _fetch_account_snapshot(args.url)
        if snapshot_status != 200 or snapshot is None:
            errors.append(f"Gateway Account snapshot HTTP {snapshot_status}")
        else:
            errors.extend(_account_snapshot_errors(snapshot, expected_sha=expected_sha))
            if not snapshot_etag:
                errors.append("Gateway Account snapshot 缺少 ETag")
            else:
                conditional_status, conditional_snapshot, conditional_etag = (
                    _fetch_account_snapshot(args.url, etag=snapshot_etag)
                )
                if conditional_status == 304:
                    if conditional_snapshot is not None or conditional_etag != snapshot_etag:
                        errors.append("Gateway Account snapshot 304 ETag 不匹配")
                elif conditional_status == 200 and conditional_snapshot is not None:
                    errors.extend(_account_snapshot_errors(
                        conditional_snapshot, expected_sha=expected_sha,
                    ))
                else:
                    errors.append(
                        f"Gateway Account snapshot 条件请求 HTTP {conditional_status}"
                    )
        parity = check_account_api_parity(project_data_dir, base_url=args.account_url)
        if parity.status != "PASS":
            errors.append(f"Account API parity {parity.status}: {parity.reason}")
        second = _fetch_payload(args.url)
        browser_payload = second
        reports_dir = _effective_reports_dir(second, process_cwd=legacy_cwd)
        if first_reports_dir != reports_dir:
            errors.append("账户刷新前后的 Dashboard reports_dir 不一致")
        errors.extend(validate_dashboard_payload(
            second, expected_cn=expected_cn,
            expected_eastmoney_cny=args.expected_eastmoney_cny,
            expected_rows=args.expected_rows,
            expected_phillips_total=phillips_total,
            expected_phillips_period=phillips_period,
        ))
        errors.extend(_trend_controller_errors(
            second,
            expected_root=args.expected_root,
            expected_sha=expected_sha,
        ))
        if account_ids:
            errors.extend(validate_integrated_candidate(
                second,
                expected_root=args.expected_root,
                expected_sha=expected_sha,
                reports_dir=reports_dir,
                account_ids=account_ids,
            ))
        if dashboard_signature(first) != dashboard_signature(second):
            errors.append("账户刷新后的 Dashboard 数据不稳定")
        if trend_advice_signature(first) != trend_advice_signature(second):
            errors.append("实盘刷新改写了冻结建议、Kelly 或模拟统计")
    except Exception as exc:
        errors.append(f"运行检查失败：{type(exc).__name__}: {exc}")
    browser_errors, blocker = _browser_check(
        args.url,
        expected_cn,
        browser_payload,
        reports_dir,
        simulate_payloads,
        history_expectations,
    )
    errors.extend(browser_errors)
    if gateway_pid is not None and gateway_started_at is not None:
        errors.extend(_log_errors(
            args.log,
            name="Frontend Gateway",
            prefix="frontend_gateway_runtime: ",
            pid=gateway_pid,
            expected_sha=expected_sha,
            expected_cwd=gateway_cwd,
            process_started_at=gateway_started_at,
        ))
    if legacy_pid is not None and legacy_started_at is not None:
        errors.extend(_log_errors(
            args.legacy_log,
            name="Legacy Dashboard",
            prefix="dashboard_runtime: ",
            pid=legacy_pid,
            expected_sha=expected_sha,
            expected_cwd=legacy_cwd,
            process_started_at=legacy_started_at,
        ))
    if account_pid is not None and account_started_at is not None:
        errors.extend(_log_errors(
            args.account_log,
            name="Account API",
            prefix="account_api_runtime: ",
            pid=account_pid,
            expected_sha=expected_sha,
            expected_cwd=account_cwd,
            process_started_at=account_started_at,
        ))
    status = classify_result(
        errors, browser_blocker=blocker, external_blocker=external_blocker
    )
    blockers = [item for item in (external_blocker, blocker) if item]
    result = {
        "status": status,
        "pid": gateway_pid,
        "gateway_pid": gateway_pid,
        "legacy_pid": legacy_pid,
        "account_pid": account_pid,
        "errors": errors,
        "blocker": "；".join(blockers) or None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
