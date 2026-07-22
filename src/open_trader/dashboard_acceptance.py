from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.request import urlopen

from .dashboard import _is_dashboard_holding, _read_csv_rows
from .daily_premarket import _optional_positive_tm_id, _read_env_file
from .futu_symbols import to_futu_symbol
from .kelly_order_execution import FutuSimulateOrderExecutionClient
from .parsers.phillips import PhillipsStatementParser
from .trend_simulate_positions import (
    TREND_SIMULATE_BROKERS,
    _action_events,
    _reports_by_hash,
)
from .trend_review import _report_hash
from .strategy_drawdown import strategy_parameter_hash


SESSION_LABELS = ("夜盘", "盘前", "盘中", "盘后")
SESSION_KEYS = {"overnight", "pre_market", "regular", "after_hours"}

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
ACCOUNT_VIEW_LABELS = {
    "tiger": ("真实持仓", "模拟盘持仓", "趋势报告", "美股复盘"),
    "phillips": ("真实持仓", "模拟盘持仓", "趋势报告", "港股复盘"),
    "eastmoney": ("真实持仓", "模拟盘持仓", "趋势报告", "A股复盘"),
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
OPTION_ATTENTION_COLUMN_LABELS = (
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
)
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
    "tablet-portfolio.png",
    "760-trend-report.png",
    "mobile-portfolio.png",
    "375-trend-report.png",
)
TREND_REASON_LABELS = {
    "protection_line_already_triggered": "活动保护线已触发",
    "danger_signal": "危险信号触发",
    "left_trend_right_side": "右侧趋势已结束",
    "holding_signal_unknown": "趋势信号不完整",
    "holding_kline_unavailable": "持仓日线数据不可用",
    "trend_intact": "趋势保持完好",
    "temperature_changed_to_flat": "趋势温度转平",
    "a_share_only": "仅限 A 股股票",
    "temperature_missing": "个股趋势温度缺失",
    "temperature_transition_not_entry": "不是温转热或温转沸",
    "filter_price_missing": "筛选价缺失",
    "filter_price_above_200": "筛选价高于 200 元",
    "strength_missing": "趋势强度缺失",
    "strength_below_95": "趋势强度低于 95",
    "industry_id_missing": "行业 ID 缺失",
    "industry_temperature_missing": "行业温度缺失",
    "industry_temperature_not_hot": "行业温度未达到热或沸",
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
        phillips_status = next(
            (
                row for row in payload.get("source_statuses") or []
                if row.get("broker") == "phillips"
            ),
            {},
        )
        if expected_phillips_period not in str(phillips_status.get("display_text", "")):
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
    errors.extend(_trend_execution_batch_errors(payload))
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
            assert report.get("broker") == broker and report.get("market") == market, (
                f"{broker} 三市场报告身份不匹配"
            )
            assert report.get("data_status") == "current", (
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
                and snapshot.get("strategy_version") == "v4"
                and re.fullmatch(
                    r"[0-9a-f]{40}", str(snapshot.get("process_version") or "")
                )
                and report.get("strategy_version") == "v4"
            ), f"{broker} 冻结 Kelly/回撤 v4 策略身份无效"
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
            assert target_values and max(
                _position_decimal(value, "名义仓位上限") for value in target_values
            ) == Decimal("0.04"), f"{broker} 固定名义仓位上限不是 4%"

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
            assert report.get("risk_skips") == judgments.get("risk_skips", []), (
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
                assert Decimal("0") < weight <= Decimal("0.04"), (
                    f"{broker} 买入目标超过固定名义仓位上限"
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
            assert bootstrap.get("parameter_hash") == strategy_parameter_hash(
                parameters
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
    with urlopen(f"{url.rstrip('/')}/api/dashboard", timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Dashboard API HTTP {response.status}")
        return json.load(response)


def _fetch_quotes_payload(url: str) -> dict[str, Any]:
    with urlopen(f"{url.rstrip('/')}/api/quotes", timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Quotes API HTTP {response.status}")
        return json.load(response)


def _fetch_json_path(url: str, path: str) -> Any:
    with urlopen(f"{url.rstrip('/')}{path}", timeout=15) as response:
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
    for _, _, _, event in _action_events(data_dir, market):
        report_hash = str(event.get("report_sha256") or "").strip().lower()
        if len(report_hash) == 64:
            latest_events[(
                report_hash,
                str(event.get("symbol") or "").strip().upper(),
                str(event.get("side") or "").strip().lower(),
            )] = event

    expectations: list[dict[str, Any]] = []
    for (report_hash, symbol, side), event in latest_events.items():
        report = reports.get(report_hash)
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
            and execution.get("updated_at") == event.get("recorded_at")
        ), f"{artifact} 历史报告动作 {symbol} 消失或执行状态不匹配"
        expectations.append({**report, "symbol": symbol, "side": side, "event": event})
    return expectations


def _check_account_view_contract(page: Any, section: Any, broker: str) -> None:
    tabs = section.locator('[role="tab"][data-account-view]')
    assert tabs.count() == 4, f"{broker} 账户视图 Tab 数量不是 4"
    actual_labels = tuple(tabs.nth(index).inner_text().strip() for index in range(4))
    assert actual_labels == ACCOUNT_VIEW_LABELS[broker], f"{broker} 账户视图 Tab 顺序不正确"
    assert tuple(
        tabs.nth(index).get_attribute("data-account-view") for index in range(4)
    ) == ("real", "simulate", "report", "review"), (
        f"{broker} 账户视图 Tab 身份不正确"
    )
    assert tabs.nth(0).get_attribute("aria-selected") == "true" and all(
        tabs.nth(index).get_attribute("aria-selected") == "false"
        for index in range(1, 4)
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
    for index in range(4):
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
        timeout=10_000,
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
    status_labels = {
        "submitted": "已提交",
        "partially_filled": "部分成交",
        "filled": "全部成交",
        "failed": "失败",
        "blocked": "受阻",
        "uncertain": "状态不确定，禁止自动重试",
        "conflict": "订单事实冲突，禁止提交",
        "missed": "已错过策略窗口",
        "incomplete": "未完成",
    }
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
        _check_report_simulation_overlay(report_root, report, simulated, broker)
        _check_trend_controller_status(
            page, panel, broker, controllers.get(broker)
        )
        _check_integrated_trend_ui(report_root, report, broker)
        assert _plain(report.get("report_date")) in report_root.inner_text(), (
            f"{broker} 当前趋势报告日期未显示"
        )
        history_button = panel.locator("[data-report-history]")
        assert history_button.count() == 1, f"{broker} 当前报告缺少历史入口"
        _check_history_control_contract(history_button, f"{broker} 历史报告入口")
        if broker == "eastmoney" and screenshot_dir is not None:
            width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
            page.screenshot(
                path=str(screenshot_dir / f"{width}-trend-report.png"),
                full_page=True,
            )
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
            _check_history_control_contract(current, f"{broker} 历史报告返回")
            event = expectation.get("event")
            if isinstance(event, Mapping):
                label = status_labels.get(str(event.get("status") or ""))
                if label:
                    assert label in panel.inner_text(), (
                        f"{broker} 精确历史报告缺少执行状态 {label}"
                    )
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

        review_tab = section.locator('[data-account-view="review"]')
        review_tab.click()
        review_root = panel.locator(".trend-review")
        review_root.wait_for()
        review = reviews.get(broker)
        assert isinstance(review, Mapping) and review.get("available") is True, (
            f"{broker} 趋势复盘不可用"
        )
        text = review_root.inner_text()
        assert "卡玛比率" in text and "夏普比率" in text, (
            f"{broker} 趋势复盘指标不完整"
        )
        assert review_tab.get_attribute("aria-selected") == "true", (
            f"{broker} 复盘 Tab 未保持选中"
        )
        assert review_tab.evaluate(
            "element => element === document.activeElement"
        ), f"{broker} 复盘打开后焦点未保持在 Tab"
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势复盘视图出现横向滚动"
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
                expectations.append(dict(local))
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
            ".strategy-tools button:visible, #refresh-quotes:visible, "
            ".broker-summary-card:visible, .account-holding-actions button:visible, "
            ".trend-report-entry button:visible",
        )
        t_signal_button = page.locator(
            '.account-holding-actions button[data-detail-mode="t_signal"]:visible'
        )
        assert t_signal_button.count() >= 1, "移动端缺少做T详情入口"
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

    page.locator("#open-kelly-lab").click()
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

    page.locator("#open-standard-backtest").click()
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


def _check_visible_decimal_precision(text: str, label: str) -> None:
    offenders = re.findall(
        r"(?<![\w.-])[+-]?\d[\d,]*\.\d{3,}(?![\w.-])", text
    )
    assert not offenders, f"{label} 数值超过两位小数：{offenders[:3]}"


def _check_report_simulation_overlay(
    report_root: Any,
    report: Mapping[str, Any],
    simulated: Mapping[str, Any] | None,
    broker: str,
) -> None:
    simulation = report_root.locator(".trend-simulation-overlay")
    assert simulation.count() == 1, f"{broker} 趋势报告缺少模拟盘执行状态"
    simulation_text = simulation.inner_text()
    assert "模拟盘执行状态" in simulation_text, f"{broker} 模拟盘状态标题缺失"
    assert "富途" in simulation_text, f"{broker} 模拟盘来源缺失"

    positions = simulated.get("positions") if simulated is not None else []
    assert isinstance(positions, list), f"{broker} 模拟盘持仓无效"
    by_symbol = {
        str(position.get("symbol") or "").strip().upper(): position
        for position in positions
        if isinstance(position, Mapping)
    }
    seen: set[str] = set()
    for key in ("hold_actions", "review_actions"):
        actions = report.get(key) or []
        assert isinstance(actions, list), f"{broker} {key} 列表无效"
        for hold in actions:
            if not isinstance(hold, Mapping) or hold.get("action") != "HOLD":
                continue
            symbol = str(hold.get("symbol") or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            position = by_symbol.get(symbol)
            if position is None:
                continue
            row = simulation.locator(f'[data-simulation-symbol="{symbol}"]')
            assert row.count() == 1, f"{broker} {symbol} 缺少模拟盘对照行"
            quantity = f"模拟持仓 {_display_number(position['quantity'])}"
            facts = {
                text.strip()
                for text in row.locator(
                    ".trend-actual-facts span"
                ).all_inner_texts()
            }
            assert quantity in facts, f"{broker} {symbol} 模拟盘数量未显示"
            status = row.locator("[data-deviation]")
            assert status.count() == 1, f"{broker} {symbol} 模拟盘偏差状态缺失"
            assert status.get_attribute("data-deviation") == "followed", (
                f"{broker} {symbol} 模拟盘偏差状态不是 followed"
            )


def _check_integrated_trend_ui(
    report_root: Any, report: Mapping[str, Any], broker: str,
) -> None:
    summary = report.get("risk_summary")
    drawdown = report.get("drawdown_summary")
    overlay = report.get("actual_overlay")
    assert (
        isinstance(summary, Mapping)
        and isinstance(drawdown, Mapping)
        and isinstance(overlay, Mapping)
    ), f"{broker} 趋势报告缺少集成风险视图数据"
    risk = report_root.locator(".trend-risk-summary")
    assert risk.count() == 1, f"{broker} 趋势报告缺少风险摘要"
    assert risk.get_attribute("data-risk-status") == summary.get("status"), (
        f"{broker} 风险状态未同时提供文字状态"
    )
    assert report_root.locator(".trend-drawdown-summary").count() == 1, (
        f"{broker} 趋势报告缺少回撤状态"
    )
    assert report_root.locator(".trend-actual-overlay").count() == 1, (
        f"{broker} 趋势报告缺少只读实盘辅助"
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
        _plain(drawdown.get("status_label")), "实盘执行辅助",
        _plain(overlay.get("broker_label")),
        "5% 是风险预算目标，不是最大损失保证。",
        "不会改写模拟建议、Kelly、模拟统计或报告哈希",
        "不会自动交易真实账户",
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
    for key in ("items", "outside_positions"):
        items = overlay.get(key)
        assert isinstance(items, list), f"{broker} 实盘偏差列表无效"
        for item in items:
            label = item.get("deviation_label") if isinstance(item, Mapping) else None
            assert isinstance(label, str) and label and label in text, (
                f"{broker} 实盘偏差状态未用文字表达"
            )
    assert "本次可用风险" not in text, f"{broker} UI 仍包含 本次可用风险"


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
    return (
        action == "MANUAL_REVIEW"
        or action not in {"SELL_ALL", "HOLD", "MANUAL_REVIEW"}
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
    sells = [
        item for item in formal
        if item.get("action") == "SELL_ALL" and not _trend_action_needs_review(item)
    ]
    buys = [
        item for item in formal
        if item.get("action") == "BUY"
        and not _trend_action_needs_review(item)
    ]
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
    holds = [
        item for item in holdings
        if item.get("action") == "HOLD" and not _trend_action_needs_review(item)
    ]
    reviews: list[Mapping[str, Any]] = []
    for item in [*formal, *holdings]:
        if _trend_action_needs_review(item) and item not in reviews:
            reviews.append(item)
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
                if field != "execution"
            }
            for item in projected
        ] == value
        for key, value in expected_actions.items()
    ), f"{broker} 冻结报告动作与 API 投影不一致"
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


def _check_action_trend_stages(
    stage_texts: list[str], report: Mapping[str, Any], broker: str,
) -> None:
    expected = (
        ("优先处理 · 卖出触发", "sell_actions", "全部卖出"),
        ("需要确认 · 人工复核", "review_actions", "人工复核"),
        (
            f"{_plain(report.get('buy_window'))} · 正式买入计划",
            "buy_actions",
            "正式买入",
        ),
        ("盘中持续 · 已有持仓", "hold_actions", "继续持有"),
    )
    assert len(stage_texts) == len(expected), f"{broker} 趋势报告阶段数量不正确"
    for text, (title, key, action) in zip(stage_texts, expected, strict=True):
        assert title in text, f"{broker} 趋势报告缺少阶段 {title}"
        rows = report.get(key) if isinstance(report.get(key), list) else []
        if not rows:
            assert "无" in text, f"{broker} 的 {title} 空阶段未显示 无"
            continue
        for item in rows:
            assert isinstance(item, Mapping), f"{broker} 的 {title} 动作格式无效"
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
                    item.get("industry_temperature"), item.get("market_cap"),
                    item.get("amount"), f"{format(weight.normalize(), 'f')}%",
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
                    TREND_REASON_LABELS.get(
                        str(item.get("reason", "")),
                        "未知动作或原因，需人工确认",
                    ),
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
                    TREND_REASON_LABELS.get(
                        str(item.get("reason", "")), "未知动作或原因，需人工确认"
                    ),
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


def _check_trend_audit(audit: Any, report: Mapping[str, Any], broker: str) -> None:
    assert audit.count() == 1 and audit.get_attribute("open") is None, (
        f"{broker} 趋势报告审计详情未保持收起"
    )
    summary = audit.locator("summary")
    assert summary.count() == 1, f"{broker} 趋势报告缺少审计摘要"
    summary.click()
    sections = audit.locator("section").all_inner_texts()
    expected_sections = 3 if broker == "eastmoney" else 4
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
    profiles = {
        "futu": ("富途", "期权增强", "跨市场期权关注"),
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
        empty = section.locator(".account-empty:visible")
        if rows.count() == 0:
            assert empty.count() == 1 and empty.inner_text().strip() == "当前筛选下没有持仓", (
                f"{broker} 无持仓账户缺少中文空状态"
            )
        else:
            assert empty.count() == 0, f"{broker} 有持仓账户错误显示空状态"
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 账户区块出现横向滚动"
        if broker in TREND_REPORT_BROKERS:
            report = reports.get(broker) if isinstance(reports, Mapping) else None
            assert isinstance(report, Mapping), f"API 缺少 {broker} 趋势报告状态"
            assert report.get("available") is True, f"{broker} 当前趋势报告不可用"
            if reports_dir is not None:
                _check_trend_artifact_projection(reports_dir, broker, report)
            continue
        entry_label = "期权关注" if broker == "futu" else "当天趋势报告"
        assert entry_label in text, f"{broker} 账户区块缺少 {entry_label}"
        report = reports.get(broker) if isinstance(reports, Mapping) else None
        assert isinstance(report, Mapping), f"API 缺少 {broker} 趋势报告状态"
        entry = section.locator(".trend-report-entry")
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
            continue
        assert trigger.count() == 1, f"{broker} 可用报告缺少入口"
        if reports_dir is not None and broker in TREND_REPORT_BROKERS:
            _check_trend_artifact_projection(reports_dir, broker, report)
        entry_text = entry.inner_text()
        if broker == "futu":
            assert "期权关注" in entry_text, "futu 入口缺少期权关注"
        else:
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
        if broker == "futu":
            workspace_text = workspace.inner_text()
            assert "期权关注" in workspace_text, "futu 期权关注工作区标题缺失"
            markets = report.get("attention_markets")
            assert isinstance(markets, list) and [
                market.get("market") for market in markets if isinstance(market, Mapping)
            ] == ["US", "HK"], "futu 期权关注市场顺序不是 US、HK"
            column_headings = workspace.locator(
                '.option-attention-table thead th[scope="col"]'
            )
            assert (
                column_headings.count() == len(OPTION_ATTENTION_COLUMN_LABELS)
                and tuple(column_headings.all_inner_texts())
                == OPTION_ATTENTION_COLUMN_LABELS
            ), "futu 期权关注列标题不匹配"
            rowgroups = workspace.locator(".option-attention-table tbody")
            assert rowgroups.count() == 2, "futu 期权关注市场分组数量不是 2"
            for index, market in enumerate(markets):
                assert isinstance(market, Mapping)
                market_name = _plain(market.get("market"))
                rowgroup = rowgroups.nth(index)
                data_status = market.get("data_status")
                assert data_status in {"current", "stale", "unavailable"}, (
                    f"futu 期权关注 {market_name} 数据状态无效"
                )
                data_date = str(market.get("data_date") or "").strip()
                status_text = str(market.get("status_text") or "").strip()
                if data_status == "current":
                    assert status_text == "今日已更新" or (
                        data_date
                        and status_text == f"今日执行（数据截至 {data_date}）"
                    ), f"futu 期权关注 {market_name} 当前状态文案无效"
                elif data_status == "stale":
                    assert data_date, "futu 期权关注过期市场缺少数据日期"
                    assert status_text == f"数据截至 {data_date}；今日未更新", (
                        f"futu 期权关注 {market_name} 过期状态文案无效"
                    )
                else:
                    assert status_text == "暂时不可用", (
                        f"futu 期权关注 {market_name} 不可用状态文案无效"
                    )
                header = rowgroup.locator(
                    ".option-attention-market-content span"
                )
                assert header.count() == 2 and header.all_inner_texts() == [
                    _plain(market.get("market_label")), status_text,
                ], (
                    f"futu 期权关注 {market_name} 分组市场或状态不匹配"
                )
                items = market.get("items")
                assert isinstance(items, list), "futu 期权关注项目不是列表"
                assert all(isinstance(item, Mapping) for item in items), (
                    "futu 期权关注项目无效"
                )
                rows = rowgroup.locator(".option-attention-row")
                for row_index in range(rows.count()):
                    cells = rows.nth(row_index).locator("td")
                    data_labels = tuple(
                        cells.nth(cell_index).get_attribute("data-label")
                        for cell_index in range(cells.count())
                    )
                    assert data_labels == OPTION_ATTENTION_COLUMN_LABELS, (
                        f"futu 期权关注 {market_name} 第 {row_index + 1} 行列标签不匹配"
                    )
                expected_symbols = [_plain(item.get("symbol")) for item in items]
                symbol_texts = rowgroup.locator(
                    '.option-attention-row td[data-label="标的"]'
                ).all_inner_texts()
                actual_symbols = [
                    text.strip().split(maxsplit=1)[0] if text.strip() else ""
                    for text in symbol_texts
                ]
                assert actual_symbols == expected_symbols, (
                    f"futu 期权关注 {market_name} 分组标的不匹配"
                )
            width = (getattr(page, "viewport_size", None) or {}).get("width", 0)
            if width <= 760:
                column_counts = page.evaluate(
                    r"""() => [...document.querySelectorAll('.option-attention-row')]
                    .map(row => getComputedStyle(row).gridTemplateColumns
                        .trim().split(/\s+/)
                        .filter(column => parseFloat(column) > 0).length)"""
                )
                expected_columns = 1 if width <= 460 else 2
                assert isinstance(column_counts, list) and all(
                    count == expected_columns for count in column_counts
                ), (
                    f"futu 期权关注卡片应为 {expected_columns} 列，实际为 "
                    f"{column_counts}"
                )
                _check_mobile_targets(
                    page,
                    "#return-to-portfolio:visible, "
                    "#trend-report-workspace:visible button:visible, "
                    "#trend-report-workspace:visible summary:visible",
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                ), "futu 期权关注工作区出现横向滚动"
                boxes = page.locator(
                    "#trend-report-workspace:visible .option-attention-workspace, "
                    "#trend-report-workspace:visible .option-attention-table, "
                    "#trend-report-workspace:visible .option-attention-market, "
                    "#trend-report-workspace:visible .option-attention-row"
                ).evaluate_all(
                    "nodes => nodes.map(node => node.getBoundingClientRect())"
                    ".map(r => ({x:r.x,width:r.width}))"
                )
                assert boxes and all(
                    box is not None
                    and box["x"] >= -1
                    and box["x"] + box["width"] <= width + 1
                    for box in boxes
                ), "futu 期权关注工作区元素超出移动端视口"
            close.click()
            assert page.locator("#trend-report-workspace:visible").count() == 0
            assert trigger.evaluate("element => element === document.activeElement")
            continue
        buy_actions = report.get("buy_actions")
        expected_buy_count = len(buy_actions) if isinstance(buy_actions, list) else 0
        _check_open_report_layout(
            page, workspace, broker, expected_buy_count=expected_buy_count
        )
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
        for required in ("报告日期", "数据截至", "生成时间", "账户状态"):
            assert required in workspace_text, f"{broker} 趋势报告工作区缺少 {required}"
        header_values = workspace.locator(".trend-report-header dd").all_inner_texts()
        assert header_values == [
            _plain(report.get(key)) for key in (
                "report_date", "data_date", "generated_at", "account_status",
            )
        ], f"{broker} 趋势报告头部内容与 API 不一致"
        counts = report.get("counts") if isinstance(report.get("counts"), Mapping) else {}
        count_labels = (
            ("正式买入", "buy"), ("全部卖出", "sell"),
            ("继续持有", "hold"), ("人工复核", "review"),
        )
        for label, key in count_labels:
            assert f"{label} {_display_number(counts.get(key) or 0)}" in workspace_text, (
                f"{broker} 趋势报告缺少 {label}计数"
            )
        for required in (
            "优先处理 · 卖出触发", "需要确认 · 人工复核",
            f"{_plain(report.get('buy_window'))} · 正式买入计划",
            "盘中持续 · 已有持仓", "全部卖出", "正式买入", "继续持有",
        ):
            assert required in workspace_text, (
                f"{broker} 趋势报告工作区缺少 {required}"
            )
        assert workspace.locator(".cn-trend-report").count() == 1, (
            f"{broker} 趋势报告未使用动作优先结构"
        )
        stage_texts = workspace.locator(".cn-trend-stage").all_inner_texts()
        _check_action_trend_stages(stage_texts, report, broker)
        assert workspace.locator(".cn-trend-table").count() == 4, (
            f"{broker} 趋势报告动作表数量与 API 不一致"
        )
        sell_actions = report.get("sell_actions")
        expected_execution_rows = expected_buy_count + (
            len(sell_actions) if isinstance(sell_actions, list) else 0
        )
        execution_rows = workspace.locator(".cn-trend-execution")
        assert execution_rows.count() == expected_execution_rows, (
            f"{broker} 执行状态行数量不是 {expected_execution_rows}"
        )
        valid_statuses = {
            "待执行", "已提交", "部分成交", "全部成交", "失败",
            "受阻", "状态不确定，禁止自动重试",
            "订单事实冲突，禁止提交", "已错过策略窗口",
            "未完成", "早期版本已执行",
        }
        assert all(
            status in valid_statuses
            for status in execution_rows.locator("span:first-child").all_inner_texts()
        ), f"{broker} 执行状态包含未知文案"
        if broker == "eastmoney":
            for required in (
                "筛选价（Trend Animals）", "执行参考价（Futu 前复权）",
                "买入纪律", "卖出纪律",
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
            disciplines = workspace.locator(".trend-discipline")
            assert disciplines.count() == 2, "eastmoney 趋势报告纪律卡数量不是 2"
            assert workspace.locator(".trend-discipline summary").all_inner_texts() == [
                "买入纪律", "卖出纪律",
            ], "eastmoney 趋势报告纪律顺序不正确"
            viewport = getattr(page, "viewport_size", None)
            expected_open = (
                0 if viewport and viewport.get("width", 0) <= 760 else 2
            )
            assert workspace.locator(".trend-discipline[open]").count() == expected_open, (
                "eastmoney 趋势报告纪律默认展开状态不正确"
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
        _check_trend_audit(audit, report, broker)
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
        _check_trend_review(page, section, broker, review)


def _check_trend_controller_status(
    page: Any,
    workspace: Any,
    broker: str,
    controller: object,
) -> None:
    assert isinstance(controller, Mapping), f"API 缺少 {broker} 趋势控制器状态"
    card = workspace.locator(".trend-controller-status")
    assert card.count() == 1, f"{broker} 趋势报告缺少控制器状态卡"
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
        value = controller.get(key)
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
            advancement = rendered_time - baseline_time
            assert timedelta(0) <= advancement <= timedelta(minutes=5), (
                f"{broker} 控制器状态卡 {label} 与 API 时间范围不一致"
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
        assert health == "healthy" and controller.get("blocking") is False, (
            f"{broker} 控制器不可用或阻塞"
        )
        assert (
            controller.get("last_success") is not None
            or _controller_allows_missing_first_success(controller)
        ), f"{broker} 控制器尚无首次成功状态"
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


def _check_trend_review(
    page: Any, section: Any, broker: str, review: Mapping[str, Any]
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
    for required in (
        f"{market_label}趋势复盘",
        _plain(review.get("broker_label")),
        _plain(snapshot.get("strategy_name")),
        f"版本 {_plain(snapshot.get('strategy_version'))}",
        "当前策略参数",
        "收益与回撤",
        "风险调整收益",
        "纪律模拟",
        "实际执行",
        "市场基准",
    ):
        assert required in text, f"{broker} 趋势复盘缺少 {required}"
    parameters = snapshot.get("parameter_rows")
    assert isinstance(parameters, list) and parameters, f"{broker} 策略参数为空"
    parameter_rows = workspace.locator(
        ".trend-review-parameter-table > div"
    ).all_inner_texts()
    assert len(parameter_rows) == len(parameters), f"{broker} 策略参数没有完整展示"
    for rendered, row in zip(parameter_rows, parameters, strict=True):
        assert isinstance(row, Mapping), f"{broker} 策略参数格式无效"
        for key in ("group", "name", "value"):
            assert _plain(row.get(key)) in rendered, f"{broker} 策略参数缺少 {key}"
    assert workspace.locator(".trend-review-chart").count() == 2, (
        f"{broker} 趋势复盘图表数量不是 2"
    )
    assert workspace.locator(".trend-review-chart figcaption").all_inner_texts() == [
        "收益与回撤", "风险调整收益",
    ], f"{broker} 趋势复盘图表顺序不正确"
    metric_labels = workspace.locator(".trend-review-metric h3").all_inner_texts()
    assert metric_labels == [
        "期间净收益率", "相对市场超额收益", "最大回撤", "卡玛比率", "夏普比率",
    ], f"{broker} 趋势复盘指标不完整或顺序错误"
    for forbidden in (
        "复盘结论", "Connected", "创建回测", "导出参数", "Alpha", "Beta",
        "Sortino", "胜率", "盈亏比",
    ):
        assert forbidden not in text, f"{broker} 趋势复盘包含未要求内容 {forbidden}"
    if (getattr(page, "viewport_size", None) or {}).get("width", 0) <= 760:
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势复盘在 375px 产生横向滚动"
        _check_mobile_targets(
            page,
            "#return-to-portfolio:visible, "
            "#trend-report-workspace:visible button:visible",
        )
    close = workspace.locator("[data-close-trend-report]")
    assert close.count() == 1, f"{broker} 趋势复盘缺少返回按钮"
    close.click()
    assert page.locator("#trend-report-workspace:visible").count() == 0, (
        f"{broker} 返回后趋势复盘工作区仍可见"
    )
    assert trigger.evaluate("element => element === document.activeElement"), (
        f"{broker} 返回后焦点未恢复到复盘入口"
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
    header = page.locator("#last-refresh").inner_text().strip()
    assert "CST" in header, "Header 获取时间缺少 CST"
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
        "#refresh-quotes": {
            "backgroundColor": "rgb(139, 94, 52)",
            "borderTopColor": "rgb(139, 94, 52)",
        },
        ".current-view-card": {
            "backgroundColor": "rgb(36, 33, 29)",
            "borderTopColor": "rgb(36, 33, 29)",
        },
        "#last-refresh": {"color": "rgb(116, 110, 100)"},
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

    focus_target = page.locator("#refresh-quotes")
    focus_target.focus()
    focus = focus_target.evaluate(
        "element => { const styles = getComputedStyle(element); return {"
        "outlineColor: styles.outlineColor, outlineStyle: styles.outlineStyle, "
        "outlineWidth: styles.outlineWidth}; }"
    )
    assert focus == {
        "outlineColor": "rgb(139, 94, 52)",
        "outlineStyle": "solid", "outlineWidth": "3px",
    }, f"主操作焦点未使用 A 色板：{focus}"


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


def _browser_check(
    url: str,
    expected_cn: int,
    payload: dict[str, Any],
    reports_dir: Path | None = None,
    simulate_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    history_expectations: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> tuple[list[str], str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "Playwright 未安装"
    errors: list[str] = []
    screenshot_started_at_ns = _prepare_acceptance_screenshots()
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
                    page.on(
                        "console",
                        lambda message: browser_errors.append(message.text)
                        if message.type == "error"
                        and _is_actionable_console_error(message.text)
                        else None,
                    )
                    page.on("pageerror", lambda error: browser_errors.append(str(error)))
                    page.on("response", lambda response: browser_errors.append(
                        f"HTTP {response.status} {response.url}"
                    ) if response.status >= 400 else None)
                    page.goto(url, wait_until="networkidle")
                    _check_visual_contract(page)
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
                    if simulate_payloads is not None and history_expectations is not None:
                        try:
                            _check_trend_account_views(
                                page,
                                payload,
                                simulate_payloads,
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
    errors.extend(_validate_acceptance_screenshots(screenshot_started_at_ns))
    return errors, None


def _log_errors(
    path: Path,
    *,
    pid: int,
    expected_sha: str,
    expected_cwd: Path,
    process_started_at: datetime,
) -> list[str]:
    try:
        if not path.exists():
            return [f"日志不存在：{path}"]
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"日志读取失败：{type(exc).__name__}: {exc}"]
    prefix = "dashboard_runtime: "
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
    matching = [item for item in records if item[1].get("pid") == pid]
    if not matching:
        errors.append(f"日志没有候选 Dashboard PID：{pid}")
        fresh_text = text
    else:
        index, record = matching[-1]
        if index != 0:
            errors.append("Dashboard 日志不是候选进程的新日志文件")
        try:
            if path.stat().st_mtime < process_started_at.timestamp():
                errors.append("Dashboard 日志修改时间早于候选进程")
        except OSError as exc:
            errors.append(f"日志状态读取失败：{type(exc).__name__}: {exc}")
        if record.get("git_sha") != expected_sha:
            errors.append("日志中的 Dashboard Git SHA 不匹配")
        if Path(str(record.get("cwd") or "")).resolve() != expected_cwd.resolve():
            errors.append("日志中的 Dashboard 工作目录不匹配")
        if record.get("source_state") != "clean":
            errors.append("日志中的 Dashboard 源码状态不是 clean")
        try:
            recorded_start = datetime.fromisoformat(
                str(record.get("started_at") or "")
            )
            if recorded_start.tzinfo is None or recorded_start.utcoffset() is None:
                raise ValueError("timezone-aware timestamp required")
            if recorded_start < process_started_at:
                errors.append("日志中的 Dashboard 启动时间早于候选进程")
        except (TypeError, ValueError):
            errors.append("日志中的 Dashboard 启动时间无效")
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
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument(
        "--expected-eastmoney-cny", type=Decimal
    )
    parser.add_argument("--expected-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-sha")
    parser.add_argument("--log", type=Path, default=Path("/tmp/open_trader_dashboard_8766.log"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors: list[str] = []
    expected_cn = 0
    expected_sha = ""
    pid: int | None = None
    cwd = args.expected_root.resolve()
    process_started_at: datetime | None = None
    browser_payload: dict[str, Any] = {}
    reports_dir: Path | None = None
    simulate_payloads: dict[str, dict[str, Any]] = {}
    history_expectations: dict[str, list[dict[str, Any]]] = {}
    account_ids: dict[str, int] = {}
    external_blocker: str | None = None
    project_data_dir: Path | None = None
    try:
        project_data_dir = _project_data_dir(args.expected_root)
        expected_cn = _expected_cn_holdings(args.expected_root)
        phillips_total, phillips_period = _latest_phillips_expectation(
            project_data_dir
        )
        pid, cwd = _listener(args.url)
        if cwd != args.expected_root.resolve():
            errors.append(f"运行目录不匹配：{cwd}")
        running_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
        expected_sha = args.expected_sha or subprocess.check_output(
            ["git", "-C", str(args.expected_root), "rev-parse", "HEAD"], text=True
        ).strip()
        if running_sha != expected_sha:
            errors.append(f"运行 Git SHA 不匹配：{running_sha[:7]} != {expected_sha[:7]}")
        process_started_at = _process_started_at(pid)
        source_changes = _source_changes(cwd)
        if source_changes:
            errors.append(f"Dashboard 源码未提交：{'；'.join(source_changes)}")
        first = _fetch_payload(args.url)
        first_reports_dir = _effective_reports_dir(first, process_cwd=cwd)
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
        quotes = _fetch_quotes_payload(args.url)
        errors.extend(validate_quotes_payload(quotes))
        second = _fetch_payload(args.url)
        browser_payload = second
        reports_dir = _effective_reports_dir(second, process_cwd=cwd)
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
        pid = None
    browser_errors, blocker = _browser_check(
        args.url,
        expected_cn,
        browser_payload,
        reports_dir,
        simulate_payloads,
        history_expectations,
    )
    errors.extend(browser_errors)
    if pid is not None and process_started_at is not None:
        errors.extend(_log_errors(
            args.log,
            pid=pid,
            expected_sha=expected_sha,
            expected_cwd=cwd,
            process_started_at=process_started_at,
        ))
    status = classify_result(
        errors, browser_blocker=blocker, external_blocker=external_blocker
    )
    blockers = [item for item in (external_blocker, blocker) if item]
    result = {
        "status": status,
        "pid": pid,
        "errors": errors,
        "blocker": "；".join(blockers) or None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
