from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime
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
SESSION_LABELS = ("夜盘", "盘前", "盘中", "盘后")
SESSION_KEYS = {"overnight", "pre_market", "regular", "after_hours"}

ACCOUNT_BROKERS = ("futu", "tiger", "phillips", "eastmoney")
TREND_REPORT_BROKERS = ("futu", "phillips", "eastmoney")
TREND_REPORT_DIRECTORIES = {
    "futu": "trend_us_futu",
    "phillips": "trend_hk_phillips",
    "eastmoney": "trend_a_share",
}
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
ACCEPTANCE_SCREENSHOT_NAMES = (
    "wide_desktop-portfolio.png",
    "1920-trend-report.png",
    "desktop-portfolio.png",
    "1440-trend-report.png",
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


def validate_quote_refresh_cycle(
    first: dict[str, Any], second: dict[str, Any],
) -> list[str]:
    try:
        first_at = datetime.fromisoformat(str(first.get("fetched_at", "")))
        second_at = datetime.fromisoformat(str(second.get("fetched_at", "")))
        if first_at.utcoffset() is None or second_at.utcoffset() is None:
            raise ValueError("timestamp has no timezone")
        if second_at <= first_at:
            return ["第二次行情 API 获取时间没有更新"]
    except (TypeError, ValueError):
        return ["行情 API 获取时间格式无效"]
    return []


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


def _fetch_quotes_payload(url: str) -> dict[str, Any]:
    with urlopen(f"{url.rstrip('/')}/api/quotes", timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Quotes API HTTP {response.status}")
        return json.load(response)


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


def _first_in_scope_holding(payload: dict[str, Any]) -> tuple[str, str, str]:
    for holding in payload.get("holdings") or []:
        if (holding.get("agent_report") or {}).get("available") is True:
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
            assert broker, "advice-backed holding has no account broker"
            return str(holding.get("market", "")), str(holding.get("symbol", "")), broker
    raise AssertionError("no advice-backed holding exists in Dashboard payload")


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
            ".account-holding-actions button:visible",
        )
        _check_mobile_targets(
            page,
            ".symbol-detail-panel.inline-symbol-detail:visible .decision-tab:visible, "
            ".symbol-detail-panel.inline-symbol-detail:visible [data-back-to-holdings]:visible",
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
    assert page.locator(".research-chat-modal:visible").count() == 1, (
        "投研讨论弹窗未显示"
    )
    if mobile:
        _check_mobile_targets(
            page, ".research-chat-modal button:visible, .research-chat-modal input:visible"
        )
    page.locator("#research-chat-close:visible").click()
    assert page.locator(".research-chat-modal:visible").count() == 0, (
        "投研讨论弹窗关闭失败"
    )


def _check_decision_tabs(page: Any, market: str, symbol: str, broker: str) -> None:
    _select_account_tab(page, broker)
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


def _display_number(value: Any) -> str:
    raw = _plain(value).strip()
    match = re.fullmatch(r"([+-]?)(\d+)(\.\d+)?", raw)
    if match is None:
        return raw
    sign, integer, fraction = match.groups()
    grouped = re.sub(r"\B(?=(\d{3})+(?!\d))", ",", integer)
    return f"{sign}{grouped}{fraction or ''}"


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
    expected_market = {"futu": "US", "phillips": "HK", "eastmoney": "CN"}[broker]
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
        report.get(key) == value for key, value in expected_actions.items()
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
                assert f"{label} {_display_number(item.get(key))}" in text, (
                    f"{broker} 的买入动作缺少 {label}"
                )
            continue
        reason = TREND_REASON_LABELS.get(
            str(item.get("reason", "")), "未知动作或原因，需人工确认"
        )
        assert reason in text, f"{broker} 的 {kind} 动作缺少原因 {reason}"
        if item.get("active_line") not in (None, ""):
            assert f"活动保护线 {_display_number(item['active_line'])}" in text, (
                f"{broker} 的 {kind} 动作缺少活动保护线"
            )


def _check_cn_trend_stages(
    stage_texts: list[str], report: Mapping[str, Any]
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
    assert len(stage_texts) == len(expected), "eastmoney 趋势报告阶段数量不正确"
    for text, (title, key, action) in zip(stage_texts, expected, strict=True):
        assert title in text, f"eastmoney 趋势报告缺少阶段 {title}"
        rows = report.get(key) if isinstance(report.get(key), list) else []
        if not rows:
            assert "无" in text, f"eastmoney 的 {title} 空阶段未显示 无"
            continue
        for item in rows:
            assert isinstance(item, Mapping), f"eastmoney 的 {title} 动作格式无效"
            assert action in text, f"eastmoney 的 {title} 缺少动作 {action}"
            for value in (item.get("symbol"), item.get("name")):
                if value:
                    assert str(value) in text, f"eastmoney 的 {title} 缺少 {value}"
            if key == "buy_actions":
                weight = Decimal(str(item.get("target_weight", "NaN"))) * 100
                facts = (
                    item.get("filter_price"), item.get("close"),
                    f"{_plain(item.get('temperature_prev'))} → {_plain(item.get('temperature_curr'))}",
                    item.get("phase"), item.get("strength"), item.get("industry"),
                    item.get("industry_temperature"), item.get("market_cap"),
                    item.get("amount"), f"{format(weight.normalize(), 'f')}%",
                    item.get("target_amount"), f"{_plain(item.get('estimated_shares'))} 股",
                    item.get("estimated_initial_line"),
                )
            else:
                facts = (
                    item.get("close"),
                    f"{_plain(item.get('temperature_prev'))} → {_plain(item.get('temperature_curr'))}",
                    item.get("strength"),
                    TREND_REASON_LABELS.get(
                        str(item.get("reason", "")), "未知动作或原因，需人工确认"
                    ),
                    item.get("active_line"),
                    *(
                        item.get("entry_hints")
                        if isinstance(item.get("entry_hints"), list)
                        else ["数据不可用"]
                    ),
                )
            for fact in facts:
                assert _plain(fact) in text, (
                    f"eastmoney 的 {title} 缺少事实 {_plain(fact)}"
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
    industries = (
        data.get("industry_concentration")
        if isinstance(data.get("industry_concentration"), list) else []
    )
    if not industries:
        assert "无" in sections[2], f"{broker} 空行业集中度未显示 无"
    for row in industries:
        for index, value in enumerate(row if isinstance(row, list) else []):
            expected = _plain(value) if index == 0 else _display_number(value)
            assert expected in sections[2], f"{broker} 行业集中度缺少 {value}"
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
    profiles = {
        "futu": ("富途", "短线", "美股趋势交易"),
        "tiger": ("老虎", "长线", "SMA200 组合策略"),
        "phillips": ("辉立", "短线", "港股趋势交易"),
        "eastmoney": ("东方财富", "偏短线", "趋势交易"),
    }
    for broker in ACCOUNT_BROKERS:
        section = _select_account_tab(page, broker)
        text = section.inner_text()
        for required in (*profiles[broker], "持仓资产", "现金", "持仓", "来源", "时间"):
            assert required in text, f"{broker} 账户区块缺少 {required}"
        if broker == "tiger":
            for required in (
                "SMA200 策略", "影子验证", "年化收益", "最大回撤", "夏普比率", "卡玛比率",
            ):
                assert required in text, f"老虎策略摘要缺少 {required}"
        for legacy in ("数据日", "账户源", "最近保护提醒", "策略指标待接入"):
            assert legacy not in text, f"账户持仓视图仍包含旧趋势摘要 {legacy}"
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
        if broker == "tiger":
            assert section.locator(".trend-report-entry").count() == 0, (
                "老虎账户不应包含趋势报告入口"
            )
            continue
        assert "当天趋势报告" in text, f"{broker} 账户区块缺少 当天趋势报告"
        report = reports.get(broker) if isinstance(reports, Mapping) else None
        assert isinstance(report, Mapping), f"API 缺少 {broker} 趋势报告状态"
        entry = section.locator(".trend-report-entry")
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
            if broker == "eastmoney" and screenshot_dir is not None:
                raise AssertionError("eastmoney 趋势报告不可用，无法生成验收截图")
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
        if broker != "eastmoney":
            for required in (
                "今日执行检查", "确认全部卖出动作", "按顺序考虑允许买入项",
                "盘中观察活动保护线", "完成人工复核",
            ):
                assert required in workspace_text, (
                    f"{broker} 趋势报告工作区缺少 {required}"
                )
        header_values = workspace.locator(".trend-report-header dd").all_inner_texts()
        assert header_values == [
            _plain(report.get(key)) for key in (
                "report_date", "data_date", "generated_at", "account_status",
            )
        ], f"{broker} 趋势报告头部内容与 API 不一致"
        counts = report.get("counts") if isinstance(report.get("counts"), Mapping) else {}
        count_labels = (
            (("全部卖出", "sell"), ("正式买入", "buy"), ("继续持有", "hold"), ("人工复核", "review"))
            if broker == "eastmoney"
            else (("卖出", "sell"), ("买入", "buy"), ("持有", "hold"), ("人工复核", "review"))
        )
        for label, key in count_labels:
            assert f"{label} {_display_number(counts.get(key) or 0)}" in workspace_text, (
                f"{broker} 趋势报告缺少 {label}计数"
            )
        if broker == "eastmoney":
            for required in (
                "优先处理 · 卖出触发", "09:30–10:00 · 正式买入计划",
                "需要确认 · 人工复核", "盘中持续 · 已有持仓", "筛选价（Trend Animals）",
                "执行参考价（Futu 前复权）", "买入纪律", "卖出纪律",
                "全部卖出", "正式买入", "继续持有",
            ):
                assert required in workspace_text, (
                    f"eastmoney 趋势报告工作区缺少 {required}"
                )
            assert workspace.locator(".cn-trend-report").count() == 1, (
                "eastmoney 趋势报告未使用 A 股动作优先结构"
            )
            stage_texts = workspace.locator(".cn-trend-stage").all_inner_texts()
            _check_cn_trend_stages(stage_texts, report)
            _check_cn_buy_rows(workspace, report)
            assert workspace.locator(".cn-trend-table").count() == 4, (
                "eastmoney 趋势报告动作表数量与 API 不一致"
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
            if viewport and viewport.get("width", 0) <= 760:
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                ), "A 股趋势报告在 375px 产生横向滚动"
                cards = workspace.locator(".cn-trend-card:visible")
                assert all(
                    box is not None and box["x"] + box["width"] <= 376
                    for box in cards.evaluate_all(
                        "nodes => nodes.map(node => node.getBoundingClientRect()).map(r => ({x:r.x,width:r.width}))"
                    )
                ), "A 股趋势报告动作卡超出 375px 视口"
        else:
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
                _check_trend_stage(
                    stage_text, report.get(key), kind=kind, broker=broker
                )
        audit = workspace.locator(".trend-audit")
        _check_trend_audit(audit, report, broker)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"{broker} 趋势报告工作区出现横向滚动"
        return_control = (
            workspace.locator("[data-close-trend-report]")
            if broker == "eastmoney"
            else close
        )
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

    if broker != "eastmoney":
        return
    buy_stage = workspace.locator(".cn-trend-buy")
    assert buy_stage.count() == 1, "A 股趋势报告缺少正式买入区"
    expected_buy_count = 1 if expected_buy_count is None else expected_buy_count
    cards = buy_stage.locator(".cn-trend-card:visible")
    if width <= 760:
        assert buy_stage.get_attribute("tabindex") == "-1", (
            "A 股正式买入区在手机端产生多余 Tab 停靠点"
        )
        assert buy_stage.get_attribute("aria-label") == "正式买入计划", (
            "A 股正式买入区手机端标签不正确"
        )
        assert cards.count() == expected_buy_count, (
            "A 股趋势报告手机端买入卡数量与 API 不一致"
        )
        if expected_buy_count == 0:
            assert "无" in buy_stage.inner_text(), "A 股零买入报告未显示 无"
        return
    assert buy_stage.get_attribute("tabindex") == "0", (
        "A 股正式买入滚动区不可通过键盘聚焦"
    )
    assert buy_stage.get_attribute("aria-label") == "正式买入计划，可横向滚动", (
        "A 股正式买入滚动区缺少无障碍标签"
    )
    buy_stage.focus()
    assert buy_stage.evaluate("element => element === document.activeElement"), (
        "A 股正式买入滚动区无法获得焦点"
    )
    focus = buy_stage.evaluate(
        "element => { const styles = getComputedStyle(element); return {"
        "outlineColor: styles.outlineColor, outlineStyle: styles.outlineStyle, "
        "outlineWidth: styles.outlineWidth}; }"
    )
    assert focus == {
        "outlineColor": "rgb(139, 94, 52)",
        "outlineStyle": "solid", "outlineWidth": "3px",
    }, f"A 股正式买入滚动区焦点样式不正确：{focus}"
    if expected_buy_count == 0:
        return
    overflow = buy_stage.evaluate(
        "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, "
        "overflowX: getComputedStyle(element).overflowX})"
    )
    assert overflow["overflowX"] == "auto", "A 股正式买入区未启用内部横向滚动"
    assert overflow["scrollWidth"] > overflow["clientWidth"], (
        "A 股正式买入宽表没有可滚动内容"
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
            for name, viewport in (
                ("wide_desktop", {"width": 1920, "height": 1080}),
                ("desktop", {"width": 1440, "height": 1000}),
                ("mobile", {"width": 375, "height": 844}),
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
                        _check_decision_tabs(page, market, symbol, decision_broker)
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
    reports_dir: Path | None = None
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
        first_quotes = _fetch_quotes_payload(args.url)
        errors.extend(validate_quotes_payload(first_quotes))
        first_reports_dir = _effective_reports_dir(first, process_cwd=cwd)
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
        second_quotes = _fetch_quotes_payload(args.url)
        errors.extend(validate_quotes_payload(second_quotes))
        errors.extend(validate_quote_refresh_cycle(first_quotes, second_quotes))
        browser_payload = second
        reports_dir = _effective_reports_dir(second, process_cwd=cwd)
        if first_reports_dir != reports_dir:
            errors.append("两个刷新周期的 Dashboard reports_dir 不一致")
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
        reports_dir,
    )
    errors.extend(browser_errors)
    status = classify_result(errors, browser_blocker=blocker)
    result = {"status": status, "pid": pid, "errors": errors, "blocker": blocker}
    print(json.dumps(result, ensure_ascii=False))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
