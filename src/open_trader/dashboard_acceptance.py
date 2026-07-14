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
        f'[data-detail-symbol="{symbol}"]'
    )
    assert button.count() == 1, f"{market}.{symbol} trading-decision button count is {button.count()}"
    button.click()
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


def _check_account_holdings(page: Any) -> None:
    text = page.locator("#account-holdings").inner_text()
    for required in (
        "富途", "中短线", "股票与期权", "老虎", "长线", "SMA200 组合策略",
        "辉立", "中线策略", "东方财富", "偏短线", "趋势交易",
        "策略指标待接入", "夏普比率", "卡玛比率", "目标", "漂移",
    ):
        assert required in text, f"账户持仓视图缺少 {required}"
    assert page.locator(".account-section").count() == 4, "账户区块数量不是 4"
    for forbidden in ("tiger-long-term-panel", "calibration_required", "provenance_incomplete"):
        assert forbidden not in text, f"账户持仓视图泄漏内部代码 {forbidden}"
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), "页面出现横向滚动"


def _browser_check(
    url: str, expected_cn: int, payload: dict[str, Any]
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
                        _check_decision_tabs(page, market, symbol)
                    except Exception as exc:
                        errors.append(f"{name}：{type(exc).__name__}: {exc}")
                    try:
                        _check_account_holdings(page)
                        page.locator('a[href="#account-tiger"]').click()
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
                    page.locator('[data-market="CN"]').first.click()
                    page.locator('button[data-broker="eastmoney"]').click()
                    page.wait_for_timeout(500)
                    if page.locator("#visible-count").inner_text().strip() != f"{expected_cn} 条":
                        errors.append(f"{name}：A 股东方财富筛选不是 {expected_cn} 条")
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
        args.url, args.expected_cn, browser_payload
    )
    errors.extend(browser_errors)
    status = classify_result(errors, browser_blocker=blocker)
    result = {"status": status, "pid": pid, "errors": errors, "blocker": blocker}
    print(json.dumps(result, ensure_ascii=False))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
