from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from open_trader.dashboard_quotes import QuoteRefreshResult
from open_trader.dashboard_web import STATIC_DIR
from open_trader.portfolio import PORTFOLIO_FIELDNAMES

from tests.test_dashboard import dashboard_config, portfolio_rows, write_csv


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
    )


def read_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        content_length = response.headers["Content-Length"]
        payload = response.read()
        assert content_length == str(len(payload))
        return json.loads(payload.decode("utf-8"))


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


def test_dashboard_static_assets_include_local_shell() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert "Open Trader" in html
    assert "持仓实时看板" in html
    assert "刷新行情" in html
    assert "全部市场" in html
    assert "symbol-detail-panel" in html
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
    assert ".analyst-dialogue" in css
    assert ".final-conclusion-list" in css
    assert ".broker-detail-section" in css
    assert "summary-holding-bar" in html
    assert "summary-holding-value" in html
    assert "summary-holding-weight" in html
    assert "summary-cash-note" in html
    assert "holding_value_hkd" in js
    assert "cash_like_value_hkd" in js
    assert "percentBarWidth" in js
    assert ".allocation-bar" in css
    assert ".summary-allocation-row" in css
    assert "隐藏英文原文" in js
    assert 'firstValue(strategy, ["plan_text_zh", "rationale_zh"])' not in js
    assert "暂无中文策略译文" not in js
    assert "重新分析" in js
    assert "未启用" in js
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
    assert ".workspace-grid.detail-mode,\n  .right-rail {" in mobile_css
    assert ".compact-kv div {\n    display: grid;\n    gap: 3px;\n  }" in mobile_css
    assert ".compact-kv dd {\n    text-align: left;\n  }" in mobile_css


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
    summary_zh: "评级低配。趋势派认为 MACD 背离。风控派建议锁定部分收益。组合结论是减仓而非清仓。",
    raw_decision: "The bull case remains possible, but risk is elevated.",
  },
  premarket_action: {
    available: true,
    suggested_action: "reduce",
    watch_trigger_zh: "跌回 50 日均线需要复核。",
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
const metricCells = decisionMetricCells(holding);
const metricMap = Object.fromEntries(metricCells);
const metricLabels = metricCells.map((cell) => cell[0]).join(",");
const metricText = metricCells.flat().join(",");
if (!metricLabels.includes("观点") || !metricLabels.includes("下次复评")) {
  throw new Error("metrics missing required labels: " + metricText);
}
if (!String(metricMap["目标价"] || "").includes("51") || !String(metricMap["目标价"] || "").includes("53")) {
  throw new Error("target metric should include target prices: " + JSON.stringify(metricMap));
}
if (!String(metricMap["下次复评"] || "").includes("财报") && !String(metricMap["下次复评"] || "").includes("复评")) {
  throw new Error("review metric should include review timing: " + JSON.stringify(metricMap));
}
const conclusionItems = finalConclusionItems(holding);
const conclusionLabels = conclusionItems.map((item) => item.label).join(",");
const conclusionText = JSON.stringify(conclusionItems);
if (!conclusionLabels.includes("结论") || !conclusionLabels.includes("失败条件")) {
  throw new Error("conclusion missing required labels: " + conclusionText);
}
if (!conclusionText.includes("低配") && !conclusionText.includes("减仓")) {
  throw new Error("conclusion missing decision text: " + conclusionText);
}
if (!conclusionText.includes("60")) {
  throw new Error("conclusion missing failure condition: " + conclusionText);
}
const html = renderAnalysisStrategySection(holding);
for (const required of ["分析与交易策略", "当前希望你做什么", "操作指令", "今天重点关注", "分析师对话", "最终结论", "查看英文原文"]) {
  if (!html.includes(required)) {
    throw new Error("missing rendered label " + required + " in " + html);
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
const noActionHolding = {
  market: "US",
  symbol: "CASH",
  strategy: { available: false },
  agent_report: { available: false },
  trade_action: { available: false },
  premarket_action: { available: false },
};
const noAction = currentDecisionAction(noActionHolding) || {};
if (noAction.action || noAction.suggested_action || noAction.limit_price || noAction.suggested_quantity || noAction.suggested_notional) {
  throw new Error("no-action holding should not expose actionable fields: " + JSON.stringify(noAction));
}
const noActionDesired = desiredActionText(noActionHolding);
if (!noActionDesired.includes("今天暂无触发中的交易动作")) {
  throw new Error("no-action desired text should be explicit: " + noActionDesired);
}
const noActionHtml = renderAnalysisStrategySection(noActionHolding);
if (!noActionHtml.includes("今天暂无触发中的交易动作")) {
  throw new Error("missing explicit no-action state: " + noActionHtml);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


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

    payload = build_quotes_payload(service)

    json.dumps(payload)
    assert service.refresh_count == 1
    assert payload["status"] == "ok"
    assert list(payload["quotes"]) == ["US.MSFT"]
    assert payload["quotes"]["US.MSFT"]["last_price"] == "500"


def test_dashboard_server_serves_dashboard_and_quotes_api(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    quote_service = FakeQuoteService(quote_result())
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=quote_service,
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
    assert quote_service.refresh_count == 1


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

    def raise_runtime_error(config) -> dict[str, Any]:
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
