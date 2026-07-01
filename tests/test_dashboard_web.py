from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

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
    assert "全部市场" in html
    assert "symbol-detail-panel" in html
    assert "dashboard-header" in html
    assert "header-market-filters" in html
    assert "header-broker-filters" in html
    assert "current-view-value" in html
    assert "broker-summary-cards" in html
    assert "source-status-list" in html
    assert "cash-detail-panel" in html
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
    assert "插件模块" in js
    assert "大模型决策模板" in js
    assert "趋势 / K 线与新闻 / 舆论读取固定决策事实，其余插件仍为占位" in js
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
    assert 'grid-template-areas: "brand assets source";' in css
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
    assert ".cash-detail-panel" in css
    assert "grid-template-columns: minmax(0, 1fr) 300px;" not in css
    assert ".right-rail" not in css
    assert 'grid-template-areas: "brand source" "assets assets";' in css
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


def test_dashboard_holdings_table_uses_compact_asset_columns() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert holdings_table_header_labels(html) == [
        "明细",
        "市场",
        "标的",
        "数量",
        "成本价",
        "实时价",
        "美元市值",
        "港元市值",
        "持仓占总资产的占比",
        "盈亏",
    ]
    assert "<th>券商</th>" not in html
    assert "<th>动作</th>" not in html
    assert "<th>持仓价</th>" not in html
    assert '<td colspan="10" class="empty-state">加载中</td>' in html


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
  const nextStart = html.indexOf("<h4>公司行动</h4>");
  if (klineStart < 0 || newsStart < 0 || nextStart < 0 || !(klineStart < newsStart && newsStart < nextStart)) {
    throw new Error("fixed decision fact card boundaries missing: " + html);
  }
  return html.slice(klineStart, nextStart);
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
const cards = fixedDecisionFactCards(renderTradingDecisionPlugins(holding));
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
  const nextStart = html.indexOf("<h4>公司行动</h4>");
  if (klineStart < 0 || newsStart < 0 || nextStart < 0 || !(klineStart < newsStart && newsStart < nextStart)) {
    throw new Error("fixed decision fact card boundaries missing: " + html);
  }
  return html.slice(klineStart, nextStart);
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
const completeCards = fixedDecisionFactCards(renderTradingDecisionPlugins({
  ...baseHolding,
  decision_facts: {
    kline: {available: true, fields: {trend: "过热拉升", position: "显著高于均线", momentum: "RSI 高位", key_levels: "支撑 580", risk: "超买风险"}},
    news_sentiment: {available: true, fields: {direction: "偏多", change: "较上次转强", catalyst: "AI 基建需求", risk: "估值过高", attention: "关注度升高"}}
  }
}));
assertStatus(cardBefore(completeCards, "<h4>新闻 / 舆论</h4>"), "可用", "ok");
assertStatus(cardFrom(completeCards, "<h4>新闻 / 舆论</h4>"), "可用", "ok");
const partialCards = fixedDecisionFactCards(renderTradingDecisionPlugins({
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
const missingCards = fixedDecisionFactCards(renderTradingDecisionPlugins({
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
  if (start < 0 || end < 0 || start >= end) {
    throw new Error("TradingAgents card boundaries missing: " + html);
  }
  return html.slice(start, end);
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
const html = renderTradingDecisionPlugins({
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
const missingCard = tradingAgentsCard(renderTradingDecisionPlugins({
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


def test_dashboard_renders_usable_kline_technical_facts_with_timeframe_labels() -> None:
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
  "日线 RSI",
  "56.88",
  "日线 MACD",
  "MACD 0.22",
  "金叉",
  "日线 当前价",
  "411.60",
  "日线 趋势",
  "价格高于主要均线。",
  "日线 ATR",
  "33.17 · 8.1%",
  "日线 支撑",
  "398.15 · 368.24",
  "日线 阻力",
  "430.00 · 445.50",
  "周线 MACD",
  "形成金叉",
  "周线 当前价",
  "409.20",
  "周线 ATR",
  "41.10",
  "周线 支撑",
  "380.00",
  "周线 阻力",
  "455.00",
  "月线 RSI",
  "61.20"
]) {
  if (!card.includes(required)) {
    throw new Error("missing K-line technical fact " + required + ": " + card);
  }
}
if (card.includes("待接入") || card.includes("占位") || card.includes("rsi:")) {
  throw new Error("usable technical facts rendered as placeholder/raw field: " + card);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


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


def test_dashboard_header_helpers_filter_assets_and_render_sources() -> None:
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
  holdings: [
    {
      market: "US",
      symbol: "VIXY",
      name: "ProShares VIX Short-Term Futures ETF",
      brokers: "futu;tiger",
      market_value_hkd: "37830.00",
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
    },
    {
      market: "HK",
      symbol: "00700",
      name: "Tencent",
      brokers: "phillips",
      market_value_hkd: "15982.00",
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
const summary = currentViewSummary();
if (summary.portfolio_value_hkd !== "15132.00") {
  throw new Error("unexpected portfolio value: " + JSON.stringify(summary));
}
if (summary.holding_value_hkd !== "15132.00") {
  throw new Error("unexpected holding value: " + JSON.stringify(summary));
}
if (summary.cash_like_value_hkd !== "") {
  throw new Error("unexpected cash value: " + JSON.stringify(summary));
}
if (summary.holding_weight_hkd !== "100.00%") {
  throw new Error("unexpected holding weight: " + JSON.stringify(summary));
}
if (summary.holding_count !== 1) {
  throw new Error("unexpected holding count: " + JSON.stringify(summary));
}
state.marketFilter = "ALL";
state.brokerFilter = "futu";
const allFutuSummary = currentViewSummary();
if (allFutuSummary.portfolio_value_hkd !== "-99071.35") {
  throw new Error("ALL/futu should use broker summary: " + JSON.stringify(allFutuSummary));
}
if (allFutuSummary.holding_value_hkd !== "15132.00") {
  throw new Error("ALL/futu holding value mismatch: " + JSON.stringify(allFutuSummary));
}
if (allFutuSummary.cash_like_value_hkd !== "-114203.35") {
  throw new Error("ALL/futu cash value mismatch: " + JSON.stringify(allFutuSummary));
}
if (allFutuSummary.holding_weight_hkd !== "-") {
  throw new Error("ALL/futu holding weight mismatch: " + JSON.stringify(allFutuSummary));
}
const singleBrokerFallback = brokerHoldingValue({
  market: "US",
  symbol: "SINGLE_DETAIL_BLANK",
  brokers: "futu",
  market_value_hkd: "780.00",
  broker_details: [
    {
      broker: "futu",
      market: "US",
      market_value_hkd: "",
    },
  ],
});
if (!singleBrokerFallback.complete || singleBrokerFallback.text !== "780.00") {
  throw new Error("single-broker detail gap should fall back to row value: " + JSON.stringify(singleBrokerFallback));
}
state.dashboard.holdings.push({
  market: "US",
  symbol: "MISSING_DETAIL",
  name: "Missing detail",
  brokers: "futu;tiger",
  market_value_hkd: "780.00",
  broker_details: [],
});
state.marketFilter = "US";
const missingDetailSummary = currentViewSummary();
if (missingDetailSummary.portfolio_value_hkd !== "" || formatMoney(missingDetailSummary.portfolio_value_hkd, "HKD") !== "-") {
  throw new Error("missing multi-broker detail should make broker summary unknown: " + JSON.stringify(missingDetailSummary));
}
state.dashboard.holdings.pop();
state.dashboard.holdings.push({
  market: "US",
  symbol: "BAD",
  name: "Malformed",
  brokers: "futu",
  market_value_hkd: "123abc",
});
state.marketFilter = "US";
const malformedSummary = currentViewSummary();
if (malformedSummary.portfolio_value_hkd !== "" || formatMoney(malformedSummary.portfolio_value_hkd, "HKD") !== "-") {
  throw new Error("malformed holding value should make summary unknown: " + JSON.stringify(malformedSummary));
}
state.dashboard.holdings.pop();
const brokerCards = renderBrokerSummaryCards();
if (!brokerCards.includes("富途") || !brokerCards.includes("HKD -99071.35")) {
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
state.marketFilter = "CASH";
state.brokerFilter = "futu";
const cashRows = filteredCashRows();
if (cashRows.length !== 2 || cashRows[0].symbol !== "HKD_CASH" || cashRows[1].symbol !== "USD_CASH") {
  throw new Error("unexpected cash rows: " + JSON.stringify(cashRows));
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
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.selectedHoldingKey = "";
state.dashboardError = null;
state.quotes = {};
renderHoldings();
if (!elements["holdings-table-wrap"].classList.contains("hidden")) {
  throw new Error("cash view should hide holdings table");
}
if (!elements["symbol-detail-panel"].classList.contains("hidden")) {
  throw new Error("cash view should hide symbol detail panel");
}
if (elements["symbol-detail-panel"].innerHTML !== "") {
  throw new Error("cash view should clear symbol detail panel");
}
if (elements["cash-detail-panel"].classList.contains("hidden")) {
  throw new Error("cash view should show cash detail panel");
}
if (!elements["cash-detail-panel"].innerHTML.includes("现金明细") || !elements["cash-detail-panel"].innerHTML.includes("HKD_CASH")) {
  throw new Error("cash detail panel missing expected rows: " + elements["cash-detail-panel"].innerHTML);
}
if (elements["visible-count"].textContent !== "2 条") {
  throw new Error("cash view visible count mismatch: " + elements["visible-count"].textContent);
}
state.marketFilter = "ALL";
renderHoldings();
if (!elements["cash-detail-panel"].classList.contains("hidden")) {
  throw new Error("non-cash view should hide cash detail panel");
}
state.brokerFilter = "ALL";
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
if (elements["holdings-table-wrap"].classList.contains("hidden")) {
  throw new Error("trading decision should keep holdings table visible");
}
if (!elements["symbol-detail-panel"].classList.contains("hidden")) {
  throw new Error("trading decision should keep bottom symbol detail panel hidden");
}
if (!elements["holdings-body"].innerHTML.includes("交易决策") || elements["holdings-body"].innerHTML.includes(">详情<")) {
  throw new Error("holdings row should expose trading decision entry: " + elements["holdings-body"].innerHTML);
}
if (!elements["holdings-body"].innerHTML.includes("decision-detail-row") || !elements["holdings-body"].innerHTML.includes("inline-symbol-detail")) {
  throw new Error("trading decision should render directly below selected holding row: " + elements["holdings-body"].innerHTML);
}
for (const required of ["交易决策 ·", "插件模块", "大模型决策模板", "TradingAgents", "趋势 / K 线与新闻 / 舆论读取固定决策事实，其余插件仍为占位", "占位"]) {
  if (!elements["holdings-body"].innerHTML.includes(required)) {
    throw new Error("trading decision detail missing " + required + ": " + elements["holdings-body"].innerHTML);
  }
}
for (const unexpected of ["插件管理", "策略阈值"]) {
  if (elements["holdings-body"].innerHTML.includes(unexpected)) {
    throw new Error("trading decision detail should not render extra panel " + unexpected);
  }
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
    ("body", "error_type"),
    [
        (b"", "ResearchChatError"),
        (b"{bad json", "JSONDecodeError"),
        (b'["not", "object"]', "ResearchChatError"),
        (b'"not object"', "ResearchChatError"),
    ],
)
def test_dashboard_server_returns_json_error_for_bad_research_chat_create_body(
    tmp_path,
    body: bytes,
    error_type: str,
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

    assert status == 500
    assert content_type == "application/json; charset=utf-8"
    assert payload["status"] == "error"
    assert payload["error_type"] == error_type
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
