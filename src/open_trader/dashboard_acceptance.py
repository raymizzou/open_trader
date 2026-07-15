from __future__ import annotations

import argparse
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.request import urlopen

from .parsers.phillips import PhillipsStatementParser


REQUIRED_SOURCE_PATHS = (
    ("tradingagents_summary",),
    ("technical_facts",),
    ("decision_facts", "kline"),
    ("decision_facts", "news_sentiment"),
    ("futu_skill_facts", "news_sentiment"),
    ("futu_skill_facts", "technical_anomaly"),
    ("futu_skill_facts", "capital_anomaly"),
    ("futu_skill_facts", "derivatives_anomaly"),
)

TREND_REPORT_BROKERS = ("futu", "phillips", "eastmoney")
TREND_REPORT_DIRECTORIES = {
    "futu": "trend_us_futu",
    "phillips": "trend_hk_phillips",
    "eastmoney": "trend_a_share",
}
TREND_REASON_LABELS = {
    "protection_line_already_triggered": "活动保护线已触发",
    "danger_signal": "危险信号触发",
    "left_trend_right_side": "右侧趋势已结束",
    "holding_signal_unknown": "趋势信号不完整",
    "holding_kline_unavailable": "持仓日线数据不可用",
    "trend_intact": "趋势保持完好",
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

    for holding in holdings:
        if (holding.get("agent_report") or {}).get("available") is not True:
            continue
        for path in REQUIRED_SOURCE_PATHS:
            source: Any = holding
            for key in path:
                source = source.get(key) if isinstance(source, Mapping) else None
            if not isinstance(source, Mapping) or (
                source.get("available") is not True
                and source.get("unsupported") is not True
            ):
                detail = next(
                    (
                        str(source.get(key))
                        for key in ("error", "blocking_reason", "status")
                        if isinstance(source, Mapping) and source.get(key)
                    ),
                    "missing",
                )
                errors.append(
                    f"{holding.get('market', '')}.{holding.get('symbol', '')} "
                    f"数据源 {'.'.join(path)} 不可用：{detail}"
                )

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
    tiger_strategy = payload.get("tiger_long_term_strategy") or {}
    if tiger_strategy.get("status") != "shadow":
        errors.append("老虎长线策略不是 shadow 状态")
    if not tiger_strategy.get("members"):
        errors.append("老虎长线策略没有组合成员")
    tiger_gate = tiger_strategy.get("gate") or {}
    if "calibration_required" not in (tiger_gate.get("reasons") or []):
        errors.append("老虎长线策略缺少 calibration_required")
    if tiger_strategy.get("order_requests"):
        errors.append("老虎长线策略包含下单请求")
    return errors


def classify_result(errors: list[str], *, browser_blocker: str | None) -> str:
    if errors:
        return "FAIL"
    return "BLOCKED" if browser_blocker else "PASS"


def dashboard_signature(payload: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    fields = ("market", "symbol", "brokers")
    rows = [*(payload.get("holdings") or []), *(payload.get("cash_rows") or [])]
    return tuple(sorted(tuple(str(row.get(field, "")) for field in fields) for row in rows))


def _fetch_payload(url: str) -> dict[str, Any]:
    with urlopen(f"{url.rstrip('/')}/api/dashboard", timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Dashboard API HTTP {response.status}")
        return json.load(response)


def _listener(url: str) -> tuple[int, Path]:
    port = url.rsplit(":", 1)[-1].rstrip("/")
    pid_text = subprocess.check_output(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], text=True
    ).strip().splitlines()
    if len(pid_text) != 1:
        raise RuntimeError(f"端口 {port} 没有唯一监听进程")
    pid = int(pid_text[0])
    output = subprocess.check_output(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True
    )
    cwd_line = next((line for line in output.splitlines() if line.startswith("n")), "")
    if not cwd_line:
        raise RuntimeError("无法读取 Dashboard 进程工作目录")
    return pid, Path(cwd_line[1:]).resolve()


def _is_actionable_console_error(message: str) -> bool:
    # Chrome can emit an unattributed favicon 404 without exposing a response.
    # HTTP failures for actual page resources and APIs are checked separately.
    return not (
        message.startswith("Failed to load resource:")
        and "status of 404" in message
    )


def _first_in_scope_holding(payload: dict[str, Any]) -> tuple[str, str]:
    for holding in payload.get("holdings") or []:
        if (holding.get("agent_report") or {}).get("available") is True:
            return str(holding.get("market", "")), str(holding.get("symbol", ""))
    raise AssertionError("no advice-backed holding exists in Dashboard payload")


def _check_decision_tabs(page: Any, market: str, symbol: str) -> None:
    button = page.locator(
        'button[data-detail-mode="decision"]'
        f'[data-detail-market="{market}"]'
        f'[data-detail-symbol="{symbol}"]:visible'
    )
    assert button.count() >= 1, f"{market}.{symbol} has no trading-decision button"
    button.first.click()
    tabs = page.locator(".decision-tab-list [data-decision-tab]")
    expected_labels = ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]
    assert tabs.all_inner_texts() == expected_labels, "decision tabs are missing or out of order"
    assert page.locator(".decision-tab-list .decision-tab-failed").count() == 0, "decision tab failed"
    for index in range(tabs.count()):
        tab = tabs.nth(index)
        tab.click()
        panel_id = tab.get_attribute("aria-controls")
        assert panel_id, f"tab {expected_labels[index]} has no controlled panel"
        panel = page.locator(f"#{panel_id}:visible")
        assert panel.count() == 1, f"tab {expected_labels[index]} has {panel.count()} visible panels"
        panel_text = panel.inner_text()
        assert "数据未生成" not in panel_text, f"tab {expected_labels[index]} contains 数据未生成"
        if index == 0:
            assert "夏普比率" in panel_text, "最终决策缺少夏普比率"
            assert "卡玛比率" in panel_text, "最终决策缺少卡玛比率"
        if index == 2:
            assert not re.search(r"当前价\s*缺失", panel_text), "趋势 / K 线当前价缺失"


def _plain(value: Any) -> str:
    return "-" if value is None or str(value).strip() == "" else str(value)


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
    expected_market = {"futu": "US", "phillips": "HK", "eastmoney": "CN"}[broker]
    assert (
        payload.get("execution_date") == report.get("report_date")
        and payload.get("as_of_date") == report.get("data_date")
        and payload.get("generated_at") == report.get("generated_at")
        and metadata.get("market") == expected_market
        and metadata.get("broker") == broker
    ), f"{broker} 冻结报告身份与 API 投影不一致"
    judgments = payload.get("strategy_judgments")
    assert isinstance(judgments, Mapping), f"{broker} 冻结报告缺少策略判断"
    formal = judgments.get("formal_actions")
    holdings = judgments.get("holding_decisions")
    account = payload.get("account")
    buy_allowed = isinstance(account, Mapping) and account.get("fresh") is True
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
        and buy_allowed
        and not _trend_action_needs_review(item)
    ]
    holds = [
        item for item in holdings
        if item.get("action") == "HOLD" and not _trend_action_needs_review(item)
    ]
    reviews: list[Mapping[str, Any]] = []
    for item in [*formal, *holdings]:
        if (
            _trend_action_needs_review(item)
            or not buy_allowed and item.get("action") == "BUY"
        ) and item not in reviews:
            reviews.append(item)
    expected_actions = {
        "sell_actions": sells,
        "buy_actions": buys,
        "hold_actions": holds,
        "review_actions": reviews,
    }
    assert all(
        report.get(key) == value for key, value in expected_actions.items()
    ), f"{broker} 冻结报告动作与 API 投影不一致"
    assert report.get("counts") == {
        "sell": len(sells),
        "buy": len(buys),
        "hold": len(holds),
        "review": len(reviews),
    }, f"{broker} 冻结报告计数与 API 投影不一致"
    assert audit.get("candidates") == judgments.get("top10_candidates", []), (
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


def _check_trend_stage(
    text: str, items: Any, *, kind: str, broker: str,
) -> None:
    rows = items if isinstance(items, list) else []
    if not rows:
        assert "无" in text, f"{broker} 的 {kind} 空阶段未显示 无"
        return
    for item in rows:
        assert isinstance(item, Mapping), f"{broker} 的 {kind} 动作格式无效"
        for key in ("symbol", "name"):
            if item.get(key):
                assert str(item[key]) in text, f"{broker} 的 {kind} 动作缺少 {key}"
        if kind == "buy":
            for label, key in (
                ("约", "estimated_shares"),
                ("金额上限", "target_amount"),
                ("预计保护线", "estimated_initial_line"),
            ):
                assert f"{label} {_plain(item.get(key))}" in text, (
                    f"{broker} 的买入动作缺少 {label}"
                )
            continue
        reason = TREND_REASON_LABELS.get(
            str(item.get("reason", "")), "未知动作或原因，需人工确认"
        )
        assert reason in text, f"{broker} 的 {kind} 动作缺少原因 {reason}"
        if item.get("active_line") not in (None, ""):
            assert f"活动保护线 {_plain(item['active_line'])}" in text, (
                f"{broker} 的 {kind} 动作缺少活动保护线"
            )


def _check_trend_audit(audit: Any, report: Mapping[str, Any], broker: str) -> None:
    assert audit.count() == 1 and audit.get_attribute("open") is None, (
        f"{broker} 趋势报告审计详情未保持收起"
    )
    summary = audit.locator("summary")
    assert summary.count() == 1, f"{broker} 趋势报告缺少审计摘要"
    summary.click()
    sections = audit.locator("section").all_inner_texts()
    assert len(sections) == 3, f"{broker} 趋势报告审计区块数量不是 3"
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
        assert f"强度 {_plain(item.get('strength'))}" in sections[0], (
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
    industries = (
        data.get("industry_concentration")
        if isinstance(data.get("industry_concentration"), list) else []
    )
    if not industries:
        assert "无" in sections[2], f"{broker} 空行业集中度未显示 无"
    for row in industries:
        for value in row if isinstance(row, list) else []:
            assert _plain(value) in sections[2], f"{broker} 行业集中度缺少 {value}"
    audit_text = audit.inner_text()
    sources = data.get("data_sources") if isinstance(data.get("data_sources"), list) else []
    for source in sources:
        assert str(source) in audit_text, f"{broker} 审计详情缺少数据来源 {source}"
    cost = data.get("actual_api_cost")
    if cost is None:
        cost = data.get("estimated_api_cost")
    if cost is None:
        cost = "未知"
    assert f"API 成本：{_plain(cost)}" in audit_text, f"{broker} 审计详情缺少 API 成本"


def _check_account_holdings(
    page: Any, payload: dict[str, Any], *, reports_dir: Path | None = None
) -> None:
    text = page.locator("#account-holdings").inner_text()
    for required in (
        "富途", "短线", "美股趋势交易", "老虎", "长线", "SMA200 组合策略",
        "辉立", "港股趋势交易", "东方财富", "偏短线", "趋势交易",
        "当天趋势报告", "报告日期", "数据截至", "夏普比率", "卡玛比率",
    ):
        assert required in text, f"账户持仓视图缺少 {required}"
    for legacy in ("数据日", "账户源", "最近保护提醒", "策略指标待接入"):
        assert legacy not in text, f"账户持仓视图仍包含旧趋势摘要 {legacy}"
    assert page.locator(".account-section").count() == 4, "账户区块数量不是 4"
    assert page.locator(".trend-report-entry").count() == 3, "趋势报告入口数量不是 3"
    assert page.locator("#account-tiger .trend-report-entry").count() == 0, (
        "老虎账户不应包含趋势报告入口"
    )
    for forbidden in ("tiger-long-term-panel", "calibration_required", "provenance_incomplete"):
        assert forbidden not in text, f"账户持仓视图泄漏内部代码 {forbidden}"
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), "页面出现横向滚动"

    reports = payload.get("trend_reports") or {}
    expected_available = [
        broker for broker in TREND_REPORT_BROKERS
        if isinstance(reports.get(broker), Mapping)
        and reports[broker].get("available") is True
    ]
    assert page.locator(".trend-report-entry [data-trend-report]").count() == len(
        expected_available
    ), "可用趋势报告入口数量与 API 不一致"
    for broker in TREND_REPORT_BROKERS:
        report = reports.get(broker) if isinstance(reports, Mapping) else None
        assert isinstance(report, Mapping), f"API 缺少 {broker} 趋势报告状态"
        entry = page.locator(f"#account-{broker} .trend-report-entry")
        assert entry.count() == 1, f"{broker} 趋势报告入口数量不是 1"
        trigger = entry.locator("[data-trend-report]")
        if report.get("available") is not True:
            assert trigger.count() == 0, f"{broker} 不可用报告仍可打开"
            button = entry.locator("button")
            assert button.count() == 1 and button.is_disabled(), (
                f"{broker} 不可用报告入口未禁用"
            )
            assert page.locator("#trend-report-workspace:visible").count() == 0, (
                f"{broker} 不可用报告错误打开工作区"
            )
            continue
        assert trigger.count() == 1, f"{broker} 可用报告缺少入口"
        if reports_dir is not None:
            _check_trend_artifact_projection(reports_dir, broker, report)
        entry_text = entry.inner_text()
        for label, key in (("报告日期", "report_date"), ("数据截至", "data_date")):
            assert f"{label} {_plain(report.get(key))}" in entry_text, (
                f"{broker} 入口缺少 {label}"
            )
        trigger.click()
        workspace = page.locator("#trend-report-workspace:visible")
        assert workspace.count() == 1, f"{broker} 趋势报告工作区未显示"
        close = workspace.locator("[data-close-trend-report]")
        assert close.count() == 1, f"{broker} 趋势报告工作区缺少返回按钮"
        assert close.evaluate("element => element === document.activeElement"), (
            f"{broker} 趋势报告打开后焦点未进入工作区"
        )
        workspace_text = workspace.inner_text()
        identity = f"{_plain(report.get('broker_label'))}｜{_plain(report.get('market_label'))}"
        assert identity in workspace_text, f"{broker} 趋势报告身份不匹配"
        for required in (
            "报告日期", "数据截至", "生成时间", "账户状态", "今日执行检查",
            "确认全部卖出动作", "按顺序考虑允许买入项", "盘中观察活动保护线",
            "完成人工复核",
        ):
            assert required in workspace_text, f"{broker} 趋势报告工作区缺少 {required}"
        header_values = workspace.locator(".trend-report-header dd").all_inner_texts()
        assert header_values == [
            _plain(report.get(key)) for key in (
                "report_date", "data_date", "generated_at", "account_status",
            )
        ], f"{broker} 趋势报告头部内容与 API 不一致"
        counts = report.get("counts") if isinstance(report.get("counts"), Mapping) else {}
        for label, key in (("卖出", "sell"), ("买入", "buy"), ("持有", "hold"), ("人工复核", "review")):
            assert f"{label} {_plain(counts.get(key) or 0)}" in workspace_text, (
                f"{broker} 趋势报告缺少 {label}计数"
            )
        stage_texts = workspace.locator(".trend-stage").all_inner_texts()
        expected_titles = [
            "开盘前", _plain(report.get("buy_window")), "盘中持续", "人工复核",
        ]
        assert len(stage_texts) == 4 and all(
            title in stage_texts[index] for index, title in enumerate(expected_titles)
        ), f"{broker} 趋势报告阶段顺序不正确"
        for stage_text, key, kind in zip(
            stage_texts,
            ("sell_actions", "buy_actions", "hold_actions", "review_actions"),
            ("sell", "buy", "hold", "review"),
            strict=True,
        ):
            _check_trend_stage(stage_text, report.get(key), kind=kind, broker=broker)
        audit = workspace.locator(".trend-audit")
        _check_trend_audit(audit, report, broker)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势报告工作区出现横向滚动"
        close.click()
        assert page.locator("#trend-report-workspace:visible").count() == 0, (
            f"{broker} 返回后趋势报告工作区仍可见"
        )
        assert page.locator(".workspace-grid:visible").count() == 1, (
            f"{broker} 返回后持仓工作区未恢复"
        )
        assert trigger.evaluate("element => element === document.activeElement"), (
            f"{broker} 返回后焦点未恢复到报告入口"
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


def _check_tiger_anchor(page: Any) -> None:
    page.locator('a[href="#account-tiger"]').click()
    assert page.evaluate("window.location.hash") == "#account-tiger", (
        "点击老虎账户锚点后 location.hash 不正确"
    )
    assert page.locator("#account-tiger").evaluate(
        """element => {
          const rect = element.getBoundingClientRect();
          return rect.bottom > 0 && rect.top < window.innerHeight;
        }"""
    ), "点击老虎账户锚点后目标不在 viewport 内"


def _check_cn_filter(page: Any, expected_cn: int) -> None:
    page.locator('[data-market="CN"]').first.click()
    page.wait_for_timeout(500)
    assert page.locator("#visible-count").inner_text().strip() == f"{expected_cn} 条", (
        f"A 股筛选不是 {expected_cn} 条"
    )
    sections = page.locator(".account-section:visible")
    assert sections.count() == 4, "A 股筛选后账户区块数量不是 4"
    for index in range(sections.count()):
        section = sections.nth(index)
        rows = section.locator(".account-holding-row:visible")
        empty = section.locator(".account-empty:visible")
        if rows.count() == 0:
            assert empty.count() == 1 and empty.inner_text().strip() == "当前筛选下没有持仓", (
                f"A 股筛选后第 {index + 1} 个无持仓账户缺少中文空状态"
            )
            continue
        assert empty.count() == 0, f"A 股筛选后第 {index + 1} 个账户错误显示空状态"
        markets = section.locator(
            ".account-holding-row:visible td:nth-child(2)"
        ).all_inner_texts()
        assert len(markets) == rows.count(), f"A 股筛选后第 {index + 1} 个账户市场列缺失"
        assert all(
            re.sub(r"\s+", " ", market).strip() in {"CN", "市场 CN"}
            for market in markets
        ), f"A 股筛选后第 {index + 1} 个账户包含非 CN 持仓"


def _browser_check(
    url: str,
    expected_cn: int,
    payload: dict[str, Any],
    reports_dir: Path | None = None,
) -> tuple[list[str], str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "Playwright 未安装"
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                market, symbol = _first_in_scope_holding(payload)
            except AssertionError as exc:
                browser.close()
                return [str(exc)], None
            for name, viewport in (
                ("desktop", {"width": 1440, "height": 1000}),
                ("mobile", {"width": 390, "height": 844}),
            ):
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
                    if "看板数据加载失败" in page.locator("body").inner_text():
                        errors.append(f"{name}：页面显示看板数据加载失败")
                    try:
                        _check_page_safety(page)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_decision_tabs(page, market, symbol)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_account_holdings(
                            page, payload, reports_dir=reports_dir
                        )
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_tiger_anchor(page)
                        assert page.locator("#account-tiger:visible").count() == 1, (
                            "点击老虎账户锚点后账户区块不可见"
                        )
                        assert page.locator(".account-section").count() == 4, (
                            "点击老虎账户锚点后账户区块数量不是 4"
                        )
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


def _log_errors(path: Path) -> list[str]:
    if not path.exists():
        return [f"日志不存在：{path}"]
    text = path.read_text(encoding="utf-8", errors="replace")
    markers = ("Traceback (most recent call last)", "看板数据加载失败")
    return [f"日志包含错误标记：{marker}" for marker in markers if marker in text]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--expected-cn", type=int, default=5)
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument(
        "--expected-eastmoney-cny", type=Decimal
    )
    parser.add_argument("--expected-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-sha")
    parser.add_argument("--log", type=Path, default=Path("/tmp/open_trader_dashboard_8766.log"))
    parser.add_argument("--wait-seconds", type=float, default=125)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors: list[str] = []
    browser_payload: dict[str, Any] = {}
    try:
        phillips_total, phillips_period = _latest_phillips_expectation(
            _project_data_dir(args.expected_root)
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
        first = _fetch_payload(args.url)
        errors.extend(validate_dashboard_payload(
            first, expected_cn=args.expected_cn,
            expected_eastmoney_cny=args.expected_eastmoney_cny,
            expected_rows=args.expected_rows,
            expected_phillips_total=phillips_total,
            expected_phillips_period=phillips_period,
        ))
        if not errors and args.wait_seconds:
            time.sleep(args.wait_seconds)
        second = _fetch_payload(args.url)
        browser_payload = second
        errors.extend(validate_dashboard_payload(
            second, expected_cn=args.expected_cn,
            expected_eastmoney_cny=args.expected_eastmoney_cny,
            expected_rows=args.expected_rows,
            expected_phillips_total=phillips_total,
            expected_phillips_period=phillips_period,
        ))
        if dashboard_signature(first) != dashboard_signature(second):
            errors.append("两个刷新周期后的 Dashboard 数据不稳定")
        errors.extend(_log_errors(args.log))
    except Exception as exc:
        errors.append(f"运行检查失败：{type(exc).__name__}: {exc}")
        pid = None
    browser_errors, blocker = _browser_check(
        args.url,
        args.expected_cn,
        browser_payload,
        args.expected_root / "reports",
    )
    errors.extend(browser_errors)
    status = classify_result(errors, browser_blocker=blocker)
    result = {"status": status, "pid": pid, "errors": errors, "blocker": blocker}
    print(json.dumps(result, ensure_ascii=False))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
