"use strict";

const state = {
  dashboard: null,
  dashboardError: null,
  accountSnapshot: null,
  accountEtag: "",
  accountError: null,
  accountRequestInFlight: false,
  accountValuationUpdates: new Set(),
  accountIntervalId: null,
  marketFilter: "ALL",
  brokerFilter: "futu",
  workspaceView: "portfolio",
  selectedKellyExperimentId: "",
  selectedHoldingKey: "",
  selectedHoldingDetail: "decision",
  selectedDecisionTab: "final",
  selectedTrendBroker: "",
  selectedTrendKind: "",
  accountViews: {tiger: "real", phillips: "real", eastmoney: "real"},
  trendSimulatePositions: {},
  trendReportHistories: {},
  trendHistoricalReports: {},
  decisionDeepLinkRestored: false,
  detailLanguage: "zh",
  statementUpload: {broker: "", busy: false, message: "", error: false},
  researchChat: {
    holdingKey: "",
    sessionId: "",
    busy: false,
    messageCount: 0,
    messages: [],
  },
  standardBacktest: {
    options: null,
    source: "holdings",
    symbolKey: "",
    strategyId: "trend_pullback/v1",
    rangePreset: "1Y",
    customStart: "",
    customEnd: "",
    initialCash: "100000",
    maxWeight: "10%",
    commissionBps: "10",
    slippageBps: "5",
    busy: false,
    error: "",
    result: null,
  },
  predictionMarket: {
    payload: null,
    strategy: "yes_no",
    historyKind: "signals",
    error: "",
    pollId: null,
    signalPollId: null,
    signalRequestInFlight: false,
    signalLastSuccessAt: "",
    signalError: "",
    signalPollEpoch: 0,
    csrfToken: "",
    activeExecutionId: "",
  },
};

const elements = {};

const WORKSPACE_VIEWS = new Set(["portfolio", "prediction_market", "kelly_lab", "standard_backtest", "trend_report"]);

const ACCOUNT_STRATEGY_PROFILES = {
  futu: {horizon: "期权增强", strategy: "跨市场期权关注"},
  tiger: {horizon: "趋势", strategy: "美股趋势交易"},
  phillips: {horizon: "趋势", strategy: "港股趋势交易"},
  eastmoney: {horizon: "偏短线", strategy: "趋势交易"},
};

const ACCOUNT_BROKERS = Object.keys(ACCOUNT_STRATEGY_PROFILES);
const ACCOUNT_SOURCE_GROUPS = [
  {label: "实时账户", brokers: ["futu", "tiger"]},
  {label: "券商结单", brokers: ["phillips", "eastmoney"]},
];
const TREND_ACCOUNT_BROKERS = ["tiger", "phillips", "eastmoney"];
const ACCOUNT_VIEW_KEYS = ["real", "simulate", "report"];

const DECISION_TABS = [
  { key: "final", label: "最终决策" },
  { key: "tradingagents", label: "TradingAgents" },
  { key: "kline", label: "趋势 / K 线" },
  { key: "news", label: "新闻 / 舆论" },
  { key: "futu", label: "富途异动" },
];

const ACTION_LABELS = {
  ADD: "加仓",
  BUY: "买入",
  HOLD: "观察",
  REVIEW: "人工复核",
  SELL_STOP: "止损卖出",
  TAKE_PROFIT: "止盈",
  TRIM: "减仓",
  accumulate: "加仓",
  buy: "买入",
  hold: "观察",
  reduce: "减仓",
  review: "人工复核",
  sell: "卖出",
  trim: "减仓",
  watch: "观察",
  Neutral: "中性",
  Overweight: "超配",
  Underweight: "低配",
  neutral: "中性",
  overweight: "超配",
  underweight: "低配",
};

const ACTION_STATUS_LABELS = {
  active: "有效",
  error: "错误",
  ok: "正常",
  manual_review: "需复核",
  ready: "待确认",
  review: "需复核",
  watch: "观察中",
};

const DETAIL_LANGUAGE_LABELS = {
  zh: "中文",
  en: "English",
};

const PRIORITY_LABELS = {
  critical: "紧急",
  high: "高",
  low: "低",
  medium: "中",
};

const TRIGGER_STATUS_LABELS = {
  add_zone: "接近加仓价",
  entry_zone: "进入买入区间",
  missing_quote: "缺失行情",
  stop_loss_hit: "达到止损价",
  target_1_hit: "达到第一目标价",
  target_2_hit: "达到第二目标价",
  watch: "未触发",
};

const REASON_LABELS = {
  "Current price is at or below the stop loss.": "当前价格已达到或低于止损价。",
  "Current price is at or above target 1.": "当前价格已达到或高于第一目标价。",
  "Current price is at or above target 2.": "当前价格已达到或高于第二目标价。",
  "Current price is inside the planned entry zone.": "当前价格位于计划买入区间。",
  "Current price is near the planned add price.": "当前价格接近计划加仓价。",
  "No plan trigger is active.": "暂无触发中的交易计划。",
  "Futu did not return a quote.": "Futu 未返回行情。",
  "missing quote": "缺失行情。",
};

document.addEventListener("DOMContentLoaded", () => {
  bindElements();
  bindEvents();
  loadDashboard();
  scheduleAccountPolling();
});

function bindElements() {
  [
    "main-topbar",
    "main-navigation",
    "dashboard-header",
    "header-market-filters",
    "current-view-label",
    "current-view-value",
    "current-view-holding-value",
    "current-view-holding-weight",
    "current-view-cash-note",
    "broker-summary-cards",
    "source-status-list",
    "dashboard-shell",
    "workspace-grid",
    "prediction-market-workspace",
    "prediction-market-root",
    "prediction-market-modal-root",
    "kelly-lab-panel",
    "holdings-panel",
    "open-kelly-lab",
    "return-to-portfolio",
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
    "market-filters",
    "broker-filters",
    "visible-count",
    "symbol-detail-panel",
    "account-tabs",
    "account-holdings",
    "action-count",
    "trade-actions",
    "connection-status",
    "connection-success",
    "connection-poll",
    "connection-task",
    "research-chat-layer",
    "research-chat-title",
    "research-chat-context-note",
    "research-chat-context-list",
    "research-chat-messages",
    "research-chat-input",
    "research-chat-send",
    "research-chat-close",
    "research-chat-finalize",
    "research-chat-status",
    "open-standard-backtest",
    "standard-backtest-workspace",
    "trend-report-workspace",
    "standard-backtest-form",
    "backtest-symbol-source",
    "backtest-symbol",
    "backtest-strategy-cards",
    "backtest-range-controls",
    "backtest-custom-range",
    "backtest-custom-start",
    "backtest-custom-end",
    "backtest-initial-cash",
    "backtest-max-weight",
    "backtest-commission",
    "backtest-slippage",
    "run-standard-backtest",
    "standard-backtest-status",
    "standard-backtest-results",
  ].forEach((id) => {
    elements[id] = document.getElementById(id);
  });
  elements["holdings-body"] = elements["account-holdings"];
}

function bindEvents() {
  elements["main-navigation"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-workspace]");
    if (!button) return;
    const workspace = button.dataset.workspace || "portfolio";
    if (workspace === "standard_backtest") {
      return openStandardBacktest();
    }
    if (workspace === "portfolio") {
      return returnToPortfolio();
    }
    setWorkspaceView(workspace);
  });
  elements["prediction-market-root"].addEventListener("click", handlePredictionMarketClick);
  elements["prediction-market-modal-root"].addEventListener("click", handlePredictionModalClick);
  document.addEventListener("keydown", handlePredictionModalKeydown);
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    window.matchMedia("(max-width: 760px)").addEventListener?.(
      "change",
      syncCnTrendBuyAccessibility,
    );
  }
  if (elements["kelly-lab-panel"]) {
    elements["kelly-lab-panel"].addEventListener("click", (event) => {
      const strategyTab = event.target.closest("[data-kelly-experiment]");
      if (strategyTab) {
        state.selectedKellyExperimentId = strategyTab.dataset.kellyExperiment || "";
        renderKellyLab();
        return;
      }
    });
  }
  elements["header-market-filters"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-market]");
    if (!button) {
      return;
    }
    state.marketFilter = button.dataset.market || "ALL";
    state.selectedHoldingKey = "";
    state.selectedHoldingDetail = "decision";
    state.selectedDecisionTab = "final";
    syncDecisionDeepLink();
    setActiveFilter(elements["header-market-filters"], button);
    renderDashboardViews();
  });
  elements["account-tabs"].addEventListener("click", handleBrokerSelection);
  elements["account-tabs"].addEventListener("keydown", handleBrokerTabKeydown);
  elements["broker-summary-cards"].addEventListener("click", handleBrokerSelection);
  elements["account-holdings"].addEventListener("click", (event) => {
    if (handleTrendOptionDialog(event)) return;
    if (handleTrendHoldingTab(event)) return;
    const industryMetric = event.target.closest?.("[data-trend-industry-help]");
    if (industryMetric) {
      if (industryMetric.dataset.trendIndustryHelpOpen === "pinned") {
        closeTrendIndustryHelp(industryMetric);
      } else {
        showTrendIndustryHelp(industryMetric, true);
      }
      return;
    }
    const accountView = event.target.closest("[data-account-view]");
    if (accountView) {
      setAccountView(
        accountView.dataset.accountBroker || "",
        accountView.dataset.accountView || "",
      );
      return;
    }
    const currentReport = event.target.closest("[data-current-trend-report]");
    if (currentReport) {
      showCurrentTrendReport(currentReport.dataset.currentTrendReport || "");
      return;
    }
    const reportHistory = event.target.closest("[data-report-history]");
    if (reportHistory) {
      openTrendReportHistory(reportHistory.dataset.reportHistory || "");
      return;
    }
    const historyArtifact = event.target.closest("[data-history-artifact]");
    if (historyArtifact) {
      loadHistoricalTrendReport(
        historyArtifact.dataset.historyBroker || state.brokerFilter,
        historyArtifact.dataset.historyArtifact || "",
      );
      return;
    }
    const statementUpload = event.target.closest("[data-statement-upload]");
    if (statementUpload) {
      if (!accountActionsEnabled()) return;
      const broker = statementUpload.dataset.statementUpload || "";
      elements["account-holdings"].querySelector(
        `[data-statement-file="${broker}"]`,
      )?.click();
      return;
    }
    const trendReview = event.target.closest("[data-trend-review]");
    if (trendReview) {
      openTrendReview(trendReview.dataset.trendReview || "");
      return;
    }
    const trendReport = event.target.closest("[data-trend-report]");
    if (trendReport) {
      openTrendReport(trendReport.dataset.trendReport || "");
      return;
    }
    const button = event.target.closest("[data-detail-key]");
    if (button) {
      showSymbolDetail(button.dataset.detailKey || "", button.dataset.detailMode || "decision");
      return;
    }
    handleSymbolDetailClick(event);
  });
  elements["account-holdings"].addEventListener("mouseover", (event) => {
    const trigger = event.target.closest?.("[data-trend-industry-help]");
    if (!trigger || (event.relatedTarget && trigger.contains?.(event.relatedTarget))) return;
    showTrendIndustryHelp(trigger);
  });
  elements["account-holdings"].addEventListener("mouseout", (event) => {
    const trigger = event.target.closest?.("[data-trend-industry-help]");
    if (!trigger || (event.relatedTarget && trigger.contains?.(event.relatedTarget))) return;
    if (trigger.dataset.trendIndustryHelpOpen !== "pinned") closeTrendIndustryHelp(trigger);
  });
  elements["account-holdings"].addEventListener("focusin", (event) => {
    const trigger = event.target.closest?.("[data-trend-industry-help]");
    if (trigger) showTrendIndustryHelp(trigger);
  });
  elements["account-holdings"].addEventListener("focusout", (event) => {
    const trigger = event.target.closest?.("[data-trend-industry-help]");
    if (!trigger || (event.relatedTarget && trigger.contains?.(event.relatedTarget))) return;
    if (trigger.dataset.trendIndustryHelpOpen !== "pinned") closeTrendIndustryHelp(trigger);
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest?.("[data-trend-industry-help]")) closeTrendIndustryHelp();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeTrendIndustryHelp();
  });
  elements["account-holdings"].addEventListener("keydown", handleAccountViewTabKeydown);
  elements["account-holdings"].addEventListener("keydown", handleTrendHoldingTabKeydown);
  elements["account-holdings"].addEventListener("change", handleStatementFileSelection);
  if (elements["trade-actions"]) {
    elements["trade-actions"].addEventListener("click", (event) => {
      const button = event.target.closest("[data-action-detail]");
      if (!button) {
        return;
      }
      openTradeActionDetail(button.dataset.actionDetail || "");
    });
  }
  elements["research-chat-close"].addEventListener("click", closeResearchChat);
  elements["research-chat-send"].addEventListener("click", sendResearchChatMessage);
  elements["research-chat-finalize"].addEventListener("click", finalizeResearchChat);
  elements["research-chat-input"].addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendResearchChatMessage();
    }
  });
  elements["symbol-detail-panel"].addEventListener("click", handleSymbolDetailClick);
  elements["open-kelly-lab"].addEventListener("click", () => setWorkspaceView("kelly_lab"));
  elements["return-to-portfolio"].addEventListener("click", returnToPortfolio);
  elements["trend-report-workspace"].addEventListener("click", (event) => {
    if (handleTrendOptionDialog(event)) return;
    if (handleTrendHoldingTab(event)) return;
    if (event.target.closest("[data-close-trend-report]")) returnToPortfolio();
  });
  elements["trend-report-workspace"].addEventListener("keydown", handleTrendHoldingTabKeydown);
  elements["open-standard-backtest"].addEventListener("click", openStandardBacktest);
  elements["backtest-symbol-source"].addEventListener("click", handleBacktestChoice);
  elements["backtest-strategy-cards"].addEventListener("click", handleBacktestChoice);
  elements["backtest-range-controls"].addEventListener("click", handleBacktestChoice);
  elements["backtest-symbol"].addEventListener("change", (event) => {
    state.standardBacktest.symbolKey = event.target.value;
  });
  elements["standard-backtest-form"].addEventListener("submit", submitStandardBacktest);
}

async function openStandardBacktest() {
  setWorkspaceView("standard_backtest");
  if (!state.standardBacktest.options) {
    elements["standard-backtest-status"].textContent = "正在加载回测选项…";
    try {
      const response = await fetch("/api/backtests/options", { cache: "no-store" });
      if (!response.ok) throw new Error(`options ${response.status}`);
      state.standardBacktest.options = await response.json();
      const defaults = state.standardBacktest.options.defaults || {};
      state.standardBacktest.rangePreset = defaults.range || state.standardBacktest.rangePreset;
      state.standardBacktest.maxWeight = decimalAsPercent(defaults.max_strategy_weight, "10%");
      state.standardBacktest.initialCash = String(defaults.initial_cash || "100000");
      state.standardBacktest.commissionBps = String(defaults.commission_bps || "10");
      state.standardBacktest.slippageBps = String(defaults.slippage_bps || "5");
      elements["standard-backtest-status"].textContent = "";
    } catch (error) {
      state.standardBacktest.error = "回测选项加载失败，请稍后重试。";
      elements["standard-backtest-status"].textContent = state.standardBacktest.error;
    }
  }
  renderStandardBacktest();
}

function openTrendReport(broker) {
  const report = state.dashboard?.trend_reports?.[broker];
  if (!report?.available) return;
  state.selectedTrendBroker = broker;
  state.selectedTrendKind = "report";
  elements["trend-report-workspace"].innerHTML = renderTrendReportWorkspace(report);
  setWorkspaceView("trend_report");
  syncCnTrendBuyAccessibility();
  elements["return-to-portfolio"].focus();
}

function openTrendReview(broker) {
  const review = state.dashboard?.trend_reviews?.[broker];
  if (!review?.available) return;
  state.selectedTrendBroker = broker;
  state.selectedTrendKind = "review";
  elements["trend-report-workspace"].innerHTML = renderTrendReviewWorkspace(review);
  setWorkspaceView("trend_report");
  elements["return-to-portfolio"].focus();
}

function returnToPortfolio() {
  const trendBroker = state.selectedTrendBroker;
  const trendKind = state.selectedTrendKind;
  if (state.workspaceView === "standard_backtest") syncStandardBacktestInputs();
  state.selectedTrendBroker = "";
  state.selectedTrendKind = "";
  setWorkspaceView("portfolio");
  renderAccountHoldings();
  if (trendBroker) {
    const attribute = trendKind === "review" ? "data-trend-review" : "data-trend-report";
    document.querySelector(`#account-${trendBroker} [${attribute}]`)?.focus();
  }
}

function handleBacktestChoice(event) {
  const source = event.target.closest("[data-backtest-source]");
  const strategy = event.target.closest("[data-strategy-id]");
  const range = event.target.closest("[data-range-preset]");
  if (source) {
    syncStandardBacktestInputs();
    state.standardBacktest.source = source.dataset.backtestSource;
    state.standardBacktest.symbolKey = "";
  } else if (strategy && !strategy.disabled) {
    state.standardBacktest.strategyId = strategy.dataset.strategyId;
  } else if (range) {
    syncStandardBacktestInputs();
    state.standardBacktest.rangePreset = range.dataset.rangePreset;
  } else {
    return;
  }
  renderStandardBacktest();
}

function renderStandardBacktest() {
  const options = state.standardBacktest.options;
  if (!options) return;
  const backtest = state.standardBacktest;
  elements["backtest-symbol-source"].innerHTML = [
    ["holdings", "当前持仓"], ["watchlist", "关注列表"],
  ].map(([key, label]) => `<button class="filter-button ${backtest.source === key ? "active" : ""}" type="button" data-backtest-source="${key}" aria-pressed="${backtest.source === key}">${label}</button>`).join("");
  const universe = backtest.source === "holdings"
    ? accountBacktestUniverse()
    : (options.universe && options.universe[backtest.source]) || [];
  if (!universe.some((row) => `${row.market}:${row.symbol}` === backtest.symbolKey)) {
    backtest.symbolKey = universe.length ? `${universe[0].market}:${universe[0].symbol}` : "";
  }
  elements["backtest-symbol"].innerHTML = universe.length
    ? universe.map((row) => `<option value="${escapeHtml(`${row.market}:${row.symbol}`)}" ${`${row.market}:${row.symbol}` === backtest.symbolKey ? "selected" : ""}>${escapeHtml(`${row.market} · ${row.symbol}${row.name ? ` · ${row.name}` : ""}`)}</option>`).join("")
    : '<option value="">暂无可回测标的</option>';
  elements["backtest-strategy-cards"].innerHTML = options.strategies.map((strategy) => `
    <button class="backtest-strategy-card ${strategy.id === backtest.strategyId ? "active" : ""}" type="button" data-strategy-id="${escapeHtml(strategy.id)}" aria-pressed="${strategy.id === backtest.strategyId}">
      <strong>${escapeHtml(strategy.name_zh)}</strong><span>${escapeHtml(strategy.description_zh)}</span>
    </button>`).join("") + '<button class="backtest-strategy-card" type="button" disabled aria-disabled="true" aria-pressed="false"><strong>自定义策略</strong><span>后续版本</span></button>';
  elements["backtest-range-controls"].innerHTML = options.ranges.map((range) => `<button class="filter-button ${range === backtest.rangePreset ? "active" : ""}" type="button" data-range-preset="${range}" aria-pressed="${range === backtest.rangePreset}">${range === "CUSTOM" ? "自定义" : range}</button>`).join("");
  const custom = backtest.rangePreset === "CUSTOM";
  elements["backtest-custom-range"].hidden = !custom;
  elements["backtest-custom-range"].classList.toggle("hidden", !custom);
  elements["backtest-custom-start"].required = custom;
  elements["backtest-custom-start"].value = backtest.customStart;
  elements["backtest-custom-end"].value = backtest.customEnd;
  elements["backtest-initial-cash"].value = backtest.initialCash;
  elements["backtest-max-weight"].value = backtest.maxWeight;
  elements["backtest-commission"].value = backtest.commissionBps;
  elements["backtest-slippage"].value = backtest.slippageBps;
}

function accountBacktestUniverse() {
  const seen = new Set();
  return (state.accountSnapshot?.positions || []).flatMap((position) => {
    const market = String(position.market || "").toUpperCase();
    const symbol = String(position.symbol || "").toUpperCase();
    const asset = String(position.asset_class || "").toLowerCase();
    const key = `${market}:${symbol}`;
    if (!["CN", "HK", "US"].includes(market)
        || !["stock", "etf"].includes(asset)
        || !symbol || seen.has(key)) return [];
    seen.add(key);
    return [{market, symbol, name: String(position.name || "")}];
  });
}

function syncStandardBacktestInputs() {
  if (!elements["backtest-max-weight"]) return;
  state.standardBacktest.customStart = elements["backtest-custom-start"].value;
  state.standardBacktest.customEnd = elements["backtest-custom-end"].value;
  state.standardBacktest.initialCash = elements["backtest-initial-cash"].value;
  state.standardBacktest.maxWeight = elements["backtest-max-weight"].value;
  state.standardBacktest.commissionBps = elements["backtest-commission"].value;
  state.standardBacktest.slippageBps = elements["backtest-slippage"].value;
}

function buildStandardBacktestRequest() {
  const backtest = state.standardBacktest;
  const separator = backtest.symbolKey.indexOf(":");
  const request = {
    market: backtest.symbolKey.slice(0, separator),
    symbol: backtest.symbolKey.slice(separator + 1),
    strategy_id: backtest.strategyId,
    range_preset: backtest.rangePreset,
    initial_cash: backtest.initialCash,
    max_strategy_weight: backtest.maxWeight,
    commission_bps: backtest.commissionBps,
    slippage_bps: backtest.slippageBps,
  };
  if (backtest.rangePreset === "CUSTOM") {
    request.custom_start = backtest.customStart;
    request.custom_end = backtest.customEnd;
  }
  return request;
}

async function submitStandardBacktest(event) {
  event.preventDefault();
  syncStandardBacktestInputs();
  const backtest = state.standardBacktest;
  if (!backtest.symbolKey || backtest.busy) return;
  const validationError = validateStandardBacktestDates();
  if (validationError) {
    elements["standard-backtest-status"].textContent = validationError;
    return;
  }
  backtest.busy = true;
  elements["run-standard-backtest"].disabled = true;
  elements["standard-backtest-status"].textContent = "正在运行回测…";
  try {
    const response = await fetch("/api/backtests/standard/run", {
      method: "POST", headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(buildStandardBacktestRequest()),
    });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (!response.ok) {
      elements["standard-backtest-status"].textContent = safeBacktestErrorMessage(payload);
      return;
    }
    if (!payload || typeof payload !== "object") {
      elements["standard-backtest-status"].textContent = "回测请求失败，请稍后重试。";
      return;
    }
    backtest.result = payload;
    renderStandardBacktestResult(payload);
    elements["standard-backtest-status"].textContent = "回测运行成功。";
  } catch (_) {
    elements["standard-backtest-status"].textContent = "回测请求失败，请稍后重试。";
  } finally {
    backtest.busy = false;
    elements["run-standard-backtest"].disabled = false;
  }
}

function renderStandardBacktestResult(result) {
  const target = document.getElementById("standard-backtest-results");
  if (!target || !result || typeof result !== "object") return;
  target.innerHTML = [
    renderBacktestComparisonMetrics(result),
    renderBacktestEquityComparison(result),
    renderBacktestPriceActions(result),
    renderBacktestTradeTable(result),
    renderBacktestRunAssumptions(result),
  ].join("");
  target.hidden = false;
}

function renderBacktestComparisonMetrics(result) {
  const strategy = result.strategy || {};
  const buyHold = result.buy_hold || {};
  const benchmark = result.market_benchmark;
  const benchmarkLabel = result.benchmark_symbol || "市场指数";
  const rows = [
    ["策略收益", strategy.total_return_pct, "pnl"],
    ["买入持有", buyHold.total_return_pct, "pnl"],
    [benchmarkLabel, benchmark && benchmark.total_return_pct, "pnl"],
    ["相对买入持有", result.strategy_excess_return_pct, "pnl"],
    ["相对市场指数", benchmark && result.market_excess_return_pct, "pnl"],
    ["最大回撤", strategy.max_drawdown_pct, "drawdown"],
    ["交易次数", Array.isArray(strategy.trades) ? strategy.trades.filter((trade) => Number(trade.quantity) !== 0).length : 0, "count"],
    ["胜率", strategy.win_rate_pct, "percent"],
  ];
  return `<section class="backtest-result-section" aria-labelledby="backtest-comparison-title"><h3 id="backtest-comparison-title">回测对比</h3><div class="backtest-comparison-grid">${rows.map(([label, value, kind]) => {
    const unavailable = (label === benchmarkLabel || label === "相对市场指数") && !benchmark;
    const rawDisplay = unavailable ? "基准行情缺失，无法比较" : kind === "count" ? formatDisplayNumber(value) : backtestPercent(value);
    const display = kind === "pnl" ? formatSignedPnl(rawDisplay)
      : kind === "drawdown" ? drawdownPercent(value, backtestPercent)
        : rawDisplay;
    const tone = kind === "pnl" || kind === "drawdown" ? pnlClass(display) : "";
    return `<article class="backtest-metric-card${unavailable ? " benchmark-unavailable" : ""}"><span>${escapeHtml(label)}</span><strong${tone ? ` class="${tone}"` : ""}>${escapeHtml(display)}</strong></article>`;
  }).join("")}</div></section>`;
}

function renderBacktestEquityComparison(result) {
  return `<section class="backtest-result-section"><h3>净值曲线</h3>${renderThreeSeriesBacktestChart(
    result.strategy && result.strategy.equity_curve,
    result.buy_hold && result.buy_hold.equity_curve,
    result.market_benchmark && result.market_benchmark.equity_curve,
    result.benchmark_symbol,
    (result.strategy && result.strategy.trades || []).map((trade) => trade.execution_date),
  )}</section>`;
}

function renderThreeSeriesBacktestChart(strategyRows, buyHoldRows, marketRows, benchmarkSymbol, actionDates) {
  const preserved = new Set(Array.isArray(actionDates) ? actionDates.map(String) : []);
  const series = [
    ["策略", downsampleBacktestRows(strategyRows, "equity", preserved), "backtest-line-strategy"],
    ["买入持有", downsampleBacktestRows(buyHoldRows, "equity", preserved), "backtest-line-buy-hold"],
    [benchmarkSymbol || "市场指数", downsampleBacktestRows(marketRows, "equity", preserved), "backtest-line-market"],
  ];
  const points = series.flatMap(([, rows]) => rows).map((row) => Number(row.equity));
  const dates = [...new Set(series.flatMap(([, rows]) => Array.isArray(rows) ? rows.map((row) => String(row.date || "")) : []).filter(Boolean))].sort();
  const [min, max] = finiteBacktestExtent(points);
  const path = (rows) => {
    const byDate = new Map((Array.isArray(rows) ? rows : []).map((row) => [String(row.date || ""), Number(row.equity)]));
    let started = false;
    return dates.map((date, index) => {
      const value = byDate.get(date);
      if (!Number.isFinite(value)) return "";
      const x = dates.length > 1 ? 20 + index * 560 / (dates.length - 1) : 300;
      const y = 180 - (value - min) * 150 / (max - min || 1);
      const command = started ? "L" : "M";
      started = true;
      return `${command}${x.toFixed(1)},${y.toFixed(1)}`;
    }).filter(Boolean).join(" ");
  };
  const legend = series.map(([label, , className]) => `<span class="${className}">${escapeHtml(label)}</span>`).join("");
  const paths = series.map(([, rows, className]) => `<path class="${className}" d="${path(rows)}" fill="none" vector-effect="non-scaling-stroke"></path>`).join("");
  return `<div class="backtest-chart" role="img" aria-label="策略、买入持有与市场指数净值曲线"><div class="backtest-chart-legend">${legend}</div><svg viewBox="0 0 600 200" aria-hidden="true">${paths}</svg></div>`;
}

function renderBacktestPriceActions(result) {
  const strategy = result.strategy || {};
  const rows = Array.isArray(strategy.equity_curve) ? strategy.equity_curve : [];
  const trades = Array.isArray(strategy.trades) ? strategy.trades : [];
  return `<section class="backtest-result-section"><h3>价格与动作</h3>${renderPriceActionChart(rows, trades)}</section>`;
}

function renderPriceActionChart(rows, trades) {
  const allowed = new Set(["BUY", "ADD", "REDUCE", "EXIT"]);
  const validDates = new Set((Array.isArray(rows) ? rows : []).filter((row) => row && Number.isFinite(Number(row.close))).map((row) => String(row.date || "")));
  const grouped = new Map();
  for (const trade of Array.isArray(trades) ? trades : []) {
    const action = String(trade.action || "");
    const executionDate = String(trade.execution_date || "");
    const price = Number(trade.raw_price);
    if (!allowed.has(action) || !validDates.has(executionDate) || !Number.isFinite(price)) continue;
    const key = `${executionDate}\u0000${action}`;
    const current = grouped.get(key);
    if (current) current.count += 1;
    else grouped.set(key, { execution_date: executionDate, action, raw_price: price, count: 1 });
  }
  const allGroups = [...grouped.values()];
  const actionGroups = sampleBacktestActionGroups(allGroups, 600);
  rows = downsampleBacktestRows(rows, "close", new Set(actionGroups.map((group) => group.execution_date)));
  const prices = rows.map((row) => Number(row.close));
  const [min, max] = finiteBacktestExtent(prices);
  const dateIndex = new Map(rows.map((row, index) => [String(row.date || ""), index]));
  const xy = (date, price) => {
    const index = dateIndex.get(String(date || "")) || 0;
    return [rows.length > 1 ? 20 + index * 560 / (rows.length - 1) : 300, 180 - (Number(price) - min) * 150 / (max - min || 1)];
  };
  const displayedGroups = actionGroups.filter((group) => dateIndex.has(group.execution_date));
  const omittedGroups = allGroups.length - displayedGroups.length;
  const pricePath = rows.map((row, index) => { const [x, y] = xy(row.date, row.close); return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`; }).join(" ");
  const explanations = { BUY: "买入", ADD: "加仓", REDUCE: "减仓", EXIT: "退出" };
  const markers = displayedGroups.map((group) => {
    const [x, y] = xy(group.execution_date, group.raw_price);
    const count = group.count > 1 ? ` ×${formatDisplayNumber(group.count)}` : "";
    return `<g class="backtest-action-marker action-${group.action.toLowerCase()}"><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5"></circle><text x="${x.toFixed(1)}" y="${(y - 9).toFixed(1)}">${group.action}${count}</text></g>`;
  }).join("");
  const summary = displayedGroups.map((group) => `${group.execution_date} ${group.action}（${explanations[group.action]}）${group.count > 1 ? `共 ${formatDisplayNumber(group.count)} 笔` : ""}`).join("；");
  const omittedNotice = omittedGroups ? `另有 ${formatDisplayNumber(omittedGroups)} 组交易标记未显示` : "";
  return `<div class="backtest-chart" role="img" aria-label="价格曲线与交易动作。${escapeHtml(summary || "没有执行动作")}。${omittedNotice}。HOLD（观察）不绘制标记。"><svg viewBox="0 0 600 200" aria-hidden="true"><path class="backtest-price-line" d="${pricePath}" fill="none" vector-effect="non-scaling-stroke"></path>${markers}</svg></div>`;
}

function sampleBacktestActionGroups(groups, limit) {
  if (groups.length <= limit) return groups;
  const selected = new Set([0, groups.length - 1]);
  const actions = ["BUY", "ADD", "REDUCE", "EXIT"];
  for (const action of actions) {
    const first = groups.findIndex((group) => group.action === action);
    if (first >= 0) selected.add(first);
    for (let index = groups.length - 1; index >= 0; index -= 1) {
      if (groups[index].action === action) { selected.add(index); break; }
    }
  }
  const remaining = limit - selected.size;
  const step = (groups.length - 1) / (remaining + 1);
  for (let index = 1; index <= remaining; index += 1) selected.add(Math.round(index * step));
  if (selected.size < limit) {
    for (let index = 0; index < groups.length && selected.size < limit; index += 1) selected.add(index);
  }
  return [...selected].sort((left, right) => left - right).slice(0, limit).map((index) => groups[index]);
}

function renderBacktestTradeTable(result) {
  const trades = result.strategy && Array.isArray(result.strategy.trades) ? result.strategy.trades : [];
  if (!trades.length) return '<section class="backtest-result-section"><h3>交易记录</h3><p class="backtest-empty-state">所选区间内没有触发交易</p></section>';
  const visible = trades.slice(0, 500);
  const notice = trades.length > visible.length ? `<p>仅显示前 500 笔，共 ${formatDisplayNumber(trades.length)} 笔</p>` : "";
  return `<section class="backtest-result-section"><h3>交易记录</h3>${notice}<div class="backtest-table-wrap"><table class="backtest-trades-table"><thead><tr><th>执行日期</th><th>动作</th><th>数量</th><th>成交价</th><th>费用</th><th>原因</th></tr></thead><tbody>${visible.map((trade) => `<tr><td>${escapeHtml(trade.execution_date)}</td><td>${escapeHtml(trade.action)}</td><td>${escapeHtml(formatDisplayNumber(trade.quantity))}</td><td>${escapeHtml(formatDisplayNumber(trade.execution_price))}</td><td>${escapeHtml(formatDisplayNumber(trade.fees))}</td><td>${escapeHtml(trade.reason)}</td></tr>`).join("")}</tbody></table></div></section>`;
}

function renderBacktestRunAssumptions(result) {
  const strategy = result.strategy || {};
  const trades = Array.isArray(strategy.trades) ? strategy.trades : [];
  const totalFees = trades.reduce((sum, trade) => sum + (Number(trade.fees) || 0), 0);
  const assumptions = result.assumptions || {};
  const definition = result.strategy_definition || {};
  const parameterLabels = { sma_short: "短期均线周期", sma_long: "长期均线周期", atr_period: "真实波幅周期", rsi_period: "强弱指标周期", stop_multiplier: "止损倍数", high_period: "突破周期", volume_period: "成交量周期", volume_multiplier: "成交量倍数", sma_exit: "退出均线周期", bollinger_period: "布林带周期", stddev_multiplier: "标准差倍数" };
  const parameters = definition.parameters && typeof definition.parameters === "object" ? Object.entries(definition.parameters) : [];
  const signals = Array.isArray(result.signals) ? result.signals : [];
  const holdSignals = signals.filter((signal) => signal.action === "HOLD");
  const artifacts = [["manifest_path", "运行清单"], ["signals_path", "策略信号"], ["trades_path", "交易记录"], ["equity_curve_path", "策略净值"], ["buy_hold_equity_path", "买入持有净值"], ["market_benchmark_equity_path", "市场指数净值"], ["metrics_path", "指标数据"], ["report_path", "回测报告"]];
  return `<section class="backtest-result-section"><h3>运行详情</h3><dl class="backtest-run-details"><dt>请求范围</dt><dd>${escapeHtml(result.requested_start || "-")} 至 ${escapeHtml(result.requested_end || "-")}</dd><dt>实际数据</dt><dd>${escapeHtml(result.actual_start || "-")} 至 ${escapeHtml(result.actual_end || "-")}</dd><dt>策略版本</dt><dd>${escapeHtml(result.strategy_id || "-")}</dd><dt>策略名称</dt><dd>${escapeHtml(definition.name_zh || "-")} · ${escapeHtml(definition.description_zh || "-")}</dd><dt>执行器版本</dt><dd>${escapeHtml(result.adapter_version || "-")}</dd><dt>运行编号</dt><dd>${escapeHtml(result.run_id || "-")}</dd></dl><h4>交易假设</h4><dl class="backtest-run-details"><dt>初始资金</dt><dd>${escapeHtml(formatDisplayNumber(assumptions.initial_cash))}</dd><dt>最大策略仓位</dt><dd>${backtestPercent(Number(assumptions.max_strategy_weight) * 100)}</dd><dt>佣金</dt><dd>${escapeHtml(formatDisplayNumber(assumptions.commission_bps))} 基点</dd><dt>滑点</dt><dd>${escapeHtml(formatDisplayNumber(assumptions.slippage_bps))} 基点</dd><dt>已实现交易费用</dt><dd>${escapeHtml(formatDisplayNumber(totalFees.toFixed(2)))}</dd></dl><h4>固定参数</h4><dl class="backtest-run-details">${parameters.map(([key, value]) => `<dt>${escapeHtml(parameterLabels[key] || key)}</dt><dd>${escapeHtml(formatDisplayNumber(value))}</dd>`).join("")}</dl><p class="backtest-signal-summary">HOLD（观察）信号 ${formatDisplayNumber(holdSignals.length)} 次${holdSignals.length ? `；${escapeHtml(holdSignals.slice(0, 10).map((signal) => signal.decision_date).join("、"))}` : ""}</p><h4>结果文件</h4><ul class="backtest-artifacts">${artifacts.filter(([key]) => result[key]).map(([key, label]) => `<li><span>${label}</span><code>${escapeHtml(result[key])}</code></li>`).join("")}</ul></section>`;
}

function finiteBacktestExtent(values) {
  let min = Infinity; let max = -Infinity;
  for (const value of values) {
    const number = Number(value);
    if (!Number.isFinite(number)) continue;
    if (number < min) min = number;
    if (number > max) max = number;
  }
  return min === Infinity ? [0, 1] : [min, max];
}

function downsampleBacktestRows(rows, valueKey, preservedDates, limit = 600) {
  const valid = (Array.isArray(rows) ? rows : []).filter((row) => row && String(row.date || "") && Number.isFinite(Number(row[valueKey])));
  if (valid.length <= limit) return valid;
  const selected = new Set([0, valid.length - 1]);
  const preserveIndexes = [];
  valid.forEach((row, index) => { if (preservedDates && preservedDates.has(String(row.date))) preserveIndexes.push(index); });
  const preserveStep = Math.max(1, Math.ceil(preserveIndexes.length / Math.max(1, limit - 2)));
  for (let index = 0; index < preserveIndexes.length && selected.size < limit; index += preserveStep) selected.add(preserveIndexes[index]);
  const remaining = limit - selected.size;
  if (remaining > 0) {
    const step = (valid.length - 1) / (remaining + 1);
    for (let index = 1; index <= remaining; index += 1) selected.add(Math.round(index * step));
  }
  return [...selected].sort((left, right) => left - right).slice(0, limit).map((index) => valid[index]);
}

function backtestPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(2)}%` : "-";
}

function drawdownPercent(value, formatter = decisionPlanPercent) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const magnitude = Math.abs(number);
  const display = formatter(magnitude);
  return magnitude === 0 ? display : `-${display}`;
}

function validateStandardBacktestDates() {
  const backtest = state.standardBacktest;
  if (backtest.rangePreset !== "CUSTOM") return "";
  if (!backtest.customStart) return "自定义区间必须填写开始日期。";
  if (backtest.customEnd && backtest.customStart >= backtest.customEnd) return "开始日期必须早于结束日期。";
  return "";
}

function safeBacktestErrorMessage(payload) {
  const message = payload && typeof payload.message === "string" ? payload.message.trim() : "";
  const isSafeChinese = message && /[\u3400-\u9fff]/.test(message) && !/[A-Za-z]/.test(message);
  return isSafeChinese ? message : "回测请求失败，请稍后重试。";
}

function decimalAsPercent(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? `${formatDisplayNumber(number * 100)}%` : fallback;
}

function handleSymbolDetailClick(event) {
  const decisionTab = event.target.closest("[data-decision-tab]");
  if (decisionTab) {
    state.selectedDecisionTab = decisionTab.dataset.decisionTab || "final";
    syncDecisionDeepLink();
    renderHoldings();
    return;
  }
  const backButton = event.target.closest("[data-back-to-holdings]");
  if (backButton) {
    state.selectedHoldingKey = "";
    state.selectedHoldingDetail = "decision";
    state.selectedDecisionTab = "final";
    syncDecisionDeepLink();
    renderHoldings();
    return;
  }
  const languageButton = event.target.closest("[data-detail-language]");
  if (languageButton) {
    state.detailLanguage = languageButton.dataset.detailLanguage === "en" ? "en" : "zh";
    renderHoldings();
    return;
  }
  const chatButton = event.target.closest("[data-research-chat]");
  if (chatButton) {
    openResearchChat(chatButton.dataset.researchChat || "");
    return;
  }
  const rawButton = event.target.closest("[data-toggle-raw-report]");
  if (!rawButton) {
    return;
  }
  const section = rawButton.closest(".detail-section") || elements["symbol-detail-panel"];
  const rawReport = section.querySelector(".raw-report");
  if (!rawReport) {
    return;
  }
  const isHidden = rawReport.classList.toggle("hidden");
  if (rawButton.classList.contains("english-source-toggle")) {
    rawButton.textContent = isHidden ? "查看英文原文" : "隐藏英文原文";
  } else {
    rawButton.textContent = isHidden ? "查看原始报告" : "隐藏原始报告";
  }
}

async function loadDashboard() {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`dashboard ${response.status}`);
    }
    state.dashboard = await response.json();
    state.dashboardError = null;
    restoreDecisionDeepLink();
    renderDashboard();
  } catch (error) {
    renderLoadError(error);
  }
}

function restoreDecisionDeepLink() {
  if (state.decisionDeepLinkRestored || typeof window === "undefined") {
    return;
  }
  state.decisionDeepLinkRestored = true;
  const params = new URLSearchParams(window.location.search || "");
  const market = String(params.get("market") || "").toUpperCase();
  const symbol = String(params.get("symbol") || "").toUpperCase();
  if (!market || !symbol) {
    return;
  }
  const groups = accountHoldingGroups();
  const match = ACCOUNT_BROKERS.flatMap((broker) => {
    const group = groups.find((item) => item.broker === broker);
    return (group?.rows || []).map((row) => ({broker, row}));
  }).find(({row}) => (
    String(row.display.market || "").toUpperCase() === market
    && String(row.display.symbol || "").toUpperCase() === symbol
  ));
  if (!match) {
    return;
  }
  state.marketFilter = "ALL";
  state.brokerFilter = match.broker;
  state.selectedHoldingKey = match.row.key;
  state.selectedHoldingDetail = "decision";
  const decisionTab = String(params.get("decision_tab") || "final");
  state.selectedDecisionTab = DECISION_TABS.some((tab) => tab.key === decisionTab)
    ? decisionTab
    : "final";
}

function syncDecisionDeepLink() {
  if (typeof window === "undefined" || !window.history || !window.location) {
    return;
  }
  const params = new URLSearchParams(window.location.search || "");
  const selected = selectedHolding();
  if (selected) {
    params.set("market", String(selected.holding.market || ""));
    params.set("symbol", String(selected.holding.symbol || ""));
    params.set("decision_tab", state.selectedDecisionTab || "final");
  } else {
    params.delete("market");
    params.delete("symbol");
    params.delete("decision_tab");
  }
  const query = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash || ""}`);
}

function scheduleAccountPolling() {
  if (state.accountIntervalId !== null) {
    window.clearInterval(state.accountIntervalId);
  }
  loadAccountSnapshot();
  state.accountIntervalId = window.setInterval(loadAccountSnapshot, 5000);
}

async function loadAccountSnapshot() {
  if (state.accountRequestInFlight) {
    return;
  }
  state.accountRequestInFlight = true;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 4000);
  try {
    const headers = state.accountEtag ? {"If-None-Match": state.accountEtag} : {};
    const response = await fetch("/api/v1/account/snapshot", {
      cache: "no-store", headers, signal: controller.signal,
    });
    if (response.status === 304) {
      state.accountError = null;
      state.accountValuationUpdates.clear();
      return;
    }
    if (!response.ok) {
      throw new Error(`account snapshot ${response.status}`);
    }
    const payload = await response.json();
    state.accountValuationUpdates = accountValuationUpdates(
      state.accountSnapshot,
      payload,
    );
    state.accountSnapshot = payload;
    state.accountEtag = response.headers.get("ETag") || "";
    state.accountError = null;
  } catch (error) {
    state.accountError = error;
  } finally {
    window.clearTimeout(timeout);
    state.accountRequestInFlight = false;
    renderHeaderSummary();
    renderSummary();
    renderBrokerCards();
    renderSourceStatusListIntoHeader();
    renderConnectionPanel();
    renderHoldings();
    if (state.workspaceView === "standard_backtest") renderStandardBacktest();
    state.accountValuationUpdates.clear();
  }
}

function renderDashboard() {
  renderBrokerCards();
  renderSourceStatusListIntoHeader();
  renderWorkspaceChrome();
  renderKellyLab();
  renderDashboardViews();
  renderTradeActions();
  renderConnectionPanel();
}

function setWorkspaceView(view) {
  state.workspaceView = WORKSPACE_VIEWS.has(view) ? view : "portfolio";
  renderWorkspaceChrome();
  if (state.workspaceView === "kelly_lab") renderKellyLab();
  if (state.workspaceView === "prediction_market") {
    renderPredictionMarket();
    fetchPredictionState();
    startPredictionPolling();
    startPredictionSignalPolling();
  } else {
    stopPredictionPolling();
    stopPredictionSignalPolling();
  }
}

function renderWorkspaceChrome() {
  const view = state.workspaceView;
  document.body?.classList?.toggle?.("prediction-market-active", view === "prediction_market");
  const toolView = view !== "portfolio";
  elements["dashboard-shell"].classList.toggle("tool-workspace-view", toolView);
  elements["return-to-portfolio"].hidden = !toolView;
  elements["return-to-portfolio"].classList.toggle("hidden", !toolView);
  elements["dashboard-header"]?.classList.toggle("hidden", view === "prediction_market");
  elements["workspace-grid"].classList.toggle("hidden", view === "standard_backtest" || view === "trend_report" || view === "prediction_market");
  elements["holdings-panel"].classList.toggle("hidden", view !== "portfolio");
  elements["kelly-lab-panel"].classList.toggle("hidden", view !== "kelly_lab");
  if (elements["prediction-market-workspace"]) {
    elements["prediction-market-workspace"].hidden = view !== "prediction_market";
    elements["prediction-market-workspace"].classList.toggle("hidden", view !== "prediction_market");
  }
  elements["standard-backtest-workspace"].hidden = view !== "standard_backtest";
  elements["standard-backtest-workspace"].classList.toggle("hidden", view !== "standard_backtest");
  elements["trend-report-workspace"].hidden = view !== "trend_report";
  elements["trend-report-workspace"].classList.toggle("hidden", view !== "trend_report");
  const navigationButtons = typeof elements["main-navigation"]?.querySelectorAll === "function"
    ? elements["main-navigation"].querySelectorAll("[data-workspace]")
    : [];
  navigationButtons.forEach((button) => {
    const active = button.dataset.workspace === view;
    button.setAttribute("aria-current", active ? "page" : "false");
  });
}

function renderKellyLab() {
  if (!elements["kelly-lab-panel"]) {
    return;
  }
  elements["kelly-lab-panel"].innerHTML = renderKellyLabPanel();
}

function renderKellyLabPanel() {
  if (state.workspaceView !== "kelly_lab") {
    return "";
  }

  const dashboard = state.dashboard || {};
  const lab = dashboard.kelly_lab;
  if (state.dashboardError) {
    return `
      <div class="section-heading compact kelly-lab-heading">
        <div>
          <h2>模拟盘策略实验室</h2>
          <p>看板数据加载失败。</p>
        </div>
        <div class="kelly-lab-heading-actions">
          <span class="status-pill status-failed">不可用</span>
        </div>
      </div>
      <div class="kelly-lab-empty">Kelly Lab 数据暂不可用。</div>
    `;
  }
  if (!state.dashboard) {
    return `
      <div class="section-heading compact kelly-lab-heading">
        <div>
          <h2>模拟盘策略实验室</h2>
          <p>等待看板数据。</p>
        </div>
        <div class="kelly-lab-heading-actions">
          <span class="status-pill status-muted">加载中</span>
        </div>
      </div>
      <div class="kelly-lab-empty">Kelly Lab 数据尚未加载。</div>
    `;
  }
  if (!lab || typeof lab !== "object" || !lab.available) {
    const message = lab && typeof lab === "object"
      ? firstPresent(lab.error, lab.message, lab.reason, "Kelly Lab 数据不可用。")
      : "缺少 Kelly Lab 数据。";
    return `
      <div class="section-heading compact kelly-lab-heading">
        <div>
          <h2>模拟盘策略实验室</h2>
          <p>${escapeHtml(formatPlain(message))}</p>
        </div>
        <div class="kelly-lab-heading-actions">
          <span class="status-pill status-muted">不可用</span>
        </div>
      </div>
      <div class="kelly-lab-empty">${escapeHtml(formatPlain(message))}</div>
    `;
  }

  const experiments = Array.isArray(lab.experiments) ? lab.experiments : [];
  const count = hasValue(lab.experiment_count) ? lab.experiment_count : experiments.length;
  const activeExperiment = activeKellyExperiment(experiments);
  const activeExperimentId = activeExperiment ? kellyExperimentKey(activeExperiment, experiments.indexOf(activeExperiment)) : "";
  const cards = activeExperiment
    ? renderKellyExperimentCard(activeExperiment)
    : `<div class="kelly-lab-empty">暂无实验。</div>`;
  return `
    <div class="section-heading compact kelly-lab-heading">
      <div>
        <h2>模拟盘策略实验室</h2>
        <p>只读实验结果。</p>
      </div>
      <div class="kelly-lab-heading-actions">
        <span class="count-pill">${escapeHtml(formatDisplayNumber(count))} 个实验</span>
      </div>
    </div>
    ${renderKellyStrategyTabs(experiments, activeExperimentId)}
    <div class="kelly-experiment-grid single">
      ${cards}
    </div>
  `;
}

const KELLY_LIFECYCLE_STATUSES = [
  {
    key: "watching",
    label: "观察中",
    meaning: "该标的在策略监控范围内，但当前没有入场信号，也没有持仓。",
    systemAction: "持续检查入场规则。",
    nextStep: "入场规则触发后进入「待下单」。",
    className: "status-muted",
  },
  {
    key: "pending_entry_order",
    label: "待下单",
    meaning: "入场规则触发，仓位计算与风控检查待执行。",
    systemAction: "等待仓位计算与风控检查。",
    nextStep: "风控检查允许入场后提交买入；未允许则记录拦截。",
    className: "status-ok",
  },
  {
    key: "holding",
    label: "持仓中",
    meaning: "模拟盘买入已成交，这笔策略样本正在进行中。",
    systemAction: "持续检查止盈、止损、移动止盈、时间退出。",
    nextStep: "任一退出规则触发后进入「待退出」。",
    className: "status-ok",
  },
  {
    key: "pending_exit_order",
    label: "待退出",
    meaning: "这笔持仓已经触发退出规则，但卖出还没有完成。",
    systemAction: "准备向模拟盘提交卖出订单。",
    nextStep: "卖出成交后进入「已完成」；卖出失败进入「执行失败」。",
    className: "status-warn",
  },
  {
    key: "completed",
    label: "已完成",
    meaning: "买入和卖出都已成交，交易样本已经闭环。",
    systemAction: "把净盈亏、持有天数、退出原因计入样本统计。",
    nextStep: "更新胜率 p、盈亏比 b、Kelly 仓位参数。",
    className: "status-muted",
  },
  {
    key: "risk_blocked",
    label: "风控拦截",
    meaning: "入场规则触发了，但账户或组合风控不允许下单。",
    systemAction: "不下单，不计入完成样本，只记录拦截事件。",
    nextStep: "风控条件解除后重新评估。",
    className: "status-warn",
  },
  {
    key: "execution_failed",
    label: "执行失败",
    meaning: "系统本来应该下单或退出，但模拟盘接口、订单同步、撤单或成交确认失败。",
    systemAction: "停止自动推进，标记需要人工检查。",
    nextStep: "人工处理后可以重试、取消，或手动标记结果。",
    className: "status-failed",
  },
];

function activeKellyExperiment(experiments) {
  const items = Array.isArray(experiments) ? experiments : [];
  if (!items.length) {
    return null;
  }
  const selected = formatPlain(state.selectedKellyExperimentId);
  return items.find((experiment, index) => kellyExperimentKey(experiment, index) === selected) || items[0];
}

function kellyExperimentKey(experiment, index) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const strategyVersion = [entry.strategy_id, entry.strategy_version].filter(hasValue).map(formatPlain).join(":");
  return firstPresent(entry.experiment_id, strategyVersion, entry.experiment_name, `experiment-${index}`);
}

function renderKellyStrategyTabs(experiments, activeExperimentId) {
  const items = Array.isArray(experiments) ? experiments : [];
  if (!items.length) {
    return "";
  }
  return `
    <div class="kelly-strategy-tabs" role="tablist" aria-label="Kelly 策略">
      ${items.map((experiment, index) => {
        const entry = experiment && typeof experiment === "object" ? experiment : {};
        const template = entry.template && typeof entry.template === "object" ? entry.template : {};
        const experimentId = kellyExperimentKey(entry, index);
        const active = experimentId === activeExperimentId;
        const label = firstPresent(entry.experiment_name, template.strategy_name, entry.strategy_id, "未命名策略");
        const detail = [entry.strategy_id, template.strategy_name, entry.strategy_version]
          .filter(hasValue)
          .map(formatPlain)
          .join(" · ");
        return `
          <button
            class="kelly-strategy-tab ${active ? "active" : ""}"
            type="button"
            role="tab"
            aria-selected="${active ? "true" : "false"}"
            data-kelly-experiment="${escapeHtml(formatPlain(experimentId))}"
          >
            <span>${escapeHtml(formatPlain(label))}</span>
            ${detail ? `<small>${escapeHtml(detail)}</small>` : ""}
          </button>
        `;
      }).join("")}
    </div>
  `;
}

function renderKellySymbolStates(experiment) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const lifecycleSamples = Array.isArray(entry.lifecycle_states)
    ? entry.lifecycle_states.filter((sample) => sample && typeof sample === "object")
    : [];
  const participants = Array.isArray(entry.participants)
    ? entry.participants.filter((participant) => participant && typeof participant === "object")
    : [];
  const samples = lifecycleSamples.length
    ? lifecycleSamples
    : participants.map((participant) => ({
      ...participant,
      status: "watching",
      reason: "等待该策略下一次入场信号。",
    }));
  if (!samples.length) {
    return "";
  }

  return `
    <section class="kelly-symbol-states" aria-label="Kelly 标的状态">
      <div class="kelly-symbol-states-header">
        <h4>标的状态</h4>
        <p>观察中 → 待下单 → 持仓中 → 待退出 → 已完成</p>
      </div>
      <div class="kelly-symbol-state-grid">
        ${samples.map(renderKellySymbolState).join("")}
      </div>
    </section>
  `;
}

function renderKellyOrderSync(experiment) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const sync = entry.order_sync && typeof entry.order_sync === "object" ? entry.order_sync : null;
  if (!sync) {
    return "";
  }
  const status = kellyOrderSyncStatus(sync.status);
  const rows = [
    ["环境", sync.environment],
    ["最近同步", sync.last_synced_at],
    ["订单", formatDisplayNumber(sync.order_count)],
    ["成交", formatDisplayNumber(sync.fill_count)],
  ];
  return `
    <section class="kelly-order-sync" aria-label="Kelly 订单同步">
      <div class="kelly-order-sync-header">
        <h4>订单同步</h4>
        <span class="status-pill ${escapeHtml(status.className)}">${escapeHtml(status.label)}</span>
      </div>
      <dl class="kelly-order-sync-grid">
        ${rows.map(([label, value]) => `
          <div>
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(formatPlain(value))}</dd>
          </div>
        `).join("")}
      </dl>
      ${hasValue(sync.message) ? `<p>${escapeHtml(formatPlain(sync.message))}</p>` : ""}
      ${hasValue(sync.next_action) ? `<small>${escapeHtml(formatPlain(sync.next_action))}</small>` : ""}
      ${renderKellyOrderSyncOrders(sync)}
    </section>
  `;
}

function renderKellyOrderSyncOrders(sync) {
  const orders = sync && Array.isArray(sync.orders)
    ? sync.orders.filter((order) => order && typeof order === "object")
    : [];
  if (!orders.length) {
    return `<p class="kelly-order-empty">暂无同步订单明细。</p>`;
  }
  const headers = ["标的", "方向", "下单时间", "订单价", "订单数量", "成交数量", "成交均价", "状态"];
  return `
    <div class="kelly-order-table" role="table" aria-label="Kelly 同步订单明细">
      <div class="kelly-order-row header" role="row">
        ${headers.map((header) => `<span role="columnheader">${escapeHtml(header)}</span>`).join("")}
      </div>
      ${orders.map(renderKellyOrderSyncOrder).join("")}
    </div>
  `;
}

function renderKellyOrderSyncOrder(order) {
  const item = order && typeof order === "object" ? order : {};
  const symbol = [item.market, item.symbol]
    .filter(hasValue)
    .map(formatPlain)
    .join(".");
  const symbolCell = `
    <strong>${escapeHtml(firstPresent(symbol, item.symbol, "-"))}</strong>
    ${hasValue(item.order_id) ? `<small>${escapeHtml(formatPlain(item.order_id))}</small>` : ""}
  `;
  const cells = [
    symbolCell,
    escapeHtml(kellyOrderSideLabel(item.side)),
    escapeHtml(formatPlain(item.submitted_at || "-")),
    escapeHtml(formatDisplayNumber(item.order_price)),
    escapeHtml(formatDisplayNumber(item.order_qty)),
    escapeHtml(formatDisplayNumber(item.filled_qty)),
    escapeHtml(formatDisplayNumber(item.avg_fill_price)),
    escapeHtml(kellyOrderStatusLabel(item.status)),
  ];
  return `
    <div class="kelly-order-row" role="row">
      ${cells.map((cell) => `<span role="cell">${cell}</span>`).join("")}
    </div>
  `;
}

function renderKellyOrderExecution(experiment) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const execution = entry.order_execution && typeof entry.order_execution === "object"
    ? entry.order_execution
    : null;
  if (!execution) {
    return "";
  }
  const status = kellyOrderExecutionStatus(execution.status);
  const rows = [
    ["环境", execution.environment],
    ["最近执行", execution.last_executed_at],
    ["执行", formatDisplayNumber(execution.execution_count)],
    ["预演", formatDisplayNumber(execution.dry_run_count)],
    ["提交", formatDisplayNumber(execution.submitted_count)],
    ["跳过", formatDisplayNumber(execution.skipped_count)],
    ["失败", formatDisplayNumber(execution.failed_count)],
  ];
  return `
    <section class="kelly-order-sync" aria-label="Kelly 订单执行">
      <div class="kelly-order-sync-header">
        <h4>订单执行</h4>
        <span class="status-pill ${escapeHtml(status.className)}">${escapeHtml(status.label)}</span>
      </div>
      <dl class="kelly-order-sync-grid">
        ${rows.map(([label, value]) => `
          <div>
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(formatPlain(value))}</dd>
          </div>
        `).join("")}
      </dl>
      ${hasValue(execution.message) ? `<p>${escapeHtml(formatPlain(execution.message))}</p>` : ""}
      ${renderKellyOrderExecutionRows(execution)}
    </section>
  `;
}

function renderKellyOrderExecutionRows(execution) {
  const executions = execution && Array.isArray(execution.executions)
    ? execution.executions.filter((item) => item && typeof item === "object")
    : [];
  if (!executions.length) {
    return `<p class="kelly-order-empty">暂无订单执行明细。</p>`;
  }
  const headers = ["标的", "方向", "价格", "数量", "计划金额", "富途订单", "状态", "错误"];
  return `
    <div class="kelly-order-table" role="table" aria-label="Kelly 订单执行明细">
      <div class="kelly-order-row header" role="row">
        ${headers.map((header) => `<span role="columnheader">${escapeHtml(header)}</span>`).join("")}
      </div>
      ${executions.map(renderKellyOrderExecutionRow).join("")}
    </div>
  `;
}

function renderKellyOrderExecutionRow(execution) {
  const item = execution && typeof execution === "object" ? execution : {};
  const symbol = firstPresent(
    item.futu_code,
    [item.market, item.symbol].filter(hasValue).map(formatPlain).join("."),
    item.symbol,
    "-",
  );
  const symbolCell = `
    <strong>${escapeHtml(formatPlain(symbol))}</strong>
    ${hasValue(item.executed_at) ? `<small>${escapeHtml(formatPlain(item.executed_at))}</small>` : ""}
  `;
  const cells = [
    symbolCell,
    escapeHtml(kellyOrderSideLabel(item.side)),
    escapeHtml(formatDisplayNumber(item.price)),
    escapeHtml(formatDisplayNumber(item.qty)),
    escapeHtml(formatDisplayNumber(item.planned_notional)),
    escapeHtml(formatPlain(item.futu_order_id || "-")),
    escapeHtml(kellyExecutionStatusLabel(item.execution_status)),
    escapeHtml(formatPlain(item.error || "-")),
  ];
  return `
    <div class="kelly-order-row" role="row">
      ${cells.map((cell) => `<span role="cell">${cell}</span>`).join("")}
    </div>
  `;
}

function kellyOrderSideLabel(side) {
  const labels = {
    buy: "买入",
    sell: "卖出",
  };
  const key = formatPlain(side).toLowerCase();
  return labels[key] || firstPresent(side, "-");
}

function kellyExecutionStatusLabel(status) {
  const labels = {
    dry_run: "预演",
    failed: "执行失败",
    skipped: "已跳过",
    submitted: "已提交",
  };
  const key = formatPlain(status).toLowerCase();
  return labels[key] || firstPresent(status, "-");
}

function kellyOrderStatusLabel(status) {
  const labels = {
    cancelled: "已撤单",
    failed: "失败",
    filled: "已成交",
    partial_filled: "部分成交",
    pending: "待成交",
    rejected: "拒单",
    submitted: "待成交",
  };
  const key = formatPlain(status).toLowerCase();
  return labels[key] || firstPresent(status, "-");
}

function kellyOrderExecutionStatus(status) {
  const labels = {
    failed: { label: "执行失败", className: "status-failed" },
    partial: { label: "部分执行", className: "status-partial" },
    running: { label: "执行中", className: "status-partial" },
    success: { label: "执行成功", className: "status-ok" },
  };
  const key = formatPlain(status).toLowerCase();
  return labels[key] || { label: firstPresent(status, "未执行"), className: "status-muted" };
}

function kellyOrderSyncStatus(status) {
  const labels = {
    failed: { label: "同步失败", className: "status-failed" },
    ok: { label: "同步成功", className: "status-ok" },
    partial: { label: "部分同步", className: "status-partial" },
    running: { label: "同步中", className: "status-partial" },
    stale: { label: "同步过期", className: "status-stale" },
    success: { label: "同步成功", className: "status-ok" },
  };
  const key = formatPlain(status).toLowerCase();
  return labels[key] || { label: firstPresent(status, "未同步"), className: "status-muted" };
}

function renderKellySymbolState(sample) {
  const item = sample && typeof sample === "object" ? sample : {};
  const status = kellyLifecycleStatus(item.status);
  const symbol = [item.market, item.symbol]
    .filter(hasValue)
    .map(formatPlain)
    .join(".");
  return `
    <article class="kelly-symbol-state-card">
      <div class="kelly-symbol-state-heading">
        <strong>${escapeHtml(firstPresent(symbol, item.symbol, "未命名标的"))}</strong>
        <span class="status-pill ${escapeHtml(status.className)}">${escapeHtml(status.label)}</span>
      </div>
      <p>${escapeHtml(formatPlain(item.reason))}</p>
      <dl>
        <div>
          <dt>状态含义</dt>
          <dd>${escapeHtml(formatPlain(status.meaning))}</dd>
        </div>
        <div>
          <dt>系统动作</dt>
          <dd>${escapeHtml(firstPresent(item.action, status.systemAction))}</dd>
        </div>
        <div>
          <dt>下一步</dt>
          <dd>${escapeHtml(formatPlain(status.nextStep))}</dd>
        </div>
      </dl>
      ${hasValue(item.updated_at) ? `<small>${escapeHtml(formatPlain(item.updated_at))}</small>` : ""}
    </article>
  `;
}

function kellyLifecycleStatus(status) {
  const key = formatPlain(status);
  return KELLY_LIFECYCLE_STATUSES.find((item) => item.key === key)
    || { label: key, className: "status-muted" };
}

function renderKellyStrategyCapital(experiment) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const capital = entry.capital && typeof entry.capital === "object" ? entry.capital : null;
  if (!capital || capital.available === false) {
    return `
      <section class="kelly-strategy-capital unavailable" aria-label="Kelly 策略资金">
        <div class="kelly-strategy-capital-header">
          <div>
            <h4>策略资金</h4>
            <p>策略资金数据暂不可用。</p>
          </div>
        </div>
      </section>
    `;
  }

  const currency = formatPlain(firstPresent(capital.currency, entry.budget_currency, "USD"));
  const positionWidth = capitalSegmentWidth(capital.position_notional, capital.budget);
  const reservedWidth = capitalSegmentWidth(capital.reserved_order_notional, capital.budget, positionWidth);
  const utilization = hasValue(capital.utilization_pct)
    ? `${formatPlain(capital.utilization_pct)}%`
    : "";
  const metrics = [
    ["总资金", formatCapitalMoney(capital.budget, currency), ""],
    ["已占用", formatCapitalMoney(capital.occupied_notional, currency), ""],
    ["可用资金", formatCapitalMoney(capital.available_notional, currency), "primary"],
    ["占用率", firstPresent(utilization, "-"), ""],
    ["未完成买单", formatDisplayNumber(capital.open_buy_order_count), ""],
    ["已实现盈亏", formatSignedMoney(capital.realized_pnl, currency), pnlClass(capital.realized_pnl)],
  ];
  return `
    <section class="kelly-strategy-capital" aria-label="Kelly 策略资金">
      <div class="kelly-strategy-capital-header">
        <div>
          <h4>策略资金</h4>
          ${hasValue(capital.updated_at) ? `<p>更新于 ${escapeHtml(formatPlain(capital.updated_at))}</p>` : ""}
        </div>
      </div>
      <dl class="kelly-capital-metric-grid">
        ${metrics.map(([label, value, className]) => `
          <div${className ? ` class="${escapeHtml(className)}"` : ""}>
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(formatPlain(value))}</dd>
          </div>
        `).join("")}
      </dl>
      <div class="kelly-capital-utilization-bar" aria-label="Kelly 资金占用率">
        <span class="position" style="width: ${escapeHtml(formatPlain(positionWidth))}%"></span>
        <span class="reserved" style="width: ${escapeHtml(formatPlain(reservedWidth))}%"></span>
      </div>
      <div class="kelly-capital-breakdown-grid">
        ${renderKellyCapitalBreakdownPane(capital, currency)}
        ${renderKellyCapitalSymbolPane(capital, currency)}
        ${renderKellyCapitalNextOrderPane(capital, currency)}
      </div>
    </section>
  `;
}

function renderKellyCapitalBreakdownPane(capital, currency) {
  const rows = [
    ["持仓占用", formatCapitalMoney(capital.position_notional, currency)],
    ["待成交买单", formatCapitalMoney(capital.reserved_order_notional, currency)],
    ["保守口径 / 买单提交即占用", "已启用"],
  ];
  return `
    <div class="kelly-capital-pane">
      <h5>占用拆分</h5>
      ${rows.map(([label, value]) => renderKellyCapitalLine(label, value)).join("")}
    </div>
  `;
}

function renderKellyCapitalSymbolPane(capital, currency) {
  const occupancy = kellyCapitalSymbolOccupancy(capital.symbol_occupancy);
  const lines = occupancy.length
    ? occupancy.map((item) => {
      const symbol = kellyCapitalSymbol(item);
      const value = firstPresent(item.occupied_notional, item.notional, item.value);
      return renderKellyCapitalLine(symbol, formatCapitalMoney(value, currency));
    }).join("")
    : renderKellyCapitalLine("标的", "暂无占用");
  return `
    <div class="kelly-capital-pane">
      <h5>标的占用</h5>
      ${lines}
    </div>
  `;
}

function renderKellyCapitalNextOrderPane(capital, currency) {
  const impact = capital.next_order_impact && typeof capital.next_order_impact === "object"
    ? capital.next_order_impact
    : null;
  if (!impact) {
    return `
      <div class="kelly-capital-pane">
        <h5>下一笔下单影响</h5>
        ${renderKellyCapitalLine("状态", "暂无待评估订单")}
      </div>
    `;
  }
  const status = kellyCapitalRiskStatus(impact.risk_status);
  const rows = [
    ["标的", kellyCapitalSymbol(impact)],
    ["预计金额", formatCapitalMoney(impact.estimated_notional, currency)],
    ["下单后可用", formatCapitalMoney(impact.available_after_order, currency)],
    ["风控", status],
  ];
  if (hasValue(impact.reason)) {
    rows.push(["原因", impact.reason]);
  }
  return `
    <div class="kelly-capital-pane">
      <h5>下一笔下单影响</h5>
      ${rows.map(([label, value]) => renderKellyCapitalLine(label, value)).join("")}
    </div>
  `;
}

function kellyCapitalSymbolOccupancy(value) {
  if (Array.isArray(value)) {
    return value.filter((item) => item && typeof item === "object");
  }
  if (value && typeof value === "object") {
    return Object.entries(value).map(([symbol, notional]) => {
      if (notional && typeof notional === "object") {
        return { symbol, ...notional };
      }
      return { symbol, occupied_notional: notional };
    });
  }
  return [];
}

function kellyCapitalSymbol(value) {
  const item = value && typeof value === "object" ? value : {};
  const rawSymbol = firstPresent(item.symbol, item.code);
  const formattedSymbol = hasValue(rawSymbol) ? formatPlain(rawSymbol) : "";
  const marketSymbol = formattedSymbol.includes(".")
    ? formattedSymbol
    : [item.market, formattedSymbol]
    .filter(hasValue)
    .map(formatPlain)
    .join(".");
  return firstPresent(item.futu_code, marketSymbol, formattedSymbol, "-");
}

function renderKellyCapitalLine(label, value) {
  return `
    <div class="kelly-capital-line">
      <span>${escapeHtml(formatPlain(label))}</span>
      <strong>${escapeHtml(formatPlain(value))}</strong>
    </div>
  `;
}

function kellyCapitalRiskStatus(status) {
  const key = formatPlain(status).toLowerCase();
  if (key === "approved" || key === "ok" || key === "pass") {
    return "资金足够";
  }
  if (key === "blocked" || key === "failed" || key === "rejected") {
    return "资金不足";
  }
  return firstPresent(status, "-");
}

function formatCapitalMoney(value, currency) {
  if (!hasValue(value)) {
    return "-";
  }
  return formatMoney(value, currency);
}

function capitalSegmentWidth(value, budget, offset = 0) {
  const amount = Number.parseFloat(String(value || "").replace(/,/g, ""));
  const total = Number.parseFloat(String(budget || "").replace(/,/g, ""));
  const used = Number.parseFloat(String(offset || ""));
  if (!Number.isFinite(amount) || !Number.isFinite(total) || total <= 0) {
    return 0;
  }
  const raw = Math.min(100, Math.max(0, (amount / total) * 100));
  return Math.min(raw, Math.max(0, 100 - (Number.isFinite(used) ? used : 0)));
}

function renderKellyExperimentCard(experiment) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const template = entry.template && typeof entry.template === "object" ? entry.template : {};
  const stats = entry.stats && typeof entry.stats === "object" ? entry.stats : {};
  const name = firstPresent(entry.experiment_name, entry.experiment_id, "未命名实验");
  const status = kellyExperimentStatusLabel(entry.status);
  const stage = kellySampleStageLabel(stats.sample_stage);
  const strategyId = formatPlain(template.strategy_id);
  const strategyName = formatPlain(template.strategy_name);
  const strategyVersion = hasValue(template.strategy_version) ? ` · ${formatPlain(template.strategy_version)}` : "";
  const ruleDescriptions = kellyStrategyRuleDescriptions(template);
  const entrySummary = firstPresent(ruleDescriptions.entry, template.entry_rule_description);
  const budget = hasValue(entry.experiment_budget)
    ? `${formatMoney(entry.experiment_budget, formatPlain(entry.budget_currency || "USD"))}`
    : "-";
  const pool = kellyMarketCapitalPool(entry);
  const metricRows = [
    ["市场", entry.market],
    ["模拟资金池", pool],
    ["阶段", stage],
    ["已完成", formatDisplayNumber(stats.completed_samples)],
    ["进行中", formatDisplayNumber(stats.open_samples)],
    ["胜率", stats.observed_win_rate],
    ["预算", budget],
    ["资金使用", hasValue(entry.capital_utilization_pct) ? `${formatPlain(entry.capital_utilization_pct)}%` : ""],
  ];
  return `
    <article class="kelly-experiment-card">
      <header class="kelly-experiment-card-header">
        <div>
          <h3>${escapeHtml(formatPlain(name))}</h3>
          <span>${escapeHtml(strategyId)} · ${escapeHtml(strategyName)}${escapeHtml(strategyVersion)}</span>
        </div>
        <span class="status-pill ${escapeHtml(status.className)}">${escapeHtml(status.label)}</span>
      </header>
      <p class="kelly-entry-rule">${escapeHtml(formatPlain(entrySummary))}</p>
      ${renderKellyStrategyCapital(entry)}
      ${renderKellyOrderSync(entry)}
      ${renderKellyOrderExecution(entry)}
      ${renderKellyStrategyRules(template, ruleDescriptions)}
      <dl class="kelly-stat-grid">
        ${metricRows.map(([label, value]) => `
          <div>
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(formatPlain(value))}</dd>
          </div>
        `).join("")}
      </dl>
      ${renderKellyParameterDerivation(stats)}
      ${renderKellySymbolStates(entry)}
    </article>
  `;
}

function kellyMarketCapitalPool(experiment) {
  const entry = experiment && typeof experiment === "object" ? experiment : {};
  const pool = entry.market_capital_pool && typeof entry.market_capital_pool === "object"
    ? entry.market_capital_pool
    : {};
  if (pool.enabled === false) {
    return "未启用";
  }
  const currency = firstPresent(pool.currency, entry.budget_currency);
  const amount = firstPresent(pool.amount, entry.experiment_budget);
  return hasValue(currency) && hasValue(amount) ? `${formatPlain(currency)} ${formatDisplayNumber(amount)}` : "";
}

function renderKellyStrategyRules(template, ruleDescriptions) {
  const item = template && typeof template === "object" ? template : {};
  const generated = ruleDescriptions && typeof ruleDescriptions === "object" ? ruleDescriptions : {};
  const ruleRows = [
    ["入场", firstPresent(generated.entry, item.entry_rule_description)],
    ["止损", firstPresent(generated.stopLoss, item.stop_loss_rule_description)],
    ["止盈", firstPresent(generated.takeProfit, item.take_profit_rule_description)],
    ["移动止盈", firstPresent(generated.trailingStop, item.trailing_stop_rule_description)],
    ["时间退出", firstPresent(generated.timeExit, item.time_exit_rule_description)],
  ];
  if (
    !hasValue(generated.stopLoss)
    && !hasValue(item.stop_loss_rule_description)
    && !hasValue(generated.takeProfit)
    && !hasValue(item.take_profit_rule_description)
  ) {
    ruleRows.push(["退出", item.exit_rule_description]);
  }
  const visibleRows = ruleRows.filter(([, value]) => hasValue(value));
  if (!visibleRows.length) {
    return "";
  }

  return `
    <section class="kelly-strategy-rules" aria-label="Kelly 策略详情">
      <h4>策略详情</h4>
      <div class="kelly-rule-grid">
        ${visibleRows.map(([label, value]) => `
          <div>
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(formatPlain(value))}</strong>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function kellyStrategyRuleDescriptions(template) {
  const rules = template && template.rules && typeof template.rules === "object"
    ? template.rules
    : {};
  return {
    entry: describeKellyRule(rules.entry, "entry"),
    stopLoss: describeKellyRule(rules.stop_loss, "stop_loss"),
    takeProfit: describeKellyRule(rules.take_profit, "take_profit"),
    trailingStop: describeKellyRule(rules.trailing_stop, "trailing_stop"),
    timeExit: describeKellyRule(rules.time_exit, "time_exit"),
  };
}

function describeKellyRule(rule, slot) {
  const item = rule && typeof rule === "object" ? rule : {};
  const type = formatPlain(item.type);
  if (slot === "entry" && type === "pullback_to_moving_average") {
    const trend = item.trend_filter && typeof item.trend_filter === "object" ? item.trend_filter : {};
    const direction = trend.direction === "up" ? "向上" : formatPlain(trend.direction);
    const trendText = hasValue(trend.ma_days)
      ? `，且 ${formatPlain(trend.ma_days)} 日均线斜率${direction}`
      : "";
    const tolerance = hasValue(item.tolerance_pct) ? ` ±${formatPlain(item.tolerance_pct)}% 内` : "附近";
    return `价格回调到 ${formatPlain(item.ma_days)} 日均线${tolerance}${trendText}。`;
  }
  if (slot === "entry" && type === "volume_breakout_high") {
    const volumeText = hasValue(item.volume_multiple)
      ? `，成交量不低于 ${formatPlain(item.volume_multiple)} 倍均量`
      : "";
    return `价格放量突破近 ${formatPlain(item.lookback_days)} 个交易日高点${volumeText}。`;
  }
  if (slot === "stop_loss" && ["any_of", "min_of"].includes(type)) {
    const children = Array.isArray(item.rules) ? item.rules : [];
    const parts = children
      .map((child) => describeKellyRuleFragment(child, "stop_loss"))
      .filter(hasValue);
    return parts.length ? `${parts.join(" 或")}。` : "";
  }
  if (slot === "take_profit" && type === "risk_multiple") {
    return `价格达到入场价 + ${formatPlain(item.trigger_r)}R 时卖出 ${formatPlain(item.sell_pct)}%。`;
  }
  if (slot === "trailing_stop" && type === "close_below_moving_average") {
    return `剩余仓位收盘跌破 ${formatPlain(item.ma_days)} 日均线时退出。`;
  }
  if (slot === "trailing_stop" && type === "close_below_recent_low") {
    return `剩余仓位收盘跌破最近 ${formatPlain(item.lookback_days)} 日最低价时退出。`;
  }
  if (slot === "time_exit" && type === "max_holding_days") {
    if (item.exit_if === "no_take_profit_or_stop_loss") {
      return `持有满 ${formatPlain(item.days)} 个交易日仍未触发止盈或止损则退出。`;
    }
    if (item.exit_if === "minimum_unrealized_r_not_reached") {
      return `持有满 ${formatPlain(item.days)} 个交易日仍未达到 ${formatPlain(item.min_unrealized_r)}R 浮盈则退出。`;
    }
    return `持有满 ${formatPlain(item.days)} 个交易日则退出。`;
  }
  return "";
}

function describeKellyRuleFragment(rule, slot) {
  const item = rule && typeof rule === "object" ? rule : {};
  const type = formatPlain(item.type);
  if (slot === "stop_loss" && type === "pct_below_moving_average") {
    return `跌破 ${formatPlain(item.ma_days)} 日均线 ${formatPlain(item.pct)}%`;
  }
  if (slot === "stop_loss" && type === "recent_swing_low_break") {
    return "跌破最近波段低点";
  }
  if (slot === "stop_loss" && type === "pct_below_reference_price") {
    return `跌回${formatPlain(item.reference || "参考价")}下方 ${formatPlain(item.pct)}%`;
  }
  if (slot === "stop_loss" && type === "atr_below_entry") {
    return `跌破入场价 - ${formatPlain(item.atr_multiple)} ATR`;
  }
  return "";
}

function renderKellyParameterDerivation(stats) {
  const item = stats && typeof stats === "object" ? stats : {};
  const sampleStageLabel = item.sample_stage === "sufficient"
    ? "样本充足"
    : item.sample_stage === "insufficient"
      ? "样本不足"
      : item.sample_stage;
  const hasDerivation = [
    item.sample_stage,
    item.completed_samples,
    item.open_samples,
    item.raw_win_rate,
    item.adjusted_win_rate,
    item.payoff_ratio,
    item.full_kelly_pct,
    item.fractional_kelly_pct,
    item.suggested_position_pct,
    item.sample_adjustment,
    item.parameter_source,
    item.skipped_order_count,
    item.source_trade_samples_generated_at,
    item.last_sample_closed_at,
    item.last_recomputed_at,
  ].some(hasValue);
  if (!hasDerivation) {
    return "";
  }

  const winLossCount = hasValue(item.winning_samples) || hasValue(item.losing_samples)
    ? `${formatDisplayNumber(item.winning_samples)} 赢 / ${formatDisplayNumber(item.losing_samples)} 亏`
    : "";
  const payoffRatio = hasValue(item.payoff_ratio) ? formatDisplayNumber(item.payoff_ratio) : "";
  const payoffDetail = [item.avg_net_win_pct, item.avg_net_loss_pct]
    .filter(hasValue)
    .map(formatSignedPnl)
    .join(" / ");
  const sourceLabel = item.parameter_source === "futu_paper_order_samples"
    ? "富途模拟盘订单样本"
    : item.parameter_source;
  const rows = [
    ["样本状态", sampleStageLabel],
    ["已完成样本", hasValue(item.completed_samples) ? formatDisplayNumber(item.completed_samples) : item.completed_samples],
    ["进行中样本", hasValue(item.open_samples) ? formatDisplayNumber(item.open_samples) : item.open_samples],
    ["原始胜率", [item.raw_win_rate, winLossCount].filter(hasValue).map(formatPlain).join(" · ")],
    ["修正胜率", [item.adjusted_win_rate, item.sample_adjustment].filter(hasValue).map(formatPlain).join(" · ")],
    ["盈亏比 b", [payoffRatio, payoffDetail].filter(hasValue).map(formatPlain).join(" · ")],
    ["Full Kelly", item.full_kelly_pct],
    ["保守 Kelly", item.fractional_kelly_pct],
    ["建议仓位", item.suggested_position_pct],
    ["参数来源", sourceLabel],
    ["跳过订单", hasValue(item.skipped_order_count) ? formatDisplayNumber(item.skipped_order_count) : item.skipped_order_count],
    ["来源样本时间", item.source_trade_samples_generated_at],
    ["最近完成样本", item.last_sample_closed_at],
    ["最近计算", item.last_recomputed_at],
  ].filter(([, value]) => hasValue(value));

  return `
    <section class="kelly-derivation" aria-label="Kelly 参数推导">
      <h4>参数推导</h4>
      <div class="kelly-derivation-grid">
        ${rows.map(([label, value]) => `
          <div>
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(formatPlain(value))}</strong>
          </div>
        `).join("")}
      </div>
      <p>f* = p - (1 - p) / b</p>
    </section>
  `;
}

let predictionModal = {kind: "", previousFocus: null, busy: false, data: null};

function predictionValue(value, fallback = "-") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function predictionHktTimestamp(value, fallback = "-") {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return predictionValue(value, fallback);
  const formatted = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Hong_Kong",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date).replace(/\//g, "-");
  return `${formatted} HKT`;
}

function predictionRelativeAge(value, now = Date.now()) {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "";
  const seconds = Math.max(0, Math.floor((Number(now) - timestamp) / 1000));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时前`;
}

function predictionClock(label, value, {danger = false} = {}) {
  const timestamp = predictionHktTimestamp(value);
  const age = predictionRelativeAge(value);
  const suffix = age ? `（${age}）` : "";
  return `<span class="pm-clock${danger ? " pm-clock-danger" : ""}"><span>${escapeHtml(label)}：${escapeHtml(timestamp)}</span>${suffix ? `<small>${escapeHtml(suffix)}</small>` : ""}</span>`;
}

function predictionNumber(value, fallback = "-") {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("en-US", {maximumFractionDigits: 2}) : fallback;
}

function predictionMoney(value, fallback = "-") {
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : (String(value || fallback));
}

function predictionPrice(value, fallback = "-") {
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toFixed(3)}` : (String(value || fallback));
}

function predictionSignedMoney(value, fallback = "-") {
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? "+" : "-"}$${Math.abs(number).toFixed(2)}` : fallback;
}

function predictionVolume(value, fallback = "-") {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value || fallback);
  if (Math.abs(number) >= 1000000) return `$${(number / 1000000).toFixed(number % 1000000 ? 1 : 0)}M`;
  if (Math.abs(number) >= 1000) return `$${(number / 1000).toFixed(number % 1000 ? 1 : 0)}K`;
  return `$${number.toFixed(2)}`;
}

function predictionTone(value) {
  const text = String(value || "").toLowerCase();
  if (text.includes("fail") || text.includes("error") || text.includes("block") || text.includes("lock") || text.includes("禁止") || text.includes("事故") || text.includes("熔断")) return "pm-tone-danger";
  if (text.includes("warn") || text.includes("check") || text.includes("执行") || text.includes("等待") || text.includes("待")) return "pm-tone-warning";
  return "pm-tone-ok";
}

function predictionReasonLabel(value) {
  const raw = String(value || "").trim();
  if (!raw) return "暂不可参与";
  const labels = {
    neg_risk: "已订阅 · Negative Risk 暂不可参与",
    fee_unverified_or_enabled: "已订阅 · 收费市场不可参与",
    readiness_stale: "交易检查未完成",
    readiness_unavailable: "交易账户检查不可用",
    book_stale: "盘口过期，等待更新",
    remediation_unsafe: "安全补救路径未通过",
    insufficient_funds: "余额或授权不足",
    llm_rejected: "LLM 校验拒绝",
    deterministic_rejected: "确定性规则校验拒绝",
    llm_unavailable: "LLM 校验不可用",
    no_threshold_candidate: "已订阅 · 净利润未达到策略门槛",
    monitor_degraded: "数据连接异常，暂不可参与",
    opportunity_unavailable: "机会已变化或已失效",
    annualized_yield_below_minimum: "年化低于 15% 入场门槛",
    annualized_yield_unavailable: "年化无法计算，禁止入场",
    operator_paused: "操作员已暂停自动下单",
    notification_delivery_failed: "通知发送失败，已暂停自动下单",
    not_armed: "自动下单未启用",
  };
  return labels[raw] || raw.replaceAll("_", " ");
}

function predictionIncidentReasonLabel(value) {
  const raw = String(value || "").trim();
  const labels = {
    one_leg_fill: "YES 成交、NO 被拒",
    no_safe_remediation: "未找到安全处置方案",
    remediation_unverified: "自动处置未确认",
    remediation_reconciliation_unverified: "处置后持仓未确认归零",
    remediation_merge_not_confirmed: "处置后的合并未确认",
    startup_open_orders: "重启时发现未完成订单",
    startup_directional_imbalance: "重启时发现单腿持仓",
    merge_not_confirmed: "合并未确认",
  };
  if (raw === "cross_dust") return "残余头寸（dust incident）";
  if (raw === "cross_circuit_breaker_open") return "跨所熔断";
  return labels[raw] || predictionReasonLabel(raw);
}

function predictionIncidentStatusLabel(value) {
  const raw = String(value || "").trim();
  const labels = {
    resolved_clean: "已确认无敞口 · 已解除熔断",
    directional_incident: "已消除敞口 · 待解除熔断",
    neutralized_incident: "已处置 · 待解除熔断",
    merge_incident: "合并未确认 · 待人工处理",
    remediating: "自动处置中",
  };
  if (raw === "dust_incident") return "残余头寸待人工处理（dust incident）";
  return labels[raw] || raw;
}

function predictionGeoblockLabel(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (["allowed", "allow", "ok", "ready", "pass"].includes(raw)) return "允许交易";
  if (["blocked", "denied", "fail", "failed", "error"].includes(raw)) return "检查失败";
  return value;
}

function predictionHealthIsNormal(payload) {
  return payload?.stale !== true
    && String(payload?.health?.status || "").trim().toLowerCase() === "healthy";
}

function predictionWatcherIsConnected(payload) {
  const overall = String(payload?.status || payload?.health?.status || "").trim().toLowerCase();
  if (["unavailable", "unknown", "mystery", "error", "failed"].includes(overall)) return false;
  const websocket = payload?.relation_discovery?.websocket;
  const status = String(websocket?.status || "").trim().toLowerCase();
  if (status) return status === "connected";
  const reasons = Array.isArray(payload?.health?.degraded_reasons) ? payload.health.degraded_reasons : [];
  return !reasons.some((reason) => ["heartbeat_missing", "heartbeat_stale", "stream_disconnected"].includes(String(reason)));
}

function predictionFailureReason(payload) {
  const reasons = payload?.health?.degraded_reasons;
  return payload?.failure_reason
    || (Array.isArray(reasons) ? reasons[0] : "")
    || payload?.readiness?.reason
    || "";
}

function predictionFailureReasonLabel(payload) {
  const raw = String(predictionFailureReason(payload) || "").trim();
  const labels = {
    configuration_unavailable: "预测市场配置不可用",
    heartbeat_missing: "盘口心跳尚未返回",
    heartbeat_stale: "盘口心跳已过期",
    stream_disconnected: "盘口数据连接已断开",
    universe_unavailable: "监控市场数据未返回",
    universe_stale: "监控市场数据已过期",
    universe_refresh_failed: "监控市场刷新失败",
    universe_retry_exhausted: "监控市场连续刷新失败，已停止自动重试",
    books_stale: "可参与盘口已过期",
    readiness_stale: "交易账户检查已过期",
    readiness_unavailable: "交易账户检查不可用",
    store_write_failed: "监控记录保存失败",
    api_key_pending: "Predict API Key 待分配",
    predict_unavailable: "Predict.fun 数据暂不可用",
    predict_degraded: "Predict.fun 数据连接降级",
    predict_construction_failed: "Predict.fun 监控初始化失败",
    predict_not_configured: "Predict.fun 尚未配置",
    cross_venue_unavailable: "跨交易所监控暂不可用",
    predict_stale: "Predict.fun 数据已过期",
    predict_auth_blocked: "Predict.fun API Key 认证受阻",
  };
  if (raw === "predict_account_unavailable") return "Predict.fun 账户状态不可用";
  return raw ? (labels[raw] || raw.replaceAll("_", " ")) : "状态详情未返回";
}

function predictionExecutionIsActive(payload) {
  const status = String(
    payload?.current_execution?.status || payload?.current_execution?.state || ""
  ).toLowerCase();
  return ["running", "executing", "pending", "submitted", "reconciling", "validating", "final_validating", "submitting", "merging"]
    .some((value) => status.includes(value));
}

function predictionHasValue(value) {
  return value !== null && value !== undefined && String(value).trim() !== "";
}

function predictionCanonicalUtcCutoff(value) {
  const match = typeof value === "string"
    ? /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$/.exec(value)
    : null;
  if (!match) return null;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return null;
  const date = new Date(timestamp);
  const fraction = match[7] || "";
  const milliseconds = Number(fraction.slice(0, 3).padEnd(3, "0") || "0");
  return date.getUTCFullYear() === Number(match[1])
    && date.getUTCMonth() + 1 === Number(match[2])
    && date.getUTCDate() === Number(match[3])
    && date.getUTCHours() === Number(match[4])
    && date.getUTCMinutes() === Number(match[5])
    && date.getUTCSeconds() === Number(match[6])
    && date.getUTCMilliseconds() === milliseconds
    ? timestamp
    : null;
}

function predictionFutureCanonicalUtcCutoff(value) {
  const timestamp = predictionCanonicalUtcCutoff(value);
  return timestamp !== null && timestamp > Date.now();
}

function predictionPolicyIsComplete(policy) {
  return ["max_wallet_balance", "max_normal_cost", "max_emergency_loss", "min_estimated_profit"]
    .every((key) => predictionHasValue(policy?.[key]) && Number.isFinite(Number(policy[key])));
}

function predictionTradingAvailable(payload, strategy = "yes_no") {
  strategy = strategy === "llm_hedge" ? "llm_hedge" : "yes_no";
  const readiness = payload?.readiness || {};
  const geoblock = String(readiness.geoblock || readiness.region || readiness.region_status || "").toLowerCase();
  const relayer = String(readiness.relayer || readiness.relayer_readiness || "").toLowerCase();
  const blockedReadiness = ["unavailable", "blocked", "fail", "failed", "error"]
    .includes(String(readiness.status || "").toLowerCase());
  const relation = payload?.relation_discovery || {};
  const degradedReasons = Array.isArray(payload?.health?.degraded_reasons)
    ? payload.health.degraded_reasons.map((reason) => String(reason || ""))
    : [];
  const topTwentyOnlyReasons = new Set([
    "books_stale",
    "universe_unavailable",
    "universe_stale",
    "universe_refresh_failed",
    "universe_retry_exhausted",
  ]);
  const hasCriticalDegradation = degradedReasons.some(
    (reason) => !topTwentyOnlyReasons.has(reason)
  );
  const topTwentyOnlyDegradation = degradedReasons.length > 0
    && degradedReasons.every((reason) => topTwentyOnlyReasons.has(reason));
  const llmGlobalHealthSafe = !hasCriticalDegradation
    && (predictionHealthIsNormal(payload) || topTwentyOnlyDegradation);
  const strategyHealthy = strategy === "llm_hedge"
    ? llmGlobalHealthSafe
      && predictionWatcherIsConnected(payload)
      && String(relation.status || "").toLowerCase() === "healthy"
      && String(relation.catalog?.status || "").toLowerCase() === "healthy"
    : predictionHealthIsNormal(payload);
  return strategyHealthy
    && !blockedReadiness
    && ["allowed", "allow", "ok", "ready", "pass"].includes(geoblock)
    && ["allowed", "allow", "ok", "ready", "pass"].includes(relayer)
    && predictionHasValue(readiness.p_usd_balance ?? readiness.balance ?? payload?.balances?.p_usd)
    && predictionHasValue(
      readiness.masked_address
        || payload?.wallet?.masked_address
        || payload?.masked_wallet
        || readiness.wallet_address
    )
    && predictionPolicyIsComplete(payload?.policy_limits)
    && payload?.breaker?.open === false
    && !predictionExecutionIsActive(payload);
}

function predictionMarketTypeLabel(value) {
  const raw = String(value || "").trim();
  if (raw === "threshold_hedge") return "阈值关系套利";
  return raw === "standard_binary" ? "普通二元" : raw || "-";
}

function predictionFeeStatusLabel(value) {
  const raw = String(value || "").trim();
  return raw === "fee_free" ? "免手续费" : raw || "-";
}

function predictionStatusLabel(payload) {
  return predictionWatcherIsConnected(payload)
    ? ["Watcher 正常", "ok"]
    : ["Watcher 不可用", "danger"];
}

function predictionOpportunityDisplay(value) {
  const source = value && typeof value === "object" ? value : {};
  const result = {...source};
  result.opportunity_id = source.opportunity_id ?? source.id ?? source.opportunityId;
  result.title = source.title ?? source.question ?? source.market_title ?? source.event_title ?? "数据未返回";
  result.yes_price = source.yes_price ?? source.yes_max_price ?? source.yes_best_bid;
  result.no_price = source.no_price ?? source.no_max_price ?? source.no_best_bid;
  result.yes_cost = source.yes_cost ?? source.yes_max_cost;
  result.no_cost = source.no_cost ?? source.no_max_cost;
  result.max_cost = source.max_cost ?? source.total_max_cost ?? source.cost;
  result.profit = source.profit ?? source.minimum_profit ?? source.estimated_profit ?? source.gross_upper_bound;
  result.quantity = source.quantity ?? source.size;
  if (String(result.market_type || "") === "threshold_hedge") {
    result.question_a = source.question_a ?? source.market_a_question;
    result.question_b = source.question_b ?? source.market_b_question;
    result.condition_id_a = source.condition_id_a ?? source.conditionA;
    result.condition_id_b = source.condition_id_b ?? source.conditionB;
    result.buy_legs = Array.isArray(source.buy_legs) ? source.buy_legs : [];
    result.llm_status = source.llm_status;
    result.llm_decision = source.llm_decision;
    result.llm_summary = source.llm_summary;
    result.llm_reason_codes = source.llm_reason_codes;
    result.llm_evidence = source.llm_evidence;
    result.llm_uncertainties = source.llm_uncertainties;
  }
  return result;
}

function predictionOpportunityIsComplete(value) {
  const opportunity = predictionOpportunityDisplay(value);
  if (predictionIsCrossVenue(opportunity)) {
    const legs = Array.isArray(opportunity.legs) ? opportunity.legs : [];
    const exchanges = new Set(legs.map((leg) => String(leg?.exchange || "").toLowerCase()));
    return [
      opportunity.opportunity_id,
      opportunity.title,
      opportunity.market_type,
      opportunity.quantity,
      opportunity.net_quantity,
      opportunity.max_cost,
      opportunity.minimum_payout,
      opportunity.profit,
      opportunity.annualized_yield,
      opportunity.canonical_cutoff,
    ].every(predictionHasValue)
      && predictionFutureCanonicalUtcCutoff(opportunity.canonical_cutoff)
      && legs.length === 2
      && exchanges.size === 2
      && exchanges.has("predict.fun")
      && exchanges.has("polymarket")
      && legs.every((leg) => [leg?.exchange, leg?.outcome, leg?.token_id, leg?.settlement_asset, leg?.net_quantity, leg?.max_price, leg?.max_cost]
        .every(predictionHasValue));
  }
  if (String(opportunity.market_type || "") === "threshold_hedge") {
    const legs = Array.isArray(opportunity.buy_legs) ? opportunity.buy_legs : [];
    return [
      opportunity.opportunity_id,
      opportunity.question_a,
      opportunity.question_b,
      opportunity.relation,
      opportunity.condition_id_a,
      opportunity.condition_id_b,
    ].every(predictionHasValue)
      && predictionHasValue(opportunity.quantity)
      && predictionHasValue(opportunity.max_cost)
      && predictionHasValue(opportunity.profit)
      && legs.length === 2
      && legs.every((leg) => [leg?.label, leg?.outcome, leg?.condition_id, leg?.token_id, leg?.quantity, leg?.max_price, leg?.max_cost]
        .every(predictionHasValue) && Number.isFinite(Number(leg.max_price)) && Number.isFinite(Number(leg.max_cost)));
  }
  const textFields = [
    opportunity.opportunity_id,
    opportunity.title,
    opportunity.market_type,
    opportunity.fee_status,
  ];
  const numericFields = [
    opportunity.yes_price,
    opportunity.no_price,
    opportunity.quantity,
    opportunity.max_cost,
    opportunity.profit,
  ];
  return textFields.every(predictionHasValue)
    && numericFields.every((item) => predictionHasValue(item) && Number.isFinite(Number(item)));
}

function predictionEventDisplay(value) {
  const source = value && typeof value === "object" ? value : {};
  const result = {...source};
  const rawOpportunities = Array.isArray(source.opportunities) ? source.opportunities : [];
  const rawMarkets = Array.isArray(source.markets) ? source.markets : [];
  const nested = (rawOpportunities.length ? rawOpportunities : rawMarkets)
    .filter((item) => item && typeof item === "object")
    .map(predictionOpportunityDisplay);
  result.title = source.title ?? source.question ?? source.event_title ?? source.market_title ?? nested[0]?.title ?? "数据未返回";
  result.volume_24h = source.volume_24h ?? source.volume24h ?? nested[0]?.volume_24h;
  result.market_count = source.market_count ?? (Array.isArray(source.markets) ? source.markets.length : source.markets);
  result.opportunities = nested;
  result.actionable = source.actionable === true || nested.some((item) => item.actionable === true);
  result.profit = source.profit ?? source.minimum_profit ?? source.gross_upper_bound ?? nested[0]?.profit;
  if (!Array.isArray(source.details) && nested.length) {
    result.details = nested.map((item) => [
      item.title || item.market_title || "数据未返回",
      item.actionable ? "可参与" : predictionReasonLabel(item.reason || item.eligibility_reason || item.status),
    ]);
  }
  return result;
}

function predictionEvents(payload) {
  const events = Array.isArray(payload?.events) ? payload.events.filter((item) => item && typeof item === "object") : [];
  const opportunities = Array.isArray(payload?.opportunities) ? payload.opportunities : [];
  if (events.length) {
    const knownEventIds = new Set(events.map((event) => String(event.event_id || event.id || "")));
    const crossEvents = opportunities.filter((opportunity) => (
      opportunity && typeof opportunity === "object"
      && predictionIsCrossVenue(opportunity)
      && !knownEventIds.has(String(opportunity.event_id || opportunity.opportunity_id || ""))
    )).map((opportunity, index) => ({
      event_id: opportunity.event_id || opportunity.opportunity_id || `cross-opportunity-${index}`,
      ...predictionEventDisplay({...predictionOpportunityDisplay(opportunity), opportunities: [opportunity]}),
    }));
    return [...events.map(predictionEventDisplay), ...crossEvents];
  }
  return opportunities.map((opportunity, index) => ({
    event_id: opportunity.event_id || opportunity.opportunity_id || `opportunity-${index}`,
    ...predictionEventDisplay({
      ...predictionOpportunityDisplay(opportunity),
      opportunities: [opportunity],
    }),
  }));
}

function predictionOpportunities(payload) {
  return (Array.isArray(payload?.opportunities) ? payload.opportunities : [])
    .filter((item) => item && typeof item === "object")
    .map(predictionOpportunityDisplay);
}

function predictionPageHeader(payload) {
  const [health, tone] = predictionStatusLabel(payload);
  const failure = predictionWatcherIsConnected(payload)
    ? ""
    : `<div class="pm-failure-reason">原因：${escapeHtml(predictionFailureReasonLabel(payload))}</div>`;
  const heartbeat = payload?.heartbeat_at || payload?.heartbeat;
  return `<header class="pm-page-head"><div><h1>预测市场套利</h1><p>先看监控范围和实盘状态，再决定是否参与套利信号。</p></div><div class="pm-updated"><span class="pm-status-line"><i class="pm-status-dot ${tone === "danger" ? "danger" : ""}"></i>${health}</span><br>${predictionClock("Watcher 数据时间", heartbeat)} · Polymarket${failure}</div></header>`;
}

function predictionStrategyTabs(strategy) {
  const selected = strategy === "llm_hedge" ? "llm_hedge" : "yes_no";
  return `<nav class="pm-strategy-tabs" aria-label="套利策略"><button type="button" data-prediction-strategy="yes_no" aria-pressed="${selected === "yes_no"}">YES/NO套利</button><button type="button" data-prediction-strategy="llm_hedge" aria-pressed="${selected === "llm_hedge"}">LLM对冲套利</button></nav>`;
}

function predictionAnnualizedPercent(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const scale = 10 ** digits;
  return `${(Math.trunc(number * 100 * scale) / scale).toFixed(digits)}%`;
}

function predictionReadinessStrip(payload, strategy = "yes_no") {
  const venues = Array.isArray(payload?.venues)
    ? payload.venues.filter((venue) => venue && typeof venue === "object")
    : [];
  if (venues.length) {
    const cards = venues.map((venue) => {
      const balance = venue.balance && typeof venue.balance === "object" ? venue.balance : {};
      const isPredict = String(venue.venue || "").toLowerCase() === "predict.fun";
      const venueName = isPredict ? "Predict Account" : "Polymarket";
      const wallet = predictionMaskedWallet(venue.wallet);
      const rest = predictionValue(venue.rest, "unavailable");
      const ws = predictionValue(venue.ws, "unavailable");
      const asset = predictionValue(balance.asset, "-");
      const amount = predictionHasValue(balance.value) ? predictionMoney(balance.value) : "-";
      const allowance = venue.allowance && typeof venue.allowance === "object" ? venue.allowance : {};
      const allowanceLine = isPredict && predictionHasValue(allowance.value)
        ? `<small>授权 ${escapeHtml(predictionMoney(allowance.value))} ${escapeHtml(predictionValue(allowance.asset, "USDT"))}${allowance.spender ? ` · spender ${escapeHtml(predictionMaskedWallet(allowance.spender))}` : ""}</small>`
        : "";
      const healthLabel = (value) => String(value).toLowerCase() === "unavailable" ? "不可用" : value;
      const detail = venue.reason
        ? `<small>原因：${escapeHtml(predictionFailureReasonLabel({failure_reason: venue.reason}))}</small>`
        : venue.last_success ? `<small>最近成功 ${escapeHtml(predictionValue(venue.last_success))}</small>` : "";
      return `<article class="pm-readiness-item pm-venue-card"><div class="pm-venue-card-title"><strong>${venueName}</strong><span class="pm-pill ${predictionTone(venue.mode)}">${escapeHtml(predictionValue(venue.mode, "只读"))}</span></div>${isPredict ? "<small>Predict.fun</small>" : ""}<div class="pm-venue-states"><span>REST：${escapeHtml(healthLabel(rest))}</span><span>WebSocket：${escapeHtml(healthLabel(ws))}</span></div><small>钱包 ${escapeHtml(wallet)}</small><small>可用余额 ${escapeHtml(amount)} ${escapeHtml(asset)}</small>${allowanceLine}${detail}</article>`;
    });
    const signer = payload?.privy_signer || payload?.signer;
    if (signer && typeof signer === "object") {
      const bnb = signer.bnb && typeof signer.bnb === "object" ? signer.bnb : {};
      cards.push(`<article class="pm-readiness-item pm-signer-card"><div class="pm-venue-card-title"><strong>Privy signer</strong><span class="pm-pill ${predictionTone(signer.mode)}">${escapeHtml(predictionValue(signer.mode, "只读"))}</span></div><div class="pm-venue-states"><span>BNB</span><span>当前 ${escapeHtml(predictionValue(bnb.current, "-"))} BNB</span></div><small>地址 ${escapeHtml(predictionValue(signer.address, "-"))}</small><small>需要 ${escapeHtml(predictionValue(bnb.required, "-"))} BNB · 最低保留 ${escapeHtml(predictionValue(bnb.minimum, "-"))} BNB</small></article>`);
    }
    return `<section class="pm-readiness pm-venue-readiness" aria-label="交易所连接与账户状态">${cards.join("")}</section>`;
  }
  const readiness = payload?.readiness || {};
  const balance = readiness.p_usd_balance ?? readiness.balance ?? payload?.balances?.p_usd;
  const wallet = readiness.masked_address || payload?.wallet?.masked_address || payload?.masked_wallet || readiness.wallet_address;
  const geoblock = predictionGeoblockLabel(readiness.geoblock || readiness.region || readiness.region_status);
  const policy = payload?.policy_limits || {};
  const policyReady = predictionPolicyIsComplete(policy);
  const available = predictionTradingAvailable(payload, strategy);
  const tradingNote = policyReady
    ? `单笔上限 ${predictionMoney(policy.max_normal_cost)} · 应急上限 ${predictionMoney(policy.max_emergency_loss)}`
    : "策略参数未返回";
  return `<section class="pm-readiness" aria-label="实盘就绪状态">
    <article class="pm-readiness-item"><span>交易钱包</span><strong>${escapeHtml(predictionValue(wallet, "-"))}</strong><small>独立低余额钱包 · Keychain</small></article>
    <article class="pm-readiness-item"><span>可用余额</span><strong>${escapeHtml(predictionMoney(balance, "-"))} pUSD</strong><small>不自动充值</small></article>
    <article class="pm-readiness-item"><span>地区与连接</span><strong class="${predictionTone(geoblock)}">${escapeHtml(predictionValue(geoblock, "-"))}</strong><small>官方 geoblock · 本机访问</small></article>
    <article class="pm-readiness-item"><span>实盘状态</span><strong class="${available ? "pm-tone-ok" : "pm-tone-danger"}">${available ? "可以交易" : "不可用"}</strong><small>${escapeHtml(tradingNote)}</small></article>
  </section>`;
}

function predictionSafeguardsHtml(payload) {
  const parts = [];
  const policy = payload?.policy_limits || {};
  if (predictionHasValue(policy.max_normal_cost)) {
    const first = String(policy.canary_status || "").toLowerCase() === "first_live_trade"
      || Number(policy.max_normal_cost) <= 5;
    parts.push(`<aside class="pm-policy pm-safeguard"><strong>${first ? "首单验证" : "常规上限"} · 单笔成本上限 ${escapeHtml(predictionMoney(policy.max_normal_cost))}</strong><p>上限只限制本次跨所订单，不改变未结算资金上限。</p></aside>`);
  }
  const signer = payload?.privy_signer || payload?.signer;
  const bnb = signer?.bnb && typeof signer.bnb === "object" ? signer.bnb : {};
  if (signer && String(signer.mode || "") === "只读") {
    const links = Array.isArray(signer.official_links)
      ? signer.official_links.map((link) => `<a href="${escapeHtml(predictionValue(link?.url, "#"))}" rel="noreferrer" target="_blank">${escapeHtml(predictionValue(link?.label, "官方链接"))}</a>`).join(" · ")
      : "";
    parts.push(`<section class="pm-alert warning pm-safeguard" role="status"><div class="pm-alert-body"><strong>Privy signer BNB 不足，只读</strong><p>当前 ${escapeHtml(predictionValue(bnb.current, "-"))} BNB · 需要 ${escapeHtml(predictionValue(bnb.required, "-"))} BNB · 最低保留 ${escapeHtml(predictionValue(bnb.minimum, "-"))} BNB</p><code class="pm-copy-text">${escapeHtml(predictionValue(signer.copy_text || signer.address, "-"))}</code>${links ? `<small>官方链接：${links}</small>` : ""}</div></section>`);
  }
  const cleanup = payload?.predict_allowance_cleanup;
  if (cleanup && typeof cleanup === "object") {
    parts.push(`<section class="pm-alert danger pm-safeguard" role="alert"><div class="pm-alert-body"><strong>Predict Account 残余授权</strong><p>残余授权 ${escapeHtml(predictionMoney(cleanup.before_allowance))} USDT；清零会消耗 Privy signer BNB，不转移 USDT。</p></div><button class="pm-button danger" type="button" data-action="open-allowance-cleanup">清理残余授权</button></section>`);
  }
  return parts.join("");
}

function predictionCrossVenueFunnel(payload) {
  const crossVenue = payload?.cross_venue && typeof payload.cross_venue === "object" ? payload.cross_venue : {};
  const funnel = crossVenue.funnel && typeof crossVenue.funnel === "object" ? crossVenue.funnel : {};
  const executed = predictionIsCrossVenue(payload?.current_execution) ? 1 : 0;
  const degraded = String(crossVenue.status || "").toLowerCase() !== "ready"
    && typeof crossVenue.discovery_error === "string"
    && crossVenue.discovery_error !== "";
  const stageNote = degraded ? "上次成功快照" : "";
  const warning = degraded
    ? `<div class="pm-funnel-empty pm-funnel-warning" role="alert"><strong>跨所发现链路失败</strong><p>${escapeHtml(crossVenue.discovery_error)} · 降级于 ${escapeHtml(predictionValue(crossVenue.stale_at))} · 保留上次成功快照</p></div>`
    : "";
  const empty = warning
    || (Number(funnel.monitored_pairs || 0) === 0
      ? `<div class="pm-funnel-empty"><strong>当前没有合格跨所市场</strong><p>扫描正常，没有失败；出现 stage-5 明确信号前不会开放按钮。</p></div>`
      : "");
  const statusPill = degraded ? `<span class="pm-pill watch">降级</span>` : "";
  return `<section class="pm-panel pm-relation-funnel pm-cross-venue-funnel" aria-label="跨所 YES/NO 漏斗"><header class="pm-funnel-header"><div><h2>跨所 YES/NO 漏斗</h2><p>只展示明确映射的两所标的；文字一致候选自动实时监控；确认前始终重新读取两所 REST 与账户事实。</p></div>${statusPill}</header><div class="pm-funnel-lane"><div class="pm-funnel-grid pm-funnel-grid-catalog">${predictionFunnelStage("正在监视", funnel.monitored_pairs, stageNote || "对应标的 " + predictionNumber(funnel.matched_pairs, "0") + " · 全部实时订阅", "live")}${predictionFunnelStage("正收益", funnel.arbitrage_space_pairs, stageNote || "实时盘口价差为正", "good")}${predictionFunnelStage("年化达标", funnel.clear_signal_pairs, stageNote || "≥ 15% · 可自动 / 可人工", "live")}${predictionFunnelStage("已提交", executed, stageNote || "自动执行 / 手动确认后", "good")}</div></div><div class="pm-funnel-meta"><span>对应标的 ${escapeHtml(predictionNumber(funnel.matched_pairs, "0"))}</span><span>准入来源（并行，非漏斗阶段）</span><span class="pm-funnel-chip">Codex 认为可以 ${escapeHtml(predictionNumber(funnel.codex_approved_pairs, "0"))} · 自动下单</span><span class="pm-funnel-chip">文字一致 ${escapeHtml(predictionNumber(funnel.manual_eligible_pairs, "0"))} · 人工下单</span>${funnel.retained_at ? `<span>保留时间 ${escapeHtml(predictionValue(funnel.retained_at))}</span>` : ""}</div>${empty}</section>`;
}

function predictionCrossVenueCandidates(payload) {
  const crossVenue = payload?.cross_venue && typeof payload.cross_venue === "object" ? payload.cross_venue : {};
  const crossAuto = payload?.cross_auto && typeof payload.cross_auto === "object" ? payload.cross_auto : {};
  const automaticMode = crossAuto.configured_mode === "auto_submit";
  const opportunities = Array.isArray(crossVenue.opportunities)
    ? crossVenue.opportunities
    : (Array.isArray(payload?.opportunities) ? payload.opportunities : []);
  const candidates = opportunities.filter((item) => (
    item && typeof item === "object"
    && item.actionable === true
    && predictionOpportunityIsComplete(item)
  ));
  const manual = candidates.filter((item) => (
    predictionCrossExecutionMode(item) === "manual_confirm" && item.manual_only === true
  ));
  const auto = candidates.filter((item) => predictionCrossExecutionMode(item) === "auto_submit");
  const card = (item, manualOnly) => {
    const approval = item.codex_approval && typeof item.codex_approval === "object" ? item.codex_approval : {};
    const reason = manualOnly
      ? `<div class="pm-alert warning"><div class="pm-alert-body"><strong>结算规则可能不一致</strong><p>${escapeHtml(predictionValue(item.manual_reason || approval.summary, "结构化理由未返回"))}</p></div></div>`
      : "";
    const action = manualOnly
      ? automaticMode
        ? `<span class="pm-relation-summary">需人工审查，自动模式不执行</span>`
        : `<button class="pm-button primary pm-participate" type="button" data-action="participate" data-opportunity-id="${escapeHtml(predictionValue(item.opportunity_id, ""))}">人工下单</button>`
      : `<span class="pm-relation-summary">系统判定文字与规则等价 · 无需批准，条件满足后自动执行</span>`;
    return `<article class="pm-manual-card"><div class="pm-manual-card-head"><div><div class="pm-manual-title">${escapeHtml(predictionValue(item.title || item.question, "数据未返回"))}</div><div class="pm-relation-summary"><span>Predict.fun × Polymarket</span><span>数据时间 ${escapeHtml(predictionValue(item.confirmed_at, "-"))}</span></div></div><span class="pm-pill ${manualOnly ? "watch" : "pm-tone-ok"}">${manualOnly ? "人工下单" : "自动下单"}</span></div><div class="pm-relation-summary"><span>年化 <strong>${escapeHtml(predictionAnnualizedPercent(item.annualized_yield, 2))}</strong></span><span>最小利润 <strong>${escapeHtml(predictionSignedMoney(item.profit))}</strong></span><span>总成本 <strong>${escapeHtml(predictionMoney(item.total_max_cost))}</strong></span><span>最坏损失 <strong>${escapeHtml(predictionMoney(item.total_max_cost))}</strong></span></div>${reason}<div style="display:flex;justify-content:flex-end">${action}</div></article>`;
  };
  return `<section class="pm-panel"><div class="pm-panel-heading"><div><h2>可下单候选</h2><p>只分两类：人工下单（文字一致 · 规则模糊）与自动下单（文字与规则系统判定）；其余候选留在漏斗计数中。</p></div></div><div style="margin-bottom:14px"><h3 style="margin:0 0 8px">人工下单 · 文字一致 / 规则模糊</h3><div style="display:grid;gap:10px">${manual.map((item) => card(item, true)).join("") || '<p class="pm-relation-summary">当前无</p>'}</div></div><div><h3 style="margin:0 0 8px">自动下单 · 文字与规则系统判定</h3><div style="display:grid;gap:10px">${auto.map((item) => card(item, false)).join("") || '<p class="pm-relation-summary">当前无</p>'}</div></div></section>`;
}

function predictionExecutionProgress(execution) {
  const status = String(execution?.status || execution?.state || "").toLowerCase();
  const crossVenue = predictionIsCrossVenue(execution);
  const reached = status.includes("merg") ? 4
    : status.includes("reconcil") ? 3
      : status.includes("submit") ? 2
        : status.includes("valid") ? 1
          : 0;
  const steps = [
    ["最终检查", reached > 1 ? "后台检查已完成" : reached === 1 ? "正在检查" : "等待后台状态"],
    ["双腿提交", reached > 2 ? "批次已提交" : reached === 2 ? "正在提交" : "等待最终检查"],
    ["成交核对", reached > 3 ? "成交结果已确认" : reached === 3 ? "正在读取两腿结果" : "等待批次提交"],
    crossVenue
      ? ["分别结算/自动兑付", reached === 4 ? "等待两所结算与兑付" : "等待成交确认"]
      : ["自动合并", reached === 4 ? "正在合并完整代币组" : "等待成交确认"],
  ];
  return `<div class="pm-progress" aria-label="订单执行进度">${steps.map(([label, detail], index) => {
    const step = index + 1;
    const className = reached > step ? "done" : reached === step ? "current" : "";
    return `<div class="pm-progress-step ${className}"><span>${step} · ${label}</span><strong>${detail}</strong></div>`;
  }).join("")}</div>`;
}

function predictionExecutionAlert(payload, strategy = "yes_no") {
  const execution = payload?.current_execution;
  const incident = payload?.breaker?.incident;
  const status = String(execution?.status || execution?.state || payload?.status || "").toLowerCase();
  if (incident || payload?.breaker?.open && status.includes("incident")) {
    const incidentId = incident?.incident_id || incident?.id || "";
    const happenedAt = incident?.happened_at || incident?.created_at || incident?.updated_at;
    const reason = incident?.reason || incident?.message;
    return `<section class="pm-alert danger" role="alert"><div class="pm-alert-body"><strong>交易已熔断</strong><p>${escapeHtml(reason ? predictionIncidentReasonLabel(reason) : "事故详情未返回")}</p><small>发生时间：${escapeHtml(predictionValue(happenedAt, "-"))}</small></div><button class="pm-button danger" type="button" data-action="open-reset" data-incident-id="${escapeHtml(incidentId)}"${incidentId ? "" : " disabled"}>查看事故并处理</button></section>`;
  }
  if (execution && ["running", "executing", "pending", "submitted", "reconciling", "validating", "final_validating", "submitting", "merging"].some((value) => status.includes(value))) {
    return `<section class="pm-alert info" role="status" aria-live="polite"><div class="pm-alert-body"><strong>正在执行：${escapeHtml(predictionValue(execution.event_title || execution.market_title || execution.question, "数据未返回"))}</strong><p>本笔完成前，其他机会暂时不可参与。</p>${predictionExecutionProgress(execution)}</div><span class="pm-pill watch">执行中</span></section>`;
  }
  if (execution && ["confirmed", "completed", "merged", "success"].some((value) => status.includes(value))) {
    const profit = execution.realized_profit ?? execution.profit ?? execution.net_profit;
    const complete = [
      execution.event_title || execution.market_title || execution.question,
      execution.quantity,
      execution.actual_cost,
      execution.merge_value,
      profit,
      execution.completed_at,
    ].every(predictionHasValue);
    if (!complete) {
      return `<section class="pm-alert success" role="status" aria-live="polite"><div class="pm-alert-body"><strong>交易已完成，详情数据未返回</strong><p>请在“交易与合并”历史中查看后台保存的最终记录。</p></div></section>`;
    }
    return `<section class="pm-alert success" role="status" aria-live="polite"><div class="pm-alert-body"><strong>两腿已成交并自动合并</strong><p>买入 ${escapeHtml(predictionValue(execution.quantity))} 组实际成本 ${escapeHtml(predictionMoney(execution.actual_cost))}，合并收回 ${escapeHtml(predictionMoney(execution.merge_value))}，本次已实现净利润 <b>${escapeHtml(predictionSignedMoney(profit))}</b>。</p></div><span class="pm-pill action">已完成 · ${escapeHtml(predictionValue(execution.completed_at))}</span></section>`;
  }
  if (execution && status === "holding_to_resolution") {
    const cross = predictionIsCrossVenue(execution);
    return `<section class="pm-alert success" role="status" aria-live="polite"><div class="pm-alert-body"><strong>两腿已成交，${cross ? "待兑付" : "待结算"}</strong><p>${cross ? "两所分别结算后自动兑付；不会 merge，结算前允许继续持有其他已确认组合。" : "这是两个独立 condition 的阈值关系组合；不会 merge，结算前允许继续持有其他已确认组合。"}</p></div><span class="pm-pill action">${cross ? "待兑付" : "待结算"}</span></section>`;
  }
  if (payload?.stale && !(strategy === "llm_hedge" && predictionTradingAvailable(payload, strategy))) {
    const health = payload?.health || {};
    const degradedReasons = Array.isArray(health.degraded_reasons)
      ? health.degraded_reasons.map((reason) => String(reason || ""))
      : [];
    const universeAttempts = Number(health.universe_refresh_attempts || 0);
    const universeExhausted = health.universe_retry_exhausted === true
      || degradedReasons.includes("universe_retry_exhausted");
    if (strategy !== "llm_hedge" && universeExhausted) {
      return `<section class="pm-alert danger" role="alert"><div class="pm-alert-body"><strong>监控市场连续 5 次刷新失败</strong><p>监控市场连续 5 次刷新失败，已停止自动重试；请重启承载预测监控的 Dashboard 服务并检查 Polymarket 连接。</p></div><span class="pm-pill watch">失败关闭</span></section>`;
    }
    if (
      strategy !== "llm_hedge"
      && universeAttempts >= 1
      && universeAttempts < 5
      && degradedReasons.includes("universe_refresh_failed")
    ) {
      return `<section class="pm-alert danger" role="alert"><div class="pm-alert-body"><strong>监控市场刷新失败</strong><p>监控市场刷新失败，正在自动重试（${universeAttempts}/5）</p></div><span class="pm-pill watch">失败关闭</span></section>`;
    }
    if (predictionWatcherIsConnected(payload)) {
      return `<section class="pm-alert danger" role="alert"><div class="pm-alert-body"><strong>当前盘口暂不可交易</strong><p>盘口数据已过期，当前不会开放下单；保留最后一次监控结果，仅供查看。</p></div><span class="pm-pill watch">失败关闭</span></section>`;
    }
    return `<section class="pm-alert danger" role="alert"><div class="pm-alert-body"><strong>Polymarket 数据连接异常</strong><p>当前不会开放下单；保留最后一次监控结果，仅供查看。</p></div><span class="pm-pill watch">失败关闭</span></section>`;
  }
  return "";
}

function predictionErrorAlert() {
  const message = String(state.predictionMarket.error || "").trim();
  if (!message) return "";
  return `<section class="pm-alert danger" role="alert"><div class="pm-alert-body"><strong>本次操作未提交</strong><p>${escapeHtml(message)}</p></div><span class="pm-pill watch">失败关闭</span></section>`;
}

function predictionMetricStrip(payload) {
  const labels = ["当前可参与", "监控事件", "市场 / Token", "过去 24 小时信号"];
  if (!predictionHealthIsNormal(payload)) {
    return `<section class="pm-metrics" aria-label="监控摘要不可用">${labels.map((label) => `<article class="pm-metric"><span>${label}</span><strong>-</strong><small>数据未返回</small></article>`).join("")}</section>`;
  }
  const events = predictionEvents(payload);
  const opportunities = predictionOpportunities(payload);
  const executionBlocked = predictionExecutionIsActive(payload);
  const actionable = payload?.breaker?.open || executionBlocked
    ? 0
    : Array.isArray(payload?.opportunities)
      ? opportunities.filter((item) => item.actionable === true
        && predictionOpportunityIsComplete(item)
        && (!predictionIsCrossVenue(item) || predictionCrossExecutionMode(item) === "manual_confirm")).length
      : "-";
  const eventCount = predictionHasValue(payload?.event_count) ? predictionNumber(payload.event_count) : "-";
  const marketCount = predictionHasValue(payload?.market_count) ? predictionNumber(payload.market_count) : "-";
  const tokenCount = predictionHasValue(payload?.token_count) ? predictionNumber(payload.token_count) : "-";
  const signals = predictionHasValue(payload?.signals_24h ?? payload?.history_count_24h)
    ? predictionNumber(payload.signals_24h ?? payload.history_count_24h)
    : "-";
  return `<section class="pm-metrics" aria-label="监控摘要"><article class="pm-metric primary"><span>当前可参与</span><strong>${actionable}</strong><small>后台检查全部通过后才显示</small></article><article class="pm-metric"><span>监控事件</span><strong>${eventCount}</strong><small>按 24h 成交量动态筛选</small></article><article class="pm-metric"><span>市场 / Token</span><strong>${escapeHtml(`${marketCount} / ${tokenCount}`)}</strong><small>不可参与市场仍持续监控</small></article><article class="pm-metric"><span>过去 24 小时信号</span><strong>${signals}</strong><small>曾达到可参与条件</small></article></section>`;
}

function predictionReplacePositiveActionLabel(value, replacement) {
  const raw = String(value || "");
  if (raw === "可参与") return replacement;
  if (raw.endsWith(" · 可参与")) return `${raw.slice(0, -3)}${replacement}`;
  return value;
}

function predictionVenueLegLabels(value) {
  const legs = Array.isArray(value) ? value : [];
  return legs.map((leg) => {
    const exchange = String(leg?.exchange || leg?.venue || "").toLowerCase();
    const venue = exchange === "predict.fun" ? "Predict.fun" : exchange === "polymarket" ? "Polymarket" : "场所未返回";
    const outcome = predictionValue(leg?.outcome, "结果未返回");
    return `${venue} · ${outcome}`;
  }).join(" / ");
}

function predictionIsCrossVenue(value) {
  return String(value?.market_type || "").toLowerCase() === "cross_venue_yes_no";
}

function predictionCrossExecutionMode(value) {
  const mode = value?.execution_mode;
  return typeof mode === "string" ? mode : "";
}

function predictionCrossExecutionModeReason(value) {
  const mode = predictionCrossExecutionMode(value);
  if (mode === "observe_only") return "当前为只观察模式，不开放下单。";
  if (!mode) return "执行模式未返回，当前不会开放下单。";
  if (mode === "blocked") return "当前执行模式已阻断，不开放下单。";
  return "执行模式未知，当前不会开放下单。";
}

function predictionCrossVenueTradingAvailable(payload) {
  const venues = Array.isArray(payload?.venues) ? payload.venues : [];
  const required = ["predict.fun", "polymarket"];
  const venuesReady = required.every((exchange) => venues.some((venue) => (
    String(venue?.venue || "").toLowerCase() === exchange
      && String(venue?.rest || "").toLowerCase() === "ready"
      && String(venue?.ws || "").toLowerCase() === "ready"
      && String(venue?.mode || "") === "可以交易"
      && predictionHasValue(venue?.balance?.value)
  )));
  return predictionTradingAvailable(payload)
    && venuesReady
    && payload?.cross_venue?.breaker?.open === false;
}

function predictionCrossVenueCandidateHtml(value, payload) {
  const opportunity = predictionOpportunityDisplay(value);
  const legs = Array.isArray(opportunity.legs) ? opportunity.legs : [];
  const titleZh = opportunity.title_zh || opportunity.title;
  const titleEn = opportunity.title_zh ? `<span class="pm-title-en">${escapeHtml(predictionValue(opportunity.title, "数据未返回"))}</span>` : "";
  const complete = predictionOpportunityIsComplete(opportunity);
  const executionMode = predictionCrossExecutionMode(opportunity);
  const executionModeAllowsAction = executionMode === "manual_confirm";
  const observeOnlyStage5 = executionMode === "observe_only" && Number(opportunity.funnel_stage) === 5;
  const actionable = executionModeAllowsAction && complete
    && opportunity.actionable === true
    && opportunity.clear_signal === true
    && predictionCrossVenueTradingAvailable(payload);
  const status = actionable ? "可下单明确信号" : "仅观察";
  const reason = (!executionModeAllowsAction && (executionMode !== "observe_only" || observeOnlyStage5))
    ? predictionCrossExecutionModeReason(opportunity)
    : actionable
      ? "确认时会重新读取两所 REST、盘口、余额和未结算额度。"
      : predictionReasonLabel(opportunity.eligibility_reason || opportunity.reason || "opportunity_unavailable");
  const legRows = legs.map((leg, index) => {
    const ids = [leg.market_id, leg.condition_id, leg.token_id].filter(predictionHasValue).join(" · ");
    const link = leg.official_url ? `<a href="${escapeHtml(leg.official_url)}" target="_blank" rel="noreferrer">官方链接</a>` : "";
    return `<article class="pm-order-leg"><span>第 ${index + 1} 腿 · ${escapeHtml(predictionVenueLegLabels([leg]).replace(" · ", " · BUY "))} · FOK</span><strong>冻结数量 ${escapeHtml(predictionValue(leg.net_quantity ?? opportunity.net_quantity, "-"))} 份 · 最高价冻结 ${escapeHtml(predictionPrice(leg.max_price))}</strong><small>最大成本 ${escapeHtml(predictionMoney(leg.max_cost))} · ${escapeHtml(predictionValue(leg.settlement_asset, "资产未返回"))}</small>${ids ? `<small>Native IDs ${escapeHtml(ids)}</small>` : ""}${link ? `<small>${link}</small>` : ""}</article>`;
  }).join("");
  return `<article class="pm-opportunity pm-cross-candidate ${actionable ? "" : "disabled"}" data-cross-opportunity-id="${escapeHtml(predictionValue(opportunity.opportunity_id, ""))}"><div class="pm-opportunity-title"><div><h3 class="pm-event-title"><span class="pm-title-zh">${escapeHtml(predictionValue(titleZh, "数据未返回"))}</span>${titleEn}</h3><p>Predict.fun × Polymarket · 跨所 YES/NO · ${escapeHtml(predictionValue(opportunity.canonical_cutoff ?? opportunity.resolution_at, "截止时间未返回"))}</p></div><span class="pm-pill ${actionable ? "action" : "watch"}">${status}</span></div><div class="pm-order-legs">${legRows || "<div class=\"pm-empty compact\">两条跨所腿数据未返回</div>"}</div><dl class="pm-cross-metrics"><div><dt>净可兑付份额</dt><dd>${escapeHtml(predictionValue(opportunity.net_quantity, "-"))}</dd></div><div><dt>含费最大成本</dt><dd>${escapeHtml(predictionMoney(opportunity.max_cost))}</dd></div><div><dt>最低赔付</dt><dd>${escapeHtml(predictionMoney(opportunity.minimum_payout))}</dd></div><div><dt>最低净利润</dt><dd class="pm-positive">${escapeHtml(predictionSignedMoney(opportunity.profit))}</dd></div><div><dt>简单年化</dt><dd>${escapeHtml(predictionAnnualizedPercent(opportunity.annualized_yield, 2))}</dd></div></dl><div class="pm-opportunity-action"><p>${escapeHtml(reason)}</p>${actionable ? `<button class="pm-button primary pm-participate" type="button" data-action="participate" data-opportunity-id="${escapeHtml(predictionValue(opportunity.opportunity_id, ""))}">查看并确认跨所订单</button>` : ""}</div></article>`;
}

function predictionConditionLabel(value) {
  const raw = String(value || "").trim();
  if (!raw) return "-";
  return raw.length > 18 ? `${raw.slice(0, 8)}…${raw.slice(-6)}` : raw;
}

function predictionLlmDecisionLabel(opportunity) {
  const decision = String(opportunity?.llm_decision || "").trim().toUpperCase();
  const status = String(opportunity?.llm_status || "").trim().toLowerCase();
  const provider = String(opportunity?.codex_model || "").toLowerCase().includes("deepseek")
    ? "DeepSeek"
    : "Codex";
  if (decision === "APPROVE" || status === "approved") return `${provider} APPROVE`;
  if (decision === "REJECT" || status === "llm_rejected") return `${provider} REJECT`;
  if (status === "llm_unavailable") return `${provider} 不可用`;
  return `${provider} 未校验`;
}

function predictionLlmReasonHtml(opportunity) {
  const reasons = Array.isArray(opportunity?.llm_reason_codes) ? opportunity.llm_reason_codes : [];
  const uncertainties = Array.isArray(opportunity?.llm_uncertainties) ? opportunity.llm_uncertainties : [];
  const evidence = Array.isArray(opportunity?.llm_evidence) ? opportunity.llm_evidence : [];
  const reasonText = [...reasons, ...uncertainties].filter(predictionHasValue).map(escapeHtml).join(" · ");
  const evidenceText = evidence.map((item) => {
    if (!item || typeof item !== "object") return String(item || "");
    return Object.entries(item).map(([key, value]) => `${key}: ${value}`).join(" · ");
  }).filter(predictionHasValue).map(escapeHtml).join("<br>");
  return `${opportunity?.llm_summary ? `<p>${escapeHtml(opportunity.llm_summary)}</p>` : ""}${reasonText ? `<small>原因：${reasonText}</small>` : ""}${evidenceText ? `<small>证据：${evidenceText}</small>` : ""}`;
}

function predictionThresholdDistributionHtml(payload) {
  const distribution = payload?.relation_discovery?.annualized_distribution || {};
  return `<section class="pm-threshold-history" aria-label="历史同类年化参考">${[
    ["current", "当前"],
    ["7d", "7 天"],
    ["30d", "30 天"],
  ].map(([key, label]) => {
    const item = distribution[key] && typeof distribution[key] === "object" ? distribution[key] : {};
    return `<div><span>${label}</span><strong>P50 ${escapeHtml(predictionAnnualizedPercent(item.median))}</strong><small>P90 ${escapeHtml(predictionAnnualizedPercent(item.p90))} · n=${escapeHtml(predictionValue(item.count, "0"))}</small></div>`;
  }).join("")}</section>`;
}

function predictionThresholdCanPreview(opportunity, payload) {
  const legs = Array.isArray(opportunity.buy_legs) ? opportunity.buy_legs : [];
  const quantity = Number(opportunity.quantity);
  const conditions = [String(opportunity.condition_id_a || ""), String(opportunity.condition_id_b || "")];
  const legByLabel = Object.fromEntries(legs.map((leg) => [String(leg?.label || "").toUpperCase(), leg]));
  const tokens = legs.map((leg) => String(leg?.token_id || ""));
  const relation = String(opportunity.relation || "").toUpperCase();
  const outcomesMatchRelation = relation === "A_IMPLIES_B"
    ? String(legByLabel.A?.outcome || "").toUpperCase() === "NO"
      && String(legByLabel.B?.outcome || "").toUpperCase() === "YES"
    : relation === "B_IMPLIES_A"
      ? String(legByLabel.A?.outcome || "").toUpperCase() === "YES"
        && String(legByLabel.B?.outcome || "").toUpperCase() === "NO"
      : false;
  const positiveNumber = (value) => predictionHasValue(value)
    && Number.isFinite(Number(value))
    && Number(value) > 0;
  return predictionOpportunityIsComplete(opportunity)
    && (opportunity.actionable === true || String(opportunity.eligibility_reason || "") === "book_stale")
    && String(opportunity.llm_status || "").toLowerCase() === "approved"
    && String(opportunity.llm_decision || "").toUpperCase() === "APPROVE"
    && conditions[0] !== conditions[1]
    && legByLabel.A?.condition_id === conditions[0]
    && legByLabel.B?.condition_id === conditions[1]
    && outcomesMatchRelation
    && tokens.length === 2
    && tokens.every(Boolean)
    && tokens[0] !== tokens[1]
    && positiveNumber(quantity)
    && legs.every((leg) => Number(leg.quantity) === quantity)
    && positiveNumber(opportunity.max_cost ?? opportunity.total_max_cost)
    && positiveNumber(opportunity.profit ?? opportunity.minimum_profit)
    && positiveNumber(opportunity.minimum_payout)
    && positiveNumber(opportunity.annualized_yield)
    && positiveNumber(opportunity.remaining_days)
    && predictionHasValue(opportunity.resolution_at)
    && predictionTradingAvailable(payload, "llm_hedge");
}

function predictionThresholdCandidateHtml(value, payload, expandedRelationKeys = new Set()) {
  const opportunity = predictionOpportunityDisplay(value);
  const relationKey = predictionValue(
    opportunity.relation_id || opportunity.opportunity_id || opportunity.id,
    ""
  );
  const previewable = predictionThresholdCanPreview(opportunity, payload);
  const actionable = opportunity.actionable === true && previewable;
  const legs = Array.isArray(opportunity.buy_legs) ? opportunity.buy_legs : [];
  const llm = predictionLlmDecisionLabel(opportunity);
  const profit = Number(opportunity.profit ?? opportunity.minimum_profit);
  const cost = Number(opportunity.max_cost ?? opportunity.total_max_cost);
  const simpleReturn = Number.isFinite(profit) && Number.isFinite(cost) && cost > 0
    ? `${(profit / cost * 100).toFixed(2)}%`
    : "-";
  const remaining = Number(opportunity.remaining_days);
  const remainingLabel = Number.isFinite(remaining) && remaining > 0
    ? `${Number.isInteger(remaining) ? remaining : remaining.toFixed(1)} 天`
    : "不可计算";
  const annualized = predictionAnnualizedPercent(opportunity.annualized_yield);
  const annualizedDetail = predictionAnnualizedPercent(opportunity.annualized_yield, 2);
  const title = opportunity.event_title
    || (opportunity.title !== "数据未返回" ? opportunity.title : "")
    || `阈值关系候选 · ${predictionValue(opportunity.relation, "关系未返回")}`;
  const open = relationKey && expandedRelationKeys.has(relationKey);
  const statusClass = llm.includes("APPROVE") ? "action" : "watch";
  const legRows = legs.length
    ? legs.map((leg) => `<article class="pm-order-leg"><span>${escapeHtml(predictionValue(leg.label))} · BUY ${escapeHtml(predictionValue(leg.outcome))} · FOK · ${escapeHtml(predictionConditionLabel(leg.condition_id))}</span><strong>${escapeHtml(predictionValue(leg.quantity, "-"))} 份 @ 最高 ${escapeHtml(predictionPrice(leg.max_price))}</strong><small>最大成本 ${escapeHtml(predictionMoney(leg.max_cost))}</small></article>`).join("")
    : `<div class="pm-empty compact">两条 BUY 腿数据未返回</div>`;
  const action = previewable
    ? `<div class="pm-opportunity-action"><p>${actionable ? "两笔订单非原子、不同 condition，不会 merge；确认时重新读取规则、盘口、费用和账户。" : "当前盘口已过期；点击后只刷新这两腿，仍为正收益才生成确认单。"}</p><button class="pm-button primary pm-participate" type="button" data-action="participate" data-opportunity-id="${escapeHtml(relationKey)}">${actionable ? "查看并确认两腿订单" : "刷新盘口并确认"}</button></div>`
    : "";
  const blockedReason = actionable
    ? ""
    : predictionReasonLabel(
      opportunity.eligibility_reason
        || (opportunity.llm_status === "approved" ? "opportunity_unavailable" : opportunity.llm_status)
    );
  const proof = opportunity.llm_proof && typeof opportunity.llm_proof === "object"
    ? Object.entries(opportunity.llm_proof).map(([key, item]) => `${key}: ${item}`).join(" · ")
    : "";
  return `<details class="pm-threshold-candidate pm-opportunity ${previewable ? "" : "disabled"}" data-relation-key="${escapeHtml(relationKey)}"${open ? " open" : ""}><summary><div class="pm-threshold-summary-title"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(predictionConditionLabel(opportunity.condition_id_a))} + ${escapeHtml(predictionConditionLabel(opportunity.condition_id_b))}</small></div><span class="pm-pill ${statusClass}">${escapeHtml(llm.replace(/^(Codex|DeepSeek) /, ""))}</span><div><span>24h 成交量</span><strong>${escapeHtml(predictionVolume(opportunity.volume_24h, "-"))}</strong></div><div><span>成本 / 最低赔付</span><strong>${escapeHtml(predictionMoney(opportunity.max_cost ?? opportunity.total_max_cost))} / ${escapeHtml(predictionMoney(opportunity.minimum_payout))}</strong></div><div><span>理论利润</span><strong class="pm-positive">${escapeHtml(predictionSignedMoney(opportunity.profit ?? opportunity.minimum_profit))}</strong></div><div class="pm-threshold-yield"><span>简单年化</span><strong>${escapeHtml(annualized)}</strong><small>查看详情</small></div></summary><div class="pm-threshold-detail"><section><h3>当前年化计算</h3><div class="pm-threshold-formula"><div><span>含费最大成本</span><strong>${escapeHtml(predictionMoney(opportunity.max_cost ?? opportunity.total_max_cost))}</strong></div><div><span>最低赔付</span><strong>${escapeHtml(predictionMoney(opportunity.minimum_payout))}</strong></div><div><span>理论最低利润</span><strong class="pm-positive">${escapeHtml(predictionSignedMoney(opportunity.profit ?? opportunity.minimum_profit))}</strong></div><div><span>预计资金占用</span><strong>${escapeHtml(remainingLabel)}</strong></div><div><span>简单收益率</span><strong>${escapeHtml(predictionMoney(opportunity.profit ?? opportunity.minimum_profit))} / ${escapeHtml(predictionMoney(opportunity.max_cost ?? opportunity.total_max_cost))} = ${escapeHtml(simpleReturn)}</strong></div><div><span>简单年化</span><strong>${escapeHtml(simpleReturn)} × 365 / ${escapeHtml(remainingLabel)} = ${escapeHtml(annualizedDetail)}</strong></div></div><p class="pm-threshold-caveat">按预计结算时间做非复利年化；结算延迟会降低实际年化。两腿成交前，利润尚未锁定。</p>${predictionThresholdDistributionHtml(payload)}</section><section><h3>合约与赔付证明</h3><div class="pm-threshold-questions"><div><span>市场 A</span><strong>${escapeHtml(predictionValue(opportunity.question_a, "数据未返回"))}</strong><small>${escapeHtml(predictionConditionLabel(opportunity.condition_id_a))}</small></div><div><span>市场 B</span><strong>${escapeHtml(predictionValue(opportunity.question_b, "数据未返回"))}</strong><small>${escapeHtml(predictionConditionLabel(opportunity.condition_id_b))}</small></div></div><div class="pm-order-legs">${legRows}</div></section><section><h3>LLM 与确定性校验</h3><div class="pm-threshold-llm"><div class="pm-llm-line"><span class="pm-pill ${statusClass}">${escapeHtml(llm)}</span><button type="button" class="pm-model-tag" aria-describedby="llm-model-tip-${escapeHtml(relationKey)}">${escapeHtml(String(opportunity?.codex_model || "LLM").trim())}<span class="pm-model-tip" id="llm-model-tip-${escapeHtml(relationKey)}" role="tooltip">${predictionLlmReasonHtml(opportunity) || "<small>该模型本次未给出评价。</small>"}</span></button></div>${predictionLlmReasonHtml(opportunity)}${proof ? `<small>证明：${escapeHtml(proof)}</small>` : ""}<small>规则关系复核：${opportunity.llm_status === "approved" ? "通过" : "未通过"}</small>${blockedReason ? `<small>当前不可确认：${escapeHtml(blockedReason)}</small>` : ""}</div></section>${action}</div></details>`;
}

function predictionFunnelStatus(value) {
  const status = String(value || "unavailable").toLowerCase();
  if (status === "healthy" || status === "connected") return "正常";
  if (status === "scanning") return "扫描中";
  if (status === "lagging") return "追赶中";
  if (status === "degraded") return "降级";
  if (status === "stale") return "已过期";
  return "不可用";
}

function predictionFunnelDuration(value) {
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds)) return "-";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(2)} 秒`;
}

function predictionFunnelStage(label, value, note = "", tone = "") {
  const display = predictionNumber(value);
  return `<article class="pm-funnel-stage ${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(display)}</strong>${note ? `<small>${escapeHtml(note)}</small>` : ""}</article>`;
}

function predictionFunnelRejections(counts) {
  const values = counts && typeof counts === "object" ? counts : {};
  const labels = {
    book_unavailable: "盘口缺失",
    minimum_depth: "最小深度",
    cost_limit: "成本上限",
    outside_5pct: "距盈亏平衡",
    codex_rejected: "Codex 拒绝",
    codex_unavailable: "Codex 不可用",
    rules_changed: "规则变化",
    readiness_blocked: "准入阻断",
    event_ineligible: "事件不合格",
    market_unparseable: "市场不可解析",
  };
  const rows = Object.entries(values)
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${labels[key] || key.replaceAll("_", " ")} ${predictionNumber(value)}`);
  return rows.length ? rows.join(" · ") : "暂无淘汰";
}

function predictionFunnelReasons(activity, payload) {
  const reasons = Array.isArray(activity?.rejection_reasons) ? activity.rejection_reasons : [];
  const fromActivity = reasons.map((item) => {
    const reason = item && typeof item === "object" ? item.eligibility_reason || item.reason : item;
    return predictionReasonLabel(reason);
  }).filter(Boolean);
  if (fromActivity.length) return [...new Set(fromActivity)];
  const opportunities = predictionOpportunities(payload)
    .filter((item) => String(item?.market_type || "") === "threshold_hedge")
    .map((item) => predictionReasonLabel(item.eligibility_reason || item.reason))
    .filter(Boolean);
  return [...new Set(opportunities)];
}

function predictionRelationFunnel(payload) {
  const discovery = payload?.relation_discovery && typeof payload.relation_discovery === "object" ? payload.relation_discovery : {};
  const catalog = discovery.catalog && typeof discovery.catalog === "object" ? discovery.catalog : {};
  const rawActivity = discovery.activity && typeof discovery.activity === "object" ? discovery.activity : {};
  const scanning = ["scanning", "lagging"].includes(String(rawActivity.status || "").toLowerCase());
  const completed = rawActivity.last_completed && typeof rawActivity.last_completed === "object" ? rawActivity.last_completed : {};
  const activity = scanning ? {...rawActivity, ...completed} : rawActivity;
  const websocket = discovery.websocket && typeof discovery.websocket === "object" ? discovery.websocket : {};
  const queue = discovery.codex_queue && typeof discovery.codex_queue === "object" ? discovery.codex_queue : {};
  const usage = discovery.codex_usage_24h && typeof discovery.codex_usage_24h === "object" ? discovery.codex_usage_24h : {};
  const usageByProvider = discovery.llm_usage_24h_by_provider && typeof discovery.llm_usage_24h_by_provider === "object" ? discovery.llm_usage_24h_by_provider : {};
  const codexUsage = usageByProvider.codex && typeof usageByProvider.codex === "object" ? usageByProvider.codex : {};
  const deepseekUsage = usageByProvider.deepseek && typeof usageByProvider.deepseek === "object" ? usageByProvider.deepseek : {};
  const logs = Array.isArray(discovery.scan_logs) ? discovery.scan_logs : [];
  const relationCount = catalog.relations_discovered ?? catalog.relation_count ?? 0;
  const positive = activity.positive_candidates ?? 0;
  const ready = activity.order_ready ?? 0;
  const catalogStatus = predictionFunnelStatus(catalog.status);
  const catalogPillClass = catalogStatus === "正常" ? "action" : "watch";
  const websocketStatus = String(websocket.status || "").toLowerCase() === "connected" ? "WebSocket 正常" : `WebSocket ${predictionFunnelStatus(websocket.status)}`;
  const logRows = logs.length
    ? logs.map((item) => `<li>${escapeHtml(typeof item === "object" ? Object.entries(item).map(([key, value]) => `${key}=${value}`).join(" · ") : String(item))}</li>`).join("")
    : "<li>暂无扫描日志</li>";
  const empty = Number(relationCount) === 0
    ? `<div class="pm-funnel-empty"><strong>本轮未发现可验证关系</strong><p>关系目录完成后，下一轮成交筛选会自动恢复。</p></div>`
    : positive > 0 && Number(ready) === 0
      ? `<div class="pm-funnel-empty pm-funnel-warning"><strong>正收益候选尚未满足下单准入</strong><p>${escapeHtml(predictionFunnelReasons(activity, payload).join(" · ") || "仍在等待实时校验")}</p></div>`
      : positive === 0
        ? `<div class="pm-funnel-empty"><strong>本轮没有正收益候选</strong><p>完整漏斗和淘汰原因仍保留，下一分钟会重新考虑全部关系。</p></div>`
        : "";
  return `<details class="pm-panel pm-relation-funnel" aria-label="实时两层漏斗"><summary class="pm-funnel-header pm-collapse-summary"><div><h2>实时两层漏斗</h2><p>关系目录每日更新；成交候选每分钟重新筛选。</p></div><span class="pm-pill ${catalogPillClass}">${escapeHtml(catalogStatus)}</span></summary><div class="pm-funnel-chips pm-funnel-chips-body"><span class="pm-funnel-chip">关系目录 ${escapeHtml(catalogStatus)}</span><span class="pm-funnel-chip">成交筛选 ${escapeHtml(predictionFunnelStatus(rawActivity.status))}</span><span class="pm-funnel-chip">${escapeHtml(websocketStatus)} · ${escapeHtml(predictionValue(websocket.last_message_age_seconds, "-"))}s</span><span class="pm-funnel-chip">Codex queue · ${escapeHtml(predictionNumber(queue.pending, "0"))} 待审</span></div><div class="pm-funnel-lane"><div class="pm-funnel-lane-title"><strong>第一层 · 关系目录</strong><small>${escapeHtml(catalogStatus)} · ${escapeHtml(predictionFunnelDuration(catalog.duration_ms))}</small></div><div class="pm-funnel-grid pm-funnel-grid-catalog">${predictionFunnelStage("扫描事件", catalog.events_seen, `合格 ${predictionNumber(catalog.events_eligible)}`)}${predictionFunnelStage("阈值市场", catalog.threshold_markets)}${predictionFunnelStage("程序关系", relationCount)}${predictionFunnelStage("持久化目录", relationCount, `唯一 Token ${predictionNumber(catalog.unique_tokens)}`, "good")}</div></div><div class="pm-funnel-lane"><div class="pm-funnel-lane-title"><strong>第二层 · 成交候选</strong><small>${escapeHtml(predictionFunnelStatus(rawActivity.status))} · ${escapeHtml(predictionFunnelDuration(activity.duration_ms))}</small></div><div class="pm-funnel-grid pm-funnel-grid-activity">${predictionFunnelStage("全部关系", activity.relations_considered)}${predictionFunnelStage("盘口可用", activity.relations_with_books, `淘汰 ${predictionNumber(activity.rejection_counts?.book_unavailable, "0")}`)}${predictionFunnelStage("最小深度", activity.relations_with_minimum_depth, `淘汰 ${predictionNumber(activity.rejection_counts?.minimum_depth, "0")}`)}${predictionFunnelStage("5%边界内", activity.relations_within_5pct, `淘汰 ${predictionNumber(activity.rejection_counts?.outside_5pct, "0")}`, "drop")}${predictionFunnelStage("Codex", activity.codex_approved, `${predictionNumber(activity.codex_pending, "0")} 待审 · ${predictionNumber(activity.codex_rejected, "0")} 拒绝`)}${predictionFunnelStage("WebSocket池", activity.subscribed_tokens, `${predictionNumber(activity.subscribed_relations, "0")} 关系`, "live")}${predictionFunnelStage("正收益", positive, `正收益 ${predictionNumber(positive)}`, positive > 0 ? "good" : "")}${predictionFunnelStage("飞书已发", activity.notifications_sent, `飞书 ${predictionNumber(activity.notifications_sent, "0")} · 可下单 ${predictionNumber(ready, "0")}`, "good")}</div></div><div class="pm-funnel-meta"><span>${escapeHtml(websocketStatus)}</span><span>本轮耗时 ${escapeHtml(predictionFunnelDuration(activity.duration_ms))}</span><details class="pm-funnel-rejections"><summary>拒绝原因</summary><p>${escapeHtml(predictionFunnelRejections({...catalog.rejection_counts, ...activity.rejection_counts}))}</p></details><details class="pm-scan-logs"><summary>扫描日志（${logs.length} 条）</summary><ul>${logRows}</ul></details><span class="pm-usage-line"><strong>Codex 24h</strong>：${escapeHtml(predictionNumber(codexUsage.calls ?? usage.calls, "0"))} calls · ${escapeHtml(predictionNumber(codexUsage.failures ?? usage.failures, "0"))} fail · ${escapeHtml(predictionNumber(codexUsage.cache_hits ?? usage.cache_hits, "0"))} cache(本次运行)</span><span class="pm-usage-line"><strong>DeepSeek 24h</strong>：${escapeHtml(predictionNumber(deepseekUsage.calls, "0"))} calls · ${escapeHtml(predictionNumber(deepseekUsage.failures, "0"))} fail · ${escapeHtml(predictionNumber(deepseekUsage.cache_hits, "0"))} cache(本次运行)</span></div>${empty}</details>`;
}

function predictionRelationDiscoveryPanel(payload) {
  const discovery = payload?.relation_discovery && typeof payload.relation_discovery === "object" ? payload.relation_discovery : {};
  const catalog = discovery.catalog && typeof discovery.catalog === "object" ? discovery.catalog : {};
  const activity = discovery.activity && typeof discovery.activity === "object" ? discovery.activity : {};
  const usage = discovery.codex_usage_24h && typeof discovery.codex_usage_24h === "object" ? discovery.codex_usage_24h : {};
  const usageByProvider = discovery.llm_usage_24h_by_provider && typeof discovery.llm_usage_24h_by_provider === "object" ? discovery.llm_usage_24h_by_provider : {};
  const codexUsage = usageByProvider.codex && typeof usageByProvider.codex === "object" ? usageByProvider.codex : {};
  const deepseekUsage = usageByProvider.deepseek && typeof usageByProvider.deepseek === "object" ? usageByProvider.deepseek : {};
  const logs = Array.isArray(discovery.scan_logs) ? discovery.scan_logs : [];
  const logRows = logs.length ? logs.map((item) => `<li>${escapeHtml(typeof item === "object" ? Object.entries(item).map(([key, value]) => `${key}=${value}`).join(" · ") : String(item))}</li>`).join("") : "<li>暂无扫描日志</li>";
  const duration = predictionFunnelDuration(catalog.duration_ms);
  const next = predictionValue(activity.next_scan_at, "按计划");
  const catalogStatus = predictionFunnelStatus(catalog.status);
  const catalogPillClass = catalogStatus === "正常" ? "action" : "watch";
  return `<details class="pm-panel pm-relation-discovery" aria-label="关联合约扫描"><summary class="pm-panel-heading pm-collapse-summary"><div><h2>关联合约扫描</h2><p>关系目录持久化，成交筛选每分钟刷新。</p></div><span class="pm-pill ${catalogPillClass}">${escapeHtml(catalogStatus)}</span></summary><div class="pm-relation-summary pm-relation-summary-grid"><span>最近全量扫描<strong>${escapeHtml(predictionValue(catalog.completed_at, "未完成"))}</strong></span><span>扫描耗时<strong>${escapeHtml(duration)}</strong></span><span>下次计划<strong>${escapeHtml(next)}</strong></span><span>LLM 24h<strong>Codex ${escapeHtml(predictionNumber(codexUsage.calls ?? usage.calls, "0"))} calls · ${escapeHtml(predictionNumber(codexUsage.failures ?? usage.failures, "0"))} fail · ${escapeHtml(predictionNumber(codexUsage.cache_hits ?? usage.cache_hits, "0"))} cache(本次运行)<br>DeepSeek ${escapeHtml(predictionNumber(deepseekUsage.calls, "0"))} calls · ${escapeHtml(predictionNumber(deepseekUsage.failures, "0"))} fail · ${escapeHtml(predictionNumber(deepseekUsage.cache_hits, "0"))} cache(本次运行)</strong></span></div><details class="pm-scan-logs"><summary>扫描日志（${logs.length} 条）</summary><ul>${logRows}</ul></details></details>`;
}

function predictionOpportunityPanel(payload) {
  const opportunities = predictionOpportunities(payload);
  const opportunity = opportunities[0];
  if (!opportunity) {
    return predictionHealthIsNormal(payload)
      ? `<div class="pm-empty"><strong>当前没有可参与机会</strong><p>Watcher 正常运行。历史信号仍可在下方查看。</p></div>`
      : `<div class="pm-empty"><strong>预测市场暂不可用</strong><p>${escapeHtml(predictionFailureReasonLabel(payload))}</p></div>`;
  }
  const complete = predictionOpportunityIsComplete(opportunity);
  const executionBlocked = predictionExecutionIsActive(payload);
  const actionable = complete && opportunity.actionable === true && predictionTradingAvailable(payload);
  if (predictionIsCrossVenue(opportunity)) {
    return predictionCrossVenueCandidateHtml(opportunity, payload);
  }
  if (String(opportunity.market_type || "") === "threshold_hedge") {
    return predictionThresholdCandidateHtml(opportunity, payload);
  }
  const title = opportunity.title || opportunity.market_title || opportunity.event_title || "数据未返回";
  const status = !complete ? "数据不完整" : actionable ? "可参与" : "暂不可参与";
  const buttonText = !complete ? "数据不完整"
    : executionBlocked ? "另一笔正在执行"
      : !predictionHealthIsNormal(payload) ? "不可用"
        : payload?.breaker?.open ? "熔断中"
          : actionable ? `参与 · 预计 ${predictionSignedMoney(opportunity.profit ?? opportunity.minimum_profit)}`
            : "暂不可参与";
  const quantity = predictionValue(opportunity.quantity ?? opportunity.size, "-");
  const threshold = payload?.policy_limits?.min_estimated_profit;
  const actionNote = predictionHasValue(threshold)
    ? `确认时会重新检查价格；净利润低于 ${predictionMoney(threshold)} 就拒绝下单。`
    : "策略参数未返回，不能下单。";
  return `<article class="pm-opportunity ${actionable ? "" : "disabled"}"><div class="pm-opportunity-title"><div><h3>${escapeHtml(title)}</h3><p>Polymarket · ${escapeHtml(predictionMarketTypeLabel(opportunity.market_type))} · ${escapeHtml(predictionFeeStatusLabel(opportunity.fee_status))} · ${escapeHtml(predictionValue(opportunity.updated_at, "-"))}</p></div><span class="pm-pill ${actionable ? "action" : "watch"}">${status}</span></div><dl class="pm-opportunity-metrics"><div><dt>YES 最高买价</dt><dd>${escapeHtml(predictionPrice(opportunity.yes_price ?? opportunity.yes_best_bid))}</dd></div><div><dt>NO 最高买价</dt><dd>${escapeHtml(predictionPrice(opportunity.no_price ?? opportunity.no_best_bid))}</dd></div><div><dt>自动数量</dt><dd>${escapeHtml(quantity === "-" ? quantity : `${quantity} 组`)}</dd></div><div><dt>最大成本</dt><dd>${escapeHtml(predictionMoney(opportunity.max_cost ?? opportunity.cost))}</dd></div><div><dt>最低净利润</dt><dd class="pm-positive">${escapeHtml(predictionSignedMoney(opportunity.profit ?? opportunity.minimum_profit))}</dd></div></dl><div class="pm-opportunity-action"><p>${escapeHtml(actionNote)}</p><button class="pm-button primary pm-participate" type="button" data-action="participate" data-opportunity-id="${escapeHtml(predictionValue(opportunity.opportunity_id || opportunity.id, ""))}"${actionable ? "" : " disabled"}>${escapeHtml(buttonText)}</button></div></article>`;
}

function predictionHistoryContent(payload, kind) {
  const rows = payload?.histories?.[kind];
  const displayRows = (Array.isArray(rows) ? rows : [])
    .map((row) => predictionHistoryDisplay(kind, row))
    .filter((row) => kind !== "executions" || row.phase !== "startup_unknown_state");
  if (!displayRows.length) return `<div class="pm-empty compact"><strong>${kind === "signals" ? "还没有历史信号" : kind === "executions" ? "还没有真实交易" : "没有交易事故"}</strong><p>${kind === "signals" ? "后台达到正式信号门槛后会保留出现时间、成交量和利润。" : kind === "executions" ? "第一笔真实机会完成后，这里会记录两腿订单、合并交易和已实现利润。" : "单腿成交、自动处置失败和合并失败会永久保留在这里。"}</p></div>`;
  if (kind === "signals") {
    const notificationLabel = (value) => {
      const stateValue = String(value || "").toLowerCase();
      if (stateValue === "sent") return "飞书已发";
      if (stateValue === "failed") return "发送失败";
      return "未发送";
    };
    const durationLabel = (row) => {
      const observed = Number(row.observed_duration_ms);
      return Number.isFinite(observed) ? `${Math.round(observed)} ms` : predictionValue(row.duration);
    };
    const closed = (row) => row.closed === true
      || ["closed", "ended", "expired"].includes(String(row.status || "").toLowerCase())
      || predictionHasValue(row.ended_at)
      || predictionHasValue(row.closed_at);
    const title = (row) => {
      const english = predictionValue(row.event_title, "-");
      const chinese = String(row.event_title_zh || row.title_zh || "").trim();
      const secondary = chinese || "中文翻译生成中";
      const legs = predictionVenueLegLabels(row.legs);
      return `<span class="pm-title-en">${escapeHtml(english)}</span><span class="pm-title-zh">${escapeHtml(secondary)}</span>${legs ? `<small class="pm-signal-legs">${escapeHtml(legs)}</small>` : ""}`;
    };
    const liveProfit = (row) => closed(row) ? "—" : predictionSignedMoney(row.live_profit ?? row.estimated_profit, "—");
    const threshold = (row) => String(row.market_type || "") === "threshold_hedge";
    const cross = (row) => predictionIsCrossVenue(row);
    const capitalUsage = (row) => {
      if (cross(row)) return `<strong>净可兑付 ${escapeHtml(predictionValue(row.net_quantity ?? row.quantity, "-"))} 份</strong><small>两所待兑付</small>`;
      if (!threshold(row)) return `<strong>${escapeHtml(durationLabel(row))}</strong>`;
      const remaining = Number(row.remaining_days);
      const remainingText = Number.isFinite(remaining) && remaining > 0
        ? `${Number.isInteger(remaining) ? remaining : remaining.toFixed(1)} 天`
        : "不可计算";
      const resolution = predictionHktTimestamp(row.resolution_at, "结算时间未返回");
      return `<strong>${escapeHtml(remainingText)}</strong><small>结算 ${escapeHtml(resolution)}</small>`;
    };
    const netReturn = (row) => {
      if (cross(row)) return `<strong class="pm-positive">${escapeHtml(predictionSignedMoney(row.minimum_profit ?? row.initial_profit))}</strong><small>年化 ${escapeHtml(predictionAnnualizedPercent(row.annualized_yield, 2))}</small>`;
      if (!threshold(row)) {
        return `<strong>${escapeHtml(predictionSignedMoney(row.initial_profit))}</strong><small>实时 ${escapeHtml(liveProfit(row))}</small>`;
      }
      const profit = predictionSignedMoney(row.minimum_profit ?? row.profit ?? row.estimated_profit);
      const annualized = predictionAnnualizedPercent(row.annualized_yield, 2);
      const cost = predictionMoney(row.total_max_cost ?? row.max_cost);
      const fee = predictionHasValue(row.maximum_fee) ? ` ${predictionMoney(row.maximum_fee)}` : "";
      return `<strong class="pm-positive">${escapeHtml(profit)}</strong><small>年化 ${escapeHtml(annualized)}</small><small>最多占用 ${escapeHtml(cost)} · 含模型手续费${escapeHtml(fee)}</small>`;
    };
    const operation = (row) => {
      const currentState = state.predictionMarket.payload || payload;
      const automaticCrossMode = currentState?.cross_auto?.configured_mode === "auto_submit";
      const healthy = !String(state.predictionMarket.signalError || "").trim()
        && predictionTradingAvailable(currentState);
      const liveCrossOpportunity = cross(row) && Array.isArray(currentState?.opportunities)
        ? currentState.opportunities.find((candidate) => (
          predictionIsCrossVenue(candidate)
          && String(candidate?.opportunity_id ?? candidate?.id ?? "") === String(row.opportunity_id ?? "")
        ))
        : null;
      const effectiveCrossRow = cross(row)
        ? liveCrossOpportunity
          ? {...row, execution_mode: liveCrossOpportunity.execution_mode}
          : {...row, execution_mode: undefined}
        : row;
      const crossModeAllowsAction = !cross(row) || predictionCrossExecutionMode(effectiveCrossRow) === "manual_confirm";
      const actionable = !automaticCrossMode && row.actionable_now === true && !closed(row) && predictionHasValue(row.opportunity_id) && healthy && crossModeAllowsAction;
      const button = `<button class="pm-button primary pm-signal-action" type="button" data-action="participate" data-opportunity-id="${escapeHtml(String(row.opportunity_id))}">重新检查</button>`;
      if (cross(row)) {
        if (String(row.status || "").includes("授权已清零")) {
          return `<span class="pm-observe">${escapeHtml(predictionValue(row.status))}</span>`;
        }
        return actionable && predictionCrossVenueTradingAvailable(currentState)
          ? button
          : `<span class="pm-observe">仅观察</span><small>${escapeHtml(!crossModeAllowsAction ? predictionCrossExecutionModeReason(effectiveCrossRow) : predictionReasonLabel(row.eligibility_reason || "opportunity_unavailable"))}</small>`;
      }
      if (threshold(row)) {
        return actionable
          ? button
          : `<span class="pm-observe">仅观察</span><small>${escapeHtml(predictionReasonLabel(row.eligibility_reason || (row.actionable_now ? "opportunity_unavailable" : "annualized_yield_unavailable")))}</small>`;
      }
      const notice = notificationLabel(row.notification_state);
      return `${escapeHtml(notice)}${actionable ? button : ""}`;
    };
    return `<table class="pm-table pm-signal-table"><thead><tr><th>出现时间（HKT）</th><th>标的</th><th>24h 成交量</th><th>资金占用</th><th>净回报</th><th>操作</th></tr></thead><tbody>${displayRows.map((row) => `<tr class="${row.actionable_now === true && !closed(row) ? "pm-signal-live" : ""}"><td data-label="出现时间（HKT）">${escapeHtml(predictionHktTimestamp(row.occurred_at))}<small class="pm-relative-age">${escapeHtml(predictionRelativeAge(row.occurred_at))}</small></td><td data-label="标的" class="pm-title-cell">${title(row)}</td><td data-label="24h 成交量">${escapeHtml(predictionVolume(row.volume_24h, "-"))}</td><td data-label="资金占用">${capitalUsage(row)}</td><td data-label="净回报">${netReturn(row)}</td><td data-label="操作" class="pm-signal-operation">${operation(row)}</td></tr>`).join("")}</tbody></table>`;
  }
  if (kind === "executions") {
    return `<table class="pm-table"><thead><tr><th>完成时间</th><th>市场</th><th>数量</th><th>实际成本</th><th>合并收回</th><th>已实现</th></tr></thead><tbody>${displayRows.map((row) => { const quantity = predictionValue(row.quantity); const quantityLabel = quantity === "-" || quantity.includes("组") ? quantity : `${quantity} 组`; const holding = row.state === "holding_to_resolution"; const legs = predictionVenueLegLabels(row.legs); const state = predictionValue(row.status ?? row.state, "-"); const lifecycle = Array.isArray(row.lifecycle) ? `<details class="pm-lifecycle"><summary>生命周期回执</summary>${row.lifecycle.map((item) => `<div><span>${escapeHtml(predictionValue(item.phase, "阶段"))}</span><strong>${escapeHtml(predictionValue(item.receipt, "-"))}</strong><small>${escapeHtml(predictionValue(item.status, "-"))}</small></div>`).join("")}</details>` : ""; return `<tr><td data-label="完成时间">${escapeHtml(predictionValue(row.completed_at))}</td><td data-label="市场">${escapeHtml(predictionValue(row.event_title))}${legs ? `<small class="pm-signal-legs">${escapeHtml(legs)}</small>` : ""}<small>状态 ${escapeHtml(state)}</small>${lifecycle}</td><td data-label="数量">${escapeHtml(quantityLabel)}</td><td data-label="实际成本">${escapeHtml(predictionMoney(row.actual_cost))}</td><td data-label="合并收回">${holding ? "待兑付（不 merge）" : escapeHtml(predictionMoney(row.merge_value))}</td><td data-label="已实现" class="pm-positive"><strong>${holding ? "待兑付" : escapeHtml(predictionSignedMoney(row.realized_profit))}</strong></td></tr>`; }).join("")}</tbody></table>`;
  }
  return `<table class="pm-table"><thead><tr><th>发生时间</th><th>市场</th><th>原因</th><th>自动处置</th><th>损失</th><th>状态</th></tr></thead><tbody>${displayRows.map((row) => { const legs = predictionVenueLegLabels(row.legs); return `<tr><td data-label="发生时间">${escapeHtml(predictionValue(row.happened_at))}</td><td data-label="市场">${escapeHtml(predictionValue(row.event_title))}${legs ? `<small class="pm-signal-legs">${escapeHtml(legs)}</small>` : ""}</td><td data-label="原因">${escapeHtml(predictionIncidentReasonLabel(row.reason))}</td><td data-label="自动处置">${escapeHtml(predictionValue(row.remediation))}</td><td data-label="损失" class="pm-tone-danger"><strong>${escapeHtml(predictionSignedMoney(row.loss))}</strong></td><td data-label="状态">${escapeHtml(predictionIncidentStatusLabel(row.status))}</td></tr>`; }).join("")}</tbody></table>`;
}

function predictionHistoryDisplay(kind, value) {
  const row = value && typeof value === "object" ? value : {};
  const evidence = row.evidence && typeof row.evidence === "object" ? row.evidence : {};
  if (kind === "signals") {
    return {
      ...row,
      occurred_at: row.occurred_at ?? row.started_at ?? row.created_at,
      event_title: row.event_title ?? row.question ?? row.title,
      event_title_zh: row.event_title_zh ?? row.title_zh,
      duration: row.duration ?? "-",
      observed_duration_ms: row.observed_duration_ms,
      initial_profit: row.initial_profit,
      peak_profit: row.peak_profit,
      final_profit: row.final_profit,
      ended_reason: row.ended_reason,
      ended_at: row.ended_at ?? row.closed_at,
      closed: row.closed,
      opportunity_id: row.opportunity_id ?? row.id,
      actionable_now: row.actionable_now === true,
      live_profit: row.live_profit ?? row.estimated_profit,
      notification_state: row.notification_state ?? row.notification_status,
      peak_edge: row.peak_edge ?? row.peak_net_edge ?? row.net_edge,
      quantity: row.quantity ?? row.peak_quantity,
      profit: row.profit ?? row.peak_estimated_profit ?? row.estimated_profit ?? row.minimum_profit,
    };
  }
  if (kind === "executions") {
    return {
      ...row,
      completed_at: row.completed_at ?? row.updated_at ?? row.created_at,
      event_title: row.event_title ?? row.question ?? row.title,
      quantity: row.quantity ?? row.peak_quantity,
      actual_cost: row.actual_cost ?? row.total_actual_cost,
      merge_value: row.merge_value ?? row.merged_value ?? row.payout ?? row.merge_amount,
      realized_profit: row.realized_profit ?? row.net_profit ?? row.actual_profit,
    };
  }
  return {
    ...row,
    happened_at: row.happened_at ?? row.created_at ?? row.updated_at,
    event_title: row.event_title ?? row.question ?? row.title,
    reason: row.reason ?? evidence.reason,
    remediation: row.remediation ?? evidence.remediation,
    loss: row.loss ?? evidence.loss ?? evidence.actual_loss,
    status: row.acknowledged && row.acknowledgement?.reconciliation === "fresh_clean"
      ? "resolved_clean"
      : row.status ?? row.state,
  };
}

function predictionHistoryPanel(payload) {
  const kind = state.predictionMarket.historyKind;
  const title = kind === "signals" ? "套利信号" : kind === "executions" ? "交易与合并" : "事故";
  const description = kind === "signals"
    ? "达到套利门槛的信号会保留；有按钮才代表当前可操作。"
    : kind === "executions"
      ? "真实交易和已实现利润单独保存。"
      : "单腿成交、自动处置失败和合并失败永久保留。";
  const signalClock = kind === "signals"
    ? predictionClock("信号刷新时间", state.predictionMarket.signalLastSuccessAt, {danger: Boolean(String(state.predictionMarket.signalError || "").trim())})
    : "";
  const signalError = kind === "signals" && state.predictionMarket.signalError
    ? `<p class="pm-signal-error" role="alert">${escapeHtml(state.predictionMarket.signalError)}；已冻结上次结果，恢复后继续刷新。</p>`
    : "";
  return `<section class="pm-panel" data-prediction-history-panel><header class="pm-panel-heading"><div><h2>${title}</h2><p>${description}</p>${signalError}</div><div class="pm-panel-heading-actions">${signalClock}<div class="pm-history-tabs" aria-label="套利历史类型"><button class="pm-history-tab" type="button" data-history="signals" aria-pressed="${kind === "signals"}">套利信号</button><button class="pm-history-tab" type="button" data-history="executions" aria-pressed="${kind === "executions"}">交易与合并</button><button class="pm-history-tab" type="button" data-history="incidents" aria-pressed="${kind === "incidents"}">事故</button></div></div></header>${predictionHistoryContent(payload, kind)}</section>`;
}

function predictionYesNoWorkspace(payload) {
  const opportunities = (Array.isArray(payload?.opportunities) ? payload.opportunities : [])
    .filter((item) => String(item?.market_type || "") !== "threshold_hedge");
  const viewPayload = {...payload, opportunities};
  return `${predictionCrossVenueFunnel(viewPayload)}${predictionCrossVenueCandidates(viewPayload)}<aside class="pm-policy"><strong>V1 仅对普通二元、免手续费市场开放实盘</strong><p>收费市场和 Negative Risk 市场仍监控，但不会出现“参与”按钮。</p></aside>${predictionHistoryPanel(viewPayload)}`;
}

function predictionCandidateTable(opportunities) {
  const rows = (Array.isArray(opportunities) ? opportunities : []).map((raw) => {
    const opportunity = predictionOpportunityDisplay(raw);
    const actionable = opportunity.actionable === true;
    const cross = predictionIsCrossVenue(opportunity);
    const combinedQuestions = opportunity.question_a && opportunity.question_b
      ? `${opportunity.question_a} / ${opportunity.question_b}`
      : "";
    const title = predictionValue(
      opportunity.title_zh || opportunity.title || opportunity.question || combinedQuestions,
      "数据未返回"
    );
    const sub = cross ? "Predict × Polymarket" : "Polymarket 阈值对冲";
    const annualized = predictionAnnualizedPercent(opportunity.annualized_yield, 2);
    const remaining = Number(opportunity.remaining_days);
    const settlement = Number.isFinite(remaining) && remaining > 0
      ? `${Number.isInteger(remaining) ? remaining : remaining.toFixed(1)} 天`
      : "不可计算";
    const resolution = predictionHktTimestamp(
      opportunity.resolution_at ?? opportunity.canonical_cutoff,
      "—"
    );
    const depthOk = String(opportunity.depth_status || "") === "pass";
    const depth = depthOk
      ? `${escapeHtml(predictionValue(opportunity.max_executable_quantity, "-"))} 份 / ${escapeHtml(predictionMoney(opportunity.max_executable_cost))}`
      : `<span class="pm-tone-danger">深度不足</span>`;
    const policy = depthOk
      ? `${escapeHtml(predictionValue(opportunity.policy_quantity, "-"))} 份 / ${escapeHtml(predictionMoney(opportunity.policy_cost))}`
      : "—";
    const status = actionable
      ? `<span class="pm-pill action">可参与</span>`
      : `<span class="pm-pill watch">仅观察</span><small>${escapeHtml(predictionReasonLabel(opportunity.eligibility_reason || "opportunity_unavailable"))}</small>${Array.isArray(opportunity.llm_reason_codes) && opportunity.llm_reason_codes[0] ? `<small>${escapeHtml(String(opportunity.llm_reason_codes[0]))}</small>` : ""}`;
    const action = actionable
      ? `<button class="pm-button primary pm-participate" type="button" data-action="participate" data-opportunity-id="${escapeHtml(predictionValue(opportunity.opportunity_id || opportunity.id, ""))}">确认</button>`
      : "—";
    return `<tr data-relation-key="${escapeHtml(predictionValue(opportunity.relation_id || opportunity.opportunity_id || opportunity.id, ""))}"><td><strong>${escapeHtml(title)}</strong><span class="sub">${escapeHtml(sub)}</span></td><td class="num"><strong>${escapeHtml(annualized)}</strong></td><td class="num"><strong>${escapeHtml(settlement)}</strong><span class="sub">${escapeHtml(resolution)}</span></td><td class="num">${depth}</td><td class="num">${escapeHtml(policy)}</td><td>${status}</td><td>${action}</td></tr>`;
  }).join("");
  return `<div class="pm-table-wrap"><table class="pm-table pm-candidate-table"><thead><tr><th>标的</th><th class="num">年化</th><th class="num">结算期</th><th class="num">理论深度</th><th class="num">政策下单量</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function predictionLlmHedgeWorkspace(payload, expandedRelationKeys) {
  const opportunities = predictionOpportunities(payload)
    .filter((item) => String(item.market_type || "") === "threshold_hedge");
  const candidates = opportunities.length
    ? predictionCandidateTable(opportunities)
    : `<div class="pm-empty"><strong>当前没有正收益候选</strong><p>关联合约扫描仍在运行；出现候选后会在这里展示校验状态和年化计算。</p></div>`;
  return `${predictionRelationFunnel(payload)}<aside class="pm-policy"><strong>所有正收益候选都会展示</strong><p>低于 15% 年化的信号不展示；Codex 结论和程序复核全部通过后才出现人工确认入口；两腿属于不同 condition，不会 merge。</p></aside><section class="pm-panel"><header class="pm-panel-heading"><div><h2>候选标的</h2><p>按可参与 → 年化 → 结算期 → 利润排序；点击确认前会重新检查价格。</p></div><span class="pm-pill">显示 ${opportunities.length}</span></header>${candidates}</section>`;
}

function predictionModeBar(payload) {
  const mode = payload.validation_mode || "observe_only";
  const stats = payload.auto_eat_stats || {};
  const modes = [
    ["observe_only", "观察"],
    ["manual", "手动"],
    ["auto", "auto"],
  ];
  const buttons = modes.map(([value, label]) =>
    `<button type="button" class="pm-mode-button${mode === value ? " active" : ""}" data-action="set-mode" data-mode="${value}">${label}</button>`
  ).join("");
  const crossAuto = predictionCrossAutoStatus(payload);
  return `<div class="pm-mode-bar" aria-label="验证期吃单模式">${buttons}<span class="pm-mode-stats">今日 ${stats.today_submitted || 0} 单 / $${Number(stats.today_cost || 0).toFixed(2)}</span>${crossAuto}</div>`;
}

function predictionCrossAutoStatus(payload) {
  const auto = payload?.cross_auto && typeof payload.cross_auto === "object" ? payload.cross_auto : {};
  if (auto.configured_mode !== "auto_submit") return "";
  const daily = auto.daily_principal && typeof auto.daily_principal === "object" ? auto.daily_principal : {};
  const attempt = auto.latest_attempt && typeof auto.latest_attempt === "object" ? auto.latest_attempt : null;
  const active = auto.armed === true && auto.effective_mode === "auto_submit";
  const pauseReason = !active && auto.pause_reason
    ? ` · ${escapeHtml(predictionReasonLabel(auto.pause_reason))} · 需要操作员处理`
    : "";
  const attemptedAt = attempt?.updated_at || attempt?.created_at;
  const attemptStatus = attempt?.decision === "rejected"
    ? `<span class="pm-mode-stats">拒绝 ${escapeHtml(predictionValue(attempt.reason_code))} · ${escapeHtml(predictionValue(attempt.reason_zh))} · 当前 ${escapeHtml(predictionValue(attempt.current))} · 上限 ${escapeHtml(predictionValue(attempt.limit))} · 场所 ${escapeHtml(predictionValue(attempt.venue))} · ${escapeHtml(predictionHktTimestamp(attemptedAt))} · ${attempt.operator_action_required === true ? "需要操作员处理" : "无需操作员处理"}${attempt.operator_action ? ` · 操作 ${escapeHtml(predictionValue(attempt.operator_action))}` : ""}</span>`
    : attempt?.decision === "submitted"
      ? `<span class="pm-mode-stats">已提交双边订单 · ${escapeHtml(predictionValue(attempt.reason_zh))} · ${escapeHtml(predictionHktTimestamp(attemptedAt))}</span>`
      : "";
  const pause = active
    ? `<button class="pm-button danger" type="button" data-action="pause-cross-auto">紧急暂停自动下单</button>`
    : "";
  return `<span class="pm-mode-stats" data-cross-auto-status>跨所自动下单 · ${active ? "已启用" : "已暂停"} · 当日新本金 ${escapeHtml(predictionMoney(daily.current))} / ${escapeHtml(predictionMoney(daily.limit))}${pauseReason}</span>${attemptStatus}${pause}`;
}

function renderPredictionMarket() {
  const root = elements["prediction-market-root"];
  if (!root) return;
  const expandedRelationKeys = new Set(
    Array.from(root.querySelectorAll(".pm-threshold-candidate[open][data-relation-key]"))
      .map((candidate) => candidate.dataset.relationKey)
  );
  const payload = state.predictionMarket.payload;
  const viewPayload = payload || {status: "loading", events: [], opportunities: []};
  const strategy = state.predictionMarket.strategy === "llm_hedge" ? "llm_hedge" : "yes_no";
  const workspace = strategy === "llm_hedge"
    ? predictionLlmHedgeWorkspace(viewPayload, expandedRelationKeys)
    : predictionYesNoWorkspace(viewPayload);
  root.innerHTML = `${predictionPageHeader(viewPayload)}${predictionModeBar(viewPayload)}${predictionReadinessStrip(viewPayload, strategy)}${predictionSafeguardsHtml(viewPayload)}${predictionStrategyTabs(strategy)}${predictionErrorAlert()}${predictionExecutionAlert(viewPayload, strategy)}${workspace}`;
}

function startPredictionPolling() {
  stopPredictionPolling();
  if (state.workspaceView !== "prediction_market") return;
  state.predictionMarket.pollId = window.setInterval(fetchPredictionState, 5000);
}

function stopPredictionPolling() {
  if (state.predictionMarket.pollId !== null) {
    window.clearInterval(state.predictionMarket.pollId);
    state.predictionMarket.pollId = null;
  }
}

function startPredictionSignalPolling() {
  stopPredictionSignalPolling();
  if (state.workspaceView !== "prediction_market" || state.predictionMarket.strategy !== "yes_no") return;
  loadPredictionHistory("signals", {panelOnly: true});
  state.predictionMarket.signalPollId = window.setInterval(() => {
    loadPredictionHistory("signals", {panelOnly: true});
  }, 5000);
}

function stopPredictionSignalPolling() {
  state.predictionMarket.signalPollEpoch += 1;
  if (state.predictionMarket.signalPollId !== null) {
    window.clearInterval(state.predictionMarket.signalPollId);
    state.predictionMarket.signalPollId = null;
  }
}

function predictionRequestUrl(path) {
  if (typeof window === "undefined" || !window.location) return path;
  const scenario = new URLSearchParams(window.location.search || "").get("prediction_state");
  return scenario ? `${path}${path.includes("?") ? "&" : "?"}scenario=${encodeURIComponent(scenario)}` : path;
}

async function fetchPredictionState() {
  if (state.workspaceView !== "prediction_market") return;
  try {
    const response = await fetch(predictionRequestUrl("/api/prediction-arbitrage/state"), {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error(`prediction state ${response.status}`);
    const payload = await response.json();
    const previousHistories = state.predictionMarket.payload?.histories || {};
    state.predictionMarket.payload = {...payload, histories: {...previousHistories, ...(payload.histories || {})}};
    state.predictionMarket.error = "";
    state.predictionMarket.csrfToken = payload.csrf_token || state.predictionMarket.csrfToken;
    if (!["signals", "executions", "incidents"].includes(state.predictionMarket.historyKind)) {
      state.predictionMarket.historyKind = "signals";
    }
  } catch (error) {
    state.predictionMarket.error = error instanceof Error ? error.message : String(error);
    if (state.predictionMarket.payload) {
      state.predictionMarket.payload = {...state.predictionMarket.payload, stale: true};
    } else {
      state.predictionMarket.payload = {status: "unavailable", stale: true, readiness: {status: "unavailable"}, events: [], opportunities: [], breaker: {open: true}};
    }
  }
  renderPredictionMarket();
  const kind = state.predictionMarket.historyKind;
  if (state.predictionMarket.payload && !Array.isArray(state.predictionMarket.payload.histories?.[kind])) {
    loadPredictionHistory(kind);
  }
}

async function loadPredictionHistory(kind, options = {}) {
  const panelOnly = options.panelOnly === true;
  if (panelOnly && state.predictionMarket.signalRequestInFlight) return;
  const requestEpoch = panelOnly ? state.predictionMarket.signalPollEpoch : null;
  if (panelOnly) state.predictionMarket.signalRequestInFlight = true;
  try {
    const response = await fetch(predictionRequestUrl(`/api/prediction-arbitrage/history?kind=${encodeURIComponent(kind)}&limit=100`), {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error(`prediction history ${response.status}`);
    const result = await response.json();
    const payload = state.predictionMarket.payload || {};
    state.predictionMarket.payload = {...payload, histories: {...(payload.histories || {}), [kind]: Array.isArray(result.items) ? result.items : []}};
    if (!panelOnly) state.predictionMarket.error = "";
    if (kind === "signals") {
      state.predictionMarket.signalLastSuccessAt = new Date().toISOString();
      state.predictionMarket.signalError = "";
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (kind === "signals" && panelOnly) {
      state.predictionMarket.signalError = message;
    } else {
      state.predictionMarket.error = message;
    }
  } finally {
    if (panelOnly) state.predictionMarket.signalRequestInFlight = false;
  }
  if (panelOnly && requestEpoch === state.predictionMarket.signalPollEpoch) {
    renderPredictionSignalPanel();
  } else if (kind === state.predictionMarket.historyKind) {
    const panel = elements["prediction-market-root"]?.querySelector("[data-prediction-history-panel]");
    if (panel) {
      panel.outerHTML = predictionHistoryPanel(state.predictionMarket.payload || {});
    } else {
      renderPredictionMarket();
    }
  }
}

function renderPredictionSignalPanel() {
  if (state.workspaceView !== "prediction_market" || state.predictionMarket.strategy !== "yes_no") return;
  if (state.predictionMarket.historyKind !== "signals") return;
  const panel = elements["prediction-market-root"]?.querySelector("[data-prediction-history-panel]");
  if (!panel) return;
  panel.outerHTML = predictionHistoryPanel(state.predictionMarket.payload || {});
}

async function predictionPost(path, body) {
  const response = await fetch(predictionRequestUrl(path), {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRF-Token": state.predictionMarket.csrfToken}, body: JSON.stringify(body)});
  if (!response.ok) {
    let message = `prediction mutation ${response.status}`;
    try { message = (await response.json()).message || message; } catch (_) { /* keep status */ }
    throw new Error(message);
  }
  return response.json();
}

function predictionIdempotencyKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `prediction-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function predictionMaskedWallet(value) {
  const wallet = String(value || "").trim();
  if (!wallet || wallet.includes("…")) return wallet;
  return wallet.length >= 10 ? `${wallet.slice(0, 6)}…${wallet.slice(-4)}` : wallet;
}

function predictionPreviewDisplay(value) {
  const source = value && typeof value === "object" ? value : {};
  const result = {
    ...predictionOpportunityDisplay(source),
    preview_id: source.preview_id ?? source.id,
    title: source.title ?? source.question ?? source.market_title ?? source.event_title,
    market_type: source.market_type,
    fee_status: source.fee_status,
    merge_value: source.merge_value,
    available_balance: source.available_balance,
    wallet: source.masked_wallet ?? source.wallet_address,
    policy_limits: source.policy_limits,
  };
  if (String(result.market_type || "") === "threshold_hedge") {
    result.question_a = source.question_a;
    result.question_b = source.question_b;
    result.relation = source.relation;
    result.condition_id_a = source.condition_id_a;
    result.condition_id_b = source.condition_id_b;
    result.buy_legs = Array.isArray(source.buy_legs) ? source.buy_legs : [];
    result.maximum_fee = source.maximum_fee;
    result.minimum_payout = source.minimum_payout;
    result.rules_hash_a = source.rules_hash_a;
    result.rules_hash_b = source.rules_hash_b;
    result.cache_key = source.cache_key;
  }
  if (predictionIsCrossVenue(result)) {
    result.buy_legs = Array.isArray(source.buy_legs) ? source.buy_legs : [];
    result.net_quantity = source.net_quantity;
    result.minimum_payout = source.minimum_payout;
    result.annualized_yield = source.annualized_yield;
    result.canonical_cutoff = source.canonical_cutoff;
    result.codex_approval = source.codex_approval;
    result.balances = source.balances && typeof source.balances === "object" && !Array.isArray(source.balances) ? source.balances : {};
    result.unsettled = source.unsettled;
  }
  return result;
}

function predictionPreviewIsComplete(value) {
  const preview = predictionPreviewDisplay(value);
  if (predictionIsCrossVenue(preview)) {
    const legs = Array.isArray(preview.buy_legs) ? preview.buy_legs : [];
    const balances = preview.balances && typeof preview.balances === "object" && !Array.isArray(preview.balances) ? preview.balances : {};
    const evidence = Array.isArray(preview.codex_approval?.evidence) ? preview.codex_approval.evidence : [];
    const exchanges = new Set(legs.map((leg) => String(leg?.exchange || "").toLowerCase()));
    const tokenIds = new Set(legs.map((leg) => leg?.token_id));
    const policy = preview.policy_limits || {};
    const finitePositive = (item, maximum) => {
      const number = Number(item);
      return predictionHasValue(item)
        && Number.isFinite(number)
        && number > 0
        && (maximum === undefined || number <= maximum);
    };
    const finiteNonNegative = (item) => {
      const number = Number(item);
      return predictionHasValue(item) && Number.isFinite(number) && number >= 0;
    };
    const identity = (item) => typeof item === "string" && item.trim().length > 0;
    const cutoffTimestamp = predictionCanonicalUtcCutoff(preview.canonical_cutoff);
    const expectedMapping = {
      predict_yes: "YES",
      predict_no: "NO",
      polymarket_yes: "YES",
      polymarket_no: "NO",
    };
    const mapping = preview.codex_approval?.direct_outcome_mapping;
    const mappingValid = mapping
      && typeof mapping === "object"
      && !Array.isArray(mapping)
      && Object.keys(mapping).length === Object.keys(expectedMapping).length
      && Object.entries(expectedMapping).every(([key, expected]) => mapping[key] === expected);
    const expectedOutcomes = {
      PREDICT_YES_POLYMARKET_NO: {"predict.fun": "YES", polymarket: "NO"},
      POLYMARKET_YES_PREDICT_NO: {"predict.fun": "NO", polymarket: "YES"},
    }[String(preview.direction || "").trim().toUpperCase()];
    const identitiesValid = legs.length === 2
      && exchanges.size === 2
      && exchanges.has("predict.fun")
      && exchanges.has("polymarket")
      && tokenIds.size === 2
      && expectedOutcomes
      && legs.every((leg) => (
        expectedOutcomes[leg?.exchange]
        && leg?.outcome === expectedOutcomes[leg?.exchange]
        && [leg?.exchange, leg?.market_id, leg?.condition_id, leg?.token_id, leg?.settlement_asset, leg?.fee_asset]
          .every(identity)
      ));
    const economicsValid = legs.every((leg) => (
      finitePositive(leg?.net_quantity)
      && finitePositive(leg?.max_price, 1)
      && finitePositive(leg?.max_cost)
      && finiteNonNegative(leg?.maximum_fee)
    ));
    const numericFieldsValid = [
      preview.net_quantity, preview.max_cost, preview.minimum_payout, preview.profit, preview.annualized_yield,
      policy.max_normal_cost, policy.max_emergency_loss,
    ].every((item) => finitePositive(item));
    const unsettledValid = [preview.unsettled?.current, preview.unsettled?.after, preview.unsettled?.limit]
      .every(finiteNonNegative);
    return [preview.preview_id, preview.title, preview.market_type, preview.canonical_cutoff, preview.codex_approval?.summary]
      .every(predictionHasValue)
      && preview.codex_approval?.decision === "APPROVE"
      && cutoffTimestamp !== null
      && cutoffTimestamp > Date.now()
      && mappingValid
      && identitiesValid
      && economicsValid
      && numericFieldsValid
      && unsettledValid
      && ["predict.fun", "polymarket"].every((exchange) => {
        const balance = balances[exchange];
        return balance
          && [balance.wallet_address, balance.asset, balance.available_balance].every(predictionHasValue)
          && Number.isFinite(Number(balance.available_balance));
      })
      && evidence.length >= 2
      && evidence.every((item) => [item?.exchange, item?.field, item?.quote].every(predictionHasValue));
  }
  if (String(preview.market_type || "") === "threshold_hedge") {
    const legs = Array.isArray(preview.buy_legs) ? preview.buy_legs : [];
    return [preview.preview_id, preview.question_a, preview.question_b, preview.relation, preview.condition_id_a, preview.condition_id_b, preview.wallet]
      .every(predictionHasValue)
      && predictionHasValue(preview.quantity)
      && predictionHasValue(preview.max_cost)
      && predictionHasValue(preview.profit)
      && predictionHasValue(preview.available_balance)
      && predictionPolicyIsComplete(preview.policy_limits)
      && legs.length === 2
      && legs.every((leg) => [leg?.label, leg?.outcome, leg?.condition_id, leg?.quantity, leg?.max_price, leg?.max_cost].every(predictionHasValue));
  }
  const policy = preview.policy_limits;
  const textFields = [
    preview.preview_id,
    preview.title,
    preview.market_type,
    preview.fee_status,
    preview.wallet,
  ];
  const numericFields = [
    preview.quantity,
    preview.yes_price,
    preview.no_price,
    preview.yes_cost,
    preview.no_cost,
    preview.max_cost,
    preview.merge_value,
    preview.profit,
    preview.available_balance,
  ];
  return textFields.every(predictionHasValue)
    && numericFields.every((item) => predictionHasValue(item) && Number.isFinite(Number(item)))
    && predictionPolicyIsComplete(policy);
}

function predictionModalHtml(kind, data = {}) {
  const reset = kind === "reset";
  const cleanup = kind === "allowance_cleanup";
  const title = cleanup ? "确认清理 Predict 残余授权" : reset ? "确认解除交易熔断" : "确认真实下单";
  const description = reset ? "事故记录会永久保留；解除后系统才重新开放“参与”按钮。" : "确认后由 Open Trader 使用独立钱包直接签名并执行，不跳转 Polymarket。";
  if (reset) {
    const incidentId = data.incident_id || data.id || "";
    const happenedAt = data.happened_at || data.created_at || data.updated_at;
    const market = data.event_title || data.question || data.title;
    const reason = data.reason || data.message;
    const loss = data.loss ?? data.actual_loss;
    return `<section class="pm-modal" role="dialog" aria-modal="true" aria-labelledby="pm-dialog-title" tabindex="-1"><header class="pm-modal-header"><h2 id="pm-dialog-title">${title}</h2><p>${description}</p></header><div class="pm-check-list"><div class="pm-check"><span>事故时间</span><strong>${escapeHtml(predictionValue(happenedAt, "-"))}</strong></div><div class="pm-check"><span>市场</span><strong>${escapeHtml(predictionValue(market, "-"))}</strong></div><div class="pm-check"><span>原因</span><strong>${escapeHtml(reason ? predictionIncidentReasonLabel(reason) : "事故详情未返回")}</strong></div><div class="pm-check"><span>实际损失</span><strong class="pm-tone-danger">${escapeHtml(predictionSignedMoney(loss))}</strong></div></div><div class="pm-risk-note"><strong>点击后会重新读取真实账户状态</strong><p>系统将检查未完成订单、方向性敞口、待合并头寸、账户数据新鲜度，以及 relayer 和通知通道；任何一项失败都会继续保持熔断并显示后台原因。</p></div><footer class="pm-modal-actions"><button class="pm-button" type="button" data-modal-action="cancel">保持熔断</button><button class="pm-button danger" type="button" data-modal-action="reset"${incidentId ? "" : " disabled"}>重新检查并解除</button></footer></section>`;
  }
  if (cleanup) {
    const armed = data.armed === true;
    return `<section class="pm-modal" role="dialog" aria-modal="true" aria-labelledby="pm-dialog-title" tabindex="-1"><header class="pm-modal-header"><h2 id="pm-dialog-title">${title}</h2><p>只把 Predict Account 对 spender 的 USDT allowance 清零；不会发起转入、转出、下单或兑付。</p></header><div class="pm-check-list"><div class="pm-check"><span>owner</span><strong>${escapeHtml(predictionValue(data.owner, "-"))}</strong></div><div class="pm-check"><span>spender</span><strong>${escapeHtml(predictionValue(data.spender, "-"))}</strong></div><div class="pm-check"><span>allowance</span><strong>${escapeHtml(predictionMoney(data.before_allowance))} → ${escapeHtml(predictionMoney(data.after_allowance))}</strong></div><div class="pm-check"><span>gas effect</span><strong>${escapeHtml(predictionValue(data.gas_effect, "消耗 Privy signer BNB"))}</strong></div></div><div class="pm-risk-note" role="note"><strong>不转移 USDT</strong><p>这是授权清零交易，不是资金划转；需要二次确认后才会向本地受保护端点发送 {"confirm":true}。</p></div><footer class="pm-modal-actions"><button class="pm-button" type="button" data-modal-action="cancel">取消</button>${armed ? `<button class="pm-button danger" type="button" data-modal-action="cleanup">二次确认 · 授权清零</button>` : `<button class="pm-button danger" type="button" data-modal-action="arm-cleanup">我知道这会消耗 BNB</button>`}</footer></section>`;
  }
  const opportunity = predictionPreviewDisplay(data);
  const policy = opportunity.policy_limits || {};
  const walletCap = predictionMoney(policy.max_wallet_balance);
  const normalCap = predictionMoney(policy.max_normal_cost);
  const emergencyCap = predictionMoney(policy.max_emergency_loss);
  const minimumProfit = predictionMoney(policy.min_estimated_profit);
  if (predictionIsCrossVenue(opportunity)) {
    const legs = Array.isArray(opportunity.buy_legs) ? opportunity.buy_legs : [];
    const balances = opportunity.balances && typeof opportunity.balances === "object" && !Array.isArray(opportunity.balances) ? opportunity.balances : {};
    const approval = opportunity.codex_approval && typeof opportunity.codex_approval === "object" ? opportunity.codex_approval : {};
    const evidence = Array.isArray(approval.evidence) ? approval.evidence : [];
    const unsettled = opportunity.unsettled && typeof opportunity.unsettled === "object" ? opportunity.unsettled : {};
    const balanceSummary = ["predict.fun", "polymarket"].map((exchange) => {
      const balance = balances[exchange];
      if (!balance) return "";
      const venue = exchange === "predict.fun" ? "Predict.fun" : "Polymarket";
      return `${venue} ${predictionMoney(balance.available_balance)} ${predictionValue(balance.asset, "-")}`;
    }).filter(Boolean).join(" · ");
    const capLabel = Number(policy.max_normal_cost) <= 5 ? "首单验证" : "常规上限";
    const manualOnly = opportunity.manual_only === true;
    const manualWarning = manualOnly
      ? `<div class="pm-alert warning"><div class="pm-alert-body"><strong>结算规则可能不一致</strong><p>两所对规则有独立解释权，文字一致不保证同向结算。若两所分歧且押错方向，两腿皆输，损失 = 总成本（含手续费和 gas）。</p></div></div>`
      : "";
    const manualWorstRow = manualOnly
      ? `<div class="pm-check"><span>最坏损失（分歧）</span><strong>${escapeHtml(predictionMoney(opportunity.total_max_cost))}</strong></div>`
      : "";
    return `<section class="pm-modal" role="dialog" aria-modal="true" aria-labelledby="pm-dialog-title" tabindex="-1"><header class="pm-modal-header"><h2 id="pm-dialog-title">${title}</h2><p>确认后分别向 Predict.fun 与 Polymarket 提交受限 FOK 订单；确认前不会提交任何订单。</p></header>${manualWarning}<div class="pm-order-market"><span>Predict.fun × Polymarket · 跨所 YES/NO · 统一结算截止 ${escapeHtml(predictionValue(opportunity.canonical_cutoff, "未返回"))} · 统一截止</span><strong>${escapeHtml(predictionValue(opportunity.title, "数据未返回"))}</strong></div><div class="pm-order-legs">${legs.map((leg, index) => { const ids = [leg.market_id, leg.condition_id, leg.token_id].filter(predictionHasValue).join(" · "); const link = leg.official_url ? `<a href="${escapeHtml(leg.official_url)}" target="_blank" rel="noreferrer">${escapeHtml(leg.official_url)}</a>` : ""; return `<article class="pm-order-leg"><span>第 ${index + 1} 腿 · ${escapeHtml(predictionVenueLegLabels([leg]).replace(" · ", " · BUY "))} · FOK · ${escapeHtml(predictionValue(leg.settlement_asset, "资产未返回"))}</span><strong>冻结数量 ${escapeHtml(predictionValue(leg.net_quantity, "-"))} 净份 · 最高 ${escapeHtml(predictionPrice(leg.max_price))} · 最高价冻结</strong><small>最大成本 ${escapeHtml(predictionMoney(leg.max_cost))} · 手续费 ${escapeHtml(predictionMoney(leg.maximum_fee))} ${escapeHtml(predictionValue(leg.fee_asset, "费用资产未返回"))}</small>${ids ? `<small>Native IDs ${escapeHtml(ids)}</small>` : ""}${link ? `<small>官方链接 ${link}</small>` : ""}${leg.quote_at ? `<small>数据时间 ${escapeHtml(predictionValue(leg.quote_at))}</small>` : ""}</article>`; }).join("")}</div><div class="pm-order-summary"><div><span>净可兑付份额</span><strong>${escapeHtml(predictionValue(opportunity.net_quantity, "-"))}</strong></div><div><span>含费最大成本</span><strong>${escapeHtml(predictionMoney(opportunity.max_cost))}</strong></div><div><span>最低赔付</span><strong>${escapeHtml(predictionMoney(opportunity.minimum_payout))}</strong></div><div><span>最低净利润</span><strong class="pm-positive">${escapeHtml(predictionSignedMoney(opportunity.profit))}</strong></div><div><span>简单年化</span><strong>${escapeHtml(predictionAnnualizedPercent(opportunity.annualized_yield, 2))}</strong></div></div><div class="pm-risk-note" role="note"><strong>两笔订单不是原子交易</strong><p>可能只成交一腿。你授权系统最多承担 ${escapeHtml(emergencyCap)} 预计损失进行补腿或平仓；两所分别结算后自动兑付，异常会熔断。</p></div><div class="pm-check-list"><div class="pm-check"><span>${capLabel} · 单笔成本上限</span><strong>${escapeHtml(normalCap)}</strong></div><div class="pm-check"><span>应急损失上限</span><strong>${escapeHtml(emergencyCap)}</strong></div><div class="pm-check"><span>Codex ${escapeHtml(predictionValue(approval.decision, "未返回"))}</span><strong>Codex ${escapeHtml(predictionValue(approval.reviewed_at || approval.review_time || approval.updated_at, predictionValue(approval.decision, "未返回")))} · ${escapeHtml(predictionValue(approval.summary, "结构化理由未返回"))}</strong></div>${evidence.map((item) => `<div class="pm-check"><span>${escapeHtml(predictionValue(item.exchange, "交易所"))} · ${escapeHtml(predictionValue(item.field, "证据"))}</span><strong>${escapeHtml(predictionValue(item.quote, "证据未返回"))}</strong></div>`).join("")}<div class="pm-check"><span>可用余额</span><strong>${escapeHtml(balanceSummary || "余额数据未返回")}</strong></div><div class="pm-check"><span>待结算占用</span><strong>${escapeHtml(`${predictionMoney(unsettled.current)} → ${predictionMoney(unsettled.after)} / ${predictionMoney(unsettled.limit)}`)}</strong></div><div class="pm-check"><span>未结算上限</span><strong>${escapeHtml(predictionMoney(policy.max_cross_unsettled_principal ?? unsettled.limit))}</strong></div><div class="pm-check"><span>自动兑付</span><strong>两所分别完成结算后读取余额和回执</strong></div><div class="pm-check"><span>确认时处理</span><strong>重新读取两所 REST、盘口、余额和未结算额度</strong></div>${manualWorstRow}</div><footer class="pm-modal-actions"><button class="pm-button" type="button" data-modal-action="cancel">取消</button><button class="pm-button primary" type="button" data-modal-action="confirm">确认下单 · 最多 ${escapeHtml(normalCap)}</button></footer></section>`;
  }
  if (String(opportunity.market_type || "") === "threshold_hedge") {
    const legs = Array.isArray(opportunity.buy_legs) ? opportunity.buy_legs : [];
    return `<section class="pm-modal" role="dialog" aria-modal="true" aria-labelledby="pm-dialog-title" tabindex="-1"><header class="pm-modal-header"><h2 id="pm-dialog-title">${title}</h2><p>${description}</p></header><div class="pm-order-market"><span>Polymarket · 阈值关系套利 · ${escapeHtml(predictionValue(opportunity.relation, "关系未返回"))}</span><strong>${escapeHtml(predictionValue(opportunity.question_a, "数据未返回"))}<br>${escapeHtml(predictionValue(opportunity.question_b, "数据未返回"))}</strong></div><div class="pm-order-legs">${legs.map((leg) => `<article class="pm-order-leg"><span>${escapeHtml(predictionValue(leg.label))} · BUY ${escapeHtml(predictionValue(leg.outcome))} · FOK · ${escapeHtml(predictionConditionLabel(leg.condition_id))}</span><strong>${escapeHtml(predictionValue(leg.quantity, "-"))} 份 @ 最高 ${escapeHtml(predictionPrice(leg.max_price))}</strong><small>最大成本 ${escapeHtml(predictionMoney(leg.max_cost))} · 全成或全撤</small></article>`).join("")}</div><div class="pm-order-summary"><div><span>含费最大成本</span><strong>${escapeHtml(predictionMoney(opportunity.max_cost))}</strong></div><div><span>最低收回</span><strong>${escapeHtml(predictionMoney(opportunity.minimum_payout))}</strong></div><div><span>最低净利润</span><strong class="pm-positive">${escapeHtml(predictionSignedMoney(opportunity.profit))}</strong></div></div><div class="pm-risk-note" role="note"><strong>两笔订单不是原子交易，也不会 merge</strong><p>两个 condition 必须分别成交和核对；若只成交一腿，系统最多按 ${escapeHtml(emergencyCap)} 预计补腿或平仓，超过上限就熔断并通知。</p></div><div class="pm-check-list"><div class="pm-check"><span>规则校验</span><strong>${escapeHtml(predictionLlmDecisionLabel(opportunity))} · ${escapeHtml(predictionValue(opportunity.llm_summary, "结构化理由未返回"))}</strong></div><div class="pm-check"><span>独立钱包</span><strong>${escapeHtml(predictionMaskedWallet(opportunity.wallet))} · 可用 ${escapeHtml(predictionMoney(opportunity.available_balance))} pUSD</strong></div><div class="pm-check"><span>确认时处理</span><strong>重新检查两条规则、两个盘口、费用、余额和地区</strong></div></div><footer class="pm-modal-actions"><button class="pm-button" type="button" data-modal-action="cancel">取消</button><button class="pm-button primary" type="button" data-modal-action="confirm">确认下单 · 最多 ${escapeHtml(normalCap)}</button></footer></section>`;
  }
  return `<section class="pm-modal" role="dialog" aria-modal="true" aria-labelledby="pm-dialog-title" tabindex="-1"><header class="pm-modal-header"><h2 id="pm-dialog-title">${title}</h2><p>${description}</p></header><div class="pm-order-market"><span>Polymarket · ${escapeHtml(predictionMarketTypeLabel(opportunity.market_type))} · ${escapeHtml(predictionFeeStatusLabel(opportunity.fee_status))}</span><strong>${escapeHtml(predictionValue(opportunity.title, "-"))}</strong></div><div class="pm-order-legs"><article class="pm-order-leg"><span>第一腿 · BUY YES · FOK</span><strong>${escapeHtml(predictionValue(opportunity.quantity, "-"))} 份 @ 最高 ${escapeHtml(predictionPrice(opportunity.yes_price))}</strong><small>最大成本 ${escapeHtml(predictionMoney(opportunity.yes_cost))} · 全成或全撤</small></article><article class="pm-order-leg"><span>第二腿 · BUY NO · FOK</span><strong>${escapeHtml(predictionValue(opportunity.quantity, "-"))} 份 @ 最高 ${escapeHtml(predictionPrice(opportunity.no_price))}</strong><small>最大成本 ${escapeHtml(predictionMoney(opportunity.no_cost))} · 全成或全撤</small></article></div><div class="pm-order-summary"><div><span>正常最大成本</span><strong>${escapeHtml(predictionMoney(opportunity.max_cost))}</strong></div><div><span>合并收回</span><strong>${escapeHtml(predictionMoney(opportunity.merge_value))}</strong></div><div><span>最低净利润</span><strong class="pm-positive">${escapeHtml(predictionSignedMoney(opportunity.profit))}</strong></div></div><div class="pm-risk-note" role="note"><strong>两笔订单不是原子交易</strong><p>可能只成交一腿。你授权系统最多承担 ${escapeHtml(emergencyCap)} 预计损失进行补腿或平仓；随后会熔断并通知。</p></div><div class="pm-check-list"><div class="pm-check"><span>独立钱包</span><strong>${escapeHtml(predictionMaskedWallet(opportunity.wallet))} · 可用 ${escapeHtml(predictionMoney(opportunity.available_balance))} pUSD</strong></div><div class="pm-check"><span>钱包余额上限</span><strong>${escapeHtml(walletCap)} pUSD</strong></div><div class="pm-check"><span>确认时处理</span><strong>重新检查价格、费用、余额和地区</strong></div><div class="pm-check"><span>失败规则</span><strong>净利润低于 ${escapeHtml(minimumProfit)} 就拒绝，不追价</strong></div></div><footer class="pm-modal-actions"><button class="pm-button" type="button" data-modal-action="cancel">取消</button><button class="pm-button primary" type="button" data-modal-action="confirm">确认下单 · 最多 ${escapeHtml(normalCap)}</button></footer></section>`;
}

function openPredictionModal(kind, trigger, data) {
  predictionModal = {kind, previousFocus: trigger || document.activeElement, busy: false, data: data || {}};
  const root = elements["prediction-market-modal-root"];
  root.innerHTML = predictionModalHtml(kind, data);
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => root.querySelector(".pm-modal")?.focus());
}

function closePredictionModal() {
  const previous = predictionModal.previousFocus;
  predictionModal = {kind: "", previousFocus: null, busy: false, data: null};
  elements["prediction-market-modal-root"].innerHTML = "";
  document.body.style.overflow = "";
  if (previous?.matches?.("[data-action='participate']")) previous.disabled = false;
  previous?.focus?.();
}

function setPredictionModalBusy(busy) {
  predictionModal.busy = busy;
  elements["prediction-market-modal-root"].querySelectorAll("button").forEach((button) => {
    button.disabled = busy;
  });
}

async function handlePredictionMarketClick(event) {
  const strategy = event.target.closest("[data-prediction-strategy]");
  if (strategy) {
    state.predictionMarket.strategy = strategy.dataset.predictionStrategy === "llm_hedge"
      ? "llm_hedge"
      : "yes_no";
    if (state.predictionMarket.strategy === "yes_no") {
      startPredictionSignalPolling();
    } else {
      stopPredictionSignalPolling();
    }
    renderPredictionMarket();
    return;
  }
  const history = event.target.closest("[data-history]");
  if (history) {
    state.predictionMarket.historyKind = history.dataset.history || "signals";
    renderPredictionMarket();
    loadPredictionHistory(state.predictionMarket.historyKind);
    return;
  }
  const reset = event.target.closest("[data-action='open-reset']");
  if (reset) {
    openPredictionModal("reset", reset, state.predictionMarket.payload?.breaker?.incident || {incident_id: reset.dataset.incidentId});
    return;
  }
  const cleanup = event.target.closest("[data-action='open-allowance-cleanup']");
  if (cleanup) {
    openPredictionModal("allowance_cleanup", cleanup, state.predictionMarket.payload?.predict_allowance_cleanup || {});
    return;
  }
  const pauseCrossAuto = event.target.closest("[data-action='pause-cross-auto']");
  if (pauseCrossAuto) {
    pauseCrossAuto.disabled = true;
    try {
      await predictionPost("/api/prediction-arbitrage/cross-auto/pause", {confirm: true});
    } catch (error) {
      state.predictionMarket.error = error instanceof Error ? error.message : String(error);
    }
    await fetchPredictionState();
    return;
  }
  const modeButton = event.target.closest("[data-action='set-mode']");
  if (modeButton) {
    const mode = String(modeButton.dataset.mode || "");
    if (!["observe_only", "manual", "auto"].includes(mode)) return;
    try {
      await predictionPost("/api/prediction-arbitrage/mode", {mode});
    } catch (error) {
      state.predictionMarket.error = error instanceof Error ? error.message : String(error);
    }
    await fetchPredictionState();
    return;
  }
  const participate = event.target.closest("[data-action='participate']");
  if (!participate || participate.disabled) return;
  const opportunityId = participate.dataset.opportunityId;
  if (!opportunityId) return;
  participate.disabled = true;
  try {
    const preview = await predictionPost("/api/prediction-arbitrage/preview", {opportunity_id: opportunityId});
    if (!preview || preview.state !== "previewed" || !String(preview.preview_id || "").trim()) {
      const reason = String(preview?.reason || "preview_unavailable");
      const messages = {
        circuit_breaker_open: "交易已熔断，暂不允许下单。",
        active_execution: "已有另一笔交易正在执行，请等待完成。",
        execution_lock: "已有另一笔操作正在确认，请稍后重试。",
        opportunity_unavailable: "机会已变化或已失效，请刷新后重新检查。",
        readiness_unavailable: "交易钱包、地区或余额检查未通过。",
      };
      state.predictionMarket.error = messages[reason] || `后台拒绝预览：${reason}`;
      participate.disabled = false;
      renderPredictionMarket();
      return;
    }
    if (!predictionPreviewIsComplete(preview)) {
      state.predictionMarket.error = "预览数据不完整，未下单";
      participate.disabled = false;
      renderPredictionMarket();
      return;
    }
    openPredictionModal("order", participate, preview);
  } catch (error) {
    state.predictionMarket.error = error instanceof Error ? error.message : String(error);
    participate.disabled = false;
    renderPredictionMarket();
  }
}

async function handlePredictionModalClick(event) {
  const action = event.target.closest("[data-modal-action]")?.dataset.modalAction;
  if (!action || predictionModal.busy) return;
  if (action === "cancel") {
    closePredictionModal();
    return;
  }
  setPredictionModalBusy(true);
  try {
    if (action === "arm-cleanup") {
      predictionModal.data = {...(predictionModal.data || {}), armed: true};
      elements["prediction-market-modal-root"].innerHTML = predictionModalHtml("allowance_cleanup", predictionModal.data);
      elements["prediction-market-modal-root"].querySelector(".pm-modal")?.focus();
      predictionModal.busy = false;
      return;
    }
    if (action === "cleanup") {
      const result = await predictionPost("/api/prediction-arbitrage/predict-allowance/cleanup", {confirm: true});
      if (!result || ["locked", "busy", "rejected"].includes(String(result.state || "").toLowerCase())) {
        throw new Error("授权清零未完成，系统继续保持只读。");
      }
      closePredictionModal();
      await fetchPredictionState();
      return;
    }
    if (action === "confirm") {
      const previewId = predictionModal.data?.preview_id || predictionModal.data?.id;
      if (!String(previewId || "").trim()) throw new Error("预览已失效，请重新获取机会。");
      const result = await predictionPost("/api/prediction-arbitrage/executions", {preview_id: String(previewId || ""), idempotency_key: predictionIdempotencyKey()});
      if (!result || ["locked", "busy", "rejected"].includes(String(result.state || "").toLowerCase()) || !String(result.execution_id || "").trim()) {
        throw new Error("确认时后台未接受订单，未提交新的执行任务。");
      }
      state.predictionMarket.activeExecutionId = result.execution_id || "";
      closePredictionModal();
      await fetchPredictionState();
      return;
    }
    if (action === "reset") {
      const incidentId = predictionModal.data?.incident_id || predictionModal.data?.id;
      const result = await predictionPost("/api/prediction-arbitrage/circuit-breaker/reset", {incident_id: String(incidentId || "")});
      if (!result || ["locked", "busy", "rejected"].includes(String(result.state || "").toLowerCase())) {
        const reason = String(result?.reason || "").trim();
        throw new Error(reason ? `事故仍未解除：${predictionReasonLabel(reason)}` : "事故仍未解除，系统继续保持熔断。");
      }
      closePredictionModal();
      await fetchPredictionState();
    }
  } catch (error) {
    state.predictionMarket.error = error instanceof Error ? error.message : String(error);
    setPredictionModalBusy(false);
    renderPredictionMarket();
  }
}

function handlePredictionModalKeydown(event) {
  if (!predictionModal.kind) return;
  if (event.key === "Escape") {
    event.preventDefault();
    if (!predictionModal.busy) closePredictionModal();
    return;
  }
  if (event.key !== "Tab") return;
  const modal = elements["prediction-market-modal-root"].querySelector(".pm-modal");
  if (!modal) return;
  const focusable = [...modal.querySelectorAll("button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex='-1'])")];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function kellyExperimentStatusLabel(status) {
  const labels = {
    active: { label: "运行中", className: "status-ok" },
    completed: { label: "已完成", className: "status-ok" },
    draft: { label: "草稿", className: "status-muted" },
    paused: { label: "已暂停", className: "status-warn" },
    running: { label: "运行中", className: "status-ok" },
    stopped: { label: "已停止", className: "status-muted" },
  };
  const key = formatPlain(status).toLowerCase();
  return labels[key] || { label: formatPlain(status), className: "status-muted" };
}

function kellySampleStageLabel(stage) {
  const labels = {
    complete: "样本完成",
    enough: "样本足够",
    insufficient: "样本不足",
    open: "采样中",
    ready: "待采样",
  };
  const key = formatPlain(stage).toLowerCase();
  return labels[key] || formatPlain(stage);
}

function renderDashboardViews() {
  renderHeaderSummary();
  const broker = state.brokerFilter;
  const selector = `#account-${broker}-view-panel`;
  const container = elements["account-holdings"] || elements["holdings-body"];
  const frozenPanel = state.accountViews[broker] === "report" && state.trendReportHistories[broker]?.open
    ? container?.querySelector(selector)
    : null;
  renderAccountHoldings();
  if (frozenPanel) container?.querySelector(selector)?.replaceWith(frozenPanel);
}

const TREND_REASON_LABELS = {
  protection_line_already_triggered: "活动保护线已触发",
  danger_signal: "危险信号触发",
  left_trend_right_side: "右侧趋势已结束",
  holding_signal_unknown: "趋势信号不完整",
  symbol_mapping_conflict: "趋势代码映射异常",
  holding_trend_excluded: "已排除趋势查询",
  holding_kline_unavailable: "持仓日线数据不可用",
  holding_lot_size_unavailable: "持仓整手信息不可用",
  trend_intact: "趋势保持完好",
  temperature_changed_to_flat: "趋势温度转平",
  overheat_take_profit: "沸腾/开香槟过热止盈",
  a_share_only: "仅限 A 股股票",
  temperature_missing: "个股趋势温度缺失",
  temperature_transition_not_entry: "不是温转热或温转沸",
  filter_price_missing: "筛选价缺失",
  filter_price_above_200: "筛选价高于 200 元",
  strength_missing: "趋势强度缺失",
  strength_below_95: "趋势强度低于 95",
  industry_id_missing: "行业 ID 缺失",
  industry_temperature_missing: "行业温度缺失",
  industry_temperature_not_hot: "行业温度未达到要求",
  phase_missing: "趋势节气缺失",
  phase_after_summer_solstice: "趋势节气晚于夏至",
  market_cap_missing: "市值缺失",
  market_cap_below_100: "市值低于 100 亿元",
  amount_missing: "日成交额缺失",
  amount_below_2: "日成交额不足 2 亿元",
  right_side_days_missing: "右侧天数缺失",
  right_side_not_true: "尚未进入右侧趋势",
  strength_not_above_90: "趋势强度未超过 90",
  right_side_days_not_below_10: "进入右侧趋势已满 10 天",
  not_tradable: "当前不可交易",
  amount_below_1: "日成交额不足 1 亿元",
  danger_unknown: "危险信号未知",
  name_missing: "标的名称缺失",
  asset_missing: "资产类型缺失",
  unsupported_asset: "不属于 A 股股票或境内 ETF",
  already_held: "当前账户已经持有",
  excluded_security: "北交所、ST 或退市标的",
  unsupported_exchange: "不属于沪深市场",
  atr_unavailable: "缺少 ATR 数据",
  data_date_mismatch: "数据日期不一致",
  strategy_identity_not_eligible: "策略身份不匹配",
  round_not_attributed: "无法归属策略",
  costs_incomplete: "成本不完整",
  net_return_unavailable: "净收益不可用",
  no_matching_opening_strategy_action: "无匹配开仓策略动作",
  scaled_entry_attribution_conflict: "加仓归属冲突",
};

const CURRENT_TREND_EXIT_DISCIPLINES = new Set(["CN:v9", "US:v6", "HK:v6"]);

function currentTrendExitDiscipline(report) {
  const market = formatPlain(report?.market).toUpperCase();
  const version = formatPlain(report?.strategy_version);
  return CURRENT_TREND_EXIT_DISCIPLINES.has(`${market}:${version}`);
}

function trendReasonLabel(item, report) {
  const reason = formatPlain(item?.reason);
  if (reason !== "protection_line_already_triggered" || !currentTrendExitDiscipline(report)) {
    return TREND_REASON_LABELS[reason] || "未知动作或原因，需人工确认";
  }
  const initial = item?.initial_line;
  const active = item?.active_line;
  return initial !== null && initial !== undefined
    && active !== null && active !== undefined
    && String(initial) === String(active)
    ? "2×ATR14 硬止损"
    : "既有活动保护线触发";
}

function renderTrendReportEntry(broker) {
  if (!TREND_ACCOUNT_BROKERS.includes(broker)) return "";
  const report = state.dashboard?.trend_reports?.[broker] || {};
  const label = "当天趋势报告";
  const reviews = state.dashboard?.trend_reviews;
  const review = reviews?.[broker];
  const reviewLabel = `${formatPlain(review?.market_label || report.market_label || {futu:"美股",phillips:"港股",eastmoney:"A股"}[broker])}复盘`.replaceAll(" ", "");
  const reportButton = report.available
    ? `<button type="button" data-trend-report="${escapeHtml(broker)}">${label}</button>`
    : `<button type="button" disabled>${label}</button>`;
  const reviewButton = !review ? "" : review.available
    ? `<button type="button" data-trend-review="${escapeHtml(broker)}">${escapeHtml(reviewLabel)}</button>`
    : `<button type="button" disabled>${escapeHtml(reviewLabel)}</button>`;
  const details = report.available
    ? `<span>${escapeHtml(formatPlain(report.status_text || "今日已更新"))}</span><span>报告日期 ${escapeHtml(formatPlain(report.report_date))}</span><span>数据截至 ${escapeHtml(formatPlain(report.data_date))}</span>`
    : `<span>${escapeHtml(formatPlain(report.status_text || "今日暂无趋势报告"))}</span>`;
  const reviewStatus = review && !review.available
    ? `<span>${escapeHtml(formatPlain(review.status_text || "暂无复盘数据"))}</span>`
    : "";
  return `<div class="trend-report-entry${report.available ? "" : " trend-report-entry-empty"}">
    <div class="trend-entry-buttons">${reportButton}${reviewButton}</div>
    <div class="trend-entry-details">${details}${reviewStatus}</div>
  </div>`;
}

const TREND_REVIEW_METRICS = [
  {key:"period_net_return", label:"期间净收益率", percent:true},
  {key:"market_excess_return", label:"相对市场超额收益", percent:true},
  {key:"max_drawdown", label:"最大回撤", percent:true},
  {key:"calmar", label:"卡玛比率", percent:false},
  {key:"sharpe", label:"夏普比率", percent:false},
];

const TREND_REVIEW_SERIES = [
  {key:"discipline", label:"纪律模拟", shape:"solid-circle"},
  {key:"actual", label:"实际执行", shape:"hollow-circle"},
  {key:"same_period_benchmark", label:"同期市场", shape:"diamond"},
  {key:"market_1y", label:"市场 1 年", shape:"square"},
  {key:"market_5y", label:"市场 5 年", shape:"ring"},
];

function formatTrendReviewValue(cell, percent) {
  const value = numericValue(cell?.value);
  if (value === null) return formatPlain(cell?.reason || "数据不足");
  const formatted = value.toLocaleString("zh-CN", {maximumFractionDigits:2});
  return percent ? `${formatted}%` : formatted;
}

function trendReviewWindow(review, series, metric) {
  const context = review.benchmark_context || {};
  if (series.key === "discipline" || series.key === "actual") {
    const cutoff = review.metric_cutoffs?.[series.key];
    return cutoff ? `同期 · 截至 ${formatPlain(cutoff)}` : "同期";
  }
  if (series.key === "same_period_benchmark") {
    const dates = Array.isArray(context.same_period_dates) ? context.same_period_dates : [];
    return dates.length ? `同期 · ${formatPlain(dates[0])} 至 ${formatPlain(dates[dates.length - 1])}` : "同期";
  }
  const windowKey = series.key === "market_1y" ? "1Y" : "5Y";
  const window = context.windows?.[windowKey] || {};
  const basis = windowKey === "5Y" && metric.key === "period_net_return" ? " · CAGR" : "";
  return `${windowKey === "1Y" ? "1 年" : "5 年"}${window.start && window.cutoff ? ` · ${formatPlain(window.start)} 至 ${formatPlain(window.cutoff)}` : ""}${basis}`;
}

function renderTrendReviewMetric(review, metric) {
  const values = TREND_REVIEW_SERIES.map((series) => numericValue(review.metrics?.[metric.key]?.[series.key]?.value));
  const numeric = values.filter((value) => value !== null);
  let minimum = Math.min(0, ...numeric);
  let maximum = Math.max(0, ...numeric);
  if (minimum === maximum) {
    minimum = -1;
    maximum = 1;
  }
  const range = maximum - minimum;
  const zeroPosition = Math.round((0 - minimum) / range * 10000) / 100;
  const points = TREND_REVIEW_SERIES.map((series, index) => {
    const value = values[index];
    if (value === null) return "";
    const cell = review.metrics?.[metric.key]?.[series.key] || {};
    const display = formatTrendReviewValue(cell, metric.percent);
    const window = trendReviewWindow(review, series, metric);
    const position = Math.round((value - minimum) / range * 10000) / 100;
    return `<i class="trend-review-point trend-review-shape-${series.shape}" data-series="${series.key}" data-value="${value}" style="--trend-review-position:${position}%" aria-label="${escapeHtml(`${series.label}，${metric.label}，${display}，${window}`)}"></i>`;
  }).join("");
  const rows = TREND_REVIEW_SERIES.map((series) => {
    const cell = review.metrics?.[metric.key]?.[series.key] || {};
    const value = numericValue(cell.value);
    const display = formatTrendReviewValue(cell, metric.percent);
    const window = trendReviewWindow(review, series, metric);
    return `<li class="trend-review-series${value === null ? " unavailable" : ""}" data-series="${series.key}" aria-label="${escapeHtml(`${series.label}，${metric.label}，${display}，${window}`)}">
      <span><i class="trend-review-marker trend-review-shape-${series.shape}" aria-hidden="true"></i>${escapeHtml(series.label)}</span>
      <span class="trend-review-window">${escapeHtml(window)}</span>
      <strong>${escapeHtml(display)}</strong>
    </li>`;
  }).join("");
  return `<section class="trend-review-metric" data-domain-min="${minimum}" data-domain-max="${maximum}" style="--trend-review-zero:${zeroPosition}%">
    <h3>${escapeHtml(metric.label)}</h3>
    <div class="trend-review-axis" role="img" aria-label="${escapeHtml(`${metric.label}共享数值标尺`)}">${points}</div>
    <div class="trend-review-domain" aria-hidden="true"><span>${escapeHtml(formatTrendReviewValue({value:minimum}, metric.percent))}</span><span>${escapeHtml(formatTrendReviewValue({value:maximum}, metric.percent))}</span></div>
    <ul class="trend-review-values">${rows}</ul>
  </section>`;
}

function renderTrendReviewStatisticsMeta(review, key, label) {
  const detail = review.sample_details?.[key];
  const sampleCutoff = review.sample_cutoffs?.[key];
  const metricCutoff = review.metric_cutoffs?.[key];
  const disposition = detail?.available === true
    ? `<span>发现 ${detail.discovered_candidate_count} · 排除 ${detail.excluded_candidate_count} · 未闭环 ${detail.incomplete_open_candidate_count}</span>`
    : "<span>统计来源不可用</span>";
  const exclusions = Array.isArray(detail?.exclusion_reasons) && detail.exclusion_reasons.length
    ? `<span>排除原因 ${detail.exclusion_reasons.map((item) => `${TREND_REASON_LABELS[item.reason] || "其他原因"} ${item.count}`).join("、")}</span>`
    : "";
  return `<div class="trend-review-statistics" data-series="${key}"><strong>${escapeHtml(label)}</strong>
    <div class="trend-entry-details">
      ${sampleCutoff ? `<span>统计截至 ${escapeHtml(formatPlain(sampleCutoff))}</span>` : ""}
      ${metricCutoff ? `<span>指标截至 ${escapeHtml(formatPlain(metricCutoff))}</span>` : ""}
      ${disposition}${exclusions}
    </div></div>`;
}

function renderTrendReviewMatrix(review) {
  const refresh = review.benchmark_refresh || {};
  return `<figure class="trend-review-matrix"><figcaption>策略与市场基准</figcaption>
    <div class="trend-review-matrix-meta">
      ${refresh.cutoff ? `<span>市场数据截至 ${escapeHtml(formatPlain(refresh.cutoff))}</span>` : ""}
      ${refresh.completed_at ? `<span>快照更新 ${escapeHtml(formatPlain(refresh.completed_at))}</span>` : ""}
      <span>5 年收益 CAGR</span>
    </div>
    <div class="trend-review-statistics-grid">
      ${renderTrendReviewStatisticsMeta(review, "discipline", "纪律模拟")}
      ${renderTrendReviewStatisticsMeta(review, "actual", "实际执行")}
    </div>
    ${TREND_REVIEW_METRICS.map((metric) => renderTrendReviewMetric(review, metric)).join("")}
  </figure>`;
}

function formatTrendReviewSampleCount(review, key, label) {
  const count = review.sample_counts?.[key];
  if (!Number.isInteger(count)) return `${label} 数据不可用`;
  const required = Number.isInteger(review.sample_counts?.required) ? review.sample_counts.required : 30;
  return count >= required ? `${label} ${count} 笔` : `${label} ${count} / ${required}，数据不足`;
}

function formatTrendReviewStrategyVersion(value) {
  const version = formatPlain(value);
  const match = /^v(.+)$/i.exec(version);
  return match ? `第 ${match[1]} 版` : version;
}

function renderTrendReviewWorkspace(review, embedded = false) {
  const snapshot = review.strategy_snapshot || {};
  const root = embedded ? "div" : "main";
  const statisticsStatus = review.statistics_status === "failed"
    ? "<span>统计刷新失败；报告继续使用上一个有效快照</span>"
    : review.statistics_status === "stale"
      ? "<span>统计快照已过期；报告继续使用上一个有效快照</span>"
    : review.statistics_status === "unavailable"
      ? "<span>统计刷新状态不可用</span>"
      : "";
  return `<${root} class="trend-review">
    <header class="trend-review-header">
      <div><p>${escapeHtml(`${formatPlain(review.broker_label)}｜${formatPlain(review.market_label)}`)}</p>
      <h1>${escapeHtml(`${formatPlain(review.market_label)}趋势复盘`)}</h1>
      <span>${escapeHtml(formatPlain(snapshot.strategy_name))}｜${escapeHtml(formatTrendReviewStrategyVersion(snapshot.strategy_version))}</span></div>
      <div class="trend-review-header-side">
        ${embedded ? "" : '<button type="button" data-close-trend-report>返回持仓看板</button>'}
        <span>${escapeHtml(formatTrendReviewSampleCount(review,"discipline","纪律模拟"))}</span>
        <span>${escapeHtml(formatTrendReviewSampleCount(review,"actual","实际执行"))}</span>
        ${review.common_cutoff ? `<span>共同截止日 ${escapeHtml(formatPlain(review.common_cutoff))}</span>` : ""}
        ${statisticsStatus}
      </div>
    </header>
    ${renderTrendReviewMatrix(review)}
  </${root}>`;
}

function renderTrendAction(item, kind, report) {
  const identity = [item.symbol, item.name].filter(Boolean).map(formatPlain).join(" ");
  const reason = trendReasonLabel(item, report);
  const fields = [identity];
  if (kind === "buy") {
    fields.push(`约 ${formatDisplayNumber(item.estimated_shares)} 股`);
    fields.push(`金额上限 ${formatDisplayNumber(item.target_amount)}`);
    fields.push(`预计保护线 ${formatDisplayNumber(item.estimated_initial_line)}`);
  } else {
    fields.push(reason);
    if (item.active_line !== null && item.active_line !== undefined && item.active_line !== "") {
      fields.push(`活动保护线 ${formatDisplayNumber(item.active_line)}`);
    }
  }
  return `<li>${fields.map(escapeHtml).join("<span>｜</span>")}</li>`;
}

function renderTrendStage(title, items, kind, report) {
  const rows = Array.isArray(items)
    ? items.filter((item) => item && typeof item === "object" && !Array.isArray(item))
    : [];
  return `<section class="trend-stage">
    <h2>${escapeHtml(title)}</h2>
    ${rows.length ? `<ol>${rows.map((item) => renderTrendAction(item, kind, report)).join("")}</ol>` : "<p>无</p>"}
  </section>`;
}

function renderTrendAudit(audit) {
  const candidates = Array.isArray(audit.candidates)
    ? audit.candidates.filter((item) => item && typeof item === "object" && !Array.isArray(item))
    : [];
  const excluded = audit.excluded && typeof audit.excluded === "object" && !Array.isArray(audit.excluded) ? audit.excluded : {};
  const accountExceptions = Array.isArray(audit.account_exceptions) ? audit.account_exceptions : [];
  const industries = Array.isArray(audit.industry_concentration)
    ? audit.industry_concentration.filter(Array.isArray)
    : [];
  const dataSources = Array.isArray(audit.data_sources) ? audit.data_sources : [];
  return `<details class="trend-audit"><summary>审计详情</summary>
    <section><h3>候选榜</h3><ol>${candidates.length
      ? candidates.map((item) => `<li>${escapeHtml([item.symbol, item.name, `强度 ${formatDisplayNumber(item.strength)}`].filter(Boolean).map(formatPlain).join("｜"))}</li>`).join("")
      : "<li>无</li>"}</ol></section>
    <section><h3>排除项</h3><ul>${Object.entries(excluded).length
      ? Object.entries(excluded).map(([symbol, reasons]) => `<li>${escapeHtml(formatPlain(symbol))}｜${escapeHtml((Array.isArray(reasons) ? reasons : []).map((reason) => TREND_REASON_LABELS[reason] || "未知原因").join("、"))}</li>`).join("")
      : "<li>无</li>"}</ul></section>
    <section><h3>账户不参与项</h3><ul>${accountExceptions.length
      ? accountExceptions.map((item) => `<li>${escapeHtml(formatPlain(item))}</li>`).join("")
      : "<li>无</li>"}</ul></section>
    <section><h3>行业集中度</h3><ul>${industries.length
      ? industries.map((item) => `<li>${escapeHtml(item.map((value, index) => index ? formatDisplayNumber(value) : formatPlain(value)).join("｜"))}</li>`).join("")
      : "<li>无</li>"}</ul></section>
    <p>数据来源：${escapeHtml(dataSources.map(formatPlain).join("、") || "无")}</p>
    <p>API 成本：${escapeHtml(formatDisplayNumber(audit.actual_api_cost ?? audit.estimated_api_cost ?? "未知"))}</p>
  </details>`;
}

function cnTrendRows(items) {
  return Array.isArray(items)
    ? items.filter((item) => item && typeof item === "object" && !Array.isArray(item))
    : [];
}

function usesFinalPlanTrendAudit(report) {
  const market = String(report?.market || "").toUpperCase();
  const version = String(report?.strategy_version || "");
  return (market === "CN" && version === "v13")
    || (["HK", "US"].includes(market) && version === "v11");
}

function renderCnTrendCell(label, value, ariaLabel = "", missingLabel = "—") {
  const display = hasValue(value) ? formatPlain(value) : missingLabel;
  return `<td data-label="${escapeHtml(label)}"${ariaLabel ? ` aria-label="${escapeHtml(ariaLabel)}"` : ""}>${escapeHtml(display)}</td>`;
}

function cnTrendIdentity(item) {
  return [item.symbol, item.name].filter(hasValue).map(formatPlain).join(" ") || "-";
}

function cnTrendTemperature(item) {
  return `${formatPlain(item.temperature_prev)} → ${formatPlain(item.temperature_curr)}`;
}

function trendTemperature(item) {
  if (!hasValue(item?.temperature_prev) || !hasValue(item?.temperature_curr)) return null;
  return `${formatPlain(item.temperature_prev)} → ${formatPlain(item.temperature_curr)}`;
}

function renderTrendCell(label, value, ariaLabel = "") {
  return renderCnTrendCell(label, value, ariaLabel, "数据未提供");
}

function trendIdentity(item) {
  const identity = [item?.symbol, item?.name].filter(hasValue).map(formatPlain).join(" ");
  return identity || null;
}

function renderTrendOptionDialog(item, anomaly) {
  const identity = trendIdentity(item) || "数据未提供";
  const categories = Array.isArray(anomaly.categories)
    ? anomaly.categories.filter((category) => category && typeof category === "object" && !Array.isArray(category))
    : [];
  const categoryRows = categories.length
    ? categories.map((category) => `
      <article class="trend-option-dialog-category">
        <header><strong>${escapeHtml(formatPlain(category.name || "缺失"))}</strong><span>${escapeHtml(translateFutuSignalValue(category.direction || category.state))}</span></header>
        <dl>
          <div><dt>状态</dt><dd>${escapeHtml(translateFutuSignalValue(category.state))}</dd></div>
          <div><dt>方向</dt><dd>${escapeHtml(translateFutuSignalValue(category.direction))}</dd></div>
          <div><dt>详情</dt><dd>${escapeHtml(formatPlain(category.detail || "缺失"))}</dd></div>
          <div><dt>证据日期</dt><dd>${escapeHtml(formatPlain(category.evidence_date || "数据未提供"))}</dd></div>
        </dl>
      </article>`).join("")
    : `<p class="trend-option-dialog-empty">未找到可展示的结构化类别。</p>`;
  const windowDays = hasValue(anomaly.window_days)
    ? `${formatPlain(anomaly.window_days)} 天`
    : "数据未提供";
  return `<dialog class="trend-option-dialog" aria-label="富途期权异动详情：${escapeHtml(identity)}">
    <header class="trend-option-dialog-header">
      <div><p>富途期权异动</p><h3>${escapeHtml(identity)}</h3></div>
      <button type="button" data-option-anomaly-close aria-label="关闭期权异动详情">×</button>
    </header>
    <dl class="trend-option-dialog-meta">
      <div><dt>数据源</dt><dd>富途</dd></div>
      <div><dt>标的</dt><dd>${escapeHtml(identity)}</dd></div>
      <div><dt>运行日期</dt><dd>${escapeHtml(formatPlain(anomaly.run_date || "数据未提供"))}</dd></div>
      <div><dt>观察窗口</dt><dd>${escapeHtml(windowDays)}</dd></div>
    </dl>
    <section class="trend-option-dialog-summary"><h4>摘要</h4><p>${escapeHtml(formatPlain(anomaly.summary || "数据未提供"))}</p></section>
    <dl class="trend-option-dialog-signal">
      <div><dt>信号</dt><dd>${escapeHtml(translateFutuSignalValue(anomaly.signal))}</dd></div>
      <div><dt>置信度</dt><dd>${escapeHtml(translateFutuSignalValue(anomaly.confidence))}</dd></div>
      <div><dt>建议约束</dt><dd>${escapeHtml(translateFutuSignalValue(anomaly.suggested_constraint))}</dd></div>
    </dl>
    <section class="trend-option-dialog-categories"><h4>异动类别</h4><div>${categoryRows}</div></section>
    <footer><button type="button" data-option-anomaly-close>关闭</button></footer>
  </dialog>`;
}

function renderTrendOptionIdentityCell(item) {
  const anomaly = item?.option_anomaly && typeof item.option_anomaly === "object"
    ? item.option_anomaly : {};
  const identity = escapeHtml(trendIdentity(item) || "数据未提供");
  const available = anomaly.available === true;
  const button = available
    ? `<button class="trend-option-button" type="button" data-option-anomaly-open aria-haspopup="dialog">期权异动</button>`
    : "";
  return `<td data-label="标的"><strong>${identity}</strong>${button}${available ? renderTrendOptionDialog(item, anomaly) : ""}</td>`;
}

function handleTrendOptionDialog(event) {
  const target = event?.target;
  const close = target?.closest?.("[data-option-anomaly-close]");
  if (close) {
    const dialog = close.closest("dialog");
    if (typeof dialog?.close === "function") dialog.close();
    return true;
  }
  const open = target?.closest?.("[data-option-anomaly-open]");
  if (!open) return false;
  const dialog = open.parentElement?.querySelector("dialog.trend-option-dialog");
  if (typeof dialog?.showModal === "function") dialog.showModal();
  return true;
}

function trendHints(item) {
  return Array.isArray(item?.entry_hints) && item.entry_hints.length
    ? item.entry_hints.map(formatPlain).join("；")
    : null;
}

function trendMoney(item, normalizedKey, legacyKey) {
  return hasValue(item?.[normalizedKey]) ? item[normalizedKey] : item?.[legacyKey];
}

function cnTrendHints(item) {
  return Array.isArray(item.entry_hints) && item.entry_hints.length
    ? item.entry_hints.map(formatPlain).join("；")
    : "数据不可用";
}

function trendIndustryContext(report, item) {
  const direct = item && item.industry_context && typeof item.industry_context === "object"
    ? item.industry_context : null;
  const contexts = Array.isArray(report?.industry_contexts)
    ? report.industry_contexts : [];
  const itemIndustryId = item?.industry_tm_id ?? item?.industry_id;
  return direct || contexts.find((candidate) => candidate && typeof candidate === "object"
    && ((hasValue(itemIndustryId) && String(candidate.industry_tm_id) === String(itemIndustryId))
      || (hasValue(item?.industry) && String(candidate.industry) === String(item.industry))));
}

function trendIndustryBuyContext(report, item) {
  const context = trendIndustryContext(report, item);
  if (!context) {
    return report?.industry_context_status?.current_complete === false
      ? "行业上下文无效 · 当前数据不完整" : "行业上下文未提供";
  }
  const breadth = hasValue(context.right_count) && hasValue(context.valid_count)
    && hasValue(context.right_share)
    ? `${formatDisplayNumber(context.right_count)} / ${formatDisplayNumber(context.valid_count)} = ${trendIndustryPercent(context.right_share)}`
    : "右侧占比 数据未提供";
  const parts = [
    hasValue(context.temperature) ? `温度 ${formatPlain(context.temperature)}` : "",
    hasValue(context.temperature_direction) ? `方向 ${trendIndustryDirection(context.temperature_direction)}` : "",
    breadth,
  ].filter(Boolean);
  if (context.valid === false) {
    parts.push(`无效 · ${Array.isArray(context.invalid_reasons) && context.invalid_reasons.length
      ? context.invalid_reasons.map(formatPlain).join("、") : "数据不可用"}`);
  }
  return parts.join(" · ") || "行业上下文未提供";
}

function trendIndustryBuyTemperature(report, item) {
  if (hasValue(item?.industry_temperature)) return item.industry_temperature;
  const context = trendIndustryContext(report, item);
  if (context?.valid !== false) return context?.temperature;
  const obsoleteReasons = new Set([
    "component_count_below_10",
    "valid_count_below_10",
  ]);
  const reasons = Array.isArray(context.invalid_reasons)
    ? context.invalid_reasons : [];
  return reasons.length > 0 && reasons.every((reason) => obsoleteReasons.has(reason))
    ? context.temperature : null;
}

function trendRiskPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${(number * 100).toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1")}%`;
}

function trendKellyPercent(value) {
  if (!hasValue(value)) return "禁用（固定风险仓位）";
  const number = Number(value);
  if (!Number.isFinite(number)) return "禁用（固定风险仓位）";
  return `${(number * 100).toFixed(2).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1")}%`;
}

function renderTrendTradeStats(stats) {
  if (!stats || typeof stats !== "object") return "";
  if (stats.available !== true) {
    return `<div><dt>交易统计</dt><dd>${escapeHtml(formatPlain(stats.status_text || "交易统计暂不可用"))}</dd></div>`;
  }
  const payoffLabels = {
    no_wins: "无盈利样本",
    no_losses: "无亏损样本",
    zero_denominator: "亏损均值为零",
  };
  const row = (label, item) => {
    const stat = item && typeof item === "object" ? item : {};
    const winRate = hasValue(stat.win_rate) ? trendRiskPercent(stat.win_rate) : "—";
    const payoff = hasValue(stat.payoff_ratio)
      ? formatDisplayNumber(stat.payoff_ratio)
      : (payoffLabels[stat.payoff_ratio_status] || "—");
    const sample = hasValue(stat.eligible_sample_count)
      ? formatDisplayNumber(stat.eligible_sample_count)
      : "—";
    return `<div><dt>${escapeHtml(label)}</dt><dd>胜率 ${escapeHtml(winRate)} · 盈亏比 ${escapeHtml(payoff)} · 样本 ${escapeHtml(sample)}</dd></div>`;
  };
  const actualLabel = hasValue(stats.actual_broker_label)
    ? `${formatPlain(stats.actual_broker_label)}实盘交易统计`
    : "实盘交易统计";
  return `${row("富途模拟盘交易统计", stats.simulation)}
      ${row(actualLabel, stats.actual)}`;
}

function renderTrendRiskSummary(summary, drawdown, reportDate) {
  const hasPlanRisk = summary && typeof summary === "object" && hasValue(summary.status);
  const hasDrawdown = drawdown && typeof drawdown === "object" && hasValue(drawdown.status);
  if (!hasPlanRisk && !hasDrawdown) return "";
  const planned = hasPlanRisk ? `${formatDisplayNumber(summary.portfolio_planned_risk)}（${trendRiskPercent(summary.portfolio_planned_risk_pct)} / ${trendRiskPercent(summary.portfolio_risk_limit_pct)}）` : "";
  const remaining = hasPlanRisk ? `${formatDisplayNumber(summary.portfolio_remaining_risk)}（${trendRiskPercent(summary.portfolio_remaining_risk_pct)}）` : "";
  const single = hasPlanRisk ? `${formatDisplayNumber(summary.single_entry_risk_limit)}（${trendRiskPercent(summary.single_entry_risk_limit_pct)}）` : "";
  const buffer = hasPlanRisk ? `${formatDisplayNumber(summary.abnormal_loss_buffer)}（${trendRiskPercent(summary.abnormal_loss_buffer_pct)}）` : "";
  const status = hasPlanRisk ? summary.status : drawdown.status;
  const kellyPhase = hasPlanRisk ? ({
    cold_start: "冷启动",
    active_all_samples: "全样本启用",
    active_rolling_200: "最近 200 个样本启用",
    unavailable: "统计不可用",
  })[summary.kelly_phase] || "" : "";
  const kellyRows = kellyPhase ? `
        <div><dt>Kelly 阶段</dt><dd>${escapeHtml(`${kellyPhase} · ${formatPlain(summary.kelly_eligible_sample_count)} 个合格模拟闭环`)}</dd></div>
        <div><dt>当前 Kelly 上限</dt><dd>${escapeHtml(trendKellyPercent(summary.kelly_cap))}</dd></div>` : "";
  const bootstrap = hasDrawdown && drawdown.bootstrap_event && typeof drawdown.bootstrap_event === "object"
    ? drawdown.bootstrap_event
    : null;
  const bootstrapNotice = bootstrap && hasValue(reportDate) &&
    String(bootstrap.occurred_at || "").slice(0, 10) === String(reportDate)
    ? `<p><strong>基准已自动建立</strong> · 基准净值 ${escapeHtml(formatDisplayNumber(bootstrap.baseline_equity))} · 快照日期 ${escapeHtml(formatPlain(bootstrap.source_date))}</p>`
    : "";
  const bootstrapRows = bootstrap ? `${bootstrapNotice}
      <details class="trend-drawdown-bootstrap-audit"><summary>回撤基准审计详情</summary><dl>
        <div><dt>事件</dt><dd>${escapeHtml(formatPlain(bootstrap.event_id))}</dd></div>
        <div><dt>基准净值</dt><dd>${escapeHtml(formatDisplayNumber(bootstrap.baseline_equity))}</dd></div>
        <div><dt>快照日期</dt><dd>${escapeHtml(formatPlain(bootstrap.source_date))}</dd></div>
        <div><dt>验收 Git SHA</dt><dd>${escapeHtml(formatPlain(bootstrap.accepted_git_sha))}</dd></div>
        <div><dt>参数哈希</dt><dd>${escapeHtml(formatPlain(bootstrap.parameter_hash))}</dd></div>
        <div><dt>任务身份</dt><dd>${escapeHtml(formatPlain(bootstrap.actor))}</dd></div>
        <div><dt>发生时间</dt><dd>${escapeHtml(formatPlain(bootstrap.occurred_at))}</dd></div>
        <div><dt>允许入场日期</dt><dd>${escapeHtml(formatPlain(bootstrap.entry_eligible_from))}</dd></div>
      </dl></details>` : "";
  const recovery = hasDrawdown && drawdown.recovery_event && typeof drawdown.recovery_event === "object"
    ? drawdown.recovery_event
    : null;
  const recoveryRows = recovery ? `<details class="trend-drawdown-recovery-audit"><summary>状态恢复审计详情</summary><dl>
        <div><dt>事件</dt><dd>${escapeHtml(formatPlain(recovery.event_id))}</dd></div>
        <div><dt>恢复快照</dt><dd>${escapeHtml(formatPlain(recovery.snapshot))}</dd></div>
        <div><dt>状态哈希</dt><dd>${escapeHtml(formatPlain(recovery.state_sha256))}</dd></div>
        <div><dt>任务身份</dt><dd>${escapeHtml(formatPlain(recovery.actor))}</dd></div>
        <div><dt>发生时间</dt><dd>${escapeHtml(formatPlain(recovery.occurred_at))}</dd></div>
      </dl></details>` : "";
  const headline = hasPlanRisk
    ? formatPlain(summary.status_label || "风险预算")
    : formatPlain(drawdown.status_label || "策略回撤");
  return `<details class="trend-risk-summary" data-risk-status="${escapeHtml(formatPlain(status))}" aria-label="模拟策略风险摘要">
    <summary>组合计划风险 <span>${escapeHtml(headline)}</span></summary>
    <div class="trend-risk-summary-body">
    ${hasPlanRisk ? `<header><strong>组合计划风险</strong><span>${escapeHtml(formatPlain(summary.status_label))}</span></header>
      ${hasValue(summary.pause_reason) ? `<p class="trend-risk-pause">${escapeHtml(formatPlain(summary.pause_reason))}</p>` : ""}
      <dl>
        <div><dt>组合计划风险</dt><dd>${escapeHtml(planned)}</dd></div>
        <div><dt>组合剩余风险</dt><dd>${escapeHtml(remaining)}</dd></div>
        <div><dt>单笔风险上限</dt><dd>${escapeHtml(single)}</dd></div>
        <div><dt>异常损失缓冲</dt><dd>${escapeHtml(buffer)} · 不得用于开仓</dd></div>
        ${kellyRows}
        ${renderTrendTradeStats(summary.trade_stats)}
      </dl>
      ${hasValue(summary.kelly_reason) ? `<p>${escapeHtml(formatPlain(summary.kelly_reason))}</p>` : ""}
      ${hasValue(summary.kelly_source) ? `<p>${escapeHtml(formatPlain(summary.kelly_source))}</p>` : ""}
      ${summary.trade_stats?.available === true && hasValue(summary.trade_stats.statistics_cutoff_at) ? `<p>统计截至 ${escapeHtml(formatPlain(summary.trade_stats.statistics_cutoff_at))}</p>` : ""}
      <p>${escapeHtml(formatPlain(summary.portfolio_remaining_risk_note))}</p>
      <p>${escapeHtml(formatPlain(summary.disclaimer))}</p>` : ""}
    ${hasDrawdown ? `<div class="trend-drawdown-summary"><header><strong>策略累计回撤</strong><span>${escapeHtml(formatPlain(drawdown.status_label))}</span></header>
      ${hasValue(drawdown.pause_reason) ? `<p class="trend-risk-pause">${escapeHtml(formatPlain(drawdown.pause_reason))}</p>` : ""}
      <dl><div><dt>策略累计回撤</dt><dd>${trendRiskPercent(drawdown.drawdown_pct)} / ${trendRiskPercent(drawdown.drawdown_limit_pct)}</dd></div>
      <div><dt>策略模拟净值</dt><dd>${escapeHtml(formatDisplayNumber(drawdown.current_equity))}</dd></div>
      <div><dt>净值高点</dt><dd>${escapeHtml(formatDisplayNumber(drawdown.high_water_mark))}</dd></div></dl>${bootstrapRows}${recoveryRows}</div>` : ""}
    </div>
  </details>`;
}

function renderCnTrendTable(title, kind, headings, rows, note = "") {
  const desktopScroller = kind === "buy" && !isCnTrendMobile();
  const scrollerAttributes = kind === "buy"
    ? ` tabindex="${desktopScroller ? "0" : "-1"}" aria-label="${desktopScroller ? "正式买入计划，可横向滚动" : "正式买入计划"}"`
    : "";
  return `<section class="trend-stage cn-trend-stage cn-trend-${escapeHtml(kind)}"${scrollerAttributes}>
    <h2>${escapeHtml(title)}</h2>
    ${note ? `<p class="cn-trend-price-sources">${escapeHtml(note)}</p>` : ""}
    <table class="cn-trend-table"><thead><tr>${headings.map((heading) => `<th scope="col">${escapeHtml(heading)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>
    ${rows.length ? "" : "<p>无</p>"}
  </section>`;
}

function isCnTrendMobile() {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(max-width: 760px)").matches;
}

function syncCnTrendBuyAccessibility() {
  const workspace = elements["trend-report-workspace"];
  if (!workspace || typeof workspace.querySelector !== "function") return;
  const scroller = workspace.querySelector(".cn-trend-buy");
  if (!scroller) return;
  const mobile = isCnTrendMobile();
  scroller.tabIndex = mobile ? -1 : 0;
  scroller.setAttribute(
    "aria-label",
    mobile ? "正式买入计划" : "正式买入计划，可横向滚动",
  );
}

function trendHoldingActionLabel(item) {
  return {
    SELL_ALL: "全部卖出",
    SELL_PARTIAL: "止盈减仓 30%",
    MANUAL_REVIEW: "人工复核",
    HOLD: "继续持有",
  }[item?.action] || (hasValue(item?.action) ? "数据未提供" : "继续持有");
}

function trendHoldingHeadings() {
  return [
    "标的", "动作", "执行参考价", "温度变化", "节气", "大类内强度", "全局强度", "行业",
    "当前判断", "活动保护线", "持仓提示",
  ];
}

function trendRealHoldingStatus(report) {
  return report?.real_position_status ?? report?.real_holdings_status;
}

function trendRealHoldingReason(report) {
  return report?.real_position_reason ?? report?.real_holdings_reason;
}

function trendRealHoldingSource(report) {
  const source = report?.real_position_source ?? report?.real_holdings_source;
  return source && typeof source === "object" ? source : {};
}

function trendHoldingRowClass(item) {
  return {
    included: "trend-holding-included",
    excluded: "trend-holding-excluded",
    blacklisted: "trend-holding-blacklisted",
  }[item?.trend_report_state] || "trend-holding-excluded";
}

function renderTrendHoldingRows(items, report) {
  const optionMarket = ["US", "HK"].includes(String(report?.market || "").toUpperCase());
  return cnTrendRows(items).map((item) => `<tr class="cn-trend-card ${trendHoldingRowClass(item)}">
    ${optionMarket ? renderTrendOptionIdentityCell(item) : renderTrendCell("标的", trendIdentity(item))}
    ${renderTrendCell("动作", trendHoldingActionLabel(item))}
    ${renderTrendCell("执行参考价", hasValue(item.close) ? formatDisplayNumber(item.close) : null)}
    ${renderTrendCell("温度变化", trendTemperature(item))}
    ${renderTrendCell("节气", item.phase)}
    ${renderTrendCell("大类内强度", hasValue(item.strength) ? formatDisplayNumber(item.strength) : null)}
    ${renderTrendCell("全局强度", hasValue(item.global_strength) ? formatDisplayNumber(item.global_strength) : null)}
    ${renderTrendCell("行业", item.industry)}
    ${renderTrendCell("当前判断", trendReasonLabel(item, report))}
    ${renderTrendCell("活动保护线", hasValue(item.active_line) ? formatDisplayNumber(item.active_line) : null)}
    ${renderTrendCell("持仓提示", trendHints(item))}
  </tr>`);
}

function renderTrendHoldingTable(items, report) {
  const headings = trendHoldingHeadings();
  const rows = renderTrendHoldingRows(items, report);
  return `<table class="cn-trend-table"><thead><tr>${headings.map((heading) => `<th scope="col">${escapeHtml(heading)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>${rows.length ? "" : "<p>无</p>"}`;
}

function renderTrendHoldingSource(report) {
  const status = trendRealHoldingStatus(report);
  if (status !== "available") return "";
  const source = trendRealHoldingSource(report);
  const broker = source.broker_label || report?.broker_label || "数据源";
  const period = source.snapshot_period || "数据未提供";
  const kind = source.source_kind === "live_account" ? "账户" : "结单";
  const freshness = source.freshness_text || "数据未提供";
  const readOnly = source.read_only_text || "只读，不自动下单";
  return `<p class="cn-trend-price-sources">${escapeHtml(formatPlain(`${broker} · ${kind} ${period} · ${freshness} · ${readOnly}`))}</p>`;
}

function renderTrendHoldingPanel(report, view, items) {
  const status = trendRealHoldingStatus(report);
  if (view === "real") {
    if (status === undefined || status === null) {
      return '<p class="account-empty">当前报告未包含真实持仓判断</p>';
    }
    if (status === "legacy") {
      return '<p class="account-empty">当前报告未包含真实持仓判断</p>';
    }
    if (status === "unavailable") {
      const reason = trendRealHoldingReason(report) || "数据未提供";
      return `<p class="account-empty missing-text">真实持仓数据不可用：${escapeHtml(formatPlain(reason))}</p>`;
    }
    const rows = Array.isArray(items) ? items : [];
    return `${renderTrendHoldingSource(report)}${renderTrendHoldingTable(rows, report)}`;
  }
  const rows = Array.isArray(items) ? items : [];
  return renderTrendHoldingTable(rows, report);
}

function renderTrendHoldingStage(report) {
  const realItems = Array.isArray(report?.real_position_actions) ? report.real_position_actions : [];
  const simulatedItems = Array.isArray(report?.hold_actions) ? report.hold_actions : [];
  return `<section class="trend-stage cn-trend-stage cn-trend-hold" data-trend-holding-section>
    <h2>盘中持续 · 已有持仓</h2>
    <div class="account-view-tabs" role="tablist" aria-label="趋势报告持仓视图">
      <button id="trend-holding-real-tab" class="account-view-tab" type="button" role="tab" data-trend-holding-view="real" aria-selected="true" tabindex="0" aria-controls="trend-holding-real-panel">真实持仓</button>
      <button id="trend-holding-simulate-tab" class="account-view-tab" type="button" role="tab" data-trend-holding-view="simulate" aria-selected="false" tabindex="-1" aria-controls="trend-holding-simulate-panel">模拟盘持仓</button>
    </div>
    <div id="trend-holding-real-panel" class="trend-holding-panel" role="tabpanel" aria-labelledby="trend-holding-real-tab" data-trend-holding-panel="real">
      ${renderTrendHoldingPanel(report, "real", realItems)}
    </div>
    <div id="trend-holding-simulate-panel" class="trend-holding-panel" role="tabpanel" aria-labelledby="trend-holding-simulate-tab" data-trend-holding-panel="simulate" hidden>
      ${renderTrendHoldingPanel(report, "simulate", simulatedItems)}
    </div>
  </section>`;
}

function handleTrendHoldingTab(event) {
  const button = event?.target?.closest?.("[data-trend-holding-view]");
  if (!button) return false;
  const section = button.closest("[data-trend-holding-section]");
  if (!section) return false;
  const view = button.dataset.trendHoldingView;
  if (!["real", "simulate"].includes(view)) return false;
  section.querySelectorAll("[data-trend-holding-view]").forEach((tab) => {
    const selected = tab.dataset.trendHoldingView === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  section.querySelectorAll("[data-trend-holding-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.trendHoldingPanel !== view;
  });
  return true;
}

function handleTrendHoldingTabKeydown(event) {
  const tab = event?.target?.closest?.("[data-trend-holding-view]");
  if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const section = tab.closest("[data-trend-holding-section]");
  if (!section) return;
  event.preventDefault();
  const tabs = [...section.querySelectorAll("[data-trend-holding-view]")];
  const current = tabs.indexOf(tab);
  const index = event.key === "Home" ? 0
    : event.key === "End" ? tabs.length - 1
      : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  handleTrendHoldingTab({target: tabs[index]});
  tabs[index].focus();
}

function renderTrendSellOrHoldStage(title, items, kind, report) {
  if (kind === "hold") {
    const rows = renderTrendHoldingRows(items, report);
    return renderCnTrendTable(title, kind, trendHoldingHeadings(), rows);
  }
  const action = { sell: trendSellActionLabel, review: () => "人工复核" }[kind] || (() => "继续持有");
  const reasonHeading = kind === "sell" ? "触发原因" : kind === "review" ? "复核原因" : "当前判断";
  const optionMarket = ["US", "HK"].includes(String(report?.market || "").toUpperCase());
  const headings = [
    "标的", "动作", "执行参考价", "温度变化", "节气", "强度",
    ...(kind === "hold" ? ["行业"] : []),
    reasonHeading, "活动保护线", "持仓提示",
  ];
  const rows = cnTrendRows(items).map((item) => `<tr class="cn-trend-card">
    ${kind === "hold" && optionMarket ? renderTrendOptionIdentityCell(item) : renderTrendCell("标的", trendIdentity(item))}
    ${renderTrendCell("动作", action(item))}
    ${renderTrendCell("执行参考价", hasValue(item.close) ? formatDisplayNumber(item.close) : null)}
    ${renderTrendCell("温度变化", trendTemperature(item))}
    ${renderTrendCell("节气", item.phase)}
    ${renderTrendCell("强度", hasValue(item.strength) ? formatDisplayNumber(item.strength) : null)}
    ${kind === "hold" ? renderTrendCell("行业", item.industry) : ""}
    ${renderTrendCell(reasonHeading, trendReasonLabel(item, report))}
    ${renderTrendCell("活动保护线", hasValue(item.active_line) ? formatDisplayNumber(item.active_line) : null)}
    ${renderTrendCell("持仓提示", trendHints(item))}
  </tr>`);
  return renderCnTrendTable(title, kind, headings, rows);
}

function isTrendRotationBuyAction(item) {
  return [item?.reason, item?.action].some((value) =>
    String(value || "").trim().toLowerCase() === "relative_rotation"
  );
}

function trendFinalPlanBuyActions(report) {
  const rows = cnTrendRows(report?.buy_actions);
  return usesFinalPlanTrendAudit(report)
    ? rows.filter((item) => !isTrendRotationBuyAction(item))
    : rows;
}

function renderTrendBuyStage(report) {
  const headings = [
    "标的", "动作", "筛选价（Trend Animals）", "执行参考价",
    "温度变化", "节气", "大类内强度", "全局强度", "行业", "行业温度", "行业确认", "市值（亿元）",
    "日成交额（亿元）", "目标仓位（占净值）", "目标金额", "预计数量", "预计保护线",
  ];
  const optionMarket = ["US", "HK"].includes(String(report?.market || "").toUpperCase());
  const row = (item, action) => {
    const targetWeight = decimalAsPercent(item.target_weight, null);
    const shares = hasValue(item.estimated_shares)
      ? `${formatDisplayNumber(item.estimated_shares)} 股` : null;
    return `<tr class="cn-trend-card">
      ${optionMarket ? renderTrendOptionIdentityCell(item) : renderTrendCell("标的", trendIdentity(item))}
      ${renderTrendCell("动作", action)}
      ${renderTrendCell("筛选价（Trend Animals）", hasValue(item.filter_price) ? formatDisplayNumber(item.filter_price) : null)}
      ${renderTrendCell("执行参考价", hasValue(item.close) ? formatDisplayNumber(item.close) : null)}
      ${renderTrendCell("温度变化", trendTemperature(item))}
      ${renderTrendCell("节气", item.phase)}
      ${renderTrendCell("大类内强度", hasValue(item.strength) ? formatDisplayNumber(item.strength) : null)}
      ${renderTrendCell("全局强度", hasValue(item.global_strength) ? formatDisplayNumber(item.global_strength) : null)}
      ${renderTrendCell("行业", item.industry)}
      ${renderTrendCell("行业温度", trendIndustryBuyTemperature(report, item))}
      ${renderTrendCell("行业确认", trendIndustryBuyContext(report, item))}
      ${renderTrendCell("市值（亿元）", hasValue(trendMoney(item, "market_cap_cny_100m", "market_cap")) ? formatDisplayNumber(trendMoney(item, "market_cap_cny_100m", "market_cap")) : null)}
      ${renderTrendCell("日成交额（亿元）", hasValue(trendMoney(item, "amount_cny_100m", "amount")) ? formatDisplayNumber(trendMoney(item, "amount_cny_100m", "amount")) : null)}
      ${renderTrendCell("目标仓位（占净值）", targetWeight, hasValue(targetWeight) ? `目标仓位 ${targetWeight}` : "")}
      ${renderTrendCell("目标金额", hasValue(item.target_amount) ? formatDisplayNumber(item.target_amount) : null)}
      ${renderTrendCell("预计数量", shares)}
      ${renderTrendCell("预计保护线", hasValue(item.estimated_initial_line) ? formatDisplayNumber(item.estimated_initial_line) : null)}
    </tr>`;
  };
  const rows = trendFinalPlanBuyActions(report).map((item) => row(item, "正式买入"));
  const simulateStage = renderCnTrendTable(
    `${formatPlain(report.buy_window)} · 模拟盘正式买入计划`, "buy", headings, rows,
    "价格口径：筛选价（Trend Animals）｜执行参考价（各市场报告来源）",
  );
  if (usesFinalPlanTrendAudit(report)) return simulateStage;
  const realHeadings = ["卖出", "买入", "动作", "强度差", "目标仓位（占净值）", "目标金额", "预计数量", "目标交易日", "状态"];
  const realRows = cnTrendRows(report.real_rotation_pairs).map((pair) => {
    const executionStatus = [pair.execution_status, pair.status, pair.order_status].find(hasValue);
    const status = executionStatus ?? "待人工确认";
    return `<tr class="cn-trend-card">
      ${renderTrendCell("卖出", trendIdentity({symbol: pair.sell_symbol, name: pair.sell_name}))}
      ${renderTrendCell("买入", trendIdentity({symbol: pair.buy_symbol, name: pair.buy_name}))}
      ${renderTrendCell("动作", "卖出后买入")}
      ${renderTrendCell("强度差", hasValue(pair.strength_gap) ? formatDisplayNumber(pair.strength_gap) : null)}
      ${renderTrendCell("目标仓位（占净值）", decimalAsPercent(pair.target_weight, null))}
      ${renderTrendCell("目标金额", hasValue(pair.target_amount) ? formatDisplayNumber(pair.target_amount) : null)}
      ${renderTrendCell("预计数量", hasValue(pair.estimated_shares) ? `${formatDisplayNumber(pair.estimated_shares)} 股` : null)}
      ${renderTrendCell("目标交易日", pair.execution_date || report.report_date)}
      ${renderTrendCell("状态", status)}
    </tr>`;
  });
  const realStage = renderCnTrendTable(
    "实盘买入计划 · 人工确认", "buy", realHeadings, realRows,
    "按实盘持仓独立生成；仅人工确认，不自动下单。",
  );
  return simulateStage + realStage;
}

const TREND_DISCIPLINE_ROW_NAMES = {
  holding: new Set(["初始保护线", "过热跟踪", "保护线不下降", "活动保护线"]),
  exit: new Set([
    "退出条件", "过热止盈比例", "过热止盈信号", "过热止盈次数",
    "过热止盈取整", "不足一手处理", "清仓优先级",
  ]),
};

function trendFrozenStrategyRows(report) {
  const rows = report && Array.isArray(report.strategy_parameter_rows)
    ? report.strategy_parameter_rows
    : [];
  return rows.filter((row) => row && typeof row === "object" && !Array.isArray(row)
    && hasValue(row.group) && hasValue(row.name) && hasValue(row.value));
}

function trendDisplayedStrategyRows(report) {
  const current = report && Array.isArray(report.current_strategy_parameter_rows)
    ? report.current_strategy_parameter_rows
    : null;
  return current === null ? trendFrozenStrategyRows(report)
    : current.filter((row) => row && typeof row === "object" && !Array.isArray(row)
      && hasValue(row.group) && hasValue(row.name) && hasValue(row.value));
}

function trendDisciplineLifecycle(parameterRows) {
  const rows = Array.isArray(parameterRows)
    ? parameterRows.filter((row) => row && typeof row === "object" && !Array.isArray(row)
      && hasValue(row.group) && hasValue(row.name) && hasValue(row.value))
    : [];
  const groups = [
    {key: "entry", title: "入场门槛", rows: []},
    {key: "sort", title: "候选排序", rows: []},
    {key: "execution", title: "仓位与执行", rows: []},
    {key: "holding", title: "持有管理", rows: []},
    {key: "exit", title: "退出规则", rows: []},
    {key: "other", title: "其他设置", rows: []},
  ];
  const byKey = Object.fromEntries(groups.map((group) => [group.key, group]));
  rows.forEach((row) => {
    const group = formatPlain(row.group);
    const name = formatPlain(row.name);
    let key = "other";
    if (group === "候选来源" || group === "入场过滤") key = "entry";
    else if (group === "候选排序") key = "sort";
    else if (group === "仓位执行") key = "execution";
    else if (group === "退出保护") {
      key = TREND_DISCIPLINE_ROW_NAMES.holding.has(name) ? "holding"
        : TREND_DISCIPLINE_ROW_NAMES.exit.has(name) ? "exit" : "other";
    } else if (group === "累计回撤") {
      key = "other";
    }
    byKey[key].rows.push(row);
  });
  return groups;
}

function renderTrendDisciplineCategory(card) {
  const rows = card.rows || [];
  const compact = rows.slice(0, 2).map((row) => `${formatPlain(row.name)}：${formatPlain(row.value)}`).join("；");
  const emptyLabel = "本报告未提供该类纪律参数";
  const body = rows.length
    ? `<dl>${rows.map((row) => `<div><dt>${escapeHtml(`${formatPlain(row.group)} · ${formatPlain(row.name)}`)}</dt><dd>${escapeHtml(formatPlain(row.value))}</dd></div>`).join("")}</dl>`
    : `<p>${escapeHtml(emptyLabel)}</p>`;
  return `<details class="trend-discipline-category" data-discipline="${escapeHtml(card.key)}">
    <summary><strong>${escapeHtml(card.title)}</strong><span>${escapeHtml(`${rows.length} 项`)}</span><small>${escapeHtml(compact || emptyLabel)}</small></summary>
    <div class="trend-discipline-category-body">${body}</div>
  </details>`;
}

function renderTrendDisciplineCards(report) {
  const rows = trendDisplayedStrategyRows(report);
  const cards = trendDisciplineLifecycle(rows);
  const version = formatPlain(report?.current_strategy_version || report?.strategy_version || "-");
  const current = Array.isArray(report?.current_strategy_parameter_rows);
  const headline = `${cards.length} 类 · ${rows.length} 项 · ${current ? `当前版本 ${version}` : `报告版本 ${version}`}`;
  const note = current
    ? "展示当前生效纪律；所选报告的冻结参数仍保留在历史审计数据中。"
    : "当前纪律不可用，展示所选报告生成时冻结的策略参数。";
  return `<details class="trend-discipline-workspace" aria-label="纪律">
    <summary>纪律 <span>${escapeHtml(headline)}</span><small>已折叠</small></summary>
    <div class="trend-discipline-workspace-body">
      <p class="trend-discipline-note">${escapeHtml(note)}</p>
      <div class="trend-discipline-grid">${cards.map((card) => renderTrendDisciplineCategory(card)).join("")}</div>
    </div>
  </details>`;
}

function formatTrendApiCost(value) {
  const raw = formatPlain(value).trim();
  const match = raw.match(/^([+-]?)(\d+)(?:\.(\d+))?$/);
  if (!match) return raw;
  const sign = match[1];
  const integer = match[2];
  const fraction = match[3] || "";
  if (/^0+$/.test(`${integer}${fraction}`)) return "0";
  const normalizedInteger = integer.replace(/^0+(?=\d)/, "");
  const normalizedFraction = fraction.replace(/0+$/, "");
  return `${sign === "-" ? "-" : ""}${normalizedInteger}${normalizedFraction ? `.${normalizedFraction}` : ""}`;
}

function trendReportCostLabel(report) {
  const cost = report && report.api_cost && typeof report.api_cost === "object"
    ? report.api_cost : null;
  if (cost && hasValue(cost.label)) return formatPlain(cost.label);
  const actual = cost?.actual ?? report?.audit?.actual_api_cost;
  const estimated = cost?.estimated ?? report?.audit?.estimated_api_cost;
  if (hasValue(actual)) return `本报告 API 费用：实扣 ${formatTrendApiCost(actual)} Trend Animals 余额单位`;
  if (hasValue(estimated)) {
    const estimateComplete = cost?.estimate_complete ?? report?.estimated_api_cost_complete ?? true;
    return estimateComplete === false
      ? `本报告 API 费用：未知（快照估算 ${formatTrendApiCost(estimated)} Trend Animals 余额单位；成分费用未计）`
      : `本报告 API 费用：估算 ${formatTrendApiCost(estimated)} Trend Animals 余额单位（实扣不可得）`;
  }
  return "本报告 API 费用：未知";
}

function trendIndustryDirection(value) {
  return ({rising: "上升", falling: "下降", unchanged: "持平"})[String(value)] || formatPlain(value);
}

function trendIndustryPercent(value) {
  if (!hasValue(value)) return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  const percent = Math.abs(number) <= 1 ? number * 100 : number;
  return `${percent.toLocaleString("zh-CN", {maximumFractionDigits: 2})}%`;
}

function trendIndustryTransition(current, prior) {
  const currentText = trendIndustryPercent(current);
  if (!currentText) return "未提供";
  const priorText = trendIndustryPercent(prior);
  return priorText ? `${priorText} → ${currentText}` : `${currentText} · 基准建立中`;
}

function trendIndustryStructureCopy(context) {
  if (!hasValue(context.aggregate_right_count_ratio)
      || !hasValue(context.aggregate_right_market_cap_ratio)) return "";
  const count = Number(context.aggregate_right_count_ratio);
  const marketCap = Number(context.aggregate_right_market_cap_ratio);
  if (!Number.isFinite(count) || !Number.isFinite(marketCap)) return "";
  const gap = (marketCap - count) * 100;
  const relation = gap > 0 ? "高于" : gap < 0 ? "低于" : "等于";
  const bias = gap > 0 ? "右侧更偏大市值成分"
    : gap < 0 ? "右侧更偏小市值成分" : "两个占比相同";
  let text = `当前右侧市值占比${relation}右侧个数占比 ${formatDisplayNumber(Math.abs(gap))} 个百分点，${bias}。`;
  const priorCount = hasValue(context.prior_aggregate_right_count_ratio)
    ? Number(context.prior_aggregate_right_count_ratio) : null;
  const priorMarketCap = hasValue(context.prior_aggregate_right_market_cap_ratio)
    ? Number(context.prior_aggregate_right_market_cap_ratio) : null;
  if (Number.isFinite(priorMarketCap)) {
    const marketCapChange = (marketCap - priorMarketCap) * 100;
    const marketCapDirection = marketCapChange > 0 ? "上升"
      : marketCapChange < 0 ? "下降" : "持平";
    text = `较前一有效交易日${marketCapDirection} ${formatDisplayNumber(Math.abs(marketCapChange))} 个百分点。${text}`;
  }
  if (Number.isFinite(priorCount) && Number.isFinite(priorMarketCap)) {
    const change = gap - (priorMarketCap - priorCount) * 100;
    const direction = change > 0 ? "扩大" : change < 0 ? "收窄" : "持平";
    text += `结构差较前值${direction} ${formatDisplayNumber(Math.abs(change))} 个百分点。`;
  }
  return `${text}该指标不是账户仓位或上涨概率。`;
}

function trendIndustryRatioChangeCopy(label, definition, current, prior) {
  if (!hasValue(current) || !Number.isFinite(Number(current))) return "";
  let text = `${label}：${definition}。`;
  if (hasValue(prior) && Number.isFinite(Number(prior))) {
    const change = (Number(current) - Number(prior)) * 100;
    const direction = change > 0 ? "上升" : change < 0 ? "下降" : "持平";
    text += `较前一有效交易日${direction} ${formatDisplayNumber(Math.abs(change))} 个百分点。`;
  } else {
    text += "历史基准建立中。";
  }
  return text;
}

function trendIndustryMetric(value, help) {
  const visible = value || "未提供";
  const label = help ? `${visible}：${help}` : visible;
  return `<button type="button" class="trend-industry-metric" data-trend-industry-help="${escapeHtml(help)}" aria-expanded="false" aria-label="${escapeHtml(label)}">${escapeHtml(visible)}</button>`;
}

function showTrendIndustryHelp(trigger, pinned = false) {
  const section = trigger.closest?.(".trend-industry-context");
  const tooltip = section?.querySelector?.(".trend-industry-tooltip");
  const text = trigger.dataset.trendIndustryHelp || "";
  if (!tooltip || !text) return;
  closeTrendIndustryHelp();
  tooltip.textContent = text;
  tooltip.hidden = false;
  tooltip.setAttribute("aria-hidden", "false");
  trigger.dataset.trendIndustryHelpOpen = pinned ? "pinned" : "hover";
  trigger.setAttribute("aria-expanded", String(pinned));
  const target = trigger.getBoundingClientRect();
  const box = tooltip.getBoundingClientRect();
  const viewportWidth = typeof window !== "undefined" ? window.innerWidth : 1024;
  tooltip.style.left = `${Math.min(Math.max(12, target.left), viewportWidth - box.width - 12)}px`;
  tooltip.style.top = `${target.top >= box.height + 20 ? target.top - box.height - 8 : target.bottom + 8}px`;
}

function closeTrendIndustryHelp(trigger = null) {
  const sections = trigger?.closest?.(".trend-industry-context")
    ? [trigger.closest(".trend-industry-context")]
    : [...(document.querySelectorAll?.(".trend-industry-context") || [])];
  sections.forEach((section) => {
    const tooltip = section.querySelector?.(".trend-industry-tooltip");
    if (tooltip) {
      tooltip.hidden = true;
      tooltip.setAttribute("aria-hidden", "true");
      tooltip.textContent = "";
    }
    section.querySelectorAll?.("[data-trend-industry-help]").forEach((item) => {
      delete item.dataset.trendIndustryHelpOpen;
      item.setAttribute("aria-expanded", "false");
    });
  });
}

function trendIndustryContextFallback(status, contexts) {
  if (!(contexts || []).length && !hasValue(status?.ordering_mode)
      && status?.current_complete !== true) {
    return '<p class="trend-industry-context-fallback">当前行业上下文未提供，无法确认排序；未使用当前规则</p>';
  }
  const mode = formatPlain(status?.ordering_mode);
  const invalid = mode.startsWith("legacy") || status?.current_complete === false;
  if (!invalid) return "";
  const reasons = [];
  if (status && hasValue(status.fallback_reason)) reasons.push(formatPlain(status.fallback_reason));
  if (status && status.validation_reasons && typeof status.validation_reasons === "object") {
    Object.values(status.validation_reasons).flatMap((value) => Array.isArray(value) ? value : [value])
      .filter(hasValue).forEach((reason) => reasons.push(formatPlain(reason)));
  }
  (contexts || []).filter((context) => context && context.valid === false).forEach((context) => {
    (Array.isArray(context.invalid_reasons) ? context.invalid_reasons : ["行业上下文无效"])
      .filter(hasValue).forEach((reason) => reasons.push(formatPlain(reason)));
  });
  const reasonText = [...new Set(reasons)].join("、") || "当前行业上下文不完整";
  return `<p class="trend-industry-context-fallback">当前行业上下文无效，已回退旧排序：${escapeHtml(reasonText)}</p>`;
}

function renderTrendIndustryContext(report) {
  const contexts = Array.isArray(report?.industry_contexts)
    ? report.industry_contexts.filter((context) => context && typeof context === "object" && !Array.isArray(context))
    : [];
  const status = report?.industry_context_status && typeof report.industry_context_status === "object"
    ? report.industry_context_status : {};
  if (usesFinalPlanTrendAudit(report)) {
    const rows = contexts.map((context) => `<tr class="trend-industry-context-row${context.valid === false ? " invalid" : ""}">
      <th scope="row" data-label="行业"><strong>${escapeHtml(formatPlain(context.industry || "行业"))}</strong></th>
      <td data-label="当前温度">${escapeHtml(hasValue(context.temperature) ? formatPlain(context.temperature) : "数据未提供")}</td>
      <td data-label="温度方向">${escapeHtml(hasValue(context.temperature_direction) ? trendIndustryDirection(context.temperature_direction) : "数据未提供")}</td>
    </tr>`).join("");
    return `<section class="trend-industry-context" aria-label="行业上下文">
      <header><h2>行业上下文</h2><span>${escapeHtml(status.current_complete === false ? "当前数据不完整" : `${contexts.length} 个行业`)}</span></header>
      ${trendIndustryContextFallback(status, contexts)}
      ${rows
        ? `<div class="trend-industry-context-table-wrap"><table class="trend-industry-context-table"><thead><tr><th scope="col">行业</th><th scope="col">当前温度</th><th scope="col">温度方向</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : "<p>行业上下文数据未提供</p>"}
    </section>`;
  }
  const rows = contexts.map((context) => {
    const countValue = trendIndustryTransition(
      context.aggregate_right_count_ratio,
      context.prior_aggregate_right_count_ratio,
    );
    const marketCapValue = trendIndustryTransition(
      context.aggregate_right_market_cap_ratio,
      context.prior_aggregate_right_market_cap_ratio,
    );
    const countHelp = trendIndustryRatioChangeCopy(
      "右侧个数占比", "右侧成分数 ÷ 行业有效成分数",
      context.aggregate_right_count_ratio,
      context.prior_aggregate_right_count_ratio,
    );
    const marketCapHelp = trendIndustryRatioChangeCopy(
      "右侧市值占比", "右侧成分总市值 ÷ 行业有效成分总市值",
      context.aggregate_right_market_cap_ratio,
      context.prior_aggregate_right_market_cap_ratio,
    );
    const countExplanation = countHelp || "当前右侧个数占比不可用。";
    const marketCapExplanation = marketCapHelp
      ? `${marketCapHelp}${trendIndustryStructureCopy(context)}`
      : "当前右侧市值占比不可用。";
    return `<tr class="trend-industry-context-row${context.valid === false ? " invalid" : ""}">
      <th scope="row" data-label="行业"><strong>${escapeHtml(formatPlain(context.industry || "行业"))}</strong></th>
      <td data-label="当前温度">${escapeHtml(hasValue(context.temperature) ? formatPlain(context.temperature) : "数据未提供")}</td>
      <td data-label="温度方向">${escapeHtml(hasValue(context.temperature_direction) ? trendIndustryDirection(context.temperature_direction) : "数据未提供")}</td>
      <td data-label="趋势强度">${escapeHtml(hasValue(context.strength) ? formatDisplayNumber(context.strength) : "数据未提供")}</td>
      <td data-label="温转热数量">${escapeHtml(hasValue(context.warm_to_hot_count) ? formatDisplayNumber(context.warm_to_hot_count) : "数据未提供")}</td>
      <td data-label="右侧个数占比">${trendIndustryMetric(countValue, countExplanation)}</td>
      <td data-label="右侧市值占比">${trendIndustryMetric(marketCapValue, marketCapExplanation)}</td>
    </tr>`;
  }).join("");
  const countHeader = trendIndustryMetric("右侧个数占比", "右侧成分数 ÷ 行业有效成分数。");
  const marketCapHeader = trendIndustryMetric("右侧市值占比", "右侧成分总市值 ÷ 行业有效成分总市值。");
  return `<section class="trend-industry-context" aria-label="行业上下文">
    <header><h2>行业上下文</h2><span>${escapeHtml(status.current_complete === false ? "当前数据不完整" : `${contexts.length} 个行业`)}</span></header>
    ${trendIndustryContextFallback(status, contexts)}
    ${rows
      ? `<div class="trend-industry-context-table-wrap"><table class="trend-industry-context-table"><thead><tr><th scope="col">行业</th><th scope="col">当前温度</th><th scope="col">温度方向</th><th scope="col">趋势强度</th><th scope="col">温转热数量</th><th scope="col">${countHeader}</th><th scope="col">${marketCapHeader}</th></tr></thead><tbody>${rows}</tbody></table></div><div class="trend-industry-tooltip" role="tooltip" aria-hidden="true" hidden></div>`
      : "<p>行业上下文数据未提供</p>"}
  </section>`;
}

function cnTrendAuditValue(value, suffix = "") {
  if (!hasValue(value)) return "数据未提供";
  if (typeof value === "boolean") return value ? "是" : "否";
  return `${formatDisplayNumber(value)}${suffix}`;
}

function cnTrendAuditDanger(value) {
  return value === true ? "已触发"
    : value === false ? "未触发"
    : "数据未提供";
}

function cnTrendAuditList(value) {
  return Array.isArray(value) && value.length
    ? value.map(formatPlain).join("或")
    : "";
}

function cnTrendAuditRequirement(parameters, key, render) {
  const value = parameters && typeof parameters === "object"
    ? parameters[key]
    : undefined;
  return hasValue(value) ? render(value) : "冻结策略参数未提供";
}

function cnTrendAuditReason(reason, item, parameters, report) {
  const temperature = `${cnTrendAuditValue(item.temperature_prev)} → ${cnTrendAuditValue(item.temperature_curr)}`;
  const rules = {
    a_share_only: ["资产类型", cnTrendAuditValue(item.asset), "要求：仅限 A 股股票"],
    temperature_missing: ["个股温度", "数据未提供", "要求：个股温度必须存在"],
    temperature_transition_not_entry: [
      "温度变化", temperature,
      cnTrendAuditRequirement(parameters, "temperature_transition", (value) => {
        const from = cnTrendAuditList(value && value.from);
        const to = cnTrendAuditList(value && value.to);
        return from && to ? `要求：${from} → ${to}` : "冻结策略参数未提供";
      }),
    ],
    filter_price_missing: ["筛选价", "数据未提供", "要求：筛选价必须存在"],
    filter_price_above_200: [
      "筛选价", cnTrendAuditValue(item.filter_price, " 元"),
      cnTrendAuditRequirement(parameters, "max_filter_price", (value) => `要求：不高于 ${formatDisplayNumber(value)} 元`),
    ],
    strength_missing: ["趋势强度", "数据未提供", "要求：趋势强度必须存在"],
    strength_below_95: [
      "趋势强度", cnTrendAuditValue(item.strength),
      cnTrendAuditRequirement(parameters, "min_strength", (value) => `要求：不低于 ${formatDisplayNumber(value)}`),
    ],
    industry_id_missing: ["行业 ID", "数据未提供", "要求：行业 ID 必须存在"],
    industry_temperature_missing: ["行业温度", "数据未提供", "要求：行业温度必须存在"],
    industry_temperature_not_hot: [
      "行业温度", cnTrendAuditValue(item.industry_temperature),
      cnTrendAuditRequirement(parameters, "allowed_industry_temperatures", (value) => `要求：${cnTrendAuditList(value) || "冻结策略参数未提供"}`),
    ],
    phase_missing: ["趋势节气", "数据未提供", "要求：趋势节气必须存在"],
    phase_after_summer_solstice: [
      "趋势节气", cnTrendAuditValue(item.phase),
      cnTrendAuditRequirement(parameters, "allowed_phases", (value) => `要求：${cnTrendAuditList(value) || "冻结策略参数未提供"}`),
    ],
    market_cap_missing: ["总市值", "数据未提供", "要求：总市值必须存在"],
    market_cap_below_100: [
      "总市值", cnTrendAuditValue(item.market_cap, " 亿元"),
      cnTrendAuditRequirement(parameters, "min_market_cap_100m", (value) => `要求：至少 ${formatDisplayNumber(value)} 亿元`),
    ],
    amount_missing: ["日成交额", "数据未提供", "要求：日成交额必须存在"],
    amount_below_2: [
      "日成交额", cnTrendAuditValue(item.amount, " 亿元"),
      cnTrendAuditRequirement(parameters, "min_amount_100m", (value) => `要求：至少 ${formatDisplayNumber(value)} 亿元`),
    ],
    right_side_days_missing: ["右侧天数", "数据未提供", "要求：右侧天数必须存在"],
    right_side_not_true: ["右侧趋势", "未进入右侧", "要求：必须处于右侧趋势"],
    not_tradable: ["交易状态", "当前不可交易", "要求：必须可交易"],
    danger_signal: ["危险信号", cnTrendAuditDanger(item.danger), "要求：不得触发"],
    danger_unknown: ["危险信号", "数据未提供", "要求：危险信号必须明确"],
    name_missing: ["标的名称", "数据未提供", "要求：标的名称必须存在"],
    asset_missing: ["资产类型", "数据未提供", "要求：资产类型必须存在"],
    unsupported_asset: ["资产类型", cnTrendAuditValue(item.asset), "要求：A 股股票"],
    already_held: ["账户状态", "当前已持有", "要求：新开仓候选不得已持有"],
    excluded_security: [
      "证券范围",
      [item.name, item.exchange].filter(hasValue).map(formatPlain).join(" / ") || "数据未提供",
      "要求：非北交所、ST 或退市标的",
    ],
    unsupported_exchange: ["交易所", cnTrendAuditValue(item.exchange), "要求：沪深市场"],
    atr_unavailable: ["ATR14", "数据未提供", "该历史策略版本要求 ATR14"],
    data_date_mismatch: [
      "数据日期", cnTrendAuditValue(item.as_of_date),
      hasValue(report && report.data_date)
        ? `要求：与报告数据日 ${formatPlain(report.data_date)} 一致`
        : "报告数据日未提供",
    ],
    amount_below_1: [
      "日成交额", cnTrendAuditValue(item.amount, " 亿元"),
      cnTrendAuditRequirement(parameters, "min_amount_100m", (value) => `要求：至少 ${formatDisplayNumber(value)} 亿元`),
    ],
    strength_not_above_90: [
      "趋势强度", cnTrendAuditValue(item.strength),
      cnTrendAuditRequirement(parameters, "min_strength", (value) => `要求：高于 ${formatDisplayNumber(value)}`),
    ],
    right_side_days_not_below_10: [
      "右侧天数", cnTrendAuditValue(item.days, " 天"),
      cnTrendAuditRequirement(parameters, "max_right_side_days_exclusive", (value) => `要求：少于 ${formatDisplayNumber(value)} 天`),
    ],
  };
  const values = Object.prototype.hasOwnProperty.call(rules, reason)
    ? rules[reason]
    : undefined;
  return values
    ? {code: reason, label: values[0], actual: values[1], requirement: values[2]}
    : {
      code: formatPlain(reason),
      label: `未识别规则：${formatPlain(reason)}`,
      actual: "无法解析",
      requirement: "请核对冻结报告",
    };
}

function cnTrendAuditTableTemperature(item) {
  return cnTrendAuditValue(item.temperature_prev) + " → " + cnTrendAuditValue(item.temperature_curr);
}

function renderFinalPlanTrendAudit(report) {
  const audit = report?.audit && typeof report.audit === "object" && !Array.isArray(report.audit)
    ? report.audit : {};
  const market = String(report?.market || "").trim().toUpperCase();
  const auditKey = (item, symbol = item?.symbol, futuSymbol = item?.futu_symbol) =>
    normalizeActionKey("", futuSymbol)
      || normalizeActionKey("", symbol)
      || normalizeActionKey(market, symbol);
  const plannedSymbols = new Set([
    ...cnTrendRows(report?.buy_actions),
    ...cnTrendRows(report?.simulate_rotation_pairs).map((item) => ({
      symbol: item.buy_symbol, futu_symbol: item.buy_futu_symbol,
    })),
    ...cnTrendRows(report?.real_rotation_pairs).map((item) => ({
      symbol: item.buy_symbol, futu_symbol: item.buy_futu_symbol,
    })),
  ].map((item) => auditKey(item)).filter(Boolean));
  const finalSkips = cnTrendRows(report?.risk_skips).filter(
    (item) => !plannedSymbols.has(auditKey(item)),
  );
  const disciplineFailures = cnTrendRows(audit.candidates).filter(
    (item) => item.eligible === false && !plannedSymbols.has(auditKey(item)),
  );
  const finalRows = finalSkips.map((item) => `<tr class="trend-audit-row">
    <td data-label="标的"><strong>${escapeHtml(cnTrendIdentity(item))}</strong></td>
    <td data-label="最终原因">${escapeHtml(formatPlain(item.reason || "未纳入买入计划"))}</td>
  </tr>`).join("") || '<tr class="trend-audit-empty"><td colspan="2">无</td></tr>';
  const failureRows = disciplineFailures.map((item) => `<tr class="trend-audit-row">
    <td data-label="标的"><strong>${escapeHtml(cnTrendIdentity(item))}</strong></td>
    <td data-label="纪律结果">没有通过纪律</td>
  </tr>`).join("") || '<tr class="trend-audit-empty"><td colspan="2">无</td></tr>';
  return `<details class="trend-audit"><summary>候选审计 · 为什么没有进入买入计划</summary>
    <section><h3>通过纪律，但未纳入最终计划</h3><table class="trend-audit-table"><thead><tr><th scope="col">标的</th><th scope="col">最终原因</th></tr></thead><tbody>${finalRows}</tbody></table></section>
    <section><details class="trend-audit-discipline"><summary>最后 · 没有通过纪律 ${disciplineFailures.length}</summary><table class="trend-audit-table"><thead><tr><th scope="col">标的</th><th scope="col">纪律结果</th></tr></thead><tbody>${failureRows}</tbody></table></details></section>
  </details>`;
}

function renderCnTrendAudit(audit, report = {}) {
  audit = audit && typeof audit === "object" ? audit : {};
  const candidates = cnTrendRows(audit.candidates);
  const parameters = audit.strategy_parameters
    && typeof audit.strategy_parameters === "object"
    && !Array.isArray(audit.strategy_parameters)
    ? audit.strategy_parameters : {};
  const industries = Array.isArray(audit.industry_concentration)
    ? audit.industry_concentration.filter(Array.isArray) : [];
  const dataSources = Array.isArray(audit.data_sources) ? audit.data_sources : [];
  const reasonCounts = new Map();

  const rows = candidates.map((item) => {
    const reasons = Array.isArray(item.excluded_reasons)
      ? item.excluded_reasons.map((reason) =>
        cnTrendAuditReason(reason, item, parameters, report))
      : [];
    reasons.forEach((reason) =>
      reasonCounts.set(reason.label, (reasonCounts.get(reason.label) || 0) + 1));
    const status = item.eligible === true
      ? {key: "passed", text: "通过纪律"}
      : item.eligible === false && reasons.length
        ? {key: "excluded", text: "已排除 · " + reasons.length + " 项未通过"}
        : item.eligible === false
          ? {key: "missing", text: "数据缺失"}
          : {key: "review", text: "待确认"};
    const failed = reasons.length
      ? reasons.map((reason) => (
        '<div class="trend-audit-reason"><strong>' + escapeHtml(reason.label)
          + '</strong><span>' + escapeHtml(reason.actual) + " → "
          + escapeHtml(reason.requirement) + "</span></div>"
      )).join("")
      : '<span class="trend-audit-none">无</span>';
    const facts = [
      "温度 " + cnTrendAuditTableTemperature(item),
      "强度 " + cnTrendAuditValue(item.strength),
      "节气 " + cnTrendAuditValue(item.phase),
      "危险信号 " + cnTrendAuditDanger(item.danger),
    ].map((fact) => "<span>" + escapeHtml(fact) + "</span>").join("");
    const details = Object.entries(item).map(([key, value]) => {
      const display = key === "rank" && !hasValue(value)
        ? "未进入候选排名"
        : Array.isArray(value)
        ? value.map(formatPlain).join("、")
        : value && typeof value === "object"
          ? JSON.stringify(value)
          : cnTrendAuditValue(value);
      return "<div><dt>" + escapeHtml(key) + "</dt><dd>"
        + escapeHtml(display) + "</dd></div>";
    }).join("");
    return '<tr class="trend-audit-row">'
      + '<td data-label="标的"><strong>' + escapeHtml(cnTrendIdentity(item))
      + '</strong><span>' + escapeHtml(cnTrendAuditValue(item.industry)) + "</span></td>"
      + '<td data-label="结论"><span class="trend-audit-status" data-status="'
      + status.key + '">' + escapeHtml(status.text) + "</span></td>"
      + '<td data-label="未通过项目"><div class="trend-audit-reasons">' + failed + "</div></td>"
      + '<td data-label="已通过的关键事实"><div class="trend-audit-facts">' + facts + "</div></td>"
      + '<td data-label="审计"><details class="trend-audit-more"><summary>查看全部字段</summary><dl>'
      + details + "</dl></details></td></tr>";
  }).join("");
  const passed = candidates.filter((item) => item.eligible === true).length;
  const excluded = candidates.filter((item) => item.eligible === false).length;
  const reasonSummary = [...reasonCounts.entries()]
    .map(([label, count]) => "<span>" + escapeHtml(label) + " " + count + "</span>").join("");
  const tableBody = rows || '<tr class="trend-audit-empty"><td colspan="5">无候选审计数据</td></tr>';
  return '<details class="trend-audit"><summary>审计详情</summary>'
    + '<section><h3>为什么没有进入买入名单</h3>'
    + '<div class="trend-audit-summary"><span>候选 ' + candidates.length
    + "</span><span>通过 " + passed + "</span><span>排除 " + excluded + "</span>"
    + '</div><div class="trend-audit-reason-counts">'
    + (reasonSummary || "<span>无未通过原因</span>")
    + '</div><table class="trend-audit-table"><thead><tr>'
    + '<th scope="col">标的</th><th scope="col">结论</th><th scope="col">未通过项目</th>'
    + '<th scope="col">已通过的关键事实</th><th scope="col">审计</th>'
    + "</tr></thead><tbody>" + tableBody + "</tbody></table></section>"
    + '<section><h3>行业集中度</h3><ul>'
    + (industries.length
      ? industries.map((item) => "<li>" + escapeHtml(item.map(formatPlain).join("｜")) + "</li>").join("")
      : "<li>无</li>")
    + "</ul></section>"
    + '<p>数据来源：' + escapeHtml(dataSources.map(formatPlain).join("、") || "无") + "</p>"
    + '<p>API 成本：' + escapeHtml(formatPlain(audit.actual_api_cost ?? audit.estimated_api_cost ?? "未知"))
    + "</p></details>";
}

function renderTrendAllocation(report) {
  const allocation = report?.allocation;
  const roots = allocation?.roots;
  const markets = allocation?.markets;
  if (!allocation || typeof allocation !== "object" || !roots || !markets) return "";
  const currentMarket = String(report.market || "").toUpperCase();
  const cards = [
    ["CN", "A股"], ["HK", "港股"], ["US", "美股"],
  ].sort(([left], [right]) => (
    Number(markets[left]?.rank ?? Infinity) - Number(markets[right]?.rank ?? Infinity)
  )).map(([market, label]) => {
    const root = roots[market] || {};
    const values = markets[market] || {};
    const stock = root.stock || {};
    const etf = root.etf || {};
    const current = market === currentMarket ? " · 当前报告" : "";
    return `<article class="trend-allocation-card" data-market="${market}" data-current-report="${market === currentMarket}">
      <header><h3>${escapeHtml(label)}</h3><span>第 ${escapeHtml(formatPlain(values.rank))} 名${current}</span></header>
      <dl>
        <div><dt>${escapeHtml(formatPlain(stock.asset))} 全局强度</dt><dd>${escapeHtml(formatDisplayNumber(stock.global_strength))}</dd></div>
        <div><dt>${escapeHtml(formatPlain(etf.asset))} 全局强度</dt><dd>${escapeHtml(formatDisplayNumber(etf.global_strength))}</dd></div>
        <div><dt>市场分数</dt><dd>${escapeHtml(formatDisplayNumber(values.score))}</dd></div>
        <div><dt>分数来源</dt><dd>${escapeHtml(formatPlain(values.score_source))}</dd></div>
        <div aria-label="单仓基准 ${escapeHtml(decimalAsPercent(values.entry_weight, "-"))}"><dt>单仓基准</dt><dd>${escapeHtml(decimalAsPercent(values.entry_weight, "-"))}</dd></div>
        <div aria-label="10 席位名义仓位 ${escapeHtml(decimalAsPercent(values.nominal_weight, "-"))}"><dt>10 席位名义仓位</dt><dd>${escapeHtml(decimalAsPercent(values.nominal_weight, "-"))}</dd></div>
      </dl>
      <p>来源 ${escapeHtml(formatPlain(stock.as_of_date))} / ${escapeHtml(formatPlain(etf.as_of_date))}</p>
    </article>`;
  }).join("");
  const status = allocation.reused
    ? `沿用旧排名 · ${formatPlain(allocation.stale_a_trading_days)} 个 A 股交易日 · 原快照 ${formatPlain(allocation.allocation_date)}`
    : "当日排名";
  const failure = hasValue(allocation.failure_reason)
    ? `<p class="trend-allocation-warning">本次更新失败原因：${escapeHtml(formatPlain(allocation.failure_reason))}</p>`
    : "";
  return `<section class="trend-allocation-panel" aria-label="市场资源排名">
    <header><h2>市场资源排名</h2><span data-status="${allocation.reused ? "reused" : "current"}">${escapeHtml(status)}</span></header>
    <div class="trend-allocation-cards">${cards}</div>
    <p class="trend-allocation-meta">“全局强度”采用趋势动物 API 返回的全局比较值，不是小程序收藏夹显示的收藏夹内排名分位。</p>
    <p class="trend-allocation-meta">快照 ${escapeHtml(formatPlain(allocation.allocation_date))}｜生成 ${escapeHtml(formatPlain(allocation.generated_at))}｜目标交易日 ${escapeHtml(formatPlain(report.report_date))}｜SHA ${escapeHtml(String(allocation.sha256 || "-").slice(0, 12))}</p>
    ${failure}
  </section>`;
}

function renderTrendRotations(report) {
  if (!report?.allocation) return "";
  const group = (title, mode, comparisons, pairs, statusLabel) => {
    const comparisonRows = cnTrendRows(comparisons).filter(
      (comparison) => String(comparison.outcome || "") === "planned",
    );
    const headings = [
      "卖出标的", "买入标的", "比较口径", "大类内强度", "全局强度", "强度差",
      "判断", "目标仓位（占净值）", "目标金额", "预计数量", "目标交易日", statusLabel,
    ];
    const sourceRows = comparisonRows.length
      ? comparisonRows.map((comparison) => ({
          comparison,
          pair: cnTrendRows(pairs).find((item) => item.pair_index === comparison.pair_index) || {},
        }))
      : cnTrendRows(pairs).map((pair) => ({pair}));
    const rows = sourceRows.map(({comparison, pair}) => {
      comparison = comparison || {
        sell_symbol: pair.sell_symbol,
        sell_name: pair.sell_name,
        buy_symbol: pair.buy_symbol,
        buy_name: pair.buy_name,
        strength_basis: "global",
        sell_local_strength: null,
        buy_local_strength: null,
        sell_global_strength: pair.sell_global_strength,
        buy_global_strength: pair.buy_global_strength,
        strength_gap: pair.strength_gap,
      };
      const basisLabel = comparison.strength_basis === "local" ? "大类内强度" : comparison.strength_basis === "global" ? "全局强度" : "数据未提供";
      const executionStatus = [pair.execution_status, pair.status, pair.order_status].find(hasValue);
      const status = executionStatus ?? (mode === "automatic" ? "待执行" : "待人工确认");
      const route = (label, localValue, globalValue) => renderTrendCell(
        label,
        `${hasValue(localValue) ? formatDisplayNumber(localValue) : "数据未提供"} → ${hasValue(globalValue) ? formatDisplayNumber(globalValue) : "数据未提供"}`,
      );
      return `<tr class="cn-trend-card">
        ${renderTrendCell("卖出标的", trendIdentity({symbol: comparison.sell_symbol, name: comparison.sell_name}))}
        ${renderTrendCell("买入标的", trendIdentity({symbol: comparison.buy_symbol, name: comparison.buy_name}))}
        ${renderTrendCell("比较口径", basisLabel)}
        ${route("大类内强度", comparison.sell_local_strength, comparison.buy_local_strength)}
        ${route("全局强度", comparison.sell_global_strength, comparison.buy_global_strength)}
        ${renderTrendCell("强度差", hasValue(comparison.strength_gap) ? formatDisplayNumber(comparison.strength_gap) : null)}
        ${renderTrendCell("判断", "已触发")}
        ${renderTrendCell("目标仓位（占净值）", decimalAsPercent(pair.target_weight, null))}
        ${renderTrendCell("目标金额", hasValue(pair.target_amount) ? formatDisplayNumber(pair.target_amount) : null)}
        ${renderTrendCell("预计数量", hasValue(pair.estimated_shares) ? `${formatDisplayNumber(pair.estimated_shares)} 股` : null)}
        ${renderTrendCell("目标交易日", pair.execution_date || report.report_date)}
        ${renderTrendCell(statusLabel, status)}
      </tr>`;
    });
    return renderCnTrendTable(
      title, "buy", headings, rows,
      mode === "automatic"
        ? "只显示已触发（强度差 ≥ 20）的合格轮换；未达标项不显示。"
        : "按实盘持仓独立生成；仅人工确认，不自动下单。",
    );
  };
  return `<section class="trend-rotation-panel" aria-label="相对强度轮换"><h2>相对强度轮换</h2>`
    + group("模拟盘自动轮换", "automatic", report.simulate_rotation_comparisons, report.simulate_rotation_pairs, "执行状态")
    + group("实盘手动轮换", "manual", report.real_rotation_comparisons, report.real_rotation_pairs, "状态")
    + `</section>`;
}

function renderCnTrendReportWorkspace(report, embedded = false, historical = false, trailingContent = "") {
  const counts = report.counts || {};
  const audit = report.audit || {};
  const isCn = String(report.market || "").toUpperCase() === "CN";
  const finalPlanAudit = usesFinalPlanTrendAudit(report);
  const sellOrHold = renderTrendSellOrHoldStage;
  const buyStage = finalPlanAudit && !trendFinalPlanBuyActions(report).length
    ? "" : renderTrendBuyStage(report);
  const root = embedded ? "div" : "main";
  const identity = report.artifact && report.report_sha256 && report.strategy_version
    ? ` data-report-artifact="${escapeHtml(formatPlain(report.artifact))}" data-report-sha256="${escapeHtml(formatPlain(report.report_sha256))}" data-strategy-version="${escapeHtml(formatPlain(report.strategy_version))}"`
    : "";
  const strategyVersion = report.strategy_version
    ? `<span>版本 ${escapeHtml(formatPlain(report.strategy_version))}</span>`
    : "";
  const batchSha = report.execution_batch?.report_sha256;
  const revisionAnomaly = report.revision_anomaly === true
    ? `<p class="trend-revision-anomaly">发现后续报告版本，执行仍锁定原批次 · 批次 ${escapeHtml(String(batchSha || "—").slice(0, 12))} · 最新 ${escapeHtml(String(report.latest_report_sha256 || "—").slice(0, 12))}</p>`
    : "";
  const batchError = report.execution_batch_blocking === true
    ? `<p class="trend-execution-batch-error">${escapeHtml(formatPlain(report.execution_batch_error || "执行批次无效，已阻止操作投影"))}</p>`
    : "";
  const sellStage = sellOrHold("优先处理 · 卖出触发", report.sell_actions, "sell", report);
  const reviewStage = Array.isArray(report.review_actions) && report.review_actions.length
    ? sellOrHold("需要确认 · 人工复核", report.review_actions, "review", report)
    : "";
  const holdStage = renderTrendHoldingStage(report);
  const disciplineCards = renderTrendDisciplineCards(report);
  const industryContext = renderTrendIndustryContext(report);
  const riskSummary = renderTrendRiskSummary(report.risk_summary, report.drawdown_summary, report.report_date);
  const allocation = renderTrendAllocation(report);
  const rotations = renderTrendRotations(report);
  return `<${root} class="cn-trend-report"${identity}>
    <header class="trend-report-header">
      <div><p>${escapeHtml(`${formatPlain(report.broker_label)}｜${formatPlain(report.market_label)}`)}</p><h1>当天趋势报告</h1>${strategyVersion}</div>
      ${embedded
        ? historical
          ? `<button class="trend-history-button" type="button" data-current-trend-report="${escapeHtml(report.broker)}">返回当前报告</button>`
          : `<button class="trend-history-button" type="button" data-report-history="${escapeHtml(report.broker)}">历史报告</button>`
        : '<button type="button" data-close-trend-report>返回持仓看板</button>'}
      <dl>
        <div><dt>报告</dt><dd>${escapeHtml(formatPlain(report.report_date))}</dd></div>
        <div><dt>数据</dt><dd>${escapeHtml(formatPlain(report.data_date))}</dd></div>
        <div><dt>生成</dt><dd>${escapeHtml(formatPlain(report.generated_at))}</dd></div>
        <div><dt>账户</dt><dd>${escapeHtml(formatPlain(report.account_status))}</dd></div>
      </dl>
      <div class="trend-report-metrics cn-trend-counts">
        <span>卖出 ${escapeHtml(formatDisplayNumber(counts.sell || 0))}</span>
        <span>买入 ${escapeHtml(formatDisplayNumber(counts.buy || 0))}</span>
        <span>持有 ${escapeHtml(formatDisplayNumber(counts.hold || 0))}</span>
        <span>复核 ${escapeHtml(formatDisplayNumber(counts.review || 0))}</span>
      </div>
      <div class="trend-report-facts">
        <span>状态 ${escapeHtml(formatPlain(report.status_text || report.data_status || report.account_status || "数据未提供"))}</span>
        <span class="trend-report-cost">${escapeHtml(trendReportCostLabel(report))}</span>
      </div>
    </header>
    ${allocation}
    ${batchError}
    ${revisionAnomaly}
    ${sellStage}
    ${rotations}
    ${buyStage}
    ${reviewStage}
    ${holdStage}
    ${industryContext}
    ${disciplineCards}
    ${riskSummary}
    ${renderTrendControllerStatus(report.broker)}
    ${finalPlanAudit ? renderFinalPlanTrendAudit(report) : isCn ? renderCnTrendAudit(audit, report) : renderTrendAudit(audit)}
    ${trailingContent}
  </${root}>`;
}

function trendSellActionLabel(item) {
  return item?.action === "SELL_PARTIAL" ? "止盈减仓 30%" : "全部卖出";
}

function renderTrendControllerStatus(broker) {
  const configured = state.dashboard?.trend_controllers?.[broker];
  const controller = configured && typeof configured === "object"
    ? configured
    : {health: "unavailable", blocking: false, reason: "控制器状态未提供"};
  const health = String(controller.health || "unavailable");
  const blocking = controller.blocking === true;
  const headline = health === "readonly"
    ? "只读部署，不运行本机控制器"
    : health === "healthy" ? "执行主机控制器正常" : "控制器不可用";
  const facts = [
    ["执行模式", controller.effective_mode],
    ["执行主机", controller.executor_host],
    ["本地主机", controller.local_host],
    ["PID", controller.pid],
    ["Git SHA", controller.git_sha],
    ["当前阶段", controller.phase],
    ["心跳", controller.heartbeat_at],
    ["最近成功", formatTrendControllerLastSuccess(controller.last_success)],
    ["当前阻塞", controller.blocker || controller.reason],
    ["下次检查", controller.next_check_at],
  ];
  return `<details class="trend-controller-status${blocking ? " blocking" : ""}" data-health="${escapeHtml(health)}">
    <summary>策略控制器 <span>${escapeHtml(headline)}</span><small>已折叠</small></summary>
    <div class="trend-controller-status-body"><dl>${facts.map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHtml(hasValue(value) ? formatPlain(value) : "—")}</dd></div>`).join("")}</dl></div>
  </details>`;
}

function formatTrendControllerLastSuccess(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value !== "object" || Array.isArray(value)) return formatPlain(value);
  const labels = {
    status: "状态", market: "市场", date: "日期",
    submitted_count: "提交数", artifact_paths: "产物",
  };
  const keys = ["status", "market", "date", "submitted_count", "artifact_paths"]
    .filter((key) => Object.hasOwn(value, key));
  const ordered = keys.length ? keys : Object.keys(value).sort();
  const parts = ordered.map((key) => {
    const item = value[key];
    const rendered = Array.isArray(item)
      ? item.length ? item.map(formatPlain).join("，") : "无"
      : formatPlain(item);
    return `${labels[key] || key} ${rendered}`;
  });
  return parts.length ? parts.join(" · ") : "—";
}

function renderTrendReportWorkspace(report, embedded = false, historical = false, trailingContent = "") {
  return renderCnTrendReportWorkspace(report || {}, embedded, historical, trailingContent);
}

function renderHeaderSummary() {
  const summary = state.accountSnapshot?.summary || {};
  elements["current-view-value"].textContent = formatMoney(summary.portfolio_value_hkd, "HKD");
  elements["current-view-holding-value"].textContent = `持仓资产 ${formatMoney(summary.holding_value_hkd, "HKD")}`;
  elements["current-view-holding-weight"].textContent = formatPlain(summary.holding_weight_hkd);
  elements["current-view-cash-note"].textContent = `现金类资产 ${formatMoney(summary.cash_like_value_hkd, "HKD")} · 持仓 ${formatDisplayNumber(summary.holding_count)}`;
  elements["current-view-label"].textContent = currentViewLabel(activeAccountRowCount());
}

function activeAccountRowCount() {
  const group = accountHoldingGroups().find((item) => item.broker === state.brokerFilter);
  if (!group) return 0;
  return group.rows.filter(({display}) => state.marketFilter === "ALL"
    || String(display.market || "").toUpperCase() === state.marketFilter).length;
}

function currentViewLabel(count) {
  const marketLabel = state.marketFilter === "ALL" ? "全部市场" : state.marketFilter === "CN" ? "A 股" : state.marketFilter;
  const brokerLabel = brokerDisplayName(state.brokerFilter);
  return `当前视图：${marketLabel} · ${brokerLabel} · ${formatDisplayNumber(count)} 条`;
}

function renderSummary() {
  const snapshot = state.accountSnapshot || {};
  const summary = snapshot.summary || {};
  elements["summary-value"].textContent = formatMoney(summary.portfolio_value_hkd, "HKD");
  elements["summary-holding-value"].textContent = `持仓资产 ${formatMoney(summary.holding_value_hkd, "HKD")}`;
  elements["summary-holding-weight"].textContent = formatPlain(summary.holding_weight_hkd);
  elements["summary-cash-note"].textContent = `现金类资产 ${formatMoney(summary.cash_like_value_hkd, "HKD")} · ${formatPlain(summary.cash_like_weight_hkd)} · 持仓 ${formatDisplayNumber(summary.holding_count)}`;
  elements["summary-holding-bar"].style.width = percentBarWidth(summary.holding_weight_hkd);
  elements["summary-brokers"].textContent = `${formatDisplayNumber(summary.broker_count)} 个`;
  elements["summary-detail-month"].textContent = snapshot.generated_at
    ? `账户快照 ${snapshot.generated_at}` : "账户快照不可用";
  elements["summary-health"].textContent = accountActionsEnabled() ? "明细可用" : "账户不可用";
  elements["summary-health-note"].textContent = accountSnapshotStatusText();
}

function percentBarWidth(value) {
  if (!hasValue(value)) {
    return "0%";
  }
  const parsed = Number.parseFloat(String(value).replace("%", ""));
  if (!Number.isFinite(parsed)) {
    return "0%";
  }
  return `${Math.min(100, Math.max(0, parsed))}%`;
}

function firstPresent(...values) {
  return values.find((value) => hasValue(value));
}

function renderHoldings() {
  renderAccountHoldings();
}

function renderAccountTabs(groups) {
  const counts = new Map(groups.map((group) => [group.broker, group.rows.length]));
  return ACCOUNT_BROKERS.map((broker) => {
    const selected = broker === state.brokerFilter;
    return `<button id="account-tab-${escapeHtml(broker)}" class="account-tab ${selected ? "active" : ""}" type="button" role="tab"
      data-broker="${escapeHtml(broker)}" aria-selected="${selected}" tabindex="${selected ? "0" : "-1"}" aria-controls="account-holdings">
      ${escapeHtml(brokerDisplayName(broker))}<span>${escapeHtml(formatDisplayNumber(counts.get(broker) || 0))}</span>
    </button>`;
  }).join("");
}

function selectBroker(broker) {
  if (!ACCOUNT_BROKERS.includes(broker)) return;
  state.brokerFilter = broker;
  state.selectedHoldingKey = "";
  state.selectedHoldingDetail = "decision";
  syncDecisionDeepLink();
  renderAccountHoldings();
  if (elements["current-view-label"]) renderHeaderSummary();
}

function handleBrokerSelection(event) {
  const button = event.target.closest("[data-broker]");
  if (button) selectBroker(button.dataset.broker || "");
}

function handleBrokerTabKeydown(event) {
  const tab = event.target.closest('[role="tab"][data-broker]');
  if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const current = ACCOUNT_BROKERS.indexOf(tab.dataset.broker || "");
  const index = event.key === "Home" ? 0
    : event.key === "End" ? ACCOUNT_BROKERS.length - 1
      : (current + (event.key === "ArrowRight" ? 1 : -1) + ACCOUNT_BROKERS.length) % ACCOUNT_BROKERS.length;
  const broker = ACCOUNT_BROKERS[index];
  selectBroker(broker);
  elements["account-tabs"].querySelector(`[data-broker="${broker}"]`)?.focus();
}

function accountViewLabel(broker, view) {
  if (view === "real") return "真实持仓";
  if (view === "simulate") return "模拟盘持仓";
  return "趋势报告";
}

function renderAccountViewTabs(broker) {
  const selectedView = state.accountViews[broker] || "real";
  return `<div class="account-view-tabs" role="tablist" aria-label="${escapeHtml(brokerDisplayName(broker))}账户视图">
    ${ACCOUNT_VIEW_KEYS.map((view) => {
      const selected = view === selectedView;
      return `<button id="account-${escapeHtml(broker)}-view-${escapeHtml(view)}" class="account-view-tab" type="button" role="tab" data-account-broker="${escapeHtml(broker)}" data-account-view="${escapeHtml(view)}" aria-selected="${selected}" tabindex="${selected ? "0" : "-1"}" aria-controls="account-${escapeHtml(broker)}-view-panel">${escapeHtml(accountViewLabel(broker, view))}</button>`;
    }).join("")}
  </div>`;
}

async function setAccountView(broker, view) {
  if (!TREND_ACCOUNT_BROKERS.includes(broker) || !ACCOUNT_VIEW_KEYS.includes(view)) return;
  state.accountViews[broker] = view;
  state.selectedHoldingKey = "";
  state.selectedHoldingDetail = "decision";
  state.selectedDecisionTab = "final";
  syncDecisionDeepLink();
  const needsSimulation = view === "simulate";
  if (needsSimulation && !Object.hasOwn(state.trendSimulatePositions, broker)) {
    await loadTrendSimulatePositions(broker);
  } else {
    renderAccountViewPanelOnly(broker);
  }
}

function handleAccountViewTabKeydown(event) {
  const tab = event.target.closest('[role="tab"][data-account-view]');
  if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const broker = tab.dataset.accountBroker || "";
  const current = ACCOUNT_VIEW_KEYS.indexOf(tab.dataset.accountView || "");
  const index = event.key === "Home" ? 0
    : event.key === "End" ? ACCOUNT_VIEW_KEYS.length - 1
      : (current + (event.key === "ArrowRight" ? 1 : -1) + ACCOUNT_VIEW_KEYS.length) % ACCOUNT_VIEW_KEYS.length;
  const view = ACCOUNT_VIEW_KEYS[index];
  setAccountView(broker, view);
  elements["account-holdings"].querySelector(`[data-account-view="${view}"]`)?.focus();
}

async function loadTrendSimulatePositions(broker) {
  if (state.accountViews[broker] !== "simulate") return;
  state.trendSimulatePositions[broker] = {loading: true};
  renderAccountViewPanelOnly(broker);
  try {
    const response = await fetch(`/api/trend-simulate-positions/${encodeURIComponent(broker)}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`simulate positions ${response.status}`);
    state.trendSimulatePositions[broker] = await response.json();
  } catch (error) {
    state.trendSimulatePositions[broker] = {
      available: false,
      positions: [],
      error: error instanceof Error ? error.message : String(error),
    };
  }
  if (state.brokerFilter === broker && state.accountViews[broker] === "simulate") {
    renderAccountViewPanelOnly(broker);
  }
}

function accountScrollY() {
  return typeof window !== "undefined" && Number.isFinite(window.scrollY) ? window.scrollY : 0;
}

function restoreAccountScroll(scrollY) {
  if (typeof window !== "undefined" && typeof window.scrollTo === "function") {
    window.scrollTo(0, scrollY);
  }
}

async function openTrendReportHistory(broker) {
  if (!TREND_ACCOUNT_BROKERS.includes(broker)) return;
  const existing = state.trendReportHistories[broker];
  const scrollY = accountScrollY();
  if (existing && Array.isArray(existing.rows)) {
    state.trendReportHistories[broker] = {...existing, open: true, scrollY};
    renderAccountViewPanelOnly(broker);
    if (state.brokerFilter === broker) restoreAccountScroll(scrollY);
    return;
  }
  state.trendReportHistories[broker] = {open: true, loading: true, rows: [], scrollY};
  renderAccountViewPanelOnly(broker);
  try {
    const response = await fetch(`/api/trend-reports/${encodeURIComponent(broker)}/history`, {cache: "no-store"});
    if (!response.ok) throw new Error(`report history ${response.status}`);
    const rows = await response.json();
    const history = state.trendReportHistories[broker] || {};
    state.trendReportHistories[broker] = {
      ...history,
      loading: false,
      rows,
      error: "",
    };
  } catch (error) {
    const history = state.trendReportHistories[broker] || {};
    state.trendReportHistories[broker] = {
      ...history,
      loading: false,
      rows: [],
      error: error instanceof Error ? error.message : String(error),
    };
  }
  const history = state.trendReportHistories[broker];
  if (state.brokerFilter === broker && state.accountViews[broker] === "report" && history.open) {
    renderAccountViewPanelOnly(broker);
    restoreAccountScroll(history.scrollY);
  }
}

async function loadHistoricalTrendReport(broker, artifact) {
  if (!TREND_ACCOUNT_BROKERS.includes(broker) || !artifact) return;
  state.trendReportHistories[broker] = {
    ...(state.trendReportHistories[broker] || {}),
    open: true,
    scrollY: accountScrollY(),
  };
  state.accountViews[broker] = "report";
  state.trendHistoricalReports[broker] = {artifact, loading: true};
  renderAccountViewPanelOnly(broker);
  try {
    const response = await fetch(`/api/trend-reports/${encodeURIComponent(broker)}/history/${encodeURIComponent(artifact)}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`historical report ${response.status}`);
    const report = await response.json();
    if (state.trendHistoricalReports[broker]?.artifact !== artifact) return;
    state.trendHistoricalReports[broker] = {artifact, report};
  } catch (error) {
    if (state.trendHistoricalReports[broker]?.artifact !== artifact) return;
    state.trendHistoricalReports[broker] = {
      artifact,
      error: error instanceof Error ? error.message : String(error),
    };
  }
  const history = state.trendReportHistories[broker] || {};
  if (state.brokerFilter === broker
      && state.accountViews[broker] === "report"
      && history.open
      && state.trendHistoricalReports[broker]?.artifact === artifact) {
    renderAccountViewPanelOnly(broker);
    restoreAccountScroll(history.scrollY);
  }
}

function showCurrentTrendReport(broker) {
  const history = state.trendReportHistories[broker] || {};
  delete state.trendHistoricalReports[broker];
  if (Object.hasOwn(state.trendReportHistories, broker)) {
    state.trendReportHistories[broker] = {...history, open: false};
  }
  renderAccountViewPanelOnly(broker);
  if (state.brokerFilter === broker) {
    restoreAccountScroll(history.scrollY || 0);
    const historyButton = elements["account-holdings"]
      ?.querySelector(`[data-report-history="${broker}"]`);
    if (typeof historyButton?.focus === "function") historyButton.focus();
  }
}

function filterAccountRows(rows) {
  return rows.filter(({display}) => state.marketFilter === "ALL"
    || String(display.market || "").toUpperCase() === state.marketFilter);
}

function accountDisclosureKey(root, details) {
  const path = [];
  let current = details;
  while (current && current !== root) {
    const parent = current.parentElement;
    if (!parent) return "";
    const index = Array.from(parent.children).indexOf(current);
    const identity = current.tagName === "DETAILS"
      ? [
        current.className || "",
        current.getAttribute("aria-label") || "",
        current.getAttribute("data-discipline") || "",
        current.getAttribute("data-health") || "",
        current.getAttribute("data-risk-status") || "",
      ].join("|")
      : current.tagName || "";
    path.unshift(`${identity}#${index}`);
    current = parent;
  }
  return path.join("/");
}

function accountDisclosureScope(root, broker, view) {
  const section = root?.querySelector?.(`#account-${broker}`);
  if (!section || typeof section.querySelector !== "function") return null;
  const panel = section.querySelector(`#account-${broker}-view-panel`);
  if (TREND_ACCOUNT_BROKERS.includes(broker)
      && panel?.getAttribute("aria-labelledby") !== `account-${broker}-view-${view}`) {
    return null;
  }
  const report = panel?.querySelector?.(".cn-trend-report");
  return [
    broker,
    view,
    report?.dataset?.reportArtifact || "",
    report?.dataset?.reportSha256 || "",
    report?.dataset?.strategyVersion || "",
  ].join("|");
}

function captureAccountDisclosureState(root, broker, view) {
  const section = root?.querySelector?.(`#account-${broker}`);
  const scope = accountDisclosureScope(root, broker, view);
  if (!section || scope === null || typeof section.querySelectorAll !== "function") return null;
  const states = new Map();
  section.querySelectorAll("details").forEach((details) => {
    const key = accountDisclosureKey(section, details);
    if (key) states.set(key, details.open);
  });
  const holdingView = section.querySelector(
    '[data-trend-holding-view][aria-selected="true"]',
  )?.dataset?.trendHoldingView || "";
  return {scope, states, holdingView};
}

function restoreAccountDisclosureState(root, broker, view, snapshot) {
  if (!snapshot) return;
  const section = root?.querySelector?.(`#account-${broker}`);
  if (!section || accountDisclosureScope(root, broker, view) !== snapshot.scope
      || typeof section.querySelectorAll !== "function") return;
  section.querySelectorAll("details").forEach((details) => {
    const key = accountDisclosureKey(section, details);
    if (snapshot.states.has(key)) details.open = snapshot.states.get(key);
  });
  const holdingTab = snapshot.holdingView
    ? section.querySelector(`[data-trend-holding-view="${snapshot.holdingView}"]`)
    : null;
  if (holdingTab) handleTrendHoldingTab({target: holdingTab});
}

function renderAccountViewPanelOnly(broker) {
  const container = elements["account-holdings"] || elements["holdings-body"];
  const panel = state.brokerFilter === broker && typeof container?.querySelector === "function"
    ? container.querySelector(`#account-${broker}-view-panel`)
    : null;
  if (!panel) return;
  const group = accountHoldingGroups().find((item) => item.broker === broker);
  if (!group) return;
  const view = state.accountViews[broker] || "real";
  const rows = filterAccountRows(group.rows);
  const visibleRows = view === "simulate"
    ? filterAccountRows(simulatedAccountRows(broker))
    : rows;
  const disclosureSnapshot = captureAccountDisclosureState(container, broker, view);
  if (elements["visible-count"]) {
    elements["visible-count"].textContent = `${formatDisplayNumber(visibleRows.length)} 条`;
  }
  panel.innerHTML = renderAccountViewPanel({...group, rows});
  panel.setAttribute("aria-labelledby", `account-${broker}-view-${view}`);
  restoreAccountDisclosureState(container, broker, view, disclosureSnapshot);
  container.querySelectorAll?.(`#account-${broker} [data-account-view]`).forEach((tab) => {
    const selected = tab.dataset.accountView === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
}

function renderAccountHoldings() {
  const container = elements["account-holdings"] || elements["holdings-body"];
  const focusedView = document.activeElement?.dataset?.accountView || "";
  const focusedBroker = document.activeElement?.dataset?.accountBroker || "";
  elements["workspace-grid"].classList.remove("detail-mode");
  container.classList.remove("hidden");
  elements["symbol-detail-panel"].classList.add("hidden");
  elements["symbol-detail-panel"].innerHTML = "";
  if (!state.accountSnapshot) {
    setAccountHoldingsFallbackLabel("账户持仓不可用");
    elements["visible-count"].textContent = "0 条";
    const groups = accountHoldingGroups();
    const active = groups.find((group) => group.broker === state.brokerFilter) || groups[0];
    container.innerHTML = active
      ? renderAccountSection({...active, rows: []})
      : `<div class="empty-state">${escapeHtml(accountSnapshotStatusText())}</div>`;
    return;
  }
  const groups = accountHoldingGroups();
  const active = groups.find((group) => group.broker === state.brokerFilter) || groups[0];
  if (active && active.broker !== state.brokerFilter) state.brokerFilter = active.broker;
  elements["account-tabs"].innerHTML = renderAccountTabs(groups);
  if (active && typeof container.setAttribute === "function") {
    if (typeof container.removeAttribute === "function") container.removeAttribute("aria-label");
    container.setAttribute("aria-labelledby", `account-tab-${active.broker}`);
  }
  const rows = active ? filterAccountRows(active.rows) : [];
  const simulated = active ? simulatedAccountRows(active.broker) : [];
  const visibleRows = active && state.accountViews[active.broker] === "simulate"
    ? filterAccountRows(simulated)
    : rows;
  const disclosureSnapshot = active
    ? captureAccountDisclosureState(
      container,
      active.broker,
      state.accountViews[active.broker] || "real",
    )
    : null;
  elements["visible-count"].textContent = `${formatDisplayNumber(visibleRows.length)} 条`;
  container.innerHTML = active
    ? renderAccountSection({...active, rows})
    : '<div class="empty-state">暂无券商账户</div>';
  if (active) {
    restoreAccountDisclosureState(
      container,
      active.broker,
      state.accountViews[active.broker] || "real",
      disclosureSnapshot,
    );
  }
  if (active?.broker === focusedBroker && focusedView) {
    container.querySelector(`[data-account-view="${focusedView}"]`)?.focus({preventScroll: true});
  }
}

function setAccountHoldingsFallbackLabel(label) {
  const container = elements["account-holdings"] || elements["holdings-body"];
  if (elements["account-tabs"]) elements["account-tabs"].innerHTML = "";
  if (typeof container.removeAttribute === "function") container.removeAttribute("aria-labelledby");
  if (typeof container.setAttribute === "function") container.setAttribute("aria-label", label);
}

function renderAccountSection(group) {
  const headingId = `account-${group.broker}-title`;
  const rows = group.rows;
  const alias = brokerAccountAlias(group.broker, group.summary);
  const sync = brokerSyncStatus(group.broker);
  const source = sync.display;
  const sourceTime = firstPresent(
    state.accountSnapshot?.sources?.account?.brokers?.[group.broker]?.data_as_of,
    state.accountSnapshot?.sources?.account?.brokers?.[group.broker]?.as_of,
    group.summary.generated_at, group.summary.as_of, "-",
  );
  return `<section id="account-${escapeHtml(group.broker)}" class="account-section">
    <header class="account-section-header">
      <div><h2 id="${headingId}">${escapeHtml(brokerDisplayName(group.summary))}</h2>
      <span>${escapeHtml(group.profile.horizon)} · ${escapeHtml(group.profile.strategy)}</span>
      <span>${escapeHtml(formatPlain(alias))}</span></div>
      <div class="account-section-meta">
        <span>持仓资产 ${escapeHtml(formatMoney(group.summary.holding_value_hkd, "HKD"))}</span>
        <span>现金 ${escapeHtml(formatMoney(group.summary.cash_like_value_hkd, "HKD"))}</span>
        ${group.broker === "tiger" && hasValue(group.summary.available_to_trade_hkd)
          ? `<span>可交易额度 ${escapeHtml(formatMoney(group.summary.available_to_trade_hkd, "HKD"))}</span>`
          : ""}
        <span>持仓 ${escapeHtml(formatDisplayNumber(group.summary.holding_count))}</span>
        <span>来源 ${escapeHtml(formatPlain(source))}</span>
        <span>时间 ${escapeHtml(formatPlain(sourceTime))}</span>
        ${renderAccountCashDetails(group)}
      </div>
      <div class="account-section-actions">
        <strong>${escapeHtml(formatMoney(group.summary.portfolio_value_hkd, "HKD"))}</strong>
        ${renderStatementUpload(group.broker)}
      </div>
      ${TREND_ACCOUNT_BROKERS.includes(group.broker) ? renderAccountViewTabs(group.broker) : ""}
    </header>
    ${renderAccountSyncAlert(group.broker)}
    ${TREND_ACCOUNT_BROKERS.includes(group.broker)
      ? `<div id="account-${escapeHtml(group.broker)}-view-panel" class="account-view-panel" role="tabpanel" aria-labelledby="account-${escapeHtml(group.broker)}-view-${escapeHtml(state.accountViews[group.broker] || "real")}">${renderAccountViewPanel(group)}</div>`
      : rows.length ? renderAccountTable(rows) : '<p class="account-empty">当前筛选下没有持仓</p>'}
  </section>`;
}

function renderAccountSyncAlert(broker) {
  const sync = brokerSyncStatus(broker);
  if (!sync.unsafe) return "";
  const title = !state.accountSnapshot ? "账户快照不可用。"
    : sync.status === "failed" ? `${brokerDisplayName(broker)}账户同步失败。`
    : sync.status === "stale" ? `${brokerDisplayName(broker)}账户数据已过期。`
    : `${brokerDisplayName(broker)}账户同步状态未知。`;
  const detail = !state.accountSnapshot
    ? "账户数据尚未确认；新做T提醒及依赖当前持仓的动作已暂停。"
    : `${sync.display}。以下为已接受数据；新做T提醒及依赖当前持仓的动作已暂停。`;
  return `<div class="account-sync-alert ${escapeHtml(sourceStatusClass(sync.status))}" role="status"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

function renderAccountViewPanel(group) {
  const view = state.accountViews[group.broker] || "real";
  if (view === "simulate") return renderSimulatedAccountView(group.broker);
  if (view === "report") return renderEmbeddedTrendReport(group.broker);
  return group.rows.length
    ? renderAccountTable(group.rows)
    : '<p class="account-empty">当前筛选下没有持仓</p>';
}

function simulatedAccountRows(broker) {
  const payload = state.trendSimulatePositions[broker] || {};
  const positions = Array.isArray(payload.positions) ? payload.positions : [];
  return positions.map((position, index) => {
    const display = {
      ...accountPositionDisplay(position),
      total_quantity: position.quantity,
      avg_cost_price: position.cost_price,
    };
    return {
      key: `simulate:${broker}:${display.market || ""}:${display.symbol || ""}:${index}`,
      broker,
      holding: position,
      display,
      index,
    };
  });
}

function accountPositionDisplay(position) {
  const valuation = position?.current_valuation;
  if (!valuation || typeof valuation !== "object") return position;
  const fields = [
    "price", "price_kind", "price_as_of", "market_value_usd", "market_value_hkd",
  ];
  if (fields.some((field) => !hasValue(valuation[field]))) return position;
  return {
    ...position,
    last_price: valuation.price,
    price_kind: valuation.price_kind,
    price_as_of: valuation.price_as_of,
    market_value_usd: valuation.market_value_usd,
    market_value_hkd: valuation.market_value_hkd,
  };
}

function accountValuationKey(position) {
  return String(
    position?.position_id
      || [position?.broker, position?.account_alias, position?.market, position?.symbol].join(":"),
  );
}

function accountValuationSignature(position) {
  const valuation = position?.current_valuation;
  if (!valuation || typeof valuation !== "object") return "";
  return JSON.stringify([
    valuation.price,
    valuation.price_kind,
    valuation.price_as_of,
    valuation.market_value_usd,
    valuation.market_value_hkd,
  ]);
}

function accountValuationUpdates(previous, next) {
  const updates = new Set();
  if (!previous || !Array.isArray(previous.positions) || !Array.isArray(next?.positions)) {
    return updates;
  }
  const previousByKey = new Map(
    previous.positions.map((position) => [accountValuationKey(position), position]),
  );
  next.positions.forEach((position) => {
    const before = previousByKey.get(accountValuationKey(position));
    const signature = accountValuationSignature(position);
    if (before && signature && accountValuationSignature(before) !== signature) {
      updates.add(accountValuationKey(position));
    }
  });
  return updates;
}

function renderSimulatedAccountView(broker) {
  const payload = state.trendSimulatePositions[broker];
  if (!payload || payload.loading) return '<p class="account-empty">模拟盘持仓加载中</p>';
  if (!payload.available) {
    return `<p class="account-empty missing-text">${escapeHtml(formatPlain(payload.error || "模拟盘持仓不可用"))}</p>`;
  }
  const rows = filterAccountRows(simulatedAccountRows(broker));
  return rows.length
    ? renderAccountTable(rows, {simulated: true})
    : '<p class="account-empty">当前无模拟盘持仓</p>';
}

function renderEmbeddedTrendReport(broker) {
  const historical = state.trendHistoricalReports[broker];
  if (historical) {
    if (historical.loading) return '<p class="account-empty">历史报告加载中</p>';
    if (historical.error) return `<div class="trend-history-panel"><button class="trend-history-button" type="button" data-current-trend-report="${escapeHtml(broker)}">返回当前报告</button><p class="missing-text">${escapeHtml(historical.error)}</p></div>`;
    return renderTrendReportWorkspace(historical.report || {}, true, true);
  }
  const history = state.trendReportHistories[broker];
  if (history?.open) return renderTrendReportHistory(broker, history);
  const report = state.dashboard?.trend_reports?.[broker] || {};
  const review = state.dashboard?.trend_reviews?.[broker];
  const reviewPanel = !review ? "" : review.available
    ? `<details class="trend-audit trend-review-disclosure"><summary>趋势复盘 <span>${escapeHtml(`${formatTrendReviewSampleCount(review, "discipline", "纪律模拟")} · ${formatTrendReviewSampleCount(review, "actual", "实际执行")}`)}</span></summary>${renderTrendReviewWorkspace(review, true)}</details>`
    : `<details class="trend-audit trend-review-disclosure"><summary>趋势复盘 <span>${escapeHtml(formatPlain(review.status_text || "暂无复盘数据"))}</span></summary><p class="account-empty">${escapeHtml(formatPlain(review.status_text || "暂无复盘数据"))}</p></details>`;
  if (report.available) return renderTrendReportWorkspace(report, true, false, reviewPanel);
  const statusClass = report.execution_batch_blocking === true
    ? "trend-execution-batch-error"
    : "account-empty";
  return `${renderTrendControllerStatus(broker)}<p class="${statusClass}">${escapeHtml(formatPlain(report.status_text || "今日暂无趋势报告"))}</p>${reviewPanel}`;
}

function renderTrendReportHistory(broker, history) {
  const rows = Array.isArray(history.rows) ? history.rows : [];
  const content = history.loading
    ? '<p class="account-empty">历史报告加载中</p>'
    : history.error
      ? `<p class="missing-text">${escapeHtml(history.error)}</p>`
      : rows.length
        ? `<ul class="trend-history-list">${rows.map((row) => row.available
          ? `<li><button type="button" data-history-broker="${escapeHtml(broker)}" data-history-artifact="${escapeHtml(row.artifact)}"><strong>报告 ${escapeHtml(formatPlain(row.execution_date))} · ${escapeHtml(formatPlain(row.strategy_version))}</strong><span class="trend-history-meta"><span>${escapeHtml(row.artifact)}</span><span>数据截至 ${escapeHtml(formatPlain(row.data_date))}</span><span>生成时间 ${escapeHtml(formatPlain(row.generated_at))}</span><span>策略版本 ${escapeHtml(formatPlain(row.strategy_version))}</span><span>执行摘要 卖出 ${escapeHtml(formatPlain(row.execution_counts?.sell))} · 买入 ${escapeHtml(formatPlain(row.execution_counts?.buy))} · 持有 ${escapeHtml(formatPlain(row.execution_counts?.hold))} · 复核 ${escapeHtml(formatPlain(row.execution_counts?.review))}</span></span></button></li>`
          : `<li><span class="missing-text">${escapeHtml(formatPlain(row.artifact))} · ${escapeHtml(formatPlain(row.status_text))}</span></li>`).join("")}</ul>`
        : '<p class="account-empty">暂无历史报告</p>';
  return `<section class="trend-history-panel"><header><h1>历史报告</h1><button class="trend-history-button" type="button" data-current-trend-report="${escapeHtml(broker)}">返回当前报告</button></header>${content}</section>`;
}

function renderAccountCashDetails(group) {
  const components = group.broker === "tiger" && Array.isArray(group.summary.cash_components)
    ? group.summary.cash_components
    : [];
  if (!components.length) return "";
  const rows = components.map((component) => `<li><span>${escapeHtml(formatPlain(component.label))}</span><strong>${escapeHtml(formatMoney(component.value_hkd, "HKD"))}</strong></li>`).join("");
  return `<details class="account-cash-details"><summary>现金构成</summary><ul>${rows}</ul></details>`;
}

function renderStatementUpload(broker) {
  if (!["phillips", "eastmoney"].includes(broker)) return "";
  const active = state.statementUpload.broker === broker;
  const busy = active && state.statementUpload.busy;
  const message = active ? state.statementUpload.message : "";
  const tone = active && state.statementUpload.error ? " error" : "";
  return `<div class="statement-upload">
    <input class="statement-upload-input" type="file" accept=".pdf,application/pdf" data-statement-file="${escapeHtml(broker)}" hidden>
    <button class="secondary-button" type="button" data-statement-upload="${escapeHtml(broker)}" ${busy || !accountActionsEnabled() ? "disabled" : ""}>${busy ? "上传中…" : "上传结单"}</button>
    <span class="statement-upload-status${tone}" role="status">${escapeHtml(message)}</span>
  </div>`;
}

async function uploadStatement(broker, file) {
  if (!/\.pdf$/i.test(String(file?.name || ""))) {
    throw new Error("请选择 PDF 文件");
  }
  if (Number(file.size) > 20 * 1024 * 1024) {
    throw new Error("PDF 不能超过 20 MiB");
  }
  const response = await fetch(`/api/v1/account/statements/${encodeURIComponent(broker)}`, {
    method: "POST",
    headers: {"Content-Type": "application/pdf"},
    body: file,
  });
  const payload = await response.json();
  if (!response.ok || payload.status === "error") {
    throw new Error(payload.message || `上传失败 (${response.status})`);
  }
  return payload;
}

async function handleStatementFileSelection(event) {
  const input = event.target.closest("[data-statement-file]");
  const file = input?.files?.[0];
  if (!input || !file) return;
  const broker = input.dataset.statementFile || "";
  state.statementUpload = {broker, busy: true, message: "", error: false};
  renderAccountHoldings();
  try {
    const payload = await uploadStatement(broker, file);
    state.statementUpload = {
      broker,
      busy: false,
      message: `已暂存 ${payload.statement_date} · 等待账户同步`,
      error: false,
    };
    setTimeout(() => {
      if (state.statementUpload.broker === broker && !state.statementUpload.error) {
        state.statementUpload.message = "";
        renderAccountHoldings();
      }
    }, 4000);
  } catch (error) {
    state.statementUpload = {
      broker,
      busy: false,
      message: error instanceof Error ? error.message : String(error),
      error: true,
    };
  } finally {
    input.value = "";
    renderAccountHoldings();
  }
}

const ACCOUNT_HOLDING_COLUMNS = [
  "明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值", "港元市值", "账户权重", "组合权重", "盈亏",
];

function renderSimulationAttribution(position, broker) {
  if (position.attribution_status === "conflict") {
    return '<span class="missing-text">报告关联冲突</span>';
  }
  const report = position.report && typeof position.report === "object" ? position.report : null;
  if (position.attribution_status !== "linked" || !report) {
    return '<span class="meta-text">未关联历史报告</span>';
  }
  return `<button class="report-attribution-link" type="button" data-history-broker="${escapeHtml(broker)}" data-history-artifact="${escapeHtml(report.artifact)}">报告 ${escapeHtml(formatPlain(report.execution_date))} · ${escapeHtml(formatPlain(report.strategy_version))}</button>`;
}

function renderAccountHoldingRow(row, {simulated = false} = {}) {
  const holding = row.holding;
  const display = row.display;
  const enrichment = holding.enrichment_status === "unavailable"
    ? '<span class="meta-text">关联不可用</span>' : "";
  const isSelected = !simulated && row.key === state.selectedHoldingKey;
  const selectedDetail = isSelected ? normalizeHoldingDetailMode(state.selectedHoldingDetail) : "";
  const pnlTone = pnlClass(display.unrealized_pnl_pct);
  const valuationClass = state.accountValuationUpdates.has(row.key)
    ? " account-valuation-updated" : "";
  const detailActions = simulated ? "" : brokerSyncStatus(row.broker).unsafe
    ? '<span class="account-review-action">人工复核</span>'
    : `<button class="${escapeHtml(tSignalButtonClass(holding))}" type="button" data-detail-key="${escapeHtml(row.key)}" data-detail-mode="t_signal">做T</button>`;
  const attribution = simulated ? renderSimulationAttribution(holding, row.broker) : "";
  const controllerFields = [
    ["quantity", display.quantity],
    ["cost-price", display.cost_price],
    ["last-price", display.last_price],
    ["price-kind", display.price_kind],
    ["price-as-of", display.price_as_of],
    ["market-value-usd", display.market_value_usd],
    ["market-value-hkd", display.market_value_hkd],
    ["account-weight-hkd", display.account_weight_hkd],
    ["portfolio-weight-hkd", display.portfolio_weight_hkd],
    ["unrealized-pnl", display.unrealized_pnl],
    ["unrealized-pnl-pct", display.unrealized_pnl_pct],
  ].map(([name, value]) => ` data-${name}="${escapeHtml(value ?? "")}"`).join("");
  const cells = `<tr class="account-holding-row ${isSelected ? "active-row" : ""}" data-broker="${escapeHtml(row.broker)}" data-symbol="${escapeHtml(String(display.symbol || "").toUpperCase())}"${controllerFields}>
    <td class="account-holding-actions"><span class="account-mobile-label">明细</span>${detailActions}</td>
    <td class="account-holding-market"><span class="account-mobile-label">市场</span>${escapeHtml(formatPlain(display.market))}</td>
    <td class="symbol-cell account-holding-symbol"><span class="account-mobile-label">标的</span><strong>${escapeHtml(formatPlain(display.symbol))}</strong><span class="meta-text">${escapeHtml(formatPlain(display.name))}</span>${enrichment}${attribution}</td>
    <td class="number-cell account-holding-quantity"><span class="account-mobile-label">数量</span>${escapeHtml(formatDisplayNumber(display.quantity))}</td>
    <td class="number-cell account-holding-cost"><span class="account-mobile-label">成本价</span>${escapeHtml(formatDisplayNumber(display.cost_price))}</td>
    <td class="number-cell account-holding-price${valuationClass}"><span class="account-mobile-label">实时价</span>${renderAccountHoldingPrice(display)}</td>
    <td class="number-cell account-holding-usd-value${valuationClass}"><span class="account-mobile-label">美元市值</span>${escapeHtml(hasValue(display.market_value_usd) ? formatMoney(display.market_value_usd, "USD") : "-")}</td>
    <td class="number-cell account-holding-market-value${valuationClass}"><span class="account-mobile-label">港元市值</span>${escapeHtml(formatMoney(display.market_value_hkd, "HKD"))}</td>
    <td class="number-cell account-holding-account-weight"><span class="account-mobile-label">账户权重</span>${escapeHtml(formatPlain(display.account_weight_hkd))}</td>
    <td class="number-cell account-holding-portfolio-weight"><span class="account-mobile-label">组合权重</span>${escapeHtml(formatPlain(display.portfolio_weight_hkd))}</td>
    <td class="number-cell account-holding-pnl${pnlTone ? ` ${pnlTone}` : ""}"><span class="account-mobile-label">盈亏</span>${escapeHtml(formatSignedPnl(display.unrealized_pnl_pct))}</td>
  </tr>`;
  if (!isSelected) return cells;
  return `${cells}<tr class="decision-detail-row"><td colspan="${ACCOUNT_HOLDING_COLUMNS.length}"><div class="symbol-detail-panel inline-symbol-detail">${selectedDetail === "t_signal"
    ? renderTSignalDetail(holding)
    : renderSymbolDetail(holding, row.index)}</div></td></tr>`;
}

function renderAccountTable(rows, options = {}) {
  const body = rows.map((row) => renderAccountHoldingRow(row, options)).join("");
  return `<div class="table-wrap account-holdings-table-wrap"><table class="account-holdings-table"><thead><tr>${ACCOUNT_HOLDING_COLUMNS.map((label) => `<th scope="col">${label}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function holdingKey(holding, index) {
  return [
    holding.market || "",
    holding.symbol || "",
    holding.name || "",
    index,
  ].map((part) => String(part)).join(":");
}

function selectedHolding(rows = accountHoldingGroups().flatMap((group) => group.rows)) {
  if (!state.selectedHoldingKey) {
    return null;
  }
  return rows.find((row) => row.key === state.selectedHoldingKey) || null;
}

function showSymbolDetail(detailKey, detailMode = "decision") {
  state.selectedHoldingKey = detailKey;
  state.selectedHoldingDetail = normalizeHoldingDetailMode(detailMode);
  state.selectedDecisionTab = "final";
  syncDecisionDeepLink();
  renderHoldings();
}

function normalizeHoldingDetailMode() {
  return "t_signal";
}

function tSignalButtonClass(holding) {
  const signal = holding && holding.t_signal && typeof holding.t_signal === "object"
    ? holding.t_signal
    : {};
  const active = (
    signal.status === "ok"
    && signal.session_phase === "regular"
    && ["BUY_T", "SELL_T"].includes(signal.action)
  );
  return active
    ? "expand-button t-signal-button t-signal-button-active"
    : "expand-button t-signal-button";
}

function openTradeActionDetail(actionKey) {
  const normalizedActionKey = normalizeActionKey("", actionKey);
  if (!normalizedActionKey) {
    return;
  }
  const rows = accountHoldingGroups().flatMap((group) => group.rows);
  for (const row of rows) {
    if (holdingActionKeys(row.holding).includes(normalizedActionKey)) {
      resetHoldingFilters(row.broker);
      state.selectedHoldingKey = row.key;
      state.selectedHoldingDetail = "decision";
      state.selectedDecisionTab = "final";
      syncDecisionDeepLink();
      renderDashboardViews();
      return;
    }
  }
}

function resetHoldingFilters(broker = state.brokerFilter) {
  state.marketFilter = "ALL";
  state.brokerFilter = ACCOUNT_BROKERS.includes(broker) ? broker : ACCOUNT_BROKERS[0];
  setFilterActiveByDataset(elements["header-market-filters"], "market", "ALL");
}

function setFilterActiveByDataset(container, datasetKey, value) {
  if (!container) {
    return;
  }
  container.querySelectorAll(".filter-button").forEach((button) => {
    button.classList.toggle("active", button.dataset[datasetKey] === value);
  });
}

function renderSymbolDetail(holding, index) {
  const title = `${formatPlain(holding.market)}.${formatPlain(holding.symbol)}`;
  return `
    <div class="detail-header trading-decision-header">
      <div>
        <button class="raw-toggle" type="button" data-back-to-holdings>返回持仓列表</button>
        <h2>交易决策 · ${escapeHtml(title)}</h2>
        <p>${escapeHtml(formatPlain(holding.name))} · 基于已接入的交易决策与市场事实数据展示。</p>
      </div>
      <button class="raw-toggle" type="button" data-back-to-holdings>收起</button>
    </div>
    <div class="trading-decision-layout">
      ${renderTradingDecisionTabs(holding)}
    </div>
  `;
}

function renderTSignalDetail(holding) {
  const title = `${formatPlain(holding.market)}.${formatPlain(holding.symbol)}`;
  const signal = holding && holding.t_signal && typeof holding.t_signal === "object"
    ? holding.t_signal
    : null;
  if (!signal || signal.available === false) {
    const message = signal && signal.error ? signal.error : "暂无做T信号数据。";
    return `
      <div class="detail-header trading-decision-header">
        <div>
          <button class="raw-toggle" type="button" data-back-to-holdings>返回持仓列表</button>
          <h2>做T信号 · ${escapeHtml(title)}</h2>
          <p>${escapeHtml(message)}</p>
        </div>
        <button class="raw-toggle" type="button" data-back-to-holdings>收起</button>
      </div>
      <section class="detail-section t-signal-section">
        <h3>当前状态</h3>
        <p class="muted-copy">该标的尚未生成做T信号，或本市场 latest 信号文件不存在。</p>
      </section>
    `;
  }

  return `
    <div class="detail-header trading-decision-header">
      <div>
        <button class="raw-toggle" type="button" data-back-to-holdings>返回持仓列表</button>
        <h2>做T信号 · ${escapeHtml(title)}</h2>
        <p>${escapeHtml(formatPlain(signal.signal_summary_zh || signal.current_status))}</p>
      </div>
      <button class="raw-toggle" type="button" data-back-to-holdings>收起</button>
    </div>
    <div class="t-signal-layout">
      <section class="detail-section t-signal-section">
        <div class="t-signal-status-row">
          <div>
            <h3>${escapeHtml(tSignalActionLabel(signal.action))}</h3>
            <p>${escapeHtml(formatPlain(signal.current_status))}</p>
          </div>
          <span class="status-pill ${escapeHtml(tSignalStatusClass(signal.status))}">${escapeHtml(tSignalStatusLabel(signal.status))}</span>
        </div>
        <div class="t-signal-metric-grid">
          ${renderTSignalMetric("确定比例", tSignalRatioText(signal.suggested_ratio))}
          ${renderTSignalMetric("更新时间", signal.updated_at)}
          ${renderTSignalMetric("交易时段", tSignalSessionLabel(signal.session_phase))}
          ${renderTSignalMetric("提醒状态", tSignalNotificationText(signal.notification))}
        </div>
        ${signal.error ? `<p class="t-signal-error">${escapeHtml(signal.error)}</p>` : ""}
      </section>
      ${renderTSignalEvidence(signal)}
      ${renderTSignalPrerequisites(signal)}
      ${renderTSignalDetails(signal)}
      ${renderTSignalTimeline(signal)}
    </div>
  `;
}

function renderTSignalMetric(label, value) {
  return `
    <div class="t-signal-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatPlain(value))}</strong>
    </div>
  `;
}

function renderTSignalEvidence(signal) {
  const evidence = Array.isArray(signal.evidence) ? signal.evidence : [];
  return `
    <section class="detail-section t-signal-section">
      <h3>信号依据</h3>
      <div class="t-signal-evidence-list">
        ${evidence.length > 0 ? evidence.map((item) => `
          <div class="t-signal-evidence-item">
            <strong>${escapeHtml(formatPlain(item.message_zh))}</strong>
            <span>${escapeHtml(tSignalDirectionLabel(item.direction))} · ${escapeHtml(tSignalStrengthLabel(item.strength))}</span>
          </div>
        `).join("") : `<p class="muted-copy">暂无明确买卖依据。</p>`}
      </div>
    </section>
  `;
}

function renderTSignalPrerequisites(signal) {
  const gates = Array.isArray(signal.hard_gates) ? signal.hard_gates : [];
  return `
    <section class="detail-section t-signal-section">
      <h3>前置条件</h3>
      <div class="t-signal-gate-grid">
        ${gates.length > 0 ? gates.map((gate) => `
          <div class="t-signal-gate">
            <span>${escapeHtml(tSignalGateNameLabel(gate.name))}</span>
            ${renderTSignalGateStatus(gate.status)}
            <small>${escapeHtml(formatPlain(gate.message_zh))}</small>
          </div>
        `).join("") : `<p class="muted-copy">暂无前置条件记录。</p>`}
      </div>
    </section>
  `;
}

function renderTSignalDetails(signal) {
  return `
    <section class="detail-section t-signal-section">
      <h3>详细信息</h3>
      <div class="t-signal-detail-grid">
        <div>
          <h4>价格</h4>
          ${renderDecisionFactRows([
            { label: "最新价", value: tSignalPriceText(nestedValue(signal.price, "last_price")) },
            { label: "日内涨跌", value: percentText(nestedValue(signal.price, "day_change_pct")) },
            { label: "VWAP", value: tSignalPriceText(nestedValue(signal.price, "vwap")) },
            { label: "日内区间", value: tSignalPriceRangeText(nestedValue(signal.price, "day_low"), nestedValue(signal.price, "day_high")) },
          ])}
        </div>
        <div>
          <h4>技术 / 盘口</h4>
          ${renderDecisionFactRows([
            { label: "5分钟 RSI", value: nestedValue(signal.technical, "rsi_5m") },
            { label: "5分钟量比", value: nestedValue(signal.technical, "volume_ratio_5m") },
            { label: "价格位置", value: tSignalPricePositionLabel(nestedValue(signal.technical, "price_position")) },
            { label: "盘口状态", value: tSignalDepthStatusLabel(nestedValue(signal.liquidity, "depth_status")) },
          ])}
        </div>
      </div>
    </section>
  `;
}

function renderTSignalTimeline(signal) {
  const timeline = Array.isArray(signal.timeline) ? signal.timeline : [];
  return `
    <section class="detail-section t-signal-section">
      <h3>消息 timeline</h3>
      <div class="t-signal-timeline">
        ${timeline.length > 0 ? timeline.map((event) => `
          <div class="t-signal-timeline-event">
            <time>${escapeHtml(formatPlain(event.event_at))}</time>
            <strong>${escapeHtml(tSignalTimelineLabel(event.event_type))}</strong>
            <span>${escapeHtml(formatPlain(event.message_zh))}</span>
          </div>
        `).join("") : `<p class="muted-copy">暂无消息记录。</p>`}
      </div>
    </section>
  `;
}

function nestedValue(source, key) {
  return source && typeof source === "object" ? source[key] : "";
}

function percentText(value) {
  if (!hasValue(value)) return "-";
  const raw = String(value).trim();
  return raw.endsWith("%") ? raw : `${raw}%`;
}

function tSignalPriceText(value) {
  return hasValue(value) ? formatDisplayNumber(value) : "-";
}

function tSignalPriceRangeText(low, high) {
  return hasValue(low) || hasValue(high)
    ? `${tSignalPriceText(low)} / ${tSignalPriceText(high)}`
    : "-";
}

function tSignalRatioText(value) {
  return hasValue(value) ? `${value}%` : "-";
}

function tSignalActionLabel(action) {
  const labels = {
    BUY_T: "买入做T",
    SELL_T: "卖出做T",
    HOLD: "观察",
    REVIEW: "人工复核",
  };
  return labels[action] || formatPlain(action);
}

function tSignalStatusLabel(status) {
  const labels = {
    ok: "有效",
    review: "需复核",
    blocked: "已阻断",
    error: "错误",
    stale: "已过期",
  };
  return labels[status] || "未知";
}

function tSignalStatusClass(status) {
  if (status === "ok") {
    return "status-ok";
  }
  if (status === "review" || status === "blocked" || status === "stale") {
    return "status-partial";
  }
  if (status === "error") {
    return "status-failed";
  }
  return "status-muted";
}

function tSignalSessionLabel(value) {
  const labels = {
    pre_market: "盘前",
    regular: "盘中",
    post_market: "盘后",
    closed: "休市",
    unknown: "未知",
  };
  return labels[value] || formatPlain(value);
}

function tSignalNotificationText(notification) {
  if (!notification || typeof notification !== "object") {
    return "-";
  }
  if (notification.notified === true) {
    return hasValue(notification.last_notified_at)
      ? `已发起提醒 · ${notification.last_notified_at}`
      : "已发起提醒";
  }
  if (hasValue(notification.last_attempted_dedupe_key)) {
    return "已尝试发起提醒";
  }
  if (notification.should_notify === true) {
    return "待提醒";
  }
  return "不提醒";
}

function tSignalDirectionLabel(value) {
  const labels = { buy: "买入依据", sell: "卖出依据", neutral: "中性", risk: "风险" };
  return labels[value] || formatPlain(value);
}

function tSignalStrengthLabel(value) {
  const labels = { low: "弱", medium: "中", high: "强" };
  return labels[value] || formatPlain(value);
}

function tSignalGateStatusLabel(value) {
  const labels = { pass: "通过", block: "阻断", warn: "提醒", missing: "缺失" };
  return labels[value] || formatPlain(value);
}

function renderTSignalGateStatus(status) {
  const normalized = ["pass", "block", "warn", "missing"].includes(status) ? status : "missing";
  return `
    <strong class="t-signal-gate-status">
      <span class="t-signal-checkmark t-signal-checkmark-${escapeHtml(normalized)}" aria-hidden="true"></span>
      <span>${escapeHtml(tSignalGateStatusLabel(normalized))}</span>
    </strong>
  `;
}

function tSignalGateNameLabel(value) {
  const labels = {
    session_phase: "交易时段",
    baseline: "底仓数量",
    technical: "技术完整性",
    liquidity: "流动性",
    symbol: "标的匹配",
  };
  return labels[value] || formatPlain(value);
}

function tSignalPricePositionLabel(value) {
  const labels = {
    near_support: "接近支撑",
    near_resistance: "接近压力",
    below_vwap_reclaim: "低于 VWAP 后回收",
    above_vwap_reject: "高于 VWAP 后受压",
    middle_range: "区间中部",
    breakout: "突破",
    breakdown: "跌破",
    unknown: "未知",
  };
  return labels[value] || formatPlain(value);
}

function tSignalDepthStatusLabel(value) {
  const labels = { pass: "正常", thin: "深度不足", wide_spread: "价差偏大", missing: "缺失" };
  return labels[value] || formatPlain(value);
}

function tSignalTimelineLabel(value) {
  const labels = {
    signal_created: "生成信号",
    signal_changed: "信号变化",
    notification_sent: "已发送提醒",
    notification_suppressed: "已抑制重复提醒",
    notification_failed: "提醒失败",
    signal_expired: "信号过期",
    review_required: "需要复核",
  };
  return labels[value] || formatPlain(value);
}

function renderDecisionPlan(holding) {
  const plan = holding && holding.decision_plan && typeof holding.decision_plan === "object"
    ? holding.decision_plan
    : {};
  if (plan.available !== true) {
    return `<div class="decision-plan-failed status-failed">${escapeHtml(plan.error || "交易计划未生成")}</div>`;
  }
  if (plan.mode === "validated_plan") {
    return renderValidatedDecisionPlan(plan);
  }
  if (plan.mode === "fallback_advice") {
    return renderFallbackDecisionPlan(plan);
  }
  return `<div class="decision-plan-failed status-failed">交易计划类型无效</div>`;
}

function renderValidatedDecisionPlan(plan) {
  const conditions = Array.isArray(plan.conditions) ? plan.conditions : [];
  const next = conditions.find((condition) => condition.condition_id === plan.next_condition_id) || conditions[0];
  const strategy = plan.strategy && typeof plan.strategy === "object" ? plan.strategy : {};
  return `
    <section class="decision-plan decision-plan-validated">
      ${renderDecisionPlanHeader(plan, "已通过回测闸门", decisionPlanStatusLabel(plan.status))}
      <div class="decision-plan-overview">
        <article><span>当前结论</span><strong>${escapeHtml(plan.action_summary || "等待条件触发")}</strong></article>
        <article><span>下一条件</span><strong>${escapeHtml(next ? decisionConditionSummary(next) : "暂无")}</strong></article>
        <article><span>策略</span><strong>${escapeHtml(strategy.name_zh || strategy.id || "-")}</strong></article>
        <article><span>仓位上限</span><strong>${escapeHtml(decisionPlanWeight(plan.max_weight))}</strong></article>
      </div>
      <div class="decision-plan-layout">
        <div>
          <div class="decision-plan-section-heading"><h4>条件与动作</h4><span>风险条件优先 · 可重复触发</span></div>
          <div class="decision-plan-condition-list">
            ${conditions.length ? conditions.map(renderDecisionPlanCondition).join("") : '<p class="decision-plan-empty">当前没有可执行条件。</p>'}
          </div>
        </div>
        <aside class="decision-plan-evidence">
          <h4>回测闸门</h4>
          ${renderDecisionPlanBacktests(plan.backtests)}
        </aside>
      </div>
      ${renderPreviousDecisionReview(plan.previous_review)}
    </section>
  `;
}

function renderDecisionPlanHeader(plan, eyebrow, status) {
  return `
    <header class="decision-plan-header">
      <div><span>${escapeHtml(eyebrow)}</span><h3>今日交易计划</h3><p>${escapeHtml(plan.run_date || "-")} · ${escapeHtml(plan.plan_id || "")}</p></div>
      <strong class="decision-plan-status decision-plan-status-${escapeHtml(plan.status || "waiting")}">${escapeHtml(status)}</strong>
    </header>
  `;
}

function renderDecisionPlanCondition(condition, index) {
  const tone = condition.priority === "risk" ? "risk" : "ordinary";
  return `
    <article class="decision-plan-condition decision-plan-condition-${tone}" data-plan-condition="${escapeHtml(condition.condition_id || String(index))}">
      <div class="decision-plan-condition-head">
        <span>${condition.priority === "risk" ? "风险优先" : "普通条件"}</span>
        <b>已触发 ${escapeHtml(formatDisplayNumber(condition.trigger_count || 0))} 次</b>
      </div>
      <h5>${escapeHtml(decisionConditionSummary(condition))}</h5>
      <div class="decision-plan-condition-metrics">
        <div><span>执行动作</span><strong>${escapeHtml(condition.suggested_action || "-")}</strong></div>
        <div><span>目标仓位</span><strong>${escapeHtml(decisionPlanWeight(condition.target_weight))}</strong></div>
        <div><span>目标数量</span><strong>${escapeHtml(formatDisplayNumber(condition.target_quantity))}</strong></div>
      </div>
      ${renderDecisionPlanProvenance(condition)}
    </article>
  `;
}

function renderDecisionPlanProvenance(item) {
  const inputs = item.inputs && typeof item.inputs === "object"
    ? Object.entries(item.inputs).map(([key, value]) => `${key}=${formatPlain(value)}`).join(" · ")
    : "-";
  return `
    <details class="decision-plan-provenance">
      <summary>参数来源</summary>
      <dl>
        <div><dt>公式</dt><dd>${escapeHtml(item.formula || "-")}</dd></div>
        <div><dt>输入</dt><dd>${escapeHtml(inputs)}</dd></div>
        <div><dt>数据日期</dt><dd>${escapeHtml(item.source_date || "-")}</dd></div>
      </dl>
    </details>
  `;
}

function renderDecisionPlanBacktests(backtests) {
  const rows = Array.isArray(backtests) ? backtests : [];
  if (!rows.length) {
    return '<p class="decision-plan-empty">没有回测证据。</p>';
  }
  return `
    <div class="decision-plan-backtests">
      ${rows.map((item) => {
        const strategy = item.strategy || {};
        const benchmark = item.market_benchmark || {};
        const heading = [item.range, item.strategy_id].filter(hasValue).join(" · ") || "-";
        const returns = [
          ["策略收益", strategy.total_return_pct, "pnl"],
          [benchmark.symbol || "基准", benchmark.total_return_pct, "pnl"],
          ["超额收益", item.market_excess_return_pct, "pnl"],
          ["最大回撤", strategy.max_drawdown_pct, "drawdown"],
        ];
        return `
          <article>
            <div><strong>${escapeHtml(heading)}</strong><span class="status-pill status-${item.gate && item.gate.passed === true ? "ok" : "failed"}">${item.gate && item.gate.passed === true ? "通过" : "未通过"}</span></div>
            <dl>
              ${returns.map(([label, value, kind]) => {
                const display = kind === "drawdown"
                  ? drawdownPercent(value)
                  : formatSignedPnl(decisionPlanPercent(value));
                const tone = pnlClass(display);
                return `<div><dt>${escapeHtml(label)}</dt><dd${tone ? ` class="${tone}"` : ""}>${escapeHtml(display)}</dd></div>`;
              }).join("")}
              <div><dt>夏普比率</dt><dd>${escapeHtml(strategy.sharpe_ratio || "-")}</dd></div>
              <div><dt>卡玛比率</dt><dd>${escapeHtml(decisionPlanRatio(strategy.calmar_ratio))}</dd></div>
            </dl>
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function renderFallbackDecisionPlan(plan) {
  const fallback = plan.fallback && typeof plan.fallback === "object" ? plan.fallback : {};
  const facts = Array.isArray(fallback.facts) ? fallback.facts : [];
  const tradingagents = fallback.tradingagents && typeof fallback.tradingagents === "object"
    ? fallback.tradingagents : {};
  return `
    <section class="decision-plan decision-plan-fallback">
      ${renderDecisionPlanHeader(plan, fallback.label || "非执行型建议", "禁止自动执行")}
      <div class="decision-plan-fallback-banner">
        <div><span>建议</span><strong>${escapeHtml(fallback.recommendation || "禁止加仓")}</strong></div>
        <div><span>仓位上限</span><strong>${escapeHtml(decisionPlanWeight(fallback.max_weight || plan.max_weight))}</strong></div>
      </div>
      <div class="decision-plan-section-heading"><h4>市场事实</h4><span>仅供判断，不构成可触发策略</span></div>
      <div class="decision-plan-fact-grid">${facts.map(renderDecisionPlanFact).join("")}</div>
      <div class="decision-plan-fallback-reason">
        <article><h4>TradingAgents 解读</h4><p>${escapeHtml(tradingagents.core_reason || tradingagents.current_action || "暂无")}</p></article>
        <article><h4>为什么没有可执行计划</h4><p>${escapeHtml(fallback.reason || "没有策略通过当前回测闸门")}</p></article>
      </div>
      <div class="decision-plan-section-heading"><h4>回测闸门</h4><span>候选策略均未通过，仅展示证据</span></div>
      ${renderDecisionPlanBacktests(plan.backtests)}
      ${renderPreviousDecisionReview(plan.previous_review)}
    </section>
  `;
}

function renderDecisionPlanFact(fact) {
  const labels = {
    ma20_distance_pct: "距 MA20",
    rsi14: "RSI 14",
    bollinger_position: "布林带位置",
    relative_volume: "相对成交量",
  };
  return `
    <article class="decision-plan-fact">
      <span>${escapeHtml(labels[fact.key] || fact.key || "事实")}</span>
      <strong>${escapeHtml(formatDisplayNumber(fact.calculated_value))}</strong>
      ${renderDecisionPlanProvenance(fact)}
    </article>
  `;
}

function renderPreviousDecisionReview(review) {
  if (!review || typeof review !== "object") {
    return "";
  }
  return `
    <details class="decision-plan-review">
      <summary>上期复盘 · ${escapeHtml(review.run_date || "-")}</summary>
      <div>
        <span>上期状态 <strong>${escapeHtml(decisionPlanStatusLabel(review.status))}</strong></span>
        <span>条件触发 <strong>${escapeHtml(formatDisplayNumber(review.trigger_count || 0))} 次</strong></span>
        <span>期初数量 <strong>${escapeHtml(formatDisplayNumber(review.starting_quantity))}</strong></span>
        <span>本期期初数量 <strong>${escapeHtml(formatDisplayNumber(review.closing_quantity))}</strong></span>
      </div>
    </details>
  `;
}

function decisionConditionSummary(condition) {
  return `价格 ${condition.operator || ""} ${formatDisplayNumber(condition.calculated_value)}`.trim();
}

function decisionPlanWeight(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(2).replace(/\.00$/, "")}%` : "-";
}

function decisionPlanPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(2).replace(/\.00$/, "")}%` : "-";
}

function decisionPlanRatio(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "-";
}

function decisionPlanStatusLabel(status) {
  return ({waiting: "等待条件", triggered: "条件已触发", expired: "计划已过期"})[status] || "状态未知";
}

function decisionTabViews(holding) {
  const facts = holding && holding.decision_facts && typeof holding.decision_facts === "object"
    ? holding.decision_facts : {};
  const futuFacts = holding && holding.futu_skill_facts && typeof holding.futu_skill_facts === "object"
    ? holding.futu_skill_facts : {};
  const summary = holding && holding.tradingagents_summary && typeof holding.tradingagents_summary === "object"
    ? holding.tradingagents_summary : {};
  const technicalFacts = holding && typeof holding.technical_facts === "object"
    ? holding.technical_facts : null;
  const futuModules = ["technical_anomaly", "capital_anomaly", "derivatives_anomaly"]
    .map((key) => futuFacts[key]);
  const futuNews = futuSkillNewsSentimentModule(holding);
  const definitions = {
    final: {
      available: Boolean(holding && holding.decision_plan && holding.decision_plan.available === true),
      error: holding && holding.decision_plan && holding.decision_plan.error,
      html: renderDecisionPlan(holding),
    },
    tradingagents: {
      available: summary.available === true,
      error: summary.error,
      html: renderTradingAgentsSummaryCard(holding),
    },
    kline: {
      available: Boolean(facts.kline && facts.kline.available === true) || technicalFactsUsable(technicalFacts),
      error: (facts.kline && facts.kline.error) || (technicalFacts && technicalFacts.error),
      html: renderDecisionPluginCard(klineDecisionFactsPlugin(holding)),
    },
    news: {
      available: Boolean(facts.news_sentiment && facts.news_sentiment.available === true)
        || Boolean(futuNews && futuNews.available === true),
      error: (facts.news_sentiment && facts.news_sentiment.error) || (futuNews && futuNews.error),
      html: renderDecisionPluginCard(newsSentimentPlugin(holding)),
    },
    futu: {
      available: futuModules.some((module) => module && module.available === true),
      error: futuModules.map((module) => module && module.error).find(hasValue),
      html: futuAnomalySignalsPlugin(holding),
    },
  };
  return DECISION_TABS.map((tab) => ({ ...tab, ...definitions[tab.key] }));
}

function renderTradingDecisionTabs(holding) {
  const views = decisionTabViews(holding);
  const selected = views.find((view) => view.key === state.selectedDecisionTab) || views[0];
  const panel = selected.available
    ? selected.html
    : `<div class="decision-tab-empty status-failed">${escapeHtml(selected.error || "数据未生成")}</div>`;
  return `
    <section class="detail-section trading-decision-section">
      <div class="trading-decision-section-header"><div><h3>交易决策</h3><p>结论先行，按证据模块逐项复核。</p></div></div>
      <div class="decision-tab-list" role="tablist" aria-label="交易决策模块">
        ${views.map((view) => `<button id="decision-tab-${view.key}" class="decision-tab${view.key === selected.key ? " active" : ""}${view.available ? "" : " decision-tab-failed"}" type="button" role="tab" aria-selected="${view.key === selected.key}" aria-controls="decision-panel-${view.key}" data-decision-tab="${view.key}">${escapeHtml(view.label)}</button>`).join("")}
      </div>
      <div id="decision-panel-${selected.key}" class="decision-tab-panel" role="tabpanel" aria-labelledby="decision-tab-${selected.key}">${panel}</div>
    </section>
  `;
}

function renderTradingAgentsSummaryCard(holding) {
  const summary = holding && holding.tradingagents_summary && typeof holding.tradingagents_summary === "object"
    ? holding.tradingagents_summary
    : {};
  const rows = [
    ["ta_view", "TA 观点"],
    ["current_action", "当前动作"],
    ["core_reason", "核心理由"],
    ["ta_report_date", "TA 报告日期"],
    ["latest_run_date", "当前 latest"],
  ].map(([key, label]) => ({
    label,
    value: formatTradingAgentsSummaryValue(summary[key]),
  }));
  return `
    <article class="decision-plugin-card">
      <div class="decision-plugin-card-header">
        <h4>TradingAgents</h4>
      </div>
      ${renderDecisionFactRows(rows)}
    </article>
  `;
}

function formatTradingAgentsSummaryValue(value) {
  return hasValue(value) ? formatPlain(value) : "缺失";
}

function decisionFactsPlugin(holding, config) {
  const module = decisionFactsModule(holding, config.moduleKey);
  const fields = module && module.fields && typeof module.fields === "object"
    ? module.fields
    : {};
  const rows = config.fieldOrder.map(([key, label]) => ({
    label,
    value: hasValue(fields[key]) ? formatPlain(fields[key]) : "缺失",
  }));
  const missingLabels = missingDecisionFactFieldLabels(fields, config.fieldOrder);
  const available = Boolean(module && module.available === true);
  const complete = available && missingLabels.length === 0;
  return {
    title: config.title,
    status: complete ? "可用" : (available ? "不完整" : "缺失"),
    tone: complete ? "ok" : "partial",
    score: config.score,
    headline: rows[0] ? rows[0].value : "缺失",
    detail: "",
    bodyHtml: renderDecisionFactRows(rows),
    condition: "",
  };
}

function klineDecisionFactsPlugin(holding) {
  const module = decisionFactsModule(holding, "kline");
  const fieldOrder = [
    ["trend", "趋势"],
    ["position", "位置"],
    ["momentum", "动能"],
    ["key_levels", "关键位"],
    ["risk", "风险"],
  ];
  const plugin = decisionFactsPlugin(holding, {
    title: "趋势 / K 线",
    moduleKey: "kline",
    fieldOrder,
    score: "K线",
  });
  const detail = holding && typeof holding.technical_facts === "object"
    ? holding.technical_facts
    : null;
  const timeframes = technicalFactsUsable(detail)
    ? detail.facts.timeframes
    : [];
  const hasFixedFields = module
    && module.fields
    && typeof module.fields === "object"
    && fieldOrder.some(([key]) => Object.prototype.hasOwnProperty.call(module.fields, key));
  if (detail && plugin.status === "缺失" && !hasFixedFields) {
    return klineTechnicalFactsPlugin(holding);
  }
  return {
    ...plugin,
    bodyHtml: `${timeframes.length ? renderBollingerSection(timeframes, holding.last_price) : ""}${plugin.bodyHtml}`,
  };
}

function newsSentimentPlugin(holding) {
  const plugin = decisionFactsPlugin(holding, {
    title: "新闻 / 舆论",
    moduleKey: "news_sentiment",
    fieldOrder: [
      ["direction", "方向"],
      ["change", "变化"],
      ["catalyst", "催化"],
      ["risk", "风险"],
      ["attention", "热度"],
    ],
    score: "舆论",
  });
  const domesticHtml = futuSkillNewsSentimentPlugin(holding);
  return {
    ...plugin,
    bodyHtml: plugin.bodyHtml + domesticHtml,
  };
}

function futuSkillNewsSentimentPlugin(holding) {
  const module = futuSkillNewsSentimentModule(holding);
  if (!module || module.available !== true) {
    return "";
  }
  const discussion = module.domestic_discussion && typeof module.domestic_discussion === "object"
    ? module.domestic_discussion
    : {};
  const rows = [
    {
      label: "讨论关键词",
      htmlValue: renderDomesticKeywordTags(discussion.keyword_counts),
    },
    { label: "国内讨论结论", value: formatPlain(discussion.summary) },
    { label: "主要关注点", value: formatPlain(discussion.focus) },
    { label: "分歧 / 风险", value: formatPlain(discussion.divergence_risk) },
    { label: "可信度", value: formatPlain(discussion.credibility), tone: "warn" },
    { label: "交易约束", value: formatPlain(discussion.trading_constraint), tone: "warn" },
  ];
  return `
    <div class="decision-fact-source-block">
      <div class="domestic-section-header">
        <b>富途社区 / 国内讨论</b>
        <span>LLM 总结 · stock_feed</span>
      </div>
      ${renderDomesticDiscussionRows(rows)}
    </div>
  `;
}

function futuSkillNewsSentimentModule(holding) {
  const facts = holding && holding.futu_skill_facts && typeof holding.futu_skill_facts === "object"
    ? holding.futu_skill_facts
    : {};
  const module = facts.news_sentiment;
  return module && typeof module === "object" ? module : null;
}

function futuAnomalySignalsPlugin(holding) {
  const facts = holding && holding.futu_skill_facts && typeof holding.futu_skill_facts === "object"
    ? holding.futu_skill_facts
    : {};
  const modules = [
    ["technical_anomaly", "技术异动"],
    ["capital_anomaly", "资金异动"],
    ["derivatives_anomaly", "衍生品异动"],
  ].map(([key, title]) => futuSignalModuleView(facts[key], key, title));
  const available = modules.filter((module) => module.available).length;
  const overall = deriveFutuSignalOverall(modules);
  return `
    <article class="decision-plugin-card futu-signal-card">
      <div class="decision-plugin-card-header">
        <h4>市场信号 · 富途异动信号</h4>
        <span class="status-pill status-${escapeHtml(overall.tone)}">${escapeHtml(available)}/3 模块可用</span>
      </div>
      <div class="futu-signal-overall">
        <strong>${escapeHtml(overall.label)}</strong>
        <div>
          <b>${escapeHtml(overall.headline)}</b>
          <span>${escapeHtml(overall.detail)}</span>
        </div>
        <div class="futu-signal-pill-row">
          <span>${escapeHtml(translateFutuSignalValue(overall.signal))}</span>
          <span>${escapeHtml(translateFutuSignalValue(overall.constraint))}</span>
        </div>
      </div>
      <div class="futu-signal-module-grid">
        ${modules.map(renderFutuSignalModule).join("")}
      </div>
      <p class="condition-box">模板约束：模块标题、状态、方向、置信度、约束、类别顺序固定；缺失、无异常和权限失败必须显式展示。</p>
    </article>
  `;
}

function futuSignalModuleView(module, key, title) {
  const value = module && typeof module === "object" ? module : {};
  const status = hasValue(value.status) ? String(value.status) : "missing";
  const signal = value.available === true && !["missing", "error", "stale", "stale_run_date"].includes(status) && hasValue(value.signal)
    ? String(value.signal)
    : status;
  return {
    key,
    title,
    available: value.available === true,
    status,
    signal,
    confidence: hasValue(value.confidence) ? String(value.confidence) : "low",
    suggestedConstraint: hasValue(value.suggested_constraint) ? String(value.suggested_constraint) : "",
    summary: hasValue(value.summary) ? String(value.summary) : "缺失",
    categories: Array.isArray(value.categories) ? value.categories.slice(0, 3) : [],
  };
}

function deriveFutuSignalOverall(modules) {
  const constraints = modules.map((module) => module.suggestedConstraint).filter(hasValue);
  const signals = modules.map((module) => module.signal).filter(hasValue);
  const constraint = constraints.includes("no_add")
    ? "no_add"
    : constraints.includes("review")
      ? "review"
      : "";
  if (signals.includes("error") || signals.includes("missing") || signals.includes("stale") || signals.includes("stale_run_date")) {
    return {
      tone: "warn",
      label: "需复核",
      signal: signals.includes("error") ? "error" : (signals.includes("stale_run_date") ? "stale_run_date" : (signals.includes("stale") ? "stale" : "missing")),
      constraint: constraint || "review",
      headline: "市场信号数据不可用，不能视为中性。",
      detail: "缺失、错误或过期模块会保留数据质量状态，不会自动改写成交易方向。",
    };
  }
  if (signals.includes("risk_up") || signals.includes("mixed")) {
    return {
      tone: constraint ? "warn" : "ok",
      label: constraint ? "谨慎" : "分歧",
      signal: signals.includes("risk_up") ? "risk_up" : "mixed",
      constraint,
      headline: "市场信号存在分歧，需要结合主结论复核。",
      detail: "统一结论只来自三个模块的结构化字段；不会展示自由发挥的长段落。",
    };
  }
  if (signals.includes("opposing")) {
    return {
      tone: "warn",
      label: "反对",
      signal: "opposing",
      constraint,
      headline: "市场信号反对当前交易方向。",
      detail: "统一结论只来自三个模块的结构化字段；不会展示自由发挥的长段落。",
    };
  }
  if (signals.includes("supportive")) {
    return {
      tone: "ok",
      label: "支持",
      signal: "supportive",
      constraint,
      headline: "市场信号支持当前交易方向。",
      detail: "统一结论只来自三个模块的结构化字段；不会展示自由发挥的长段落。",
    };
  }
  return {
    tone: "muted",
    label: "中性",
    signal: "neutral",
    constraint,
    headline: "窗口内未发现明显异动。",
    detail: "缺失、无异常和权限失败会在模块内显式展示。",
  };
}

function renderFutuSignalModule(module) {
  return `
    <section class="futu-signal-module">
      <div class="futu-signal-module-header">
        <h5>${escapeHtml(module.title)}</h5>
        <span class="status-pill status-${escapeHtml(futuSignalStatusTone(module.status))}">${escapeHtml(translateFutuSignalValue(module.status))}</span>
      </div>
      <div class="futu-signal-metrics">
        <div><span>方向</span><strong>${escapeHtml(translateFutuSignalValue(module.signal))}</strong></div>
        <div><span>${module.suggestedConstraint ? "约束" : "置信度"}</span><strong>${escapeHtml(translateFutuSignalValue(module.suggestedConstraint || module.confidence))}</strong></div>
      </div>
      <div class="futu-signal-category-list">
        ${renderFutuSignalCategories(module.categories)}
      </div>
    </section>
  `;
}

function renderFutuSignalCategories(categories) {
  if (!categories.length) {
    return `
      <div class="futu-signal-category empty">
        <div><strong>缺失</strong><span>缺失</span></div>
        <p>未找到可展示的结构化类别。</p>
      </div>
    `;
  }
  return categories.map((category) => {
    const state = hasValue(category.state) ? String(category.state) : "none";
    const direction = hasValue(category.direction) ? String(category.direction) : "";
    const date = hasValue(category.evidence_date) ? ` · ${category.evidence_date}` : "";
    return `
      <div class="futu-signal-category ${escapeHtml(futuSignalCategoryTone(state, direction))}">
        <div>
          <strong>${escapeHtml(category.name || "缺失")}</strong>
          <span>${escapeHtml(translateFutuSignalValue(direction || state) + date)}</span>
        </div>
        <p>${escapeHtml(category.detail || "缺失")}</p>
      </div>
    `;
  }).join("");
}

function translateFutuSignalValue(value) {
  const key = hasValue(value) ? String(value) : "";
  const labels = {
    supportive: "支持",
    opposing: "反对",
    neutral: "中性",
    risk_up: "风险上升",
    mixed: "分歧",
    no_add: "不加仓",
    review: "需复核",
    reduce_only: "只减不加",
    wait_for_event: "等待事件",
    ok: "正常",
    partial: "部分可用",
    missing: "缺失",
    error: "错误",
    stale: "已过期",
    stale_run_date: "已过期",
    anomaly: "异常",
    none: "无异常",
    not_applicable: "不适用",
    bullish: "偏多",
    bearish: "偏空",
    high: "高",
    medium: "中等",
    low: "低",
    "": "-",
  };
  return Object.prototype.hasOwnProperty.call(labels, key) ? labels[key] : "未知";
}

function futuSignalStatusTone(status) {
  if (status === "ok") return "ok";
  if (status === "partial") return "warn";
  if (status === "stale" || status === "stale_run_date") return "stale";
  if (status === "error") return "failed";
  return "muted";
}

function futuSignalCategoryTone(state, direction) {
  if (state === "error") return "failed";
  if (state === "none" || state === "not_applicable") return "empty";
  if (direction === "bearish" || direction === "risk_up") return "watch";
  if (direction === "bullish") return "positive";
  return "mixed";
}

function renderDomesticDiscussionRows(rows) {
  return `
    <div class="domestic-list">
      ${rows.map((row) => `
        <div class="domestic-row ${row.tone === "warn" ? "warn" : ""}">
          <span>${escapeHtml(row.label)}</span>
          ${row.htmlValue || `<strong>${escapeHtml(row.value)}</strong>`}
        </div>
      `).join("")}
    </div>
  `;
}

function renderDomesticKeywordTags(keywordCounts) {
  const items = Array.isArray(keywordCounts)
    ? keywordCounts
        .filter((item) => item && hasValue(item.keyword) && Number.isInteger(item.count) && item.count > 0)
        .slice(0, 3)
    : [];
  if (!items.length) {
    return `<strong>缺失</strong>`;
  }
  return `
    <div class="domestic-keyword-list">
      ${items.map((item) => `
        <b class="domestic-keyword">
          <span>${escapeHtml(formatPlain(item.keyword))}</span>
          <em>${escapeHtml(formatDisplayNumber(item.count))}</em>
        </b>
      `).join("")}
    </div>
  `;
}

function decisionFactsModule(holding, moduleKey) {
  const detail = holding && holding.decision_facts && typeof holding.decision_facts === "object"
    ? holding.decision_facts
    : {};
  const module = detail[moduleKey];
  return module && typeof module === "object" ? module : null;
}

function missingDecisionFactFieldLabels(fields, fieldOrder) {
  return fieldOrder
    .filter(([key]) => !hasValue(fields[key]) || formatPlain(fields[key]) === "缺失")
    .map(([, label]) => label);
}

function renderDecisionFactRows(rows) {
  return `
    <div class="decision-fact-grid">
      ${rows.map((row) => `
        <div class="decision-fact-row">
          <span>${escapeHtml(row.label)}</span>
          <strong>${escapeHtml(row.value)}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function renderDecisionPluginCard(plugin) {
  return `
    <article class="decision-plugin-card">
      <div class="decision-plugin-card-header">
        <h4>${escapeHtml(plugin.title)}</h4>
        <span class="status-pill status-${escapeHtml(plugin.tone)}">${escapeHtml(plugin.status)}</span>
      </div>
      <div class="decision-plugin-output">
        <strong>${escapeHtml(plugin.score)}</strong>
        <div>
          <b>${escapeHtml(plugin.headline)}</b>
          <span>${escapeHtml(plugin.detail)}</span>
        </div>
      </div>
      ${plugin.bodyHtml || ""}
      ${hasValue(plugin.condition) ? `<p class="condition-box">${escapeHtml(plugin.condition)}</p>` : ""}
    </article>
  `;
}

function klineTechnicalFactsPlugin(holding) {
  const detail = holding && typeof holding.technical_facts === "object"
    ? holding.technical_facts
    : null;
  if (technicalFactsUsable(detail)) {
    const timeframes = detail.facts && Array.isArray(detail.facts.timeframes)
      ? detail.facts.timeframes
      : [];
    const bollingerHtml = renderBollingerSection(timeframes, holding.last_price);
    const dateText = technicalFactsDateText(detail);
    return {
      title: "趋势 / K 线",
      status: "可用",
      tone: "ok",
      score: "K线",
      headline: dateText || "当前可用",
      detail: technicalFactsFreshnessText(detail) || "技术面事实来自日 K 行情。",
      bodyHtml: bollingerHtml,
      condition: "",
    };
  }
  const unavailable = technicalFactsUnavailableText(detail);
  return {
    title: "趋势 / K 线",
    status: "不可用",
    tone: unavailable.tone,
    score: "-",
    headline: unavailable.label,
    detail: unavailable.detail,
    bodyHtml: renderTechnicalFactsMeta(detail),
    condition: "条件：只有技术事实可用、来源未过期且周期完整时，才作为当前 K 线依据。",
  };
}

function technicalFactsUsable(detail) {
  return Boolean(
    detail
    && detail.available === true
    && detail.status === "usable"
    && detail.facts
    && Array.isArray(detail.facts.timeframes)
    && detail.facts.timeframes.length,
  );
}

function technicalFactsUnavailableText(detail) {
  const status = detail && hasValue(detail.status) ? String(detail.status) : "missing_file";
  const labels = {
    missing_file: "缺少文件",
    missing_record: "缺少记录",
    stale_source_hash: "来源已过期",
    extraction_error: "抽取失败",
    missing_source: "缺少来源",
    missing_source_hash: "缺少来源哈希",
    missing_timeframe: "缺少周期",
  };
  const tones = {
    missing_file: "partial",
    missing_record: "partial",
    stale_source_hash: "stale",
    extraction_error: "failed",
    missing_source: "failed",
    missing_source_hash: "failed",
    missing_timeframe: "failed",
  };
  return {
    label: labels[status] || "不可用",
    tone: tones[status] || "partial",
    detail: firstPresent(detail && detail.error, technicalFactsFreshnessText(detail), "暂无可用 K 线技术事实。"),
  };
}

function technicalFactsDateText(detail) {
  const parts = [];
  if (detail && hasValue(detail.data_date)) {
    parts.push(`数据日 ${detail.data_date}`);
  }
  if (detail && hasValue(detail.run_date)) {
    parts.push(`运行 ${detail.run_date}`);
  }
  return parts.join(" · ");
}

function technicalFactsRunText(detail) {
  const dates = technicalFactsDateText(detail);
  if (!dates) {
    return "";
  }
  if (detail && detail.source_type === "futu_kline") {
    return `条件：${dates}；来源为日 K 行情。`;
  }
  return `条件：${dates}；来源哈希已与最新报告校验。`;
}

function technicalFactsFreshnessText(detail) {
  const freshness = detail && detail.freshness && typeof detail.freshness === "object"
    ? detail.freshness
    : {};
  return firstPresent(freshness.message, freshness.status);
}

function renderTechnicalFactsMeta(detail) {
  const dates = technicalFactsDateText(detail);
  if (!dates) {
    return "";
  }
  return `<div class="technical-facts-meta">${escapeHtml(dates)}</div>`;
}

function renderBollingerSection(timeframes, holdingPrice) {
  const timeframesWithObjects = Array.isArray(timeframes)
    ? timeframes.filter((timeframe) => timeframe && typeof timeframe === "object")
    : [];
  const preferred = timeframesWithObjects.find((timeframe) => {
    const key = String(timeframe.timeframe || timeframe.period || "").toLowerCase();
    return key === "daily" || key === "day" || key === "1d";
  }) || timeframesWithObjects[0];
  if (!preferred) {
    return renderBollingerCard({}, "", "");
  }
  const bollinger = preferred.bollinger && typeof preferred.bollinger === "object"
    ? preferred.bollinger
    : {};
  const currentPrice = firstPresent(holdingPrice, preferred.current_price, bollinger.current_price);
  return renderBollingerCard(bollinger, currentPrice, timeframeLabel(preferred));
}

function renderBollingerCard(bollinger, currentPrice, timeframe) {
  const status = bollingerStatus(bollinger);
  const statusMeta = bollingerStatusMeta(status);
  const summary = firstPresent(
    bollinger.summary_zh,
    defaultBollingerSummary(status, timeframe),
  );
  const detail = firstPresent(
    bollinger.detail_zh,
    defaultBollingerDetail(status),
  );
  return `
    <section class="technical-bollinger-card ${escapeHtml(statusMeta.className)}">
      <div class="technical-bollinger-header">
        <span>${escapeHtml(timeframe ? `${timeframe}布林带` : "布林带")}</span>
        <strong>${escapeHtml(statusMeta.label)}</strong>
      </div>
      <div class="technical-bollinger-copy">
        <strong>${escapeHtml(summary)}</strong>
        <p>${escapeHtml(detail)}</p>
      </div>
      ${renderBollingerBand(bollinger, currentPrice)}
      ${renderBollingerMetrics(bollinger, currentPrice, status)}
    </section>
  `;
}

function bollingerStatus(bollinger) {
  const status = String(bollinger && bollinger.status ? bollinger.status : "").trim();
  if (["upper_risk", "lower_opportunity", "neutral", "unknown"].includes(status)) {
    return status;
  }
  return "unknown";
}

function bollingerStatusMeta(status) {
  const map = {
    upper_risk: { label: "回调风险升高", className: "upper-risk" },
    lower_opportunity: { label: "低位机会区域", className: "lower-opportunity" },
    neutral: { label: "中性区间", className: "middle-range" },
    unknown: { label: "布林带数据缺失", className: "missing" },
  };
  return map[status] || map.unknown;
}

function defaultBollingerSummary(status, timeframe) {
  const label = timeframe || "日线";
  if (status === "upper_risk") {
    return `当前价格贴近或超过${label}布林带上轨`;
  }
  if (status === "lower_opportunity") {
    return `当前价格接近${label}布林带下轨`;
  }
  if (status === "neutral") {
    return `当前价格位于${label}布林带中性区间`;
  }
  return "布林带数据缺失";
}

function defaultBollingerDetail(status) {
  if (status === "upper_risk") {
    return "价格靠近布林带上沿，说明短线偏热。这个状态用于提醒可能接近回调区，不直接给出交易动作。";
  }
  if (status === "lower_opportunity") {
    return "价格靠近布林带下沿，说明进入低位观察区。这个状态用于提醒可能出现低位机会，不直接给出交易动作。";
  }
  if (status === "neutral") {
    return "价格没有贴近上轨或下轨，布林带暂未给出需要特别关注的位置提醒。";
  }
  return "当前报告没有提供完整布林带事实。";
}

function renderBollingerBand(bollinger, currentPrice) {
  const lower = indicatorValue(bollinger.lower);
  const middle = indicatorValue(bollinger.middle);
  const upper = indicatorValue(bollinger.upper);
  const markerStyle = bollingerMarkerStyle(bollinger, currentPrice);
  return `
    <div class="technical-bollinger-band">
      <div class="technical-bollinger-track">
        <span class="technical-bollinger-marker" style="${escapeHtml(markerStyle)}"></span>
      </div>
      <div class="technical-bollinger-labels">
        <span>下轨 ${escapeHtml(formatDisplayNumber(lower || "缺失"))}</span>
        <span>中轨 ${escapeHtml(formatDisplayNumber(middle || "缺失"))}</span>
        <span>上轨 ${escapeHtml(formatDisplayNumber(upper || "缺失"))}</span>
      </div>
    </div>
  `;
}

function bollingerMarkerStyle(bollinger, currentPrice) {
  const lower = numericValue(indicatorValue(bollinger.lower));
  const upper = numericValue(indicatorValue(bollinger.upper));
  const current = numericValue(indicatorValue(currentPrice));
  if (lower === null || upper === null || current === null || upper <= lower) {
    return "left: 50%";
  }
  const raw = ((current - lower) / (upper - lower)) * 100;
  const clamped = Math.max(2, Math.min(98, raw));
  return `left: ${clamped.toFixed(1)}%`;
}

function renderBollingerMetrics(bollinger, currentPrice, status) {
  const referenceLabel = bollingerReferenceLabel(bollinger, status);
  const referenceValue = firstPresent(
    bollinger.reference_value,
    bollingerReferenceValue(bollinger, status),
  );
  const distance = firstPresent(bollinger.distance_pct, bollingerDistanceFallback(status));
  return renderDecisionFactRows([
    { label: "当前价", value: bollingerMetricValue(currentPrice) },
    { label: referenceLabel, value: bollingerMetricValue(referenceValue) },
    { label: "偏离幅度", value: bollingerMetricValue(distance) },
  ]);
}

function bollingerMetricValue(value) {
  return hasValue(value) ? formatDisplayNumber(value) : "缺失";
}

function bollingerReferenceLabel(bollinger, status) {
  if (status === "upper_risk") {
    return "上轨";
  }
  if (status === "lower_opportunity") {
    return "下轨";
  }
  if (status === "neutral") {
    return "中轨";
  }
  const referenceBand = String(bollinger.reference_band || "");
  if (referenceBand === "upper") {
    return "上轨";
  }
  if (referenceBand === "lower") {
    return "下轨";
  }
  return "参考轨道";
}

function bollingerReferenceValue(bollinger, status) {
  if (status === "upper_risk") {
    return bollinger.upper;
  }
  if (status === "lower_opportunity") {
    return bollinger.lower;
  }
  if (status === "neutral") {
    return bollinger.middle;
  }
  return firstPresent(bollinger.upper, bollinger.lower, bollinger.middle);
}

function bollingerDistanceFallback(status) {
  if (status === "neutral") {
    return "中性区间";
  }
  return "缺失";
}

function renderTechnicalFactRows(rows) {
  if (!rows.length) {
    return `<p class="compact-empty">暂无可展示的周期指标。</p>`;
  }
  return `
    <div class="technical-fact-grid">
      ${rows.map((row) => `
        <div class="technical-fact-row">
          <span>${escapeHtml(row.label)}</span>
          <strong>${escapeHtml(row.value)}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function technicalFactRows(facts) {
  const timeframes = facts && Array.isArray(facts.timeframes) ? facts.timeframes : [];
  return timeframes.flatMap((timeframe) => technicalFactRowsForTimeframe(timeframe));
}

function technicalFactRowsForTimeframe(timeframe) {
  if (!timeframe || typeof timeframe !== "object") {
    return [];
  }
  const label = timeframeLabel(timeframe);
  const rows = [];
  addTechnicalFactRow(rows, `${label} 当前价`, indicatorValue(timeframe.current_price));
  addTechnicalFactRow(rows, `${label} RSI`, indicatorValue(timeframe.rsi));
  addTechnicalFactRow(rows, `${label} MACD`, macdValue(timeframe.macd));
  addTechnicalFactRow(rows, `${label} MACD`, indicatorValue(timeframe.macd_golden_cross));
  addTechnicalFactRow(rows, `${label} 金叉`, goldenCrossText(timeframe.golden_cross));
  addTechnicalFactRow(rows, `${label} 趋势`, indicatorValue(timeframe.trend_summary || timeframe.trend));
  addTechnicalFactRow(rows, `${label} ATR`, atrValue(timeframe.atr));
  addTechnicalFactRow(rows, `${label} 支撑`, supportResistanceValue(timeframe, "support"));
  addTechnicalFactRow(rows, `${label} 阻力`, supportResistanceValue(timeframe, "resistance"));
  addTechnicalFactRow(rows, `${label} 均线`, movingAverageValue(timeframe));
  return rows;
}

function addTechnicalFactRow(rows, label, value) {
  if (hasValue(value)) {
    rows.push({ label, value: formatDisplayNumber(value) });
  }
}

function timeframeLabel(timeframe) {
  const explicit = timeframe.timeframe_label || timeframe.label;
  if (hasValue(explicit)) {
    return formatPlain(explicit);
  }
  const key = String(timeframe.timeframe || timeframe.period || "").toLowerCase();
  const labels = {
    daily: "日线",
    day: "日线",
    "1d": "日线",
    weekly: "周线",
    week: "周线",
    "1w": "周线",
    monthly: "月线",
    month: "月线",
    "1m": "月线",
    yearly: "年线",
    year: "年线",
    "1y": "年线",
  };
  return labels[key] || formatPlain(timeframe.timeframe || timeframe.period || "未标明周期");
}

function indicatorValue(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  return firstPresent(value.value, value.text, value.status, value.signal, value.summary);
}

function indicatorDisplayNumber(value) {
  const item = indicatorValue(value);
  return hasValue(item) ? formatDisplayNumber(item) : "";
}

function macdValue(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  const macdLine = firstPresent(value.macd, value.value);
  const parts = [
    hasValue(macdLine) ? `MACD ${formatDisplayNumber(macdLine)}` : "",
    hasValue(value.signal) ? `Signal ${formatDisplayNumber(value.signal)}` : "",
    hasValue(value.histogram) ? `Hist ${formatDisplayNumber(value.histogram)}` : "",
    indicatorValue(value.crossover),
    goldenCrossText(value.golden_cross),
  ].filter(Boolean);
  return parts.join(" · ");
}

function atrValue(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  return [
    indicatorDisplayNumber(value.value),
    indicatorDisplayNumber(value.percent_of_price),
  ].filter((part) => hasValue(part)).join(" · ");
}

function supportResistanceValue(timeframe, kind) {
  const payload = timeframe.support_resistance && typeof timeframe.support_resistance === "object"
    ? timeframe.support_resistance
    : {};
  const schemaValue = kind === "support"
    ? payload.support_levels
    : payload.resistance_levels;
  const legacyValue = kind === "support" ? timeframe.support : timeframe.resistance;
  return listValue(firstPresent(schemaValue, legacyValue));
}

function listValue(value) {
  if (Array.isArray(value)) {
    return value
      .map((item) => indicatorDisplayNumber(item))
      .filter((item) => hasValue(item))
      .join(" · ");
  }
  return indicatorDisplayNumber(value);
}

function goldenCrossText(value) {
  if (value === true) {
    return "金叉";
  }
  if (value === false) {
    return "未金叉";
  }
  return indicatorValue(value);
}

function movingAverageValue(timeframe) {
  const averages = timeframe.moving_averages || timeframe.ma || timeframe.averages;
  if (averages && typeof averages === "object" && !Array.isArray(averages)) {
    const parts = Object.entries(averages)
      .filter(([, value]) => hasValue(value))
      .map(([key, value]) => `${key.toUpperCase()} ${formatDisplayNumber(value)}`);
    if (parts.length) {
      return parts.join(" · ");
    }
  }
  const parts = [
    hasValue(timeframe.ma20) ? `MA20 ${formatDisplayNumber(timeframe.ma20)}` : "",
    hasValue(timeframe.ma50) ? `MA50 ${formatDisplayNumber(timeframe.ma50)}` : "",
    hasValue(timeframe.ma200) ? `MA200 ${formatDisplayNumber(timeframe.ma200)}` : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

function renderLLMDecisionTemplate(holding) {
  const action = currentDecisionAction(holding);
  const actionRows = operationRows(holding);
  const price = firstSafePrimaryValue(action.limit_price, action.last_price, holding.last_price);
  const quantity = firstSafePrimaryValue(action.suggested_quantity, action.target_quantity, action.quantity);
  const stopValue = firstSafePrimaryValue(action.stop_price, holding.strategy && holding.strategy.stop_loss);
  const templateRows = [
    ["最终动作", desiredActionText(holding)],
    ["执行方式", "人工确认后执行；不自动下单。"],
    ["执行时机", price ? `当前价格仍满足策略价位 ${price} 时。` : "价格信息确认后再执行。"],
    ["执行前检查", `确认实时持仓仍为 ${formatPlain(holding.total_quantity || "-")}，行情正常，订单数量 ${quantity || "需人工确认"}。`],
    ["不执行条件", stopValue ? `行情缺失、持仓不一致、价格跌破 ${stopValue}、出现重大新公告。` : "行情缺失、持仓不一致、价格跌破保护价、出现重大新公告。"],
    ["复评安排", nextReviewText(holding)],
  ];
  return `
    <section class="detail-section trading-decision-section llm-decision-template">
      <div class="trading-decision-section-header">
        <div>
          <h3>大模型决策模板</h3>
          <p>基于已接入的 TradingAgents 交易决策生成，作为执行前复核模板。</p>
        </div>
        <span class="status-pill status-partial">人工确认</span>
      </div>
      <div class="llm-template-summary">
        <strong>${escapeHtml(finalConclusionText(holding))}</strong>
        <span>${escapeHtml(finalReasonText(holding))}</span>
      </div>
      <div class="llm-template-grid">
        ${templateRows.map(([label, value]) => renderLLMTemplateField(label, value)).join("")}
      </div>
      <div class="llm-template-actions">
        <dl class="compact-kv">
          ${actionRows.map(([label, value]) => renderCompactKv(label, value)).join("")}
        </dl>
      </div>
      <p class="condition-box strong-condition">${escapeHtml(finalConditionText(holding))}</p>
    </section>
  `;
}

function renderLLMTemplateField(label, value) {
  return `
    <article class="llm-template-field">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatPlain(value))}</strong>
    </article>
  `;
}

function renderLanguageToggle() {
  return `
    <div class="language-toggle" role="group" aria-label="详情语言">
      ${Object.entries(DETAIL_LANGUAGE_LABELS).map(([value, label]) => `
        <button
          class="${state.detailLanguage === value ? "active" : ""}"
          type="button"
          data-detail-language="${value}"
        >${escapeHtml(label)}</button>
      `).join("")}
    </div>
  `;
}

function renderMetric(label, value) {
  return `
    <article class="detail-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatPlain(value))}</strong>
    </article>
  `;
}

function renderAgentReportSection(report, holding) {
  if (!sectionAvailable(report)) {
    return renderDetailSection("TradingAgents 报告", renderStatusMessage("暂无 TradingAgents 报告", report));
  }
  const reportText = firstValue(report, ["summary", "report", "analysis", "content", "markdown"]);
  const rawText = firstValue(report, ["raw_report", "raw_decision", "raw", "full_report"]);
  if (state.detailLanguage !== "en") {
    const translatedText = firstValue(report, ["summary_zh", "report_zh", "analysis_zh", "content_zh", "markdown_zh"]);
    const body = `
      ${renderChineseAgentSummary(report, holding)}
      ${hasValue(translatedText) ? `<div class="report-text translated-report">${escapeHtml(translatedText)}</div>` : renderStatusMessage("暂无中文译文，请先运行 translate-agent-reports", report)}
      ${renderEnglishSourceBlock(reportText, rawText, "查看英文原文")}
    `;
    return renderDetailSection("TradingAgents 报告", body);
  }
  const usedFallback = report.fallback_used || report.used_fallback || report.source_status === "fallback";
  const terms = [
    renderTerm("观点", report.rating || report.advice_action),
    renderTerm("状态", report.status),
    renderTerm("生成时间", report.generated_at || report.run_date),
    renderTerm("来源", report.source),
    renderTerm("来源状态", report.source_status),
    renderTerm("回退", usedFallback ? "使用历史报告回退" : ""),
    renderTerm("回退原因", report.fallback_reason),
    renderTerm("回退日期", report.fallback_from_date),
  ].filter(Boolean).join("");
  const rawReport = hasValue(rawText)
    ? `<button class="raw-toggle" type="button" data-toggle-raw-report>查看原始报告</button><pre class="raw-report hidden">${escapeHtml(rawText)}</pre>`
    : "";
  const body = `
    ${terms ? `<dl class="detail-dl">${terms}</dl>` : ""}
    ${renderStatusWarning(report)}
    ${hasValue(reportText) ? `<div class="report-text">${escapeHtml(reportText)}</div>` : renderStatusMessage("暂无 TradingAgents 报告", report)}
    ${rawReport}
  `;
  return renderDetailSection("TradingAgents 报告", body);
}

function renderChineseAgentSummary(report, holding) {
  const strategy = holding.strategy || {};
  const action = holding.trade_action || holding.premarket_action || {};
  const reason = safeChineseReason(action, strategy, report);
  const terms = [
    renderRequiredTerm("观点", firstMappedActionLabel(report.rating, report.advice_action)),
    renderRequiredTerm("报告状态", mappedActionStatusLabel(report.status)),
    renderRequiredTerm("生成时间", report.generated_at || report.run_date),
    renderChineseTerm("交易动作", firstMappedActionLabel(action.action, action.suggested_action)),
    renderChineseTerm("动作状态", mappedActionStatusLabel(action.status)),
    renderChineseTerm("触发状态", decisionTriggerText(action)),
    renderChineseTerm("核心理由", reason),
    renderChineseTerm("目标价", safeRangeText(strategy.target_1, strategy.target_2) || safePrimaryValue(strategy.target_range)),
    renderChineseTerm("止损价", firstSafePrimaryValue(strategy.stop_loss, action.stop_price)),
  ].filter(Boolean).join("");
  return terms ? `<dl class="detail-dl translated-summary">${terms}</dl>` : "";
}

function renderStrategySection(strategy, holding) {
  if (!sectionAvailable(strategy)) {
    return renderDetailSection("交易策略", renderStatusMessage("暂无交易策略", strategy));
  }
  if (state.detailLanguage !== "en") {
    const englishText = firstValue(strategy, ["plan_text", "rationale", "agent_excerpt"]);
    return renderDetailSection(
      "交易策略",
      `${renderStatusWarning(strategy)}${renderChineseStrategyTerms(strategy, holding)}${renderEnglishSourceBlock(englishText, "", "查看英文原文")}`,
    );
  }
  const terms = [
    renderRequiredTerm("观点", strategy.view || strategy.stance || strategy.signal || strategy.rating),
    renderRequiredTerm("买入区间", joinRange(strategy.entry_min, strategy.entry_max) || joinRange(strategy.entry_zone_low, strategy.entry_zone_high) || strategy.entry_range),
    renderRequiredTerm("加仓价", strategy.add_price),
    renderRequiredTerm("止损价", strategy.stop_loss),
    renderRequiredTerm("目标价", joinRange(strategy.target_1, strategy.target_2) || strategy.target_range),
    renderRequiredTerm("仓位上限", strategy.target_weight || strategy.target_position || strategy.max_weight),
    renderRequiredTerm("催化因素", strategy.catalyst),
    renderRequiredTerm("时间周期", strategy.time_horizon),
    renderTerm("风险", strategy.risk_level || strategy.risk),
    renderRequiredTerm("计划", strategy.plan_text),
    renderTerm("说明", strategy.rationale || strategy.agent_reason || strategy.agent_excerpt || strategy.notes),
  ].filter(Boolean).join("");
  return renderDetailSection("交易策略", `${renderStatusWarning(strategy)}${terms ? `<dl class="detail-dl">${terms}</dl>` : renderStatusMessage("暂无交易策略", strategy)}`);
}

function renderChineseStrategyTerms(strategy, holding) {
  const action = holding.trade_action || {};
  const terms = [
    renderRequiredTerm("观点", firstMappedActionLabel(strategy.view, strategy.stance, strategy.signal, strategy.rating)),
    renderChineseTerm("买入区间", safeRangeText(strategy.entry_min, strategy.entry_max) || safeRangeText(strategy.entry_zone_low, strategy.entry_zone_high) || safePrimaryValue(strategy.entry_range)),
    renderChineseTerm("加仓价", safePrimaryValue(strategy.add_price)),
    renderChineseTerm("止损价", firstSafePrimaryValue(strategy.stop_loss, action.stop_price)),
    renderChineseTerm("目标价", safeRangeText(strategy.target_1, strategy.target_2) || safePrimaryValue(strategy.target_range)),
    renderChineseTerm("仓位上限", firstSafePrimaryValue(strategy.target_weight, strategy.target_position, strategy.max_weight)),
    renderSafeChineseTerm("时间周期", strategy.time_horizon_zh, strategy.time_horizon),
    renderSafeChineseTerm("催化因素", strategy.catalyst_zh, strategy.catalyst),
    renderSafeChineseTerm("风险", strategy.risk_level_zh, strategy.risk_zh, strategy.risk_level, strategy.risk),
    renderChineseTerm("当前动作", firstMappedActionLabel(action.action, action.suggested_action)),
    renderChineseTerm("触发状态", decisionTriggerText(action)),
    renderSafeChineseTerm("说明", action.agent_reason_zh, strategy.agent_reason_zh, strategy.notes_zh, action.agent_reason, strategy.agent_reason, strategy.notes),
  ].filter(Boolean).join("");
  if (!terms) {
    return renderStatusMessage("暂无交易策略", strategy);
  }
  return `<dl class="detail-dl translated-summary">${terms}</dl>`;
}

function renderEnglishSourceBlock(text, rawText, buttonText) {
  const sourceText = firstAvailableText(rawText, text);
  if (!hasValue(sourceText)) {
    return "";
  }
  return `
    <button class="raw-toggle english-source-toggle" type="button" data-toggle-raw-report>${escapeHtml(buttonText)}</button>
    ${renderSplitSourceRows(sourceText)}
  `;
}

function renderTradeActionSection(detailHolding) {
  const premarketAction = detailHolding.premarket_action || {};
  const tradeAction = detailHolding.trade_action || {};
  if (!sectionAvailable(tradeAction) && !sectionAvailable(premarketAction)) {
    return renderDetailSection("当前交易动作", renderStatusMessage("暂无触发中的交易动作", tradeAction));
  }
  const action = sectionAvailable(tradeAction) ? tradeAction : premarketAction;
  const body = `
    ${renderStatusWarning(action)}
    ${renderTradeDecisionBand(action, detailHolding)}
    ${renderTradeImpactGrid(action, detailHolding)}
    ${typeof renderRationaleDialogue === "function" ? renderRationaleDialogue(detailHolding) : ""}
  `;
  return renderDetailSection("当前交易动作", body);
}

function renderAnalysisStrategySection(holding) {
  const body = `
    ${renderReportStatusLine(holding)}
    <div class="decision-dashboard">
      <article class="decision-card primary">
        <span>当前希望你做什么</span>
        <strong>${escapeHtml(desiredActionText(holding))}</strong>
        <p>${escapeHtml(decisionSubline(holding))}</p>
      </article>
      <article class="decision-card">
        <span>操作指令</span>
        <dl class="operation-list">
          ${operationRows(holding).map(([label, value]) => renderCompactKv(label, value)).join("")}
        </dl>
      </article>
      <article class="decision-card">
        <span>今天重点关注</span>
        <p>${escapeHtml(watchPointText(holding))}</p>
      </article>
    </div>
    <div class="decision-metric-strip" aria-label="分析指标">
      ${decisionMetricCells(holding).map(([label, value]) => `
        <article>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(formatPlain(value))}</strong>
        </article>
      `).join("")}
    </div>
    ${renderAnalystDialogue(holding)}
    ${renderFinalConclusion(holding)}
    ${renderSourceReview(holding)}
  `;
  return renderDetailSection("分析与交易策略", body, "analysis-strategy-section");
}

function renderReportStatusLine(holding) {
  const report = holding.agent_report || {};
  const action = currentDecisionAction(holding);
  const usedFallback = report.fallback_used || report.used_fallback || report.source_status === "fallback";
  const parts = [
    analystViewText(holding),
    mappedActionStatusLabel(report.status),
    usedFallback ? "使用历史报告回退" : "",
    mappedActionStatusLabel(action.status),
    report.generated_at || report.run_date,
    "只读 · 需要人工确认",
  ].filter((part) => hasValue(part) && part !== "-");
  const fallbackWarning = renderStatusWarning(report) || renderStatusWarning(action);
  return `
    <div class="report-status-line">
      <span>${escapeHtml(parts.join(" · ") || "只读 · 需要人工确认")}</span>
      ${fallbackWarning}
    </div>
  `;
}

function renderAnalystDialogue(holding) {
  const rows = rationaleRows(rationaleSource(holding))
    .map((row) => ({
      label: row.label,
      text: chineseDisplayText(row.text),
    }))
    .filter((row) => hasValue(row.text) && row.text !== "-" && safePrimaryValue(row.text));
  if (!rows.length) {
    return `
      <section class="analyst-dialogue">
        <h4>分析师对话</h4>
        <p class="compact-empty">暂无可展示的中文分析对话。</p>
      </section>
    `;
  }
  return `
    <section class="analyst-dialogue">
      <h4>分析师对话</h4>
      <div class="dialogue-list">
        ${rows.map((row) => `
          <div class="dialogue-row">
            <strong>${escapeHtml(row.label)}</strong>
            <span>${escapeHtml(row.text)}</span>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function renderFinalConclusion(holding) {
  return renderResearchConclusions(holding);
}

function renderResearchConclusions(holding) {
  const researchView = holding.research_view || {};
  const original = researchConclusionWithFallback(
    researchView.tradingagents_conclusion,
    holding,
  );
  const userConclusion = researchConclusion(researchView.user_llm_conclusion);
  const detailKey = holdingKey(holding);
  return `
    <section class="final-conclusion research-conclusion-section">
      <div class="research-conclusion-header">
        <h4>最终结论</h4>
        <span>展示两个来源：投研原始结论，以及你和 LLM 讨论后的最终结论。</span>
      </div>
      <div class="research-conclusion-grid">
        ${renderResearchConclusionCard({
          title: "投研给出的结论",
          conclusion: original,
          actionHtml: renderSourceReviewButton(holding),
          missingText: "缺失",
        })}
        ${renderResearchConclusionCard({
          title: "我和 LLM 探讨后的结论",
          conclusion: userConclusion,
          actionHtml: `<button class="raw-toggle" type="button" data-research-chat="${escapeHtml(detailKey)}">${userConclusion.present ? "继续讨论" : "开始讨论"}</button>`,
          missingText: "缺失",
        })}
      </div>
    </section>
  `;
}

function researchConclusion(value) {
  const conclusion = value && typeof value === "object" ? value : {};
  const content = meaningfulConclusionText(conclusion.content || "");
  return {
    present: conclusion.status === "present" && hasValue(content),
    content,
    reason: formatPlain(conclusion.reason || ""),
    condition: formatPlain(conclusion.condition || conclusion.conditions || ""),
    failure: formatPlain(conclusion.failure_condition || conclusion.failure || ""),
  };
}

function researchConclusionWithFallback(value, holding) {
  const conclusion = researchConclusion(value);
  if (conclusion.present) {
    return conclusion;
  }
  return legacyFinalConclusion(holding);
}

function legacyFinalConclusion(holding) {
  const fields = Object.fromEntries(
    finalConclusionItems(holding).map((item) => [item.label, formatPlain(item.text)]),
  );
  const content = meaningfulConclusionText(fields["结论"]);
  return {
    present: hasValue(content),
    content,
    reason: meaningfulConclusionText(fields["理由"]),
    condition: meaningfulConclusionText(fields["条件"]),
    failure: meaningfulConclusionText(fields["失败条件"]),
  };
}

function meaningfulConclusionText(value) {
  const text = formatPlain(value);
  if (!hasValue(text) || text === "-" || text === "暂无明确结论。") {
    return "";
  }
  return text;
}

function renderResearchConclusionCard({ title, conclusion, actionHtml, missingText }) {
  const statusText = conclusion.present ? "已生成" : "缺失";
  const body = conclusion.present
    ? `
      <div class="research-conclusion-body">
        <strong>${escapeHtml(conclusion.content)}</strong>
        ${renderResearchConclusionField("理由", conclusion.reason)}
        ${renderResearchConclusionField("条件", conclusion.condition)}
        ${renderResearchConclusionField("失败条件", conclusion.failure)}
      </div>
    `
    : `
      <div class="research-conclusion-body missing">
        <strong>${escapeHtml(missingText)}</strong>
        <p>打开聊天窗口后，系统会自动加载投研结论、原始资料、你的仓位与关注点。只有点击“生成最终结论”后才写入这里。</p>
      </div>
    `;
  return `
    <article class="research-conclusion-card">
      <div class="research-conclusion-card-header">
        <h5>${escapeHtml(title)}</h5>
        <span class="status-pill ${conclusion.present ? "status-ok" : "status-muted"}">${escapeHtml(statusText)}</span>
      </div>
      ${body}
      <div class="research-conclusion-actions">${actionHtml}</div>
    </article>
  `;
}

function renderResearchConclusionField(label, value) {
  if (!hasValue(value) || value === "-") {
    return "";
  }
  return `
    <div class="research-conclusion-field">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function renderSourceReviewButton(holding) {
  return hasValue(sourceReviewText(holding))
    ? `<button class="raw-toggle english-source-toggle" type="button" data-toggle-raw-report>查看英文原文</button>`
    : "";
}

function renderSourceReview(holding) {
  const sourceText = sourceReviewText(holding);
  if (!hasValue(sourceText)) {
    return "";
  }
  return `
    <section class="source-review">
      ${renderSplitSourceRows(sourceText)}
    </section>
  `;
}

async function openResearchChat(detailKey) {
  const holding = holdingByKey(detailKey);
  if (!holding) {
    return;
  }
  const researchView = holding.research_view || {};
  const previousKey = state.researchChat.holdingKey;
  state.researchChat.holdingKey = detailKey;
  if (previousKey !== detailKey) {
    state.researchChat.sessionId = "";
  }
  elements["research-chat-title"].textContent = `LLM 深度讨论 · ${holding.market}.${holding.symbol}`;
  elements["research-chat-context-note"].textContent = `上下文已自动加载 · ${researchView.research_date || "-"}`;
  renderResearchChatContext(holding);
  renderResearchChatMessages([]);
  openResearchChatLayer();
  if (!researchView.available) {
    state.researchChat.sessionId = "";
    elements["research-chat-context-note"].textContent = "暂无投研上下文";
    elements["research-chat-messages"].innerHTML = `<p class="compact-empty">暂无投研上下文，无法开始讨论。</p>`;
    setResearchChatBusy(false, "暂无投研上下文，无法开始讨论");
    return;
  }
  await createResearchChatSession(holding);
}

function openResearchChatLayer() {
  elements["research-chat-layer"].hidden = false;
  elements["research-chat-layer"].classList.remove("hidden");
  elements["research-chat-input"].focus();
}

function closeResearchChat() {
  elements["research-chat-layer"].hidden = true;
  elements["research-chat-layer"].classList.add("hidden");
}

function renderResearchChatContext(holding) {
  const researchView = holding.research_view || {};
  const original = researchConclusion(researchView.tradingagents_conclusion);
  elements["research-chat-context-list"].innerHTML = `
    <div><dt>投研结论</dt><dd>${escapeHtml(original.content || "缺失")}</dd></div>
    <div><dt>用户上下文</dt><dd>组合权重 ${escapeHtml(formatPlain(holding.portfolio_weight_hkd || "-"))}；风险标记 ${escapeHtml(formatPlain(holding.risk_flag || "-"))}</dd></div>
    <div><dt>输出目标</dt><dd>生成 user_llm_conclusion.json 后刷新看板。</dd></div>
  `;
}

async function createResearchChatSession(holding) {
  const requestKey = state.researchChat.holdingKey || holdingKey(holding);
  setResearchChatBusy(true, "正在加载上下文...");
  try {
    const session = await postDashboardJson("/api/research-chat/sessions", {
      market: holding.market,
      symbol: holding.symbol,
    });
    if (state.researchChat.holdingKey !== requestKey) {
      return;
    }
    state.researchChat.sessionId = session.session_id || "";
    renderResearchChatMessages(session.messages || []);
    setResearchChatStatus("上下文已自动加载。");
  } catch (error) {
    if (state.researchChat.holdingKey === requestKey) {
      setResearchChatStatus(error.message || String(error));
    }
  } finally {
    if (state.researchChat.holdingKey === requestKey) {
      setResearchChatBusy(false);
    }
  }
}

async function sendResearchChatMessage() {
  const content = elements["research-chat-input"].value.trim();
  if (!content || !state.researchChat.sessionId || state.researchChat.busy) {
    return;
  }
  const optimisticMessages = [
    ...state.researchChat.messages,
    { role: "user", content, localOnly: true },
    { role: "assistant", content: "LLM 正在处理...", pending: true },
  ];
  elements["research-chat-input"].value = "";
  renderResearchChatMessages(optimisticMessages);
  setResearchChatBusy(true, "LLM 正在处理...");
  try {
    const session = await postDashboardJson(
      `/api/research-chat/sessions/${encodeURIComponent(state.researchChat.sessionId)}/messages`,
      { content },
    );
    renderResearchChatMessages(session.messages || []);
    setResearchChatStatus("对话已保存。");
  } catch (error) {
    renderResearchChatMessages([
      ...state.researchChat.messages.filter((message) => !message.pending),
      {
        role: "assistant",
        content: `发送失败：${error.message || String(error)}`,
        localOnly: true,
      },
    ]);
    setResearchChatStatus(error.message || String(error));
  } finally {
    setResearchChatBusy(false);
  }
}

async function finalizeResearchChat() {
  if (!state.researchChat.sessionId || state.researchChat.busy) {
    return;
  }
  setResearchChatBusy(true, "正在生成最终结论...");
  try {
    await postDashboardJson(
      `/api/research-chat/sessions/${encodeURIComponent(state.researchChat.sessionId)}/finalize`,
      {},
    );
    setResearchChatStatus("最终结论已生成。");
    closeResearchChat();
    await loadDashboard();
  } catch (error) {
    setResearchChatStatus(error.message || String(error));
  } finally {
    setResearchChatBusy(false);
  }
}

function renderResearchChatMessages(messages) {
  const rows = Array.isArray(messages) ? messages : [];
  state.researchChat.messages = rows;
  elements["research-chat-messages"].innerHTML = rows.length
    ? rows.map((message) => `
      <div class="research-chat-message ${message.role === "user" ? "user" : "assistant"}${message.pending ? " pending" : ""}">
        <strong>${message.role === "user" ? "你" : "LLM"}</strong>
        <span>${escapeHtml(message.content || "")}</span>
      </div>
    `).join("")
    : `<p class="compact-empty">上下文已加载，可以开始讨论。</p>`;
  state.researchChat.messageCount = rows.filter((message) => !message.pending && !message.localOnly).length;
  elements["research-chat-finalize"].disabled = state.researchChat.messageCount < 2;
  elements["research-chat-messages"].scrollTop = elements["research-chat-messages"].scrollHeight;
}

function setResearchChatBusy(busy, statusText) {
  state.researchChat.busy = busy;
  elements["research-chat-send"].disabled = busy || !state.researchChat.sessionId;
  elements["research-chat-finalize"].disabled = busy
    || !state.researchChat.sessionId
    || state.researchChat.messageCount < 2;
  if (statusText) {
    setResearchChatStatus(statusText);
  }
}

function setResearchChatStatus(text) {
  elements["research-chat-status"].textContent = text;
}

async function postDashboardJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(payload || {}),
  });
  const data = await response.json();
  if (!response.ok || data.status === "error") {
    throw new Error(data.message || `request ${response.status}`);
  }
  return data;
}

function holdingByKey(detailKey) {
  return holdingByKeyFromRows(getHoldings(), detailKey)
    || accountHoldingGroups().flatMap((group) => group.rows)
      .find((row) => row.key === detailKey)?.holding
    || null;
}

function holdingByKeyFromRows(rows, detailKey) {
  for (let index = 0; index < rows.length; index += 1) {
    if (
      holdingKey(rows[index], index) === detailKey
      || holdingKey(rows[index]) === detailKey
    ) {
      return rows[index];
    }
  }
  return null;
}

function renderTradeDecisionBand(action, holding) {
  return `
    <div class="decision-band">
      <article class="decision-block">
        <h4>清晰交易策略</h4>
        <strong>${escapeHtml(strategyHeadline(action, holding))}</strong>
        <p>${escapeHtml(strategySubline(action, holding))}</p>
      </article>
      <article class="decision-block">
        <h4>操作方向与价位</h4>
        <dl class="compact-kv">
          ${renderCompactKv("动作", reportActionStatusLabel(action))}
          ${renderCompactKv("限价", formatDisplayNumber(firstSafePrimaryValue(action.limit_price, action.last_price)))}
          ${renderCompactKv("数量", formatDisplayNumber(firstSafePrimaryValue(action.suggested_quantity, action.target_quantity, action.quantity)))}
          ${renderCompactKv("金额", safeActionNotionalText(action))}
          ${renderCompactKv("止损", formatDisplayNumber(firstSafePrimaryValue(action.stop_price)))}
        </dl>
      </article>
      <article class="decision-block">
        <h4>简短触发理由</h4>
        <p class="decision-reason">${escapeHtml(shortActionReason(action))}</p>
      </article>
    </div>
  `;
}

function renderTradeImpactGrid(action, holding) {
  const cells = [
    ["当前数量", formatDisplayNumber(firstSafePrimaryValue(action.current_quantity, holding.total_quantity))],
    ["交易后数量", formatDisplayNumber(firstSafePrimaryValue(action.post_trade_quantity))],
    ["建议金额", safeActionNotionalText(action)],
    ["交易后权重", firstSafePrimaryValue(action.post_trade_weight)],
    ["下一触发", nextTriggerText(action, holding)],
  ];
  return `
    <div class="impact-grid" aria-label="交易影响">
      ${cells.map(([label, value]) => `
        <article class="impact-cell">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(formatPlain(value))}</strong>
        </article>
      `).join("")}
    </div>
  `;
}

function renderRationaleDialogue(holding) {
  const rows = rationaleRows(rationaleSource(holding))
    .map((row) => ({
      label: row.label,
      text: chineseDisplayText(row.text),
    }))
    .filter((row) => {
      return hasValue(row.text) && row.text !== "-" && safePrimaryValue(row.text);
    });
  if (!rows.length) {
    return "";
  }
  return `
    <div class="rationale-dialogue">
      <h4>理由对话</h4>
      <div class="dialogue-list">
        ${rows.map((row) => `
          <div class="dialogue-row">
            <strong>${escapeHtml(row.label)}</strong>
            <span>${escapeHtml(row.text)}</span>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function renderSplitSourceRows(text) {
  const rows = sourceRows(text);
  if (!rows.length) {
    return `<pre class="raw-report english-source hidden">${escapeHtml(text)}</pre>`;
  }
  return `
    <div class="raw-report english-source split-source hidden">
      ${rows.map((row) => `
        <div class="dialogue-row">
          <strong>${escapeHtml(row.label)}</strong>
          <span>${escapeHtml(row.text)}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function sourceRows(text) {
  return splitRationaleText(text).map((sentence, index, sentences) => ({
    label: rationaleLabel(sentence, index, sentences.length),
    text: sentence,
  }));
}

function renderCompactKv(label, value) {
  return `
    <div>
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(formatPlain(value))}</dd>
    </div>
  `;
}

function strategyHeadline(action, holding) {
  const symbol = actionSymbol(action) !== "-" ? actionSymbol(action) : `${formatPlain(holding.market)}.${formatPlain(holding.symbol)}`;
  const actionText = firstMappedActionLabel(action.action, action.suggested_action);
  if (actionText === "-") {
    return `${symbol} 交易策略`;
  }
  return `${actionText} ${symbol}`;
}

function strategySubline(action, holding) {
  const strategy = holding.strategy || {};
  const view = firstMappedActionLabel(strategy.view, strategy.stance, strategy.signal, strategy.rating);
  const status = mappedActionStatusLabel(action.status);
  const parts = [view, status].filter((part) => part && part !== "-");
  if (parts.length) {
    return `${parts.join(" · ")}；执行前保持人工确认。`;
  }
  return "执行前保持人工确认。";
}

function nextTriggerText(action, holding) {
  const watchTrigger = primaryChineseText(action.watch_trigger_zh)
    || firstMappedLabel(TRIGGER_STATUS_LABELS, action.watch_trigger)
    || firstMappedLabel(REASON_LABELS, action.watch_trigger)
    || safePrimaryValue(action.watch_trigger);
  if (watchTrigger) {
    return watchTrigger;
  }
  const strategy = holding.strategy || {};
  const targetText = safeRangeText(strategy.target_1, strategy.target_2) || safePrimaryValue(strategy.target_range);
  if (hasValue(targetText)) {
    return `目标价 ${targetText}`;
  }
  const planText = primaryChineseText(strategy.plan_text_zh, strategy.rationale_zh)
    || firstSafePrimaryValue(strategy.plan_text);
  if (planText) {
    return compactSentence(planText, 48);
  }
  return "";
}

function currentDecisionAction(holding) {
  const tradeAction = holding.trade_action || {};
  if (sectionAvailable(tradeAction)) {
    return tradeAction;
  }
  const premarketAction = holding.premarket_action || {};
  if (sectionAvailable(premarketAction)) {
    return premarketAction;
  }
  return {};
}

function desiredActionText(holding) {
  const action = currentDecisionAction(holding);
  const symbol = detailSymbol(holding);
  const actionText = firstMappedActionLabel(action.action, action.suggested_action);
  if (actionText === "-") {
    return `今天暂无触发中的交易动作`;
  }
  const quantity = firstSafePrimaryValue(action.suggested_quantity, action.target_quantity, action.quantity);
  const quantityText = quantity ? `，数量 ${quantity}` : "";
  return `${actionText} ${symbol}${quantityText}`;
}

function detailSymbol(holding) {
  const market = formatPlain(holding.market);
  const symbol = formatPlain(holding.symbol);
  if (market === "-" && symbol === "-") {
    return "-";
  }
  if (market === "-") {
    return symbol;
  }
  if (symbol === "-") {
    return market;
  }
  return `${market}.${symbol}`;
}

function decisionTriggerText(action) {
  const mappedTrigger = firstMappedLabel(TRIGGER_STATUS_LABELS, action.trigger_status, action.watch_trigger);
  if (mappedTrigger) {
    return mappedTrigger;
  }
  const direct = primaryChineseText(action.trigger_status_zh, action.watch_trigger_zh);
  if (direct) {
    return direct;
  }
  return safePrimaryValue(action.watch_trigger) || "-";
}

function primaryChineseText(...values) {
  for (const value of values) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (text && /[\u3400-\u9fff]/.test(text) && !hasRawEnglishProse(text)) {
      return text;
    }
  }
  return "";
}

function safePrimaryValue(value) {
  const text = formatPlain(value);
  if (text === "-") {
    return "";
  }
  if (/[\u3400-\u9fff]/.test(text)) {
    return hasRawEnglishProse(text) ? "" : text;
  }
  const englishWords = text.match(/\b[A-Za-z][A-Za-z'-]*\b/g) || [];
  if (!englishWords.length) {
    return text;
  }
  const allowedTokens = new Set(["HKD", "USD", "ETF", "ETFS", "MACD", "RSI", "YOY", "QOQ", "OPENAI", "IPHONE"]);
  const hasUnsafeEnglish = englishWords.some((word) => !allowedTokens.has(word.toUpperCase()));
  if (hasUnsafeEnglish) {
    return "";
  }
  return text;
}

function firstSafePrimaryValue(...values) {
  for (const value of values) {
    const safe = safePrimaryValue(value);
    if (safe) {
      return safe;
    }
  }
  return "";
}

function safeRangeText(low, high) {
  const safeLow = safePrimaryValue(low);
  const safeHigh = safePrimaryValue(high);
  return joinRange(safeLow, safeHigh);
}

function mappedActionLabel(value) {
  const mapped = firstMappedLabel(ACTION_LABELS, value);
  if (mapped) {
    return mapped;
  }
  const safe = safePrimaryValue(value);
  return safe || "-";
}

function firstMappedActionLabel(...values) {
  for (const value of values) {
    const label = mappedActionLabel(value);
    if (label !== "-") {
      return label;
    }
  }
  return "-";
}

function mappedActionStatusLabel(value) {
  const mapped = firstMappedLabel(ACTION_STATUS_LABELS, value);
  return mapped || "-";
}

function reportActionStatusLabel(action) {
  const actionText = firstMappedActionLabel(action.action, action.suggested_action);
  const statusText = mappedActionStatusLabel(action.status);
  if (actionText === "-" && statusText === "-") {
    return "-";
  }
  if (actionText === "-") {
    return statusText;
  }
  if (statusText === "-") {
    return actionText;
  }
  return `${actionText} · ${statusText}`;
}

function decisionSubline(holding) {
  const action = currentDecisionAction(holding);
  if (!sectionAvailable(action)) {
    const view = analystViewText(holding);
    return view === "-" ? "暂无触发动作，继续观察。" : `${view}，暂无触发动作，继续观察。`;
  }
  const trigger = decisionTriggerText(action);
  const reason = shortActionReason(action);
  const parts = [trigger, reason].filter((part) => part && part !== "-");
  if (!parts.length) {
    return "执行前保持人工确认。";
  }
  return `${parts.join("；")} 执行前保持人工确认。`;
}

function operationRows(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  return [
    ["动作", reportActionStatusLabel(action)],
    ["价格", firstSafePrimaryValue(action.limit_price, action.last_price, strategy.target_1, strategy.target_range)],
    ["仓位", firstSafePrimaryValue(action.suggested_quantity, action.suggested_notional, strategy.max_weight, strategy.target_weight)],
    ["止损", firstSafePrimaryValue(action.stop_price, strategy.stop_loss)],
  ];
}

function watchPointText(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  const direct = primaryChineseText(
    action.trigger_reason_zh,
    action.watch_trigger_zh,
    strategy.catalyst_zh,
    strategy.plan_text_zh,
    strategy.rationale_zh,
  );
  if (direct) {
    return compactSentence(direct, 92);
  }
  const mappedTrigger = firstMappedLabel(TRIGGER_STATUS_LABELS, action.trigger_status, action.watch_trigger);
  if (mappedTrigger && mappedTrigger !== "未触发") {
    const reviewText = nextReviewText(holding);
    const reviewSuffix = reviewText && reviewText !== "-"
      ? `继续观察 ${reviewText}。`
      : "执行前保持人工确认。";
    return compactSentence(`${mappedTrigger}；${reviewSuffix}`, 92);
  }
  const catalyst = firstSafePrimaryValue(strategy.catalyst, strategy.time_horizon, strategy.plan_text);
  if (catalyst) {
    return compactSentence(catalyst, 92);
  }
  return "暂无新的触发条件，继续观察。";
}

function decisionMetricCells(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  const target = safeRangeText(
    formatDisplayNumber(strategy.target_1),
    formatDisplayNumber(strategy.target_2),
  ) || formatDecisionTarget(strategy.target_range);
  return [
    ["观点", analystViewText(holding)],
    ["目标价", target],
    ["触发状态", decisionTriggerText(action)],
    ["动作状态", mappedActionStatusLabel(action.status)],
    ["下次复评", nextReviewText(holding)],
  ];
}

function analystViewText(holding) {
  const strategy = holding.strategy || {};
  const report = holding.agent_report || {};
  return firstMappedActionLabel(strategy.view, strategy.stance, strategy.signal, strategy.rating, report.rating, report.advice_action);
}

function nextReviewText(holding) {
  const strategy = holding.strategy || {};
  const action = currentDecisionAction(holding);
  const direct = primaryChineseText(strategy.catalyst_zh, strategy.time_horizon_zh, action.watch_trigger_zh);
  if (direct) {
    return compactSentence(direct, 32);
  }
  const text = firstSafePrimaryValue(strategy.catalyst, strategy.time_horizon, action.watch_trigger);
  return text ? compactSentence(text, 32) : "-";
}

function finalConclusionItems(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  const stopValue = firstSafePrimaryValue(action.stop_price, strategy.stop_loss);
  return [
    ["结论", finalConclusionText(holding)],
    ["理由", finalReasonText(holding)],
    ["条件", finalConditionText(holding)],
    ["失败条件", stopValue ? `跌破 ${stopValue} 后进入防守复核。` : "触发风险条件后进入人工复核。"],
  ].map(([label, text]) => ({ label, text: formatPlain(text) }));
}

function finalConclusionText(holding) {
  const action = currentDecisionAction(holding);
  const view = analystViewText(holding);
  const actionText = firstMappedActionLabel(action.action, action.suggested_action);
  if (actionText === "-" && view === "-") {
    return "暂无明确结论。";
  }
  if (actionText === "-") {
    return `${view}，但今天暂无触发动作。`;
  }
  if (view === "-") {
    return `${actionText}，执行前保持人工确认。`;
  }
  return `${view}，当前动作是${actionText}。`;
}

function finalReasonText(holding) {
  const action = currentDecisionAction(holding);
  const reason = primaryChineseText(
    action.trigger_reason_zh,
    action.reason_zh,
    action.agent_reason_zh,
    holding.strategy && holding.strategy.agent_reason_zh,
    holding.agent_report && holding.agent_report.summary_zh,
  );
  if (reason) {
    return compactSentence(reason, 82);
  }
  const mapped = firstMappedLabel(REASON_LABELS, action.trigger_reason, action.reason);
  return mapped || "理由见分析师对话。";
}

function finalConditionText(holding) {
  const strategy = holding.strategy || {};
  const action = currentDecisionAction(holding);
  const text = primaryChineseText(strategy.plan_text_zh, strategy.catalyst_zh, action.watch_trigger_zh);
  if (text) {
    return compactSentence(text, 82);
  }
  const trigger = firstMappedLabel(TRIGGER_STATUS_LABELS, action.watch_trigger, action.trigger_status);
  return trigger ? `${trigger} 后复核。` : "出现新的价格或事件触发后复核。";
}

function sourceReviewText(holding) {
  const report = holding.agent_report || {};
  const strategy = holding.strategy || {};
  const action = currentDecisionAction(holding);
  return uniqueSourceText(
    report.raw_decision,
    report.raw_report,
    report.full_report,
    report.summary,
    strategy.agent_excerpt,
    strategy.plan_text,
    strategy.rationale,
    strategy.agent_reason,
    strategy.notes,
    action.agent_excerpt,
    action.agent_reason,
    action.reason,
    action.trigger_reason,
    action.watch_trigger,
  );
}

function uniqueSourceText(...values) {
  const seen = new Set();
  const parts = [];
  for (const value of values) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (!text || seen.has(text)) {
      continue;
    }
    seen.add(text);
    parts.push(text);
  }
  return parts.join("\n");
}

function suggestedNotionalText(action) {
  if (hasValue(action.suggested_notional)) {
    const currency = formatPlain(action.notional_currency);
    return currency === "-" ? action.suggested_notional : `${action.suggested_notional} ${currency}`;
  }
  if (hasValue(action.order_value_hkd)) {
    return formatMoney(action.order_value_hkd, "HKD");
  }
  return "";
}

function renderBrokerDetailSection(details) {
  if (!Array.isArray(details) || details.length === 0) {
    return renderDetailSection("券商账户明细", renderStatusMessage("暂无券商账户明细"), "broker-detail-section");
  }
  const rows = details.map((detail) => {
    const pnlTone = pnlClass(detail.unrealized_pnl);
    return `
      <tr>
        <td>${escapeHtml(formatPlain(detail.broker))}</td>
        <td>${escapeHtml(formatPlain(detail.account_alias))}</td>
        <td class="number-cell">${escapeHtml(formatDisplayNumber(detail.quantity))}</td>
        <td class="number-cell">${escapeHtml(formatDisplayNumber(detail.cost_price))}</td>
        <td class="number-cell">${escapeHtml(formatDisplayNumber(detail.last_price))}</td>
        <td class="number-cell">${escapeHtml(formatDisplayNumber(detail.market_value))}</td>
        <td class="number-cell${pnlTone ? ` ${pnlTone}` : ""}">${escapeHtml(formatSignedPnl(detail.unrealized_pnl))}</td>
      </tr>
    `;
  }).join("");
  return renderDetailSection("券商账户明细", `
    <div class="compact-detail-table">
      <table>
        <thead>
          <tr>
            <th>券商</th>
            <th>账户</th>
            <th>数量</th>
            <th>成本价</th>
            <th>持仓价</th>
            <th>市值</th>
            <th>盈亏</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `, "broker-detail-section");
}

function renderDetailSection(title, body, extraClass = "") {
  const classes = ["detail-section", extraClass].filter(Boolean).join(" ");
  return `
    <section class="${escapeHtml(classes)}">
      <h3>${escapeHtml(title)}</h3>
      ${body}
    </section>
  `;
}

function renderStatusMessage(emptyText, section) {
  const error = section && hasValue(section.error)
    ? `<span class="detail-warning">${escapeHtml(section.error)}</span>`
    : "";
  return `<p class="compact-empty">${escapeHtml(emptyText)}${error}</p>`;
}

function renderStatusWarning(section) {
  if (!section || typeof section !== "object") {
    return "";
  }
  if (section.status === "manual_review") {
    return `<div class="detail-warning">需要人工复核${hasValue(section.error) ? `：${escapeHtml(section.error)}` : ""}</div>`;
  }
  if (section.status === "error") {
    return `<div class="detail-warning">${escapeHtml(formatPlain(section.error || "数据读取错误"))}</div>`;
  }
  return "";
}

function renderTerm(label, value) {
  if (!hasValue(value) || value === "-") {
    return "";
  }
  return renderRequiredTerm(label, value);
}

function renderRequiredTerm(label, value) {
  return `
    <div>
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(formatPlain(value))}</dd>
    </div>
  `;
}

function renderChineseTerm(label, value) {
  const text = chineseDisplayText(value);
  if (!hasValue(text) || text === "-") {
    return "";
  }
  return renderRequiredTerm(label, text);
}

function renderSafeChineseTerm(label, ...values) {
  const text = firstSafePrimaryValue(...values);
  if (!hasValue(text) || text === "-") {
    return "";
  }
  return renderRequiredTerm(label, text);
}

function chineseDisplayText(value) {
  const raw = formatPlain(value);
  if (raw === "-") {
    return raw;
  }
  const mapped = formatActionReason(formatTriggerStatus(formatActionStatus(formatAction(raw))));
  let text = mapped
    .replace(/\bOverweight\b/gi, "超配")
    .replace(/\bUnderweight\b/gi, "低配")
    .replace(/\bNeutral\b/gi, "中性")
    .replace(/\bHold\b/gi, "持有")
    .replace(/\bReduce\b/gi, "减仓")
    .replace(/\bTrim\b/gi, "减仓")
    .replace(/\bBuy\b/gi, "买入")
    .replace(/\bSell\b/gi, "卖出")
    .replace(/\bmonths\b/gi, "个月")
    .replace(/\bmonth\b/gi, "个月")
    .replace(/\breassess\b/gi, "复评")
    .replace(/\bearnings\b/gi, "财报");
  if (hasRawEnglishProse(text)) {
    return "";
  }
  return text;
}

function safeChineseDisplayText(value) {
  const text = chineseDisplayText(value);
  return hasValue(text) && text !== "-" ? text : "";
}

function safeChineseReason(action, strategy, report) {
  return primaryChineseText(
    action.reason_zh,
    action.agent_reason_zh,
    action.trigger_reason_zh,
    action.watch_trigger_zh,
    strategy.agent_reason_zh,
    strategy.rationale_zh,
    strategy.plan_text_zh,
    report.summary_zh,
    report.analysis_zh,
    report.report_zh,
  ) || firstMappedLabel(
    REASON_LABELS,
    action.reason,
    action.agent_reason,
    action.trigger_reason,
    action.watch_trigger,
    strategy.agent_reason,
    report.agent_reason,
  ) || firstMappedLabel(TRIGGER_STATUS_LABELS, action.trigger_status);
}

function hasRawEnglishProse(text) {
  const residual = String(text || "")
    .replace(/\b(?:HKD|USD|ETF|ETFs|MACD|RSI|YoY|QoQ|OpenAI|iPhone)\b/gi, "");
  const words = residual.match(/\b[A-Za-z][A-Za-z'-]{2,}\b/g) || [];
  return words.length >= 2;
}

function dataHealthText(holding) {
  const confidence = formatPlain(holding.confidence);
  const riskFlag = formatPlain(holding.risk_flag);
  if (confidence !== "-" && riskFlag !== "-") {
    return `${confidence} · ${riskFlag}`;
  }
  if (confidence !== "-") {
    return confidence;
  }
  return riskFlag;
}

function joinRange(min, max) {
  if (hasValue(min) && hasValue(max)) {
    return `${min} - ${max}`;
  }
  if (hasValue(min)) {
    return `>= ${min}`;
  }
  if (hasValue(max)) {
    return `<= ${max}`;
  }
  return "";
}

function sectionAvailable(section) {
  if (!section || typeof section !== "object") {
    return false;
  }
  if (section.available === false) {
    return false;
  }
  return section.available === true || Object.keys(section).some((key) => key !== "available" && key !== "error" && hasValue(section[key]));
}

function firstValue(source, keys) {
  if (!source || typeof source !== "object") {
    return "";
  }
  for (const key of keys) {
    if (hasValue(source[key])) {
      return source[key];
    }
  }
  return "";
}

function firstAvailableText(...values) {
  for (const value of values) {
    if (hasValue(value)) {
      return value;
    }
  }
  return "";
}

function sortedTradeActions(actions) {
  return [...actions].sort((left, right) => {
    const statusDelta = actionStatusRank(left.status) - actionStatusRank(right.status);
    if (statusDelta !== 0) {
      return statusDelta;
    }
    const priorityDelta = priorityRank(left.priority) - priorityRank(right.priority);
    if (priorityDelta !== 0) {
      return priorityDelta;
    }
    return `${left.market || ""}.${left.symbol || ""}`.localeCompare(`${right.market || ""}.${right.symbol || ""}`);
  });
}

function actionStatusRank(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "ready") {
    return 0;
  }
  if (normalized === "review") {
    return 1;
  }
  if (normalized === "watch") {
    return 2;
  }
  return 3;
}

function priorityRank(priority) {
  const normalized = String(priority || "").trim().toLowerCase();
  const ranks = { critical: 0, high: 1, medium: 2, low: 3 };
  return Object.prototype.hasOwnProperty.call(ranks, normalized) ? ranks[normalized] : 4;
}

function tradeActionCounts(actions) {
  const counts = { ready: 0, review: 0, watch: 0 };
  for (const action of actions) {
    const status = String(action.status || "").trim().toLowerCase();
    if (status === "ready") {
      counts.ready += 1;
    } else if (status === "review") {
      counts.review += 1;
    } else if (status === "watch") {
      counts.watch += 1;
    }
  }
  return counts;
}

function actionDetailKey(action) {
  if (!action) {
    return "";
  }
  return normalizeActionKey("", action.futu_symbol)
    || normalizeActionKey(action.market, action.symbol);
}

function holdingActionKeys(holding) {
  const keys = [
    normalizeActionKey("", holding && holding.futu_symbol),
    normalizeActionKey(holding && holding.market, holding && holding.symbol),
    actionDetailKey(holding && holding.trade_action),
    actionDetailKey(holding && holding.premarket_action),
  ].filter(Boolean);
  return Array.from(new Set(keys));
}

function normalizeActionKey(market, symbol) {
  let normalizedMarket = String(market || "").trim().toUpperCase();
  let normalizedSymbol = String(symbol || "").trim().toUpperCase();
  if (!normalizedMarket && normalizedSymbol.includes(".")) {
    const parts = normalizedSymbol.split(".");
    normalizedMarket = parts.shift() || "";
    normalizedSymbol = parts.join(".");
  }
  if (!normalizedMarket || !normalizedSymbol) {
    return "";
  }
  if (normalizedMarket === "HK" && /^\d+$/.test(normalizedSymbol)) {
    normalizedSymbol = normalizedSymbol.padStart(5, "0");
  }
  return `${normalizedMarket}.${normalizedSymbol}`;
}

function actionSymbol(action) {
  const futu = formatPlain(action.futu_symbol);
  if (futu !== "-") {
    return futu;
  }
  const market = formatPlain(action.market);
  const symbol = formatPlain(action.symbol);
  if (market === "-" && symbol === "-") {
    return "-";
  }
  if (market === "-") {
    return symbol;
  }
  if (symbol === "-") {
    return market;
  }
  return `${market}.${symbol}`;
}

function actionSourceContext(action) {
  const parts = [
    formatTriggerStatus(action.trigger_status),
    formatPriority(action.priority),
  ].filter((part) => part && part !== "-");
  return parts.join(" · ") || "交易计划触发";
}

function shortActionReason(action) {
  const translatedReason = primaryChineseText(
    action.trigger_reason_zh,
    action.reason_zh,
    action.agent_reason_zh,
    action.watch_trigger_zh,
  );
  if (translatedReason) {
    return compactSentence(translatedReason, 96);
  }

  const mappedReason = firstMappedLabel(
    REASON_LABELS,
    action.trigger_reason,
    action.reason,
    action.agent_reason,
    action.rationale,
    action.watch_trigger,
  );
  if (mappedReason) {
    return compactSentence(mappedReason, 96);
  }

  const mappedTrigger = firstMappedLabel(TRIGGER_STATUS_LABELS, action.trigger_status, action.watch_trigger);
  if (mappedTrigger && mappedTrigger !== "未触发") {
    return compactSentence(`${mappedTrigger}，请查看完整策略。`, 96);
  }

  return fallbackShortActionReason(action);
}

function firstChineseText(...values) {
  for (const value of values) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (text && /[\u3400-\u9fff]/.test(text)) {
      return text;
    }
  }
  return "";
}

function firstMappedLabel(map, ...values) {
  for (const value of values) {
    const mapped = mappedLabel(map, value);
    if (mapped) {
      return mapped;
    }
  }
  return "";
}

function mappedLabel(map, value) {
  const raw = formatPlain(value);
  if (raw === "-") {
    return "";
  }
  return map[raw] || map[raw.toLowerCase()] || "";
}

function fallbackShortActionReason(action) {
  const status = String(action.status || "").trim().toLowerCase();
  const actionType = String(action.action || action.suggested_action || "").trim().toLowerCase();
  const trigger = String(action.trigger_status || action.watch_trigger || "").trim().toLowerCase();
  if (status === "review" || actionType === "review" || status === "error" || trigger === "missing_quote") {
    return "需要人工复核后再决定。";
  }
  if (status === "watch" || actionType === "hold" || actionType === "watch" || trigger === "watch" || trigger === "no_trigger") {
    return "暂无触发中的交易计划。";
  }
  return "交易计划已触发，请查看完整策略。";
}

function compactSentence(text, maxLength) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) {
    return "";
  }
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
}

function actionNotionalText(action) {
  if (hasValue(action.suggested_notional)) {
    const currency = formatPlain(action.notional_currency);
    const notional = formatDisplayNumber(action.suggested_notional);
    return currency === "-" ? notional : `${currency} ${notional}`;
  }
  if (hasValue(action.order_value_hkd)) {
    return formatMoney(action.order_value_hkd, "HKD");
  }
  return "-";
}

function safeActionNotionalText(action) {
  const notional = safePrimaryValue(action.suggested_notional);
  if (notional) {
    const currency = safePrimaryValue(action.notional_currency);
    const formattedNotional = formatDisplayNumber(notional);
    return currency ? `${currency} ${formattedNotional}` : formattedNotional;
  }
  const orderValueHkd = safePrimaryValue(action.order_value_hkd);
  if (orderValueHkd) {
    return formatMoney(orderValueHkd, "HKD");
  }
  return "";
}

function actionCardStatusLabel(action) {
  const actionText = formatAction(action.action || action.suggested_action);
  const statusText = formatActionStatus(action.status);
  if (actionText === "-" && statusText === "-") {
    return "-";
  }
  if (actionText === "-") {
    return statusText;
  }
  if (statusText === "-") {
    return actionText;
  }
  return `${actionText} · ${statusText}`;
}

function rationaleSource(holding) {
  const action = sectionAvailable(holding.trade_action) ? holding.trade_action : (holding.premarket_action || {});
  const strategy = holding.strategy || {};
  const report = holding.agent_report || {};
  return firstAvailableText(
    action.agent_reason_zh,
    action.reason_zh,
    action.trigger_reason_zh,
    action.agent_excerpt_zh,
    strategy.plan_text_zh,
    strategy.rationale_zh,
    strategy.agent_reason_zh,
    strategy.agent_excerpt_zh,
    report.summary_zh,
    report.report_zh,
    report.analysis_zh,
    action.agent_reason,
    action.reason,
    action.trigger_reason,
    action.agent_excerpt,
    strategy.plan_text,
    strategy.rationale,
    strategy.agent_reason,
    strategy.agent_excerpt,
    report.summary,
    report.raw_decision,
  );
}

function rationaleRows(text) {
  const sentences = splitRationaleText(text);
  const rows = sentences.map((sentence, index) => ({
    label: rationaleLabel(sentence, index, sentences.length),
    text: sentence,
  }));
  if (rows.length === 0) {
    return [];
  }
  return rows.slice(0, 8);
}

function splitRationaleText(text) {
  const raw = String(text || "").trim();
  if (!raw) {
    return [];
  }
  const lineParts = raw
    .split(/\r?\n+/)
    .map((part) => part.trim())
    .filter(Boolean);
  const sourceParts = lineParts.length > 1 ? lineParts : splitOnSentenceEnd(raw);
  const parts = sourceParts
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => cleanListMarker(part))
    .filter(Boolean);
  const rows = [];
  let buffer = "";
  for (const part of parts) {
    const candidate = buffer ? `${buffer} ${part}` : part;
    if (candidate.length <= 120) {
      buffer = candidate;
    } else {
      if (buffer) {
        rows.push(buffer);
      }
      buffer = part;
    }
  }
  if (buffer) {
    rows.push(buffer);
  }
  return rows;
}

function splitOnSentenceEnd(text) {
  const parts = [];
  let buffer = "";
  for (const character of String(text || "")) {
    buffer += character;
    if ("。！？!?".includes(character)) {
      parts.push(buffer.trim());
      buffer = "";
    }
  }
  if (buffer.trim()) {
    parts.push(buffer.trim());
  }
  return parts;
}

function cleanListMarker(text) {
  return String(text || "")
    .replace(/^\s*(?:[-*•]\s+|\d{1,3}(?:[.)]\s+|、\s*))/, "")
    .trim();
}

function rationaleLabel(text, index, total) {
  const lower = String(text || "").toLowerCase();
  if (/(macd|rsi|趋势|反弹|突破|阻力|支撑|均线|technical|trend|momentum)/i.test(lower)) {
    return "趋势派";
  }
  if (/(风险|止损|回撤|衰减|升水|仓位|风控|risk|stop|drawdown|decay|contango|position)/i.test(lower)) {
    return "风控派";
  }
  if (/(宏观|政策|财报|油|利率|伊朗|地缘|事件|macro|policy|earnings|oil|rate|geopolitical)/i.test(lower)) {
    return "事件派";
  }
  if (index === total - 1 || /(结论|因此|所以|减仓|买入|卖出|持有|配置|action|trim|buy|sell|hold|allocation)/i.test(lower)) {
    return "组合结论";
  }
  return `依据${index + 1}`;
}

function renderTradeActions() {
  if (!elements["action-count"] || !elements["trade-actions"]) {
    return;
  }
  const actions = sortedTradeActions((state.dashboard && state.dashboard.trade_actions) || []);
  const counts = tradeActionCounts(actions);
  const pendingCount = counts.ready + counts.review;
  elements["action-count"].textContent = `${pendingCount} 待处理`;
  if (!actions.length) {
    elements["trade-actions"].innerHTML = `<div class="empty-state">暂无交易动作</div>`;
    return;
  }
  elements["trade-actions"].innerHTML = `
    ${renderActionQueueSummary(counts)}
    <div class="action-card-list">
      ${actions.map(renderActionCard).join("")}
    </div>
  `;
}

function renderActionQueueSummary(counts) {
  return `
    <div class="action-summary-grid" role="group" aria-label="交易动作摘要">
      <div><span>待确认</span><strong>${escapeHtml(String(counts.ready))}</strong></div>
      <div><span>复核</span><strong>${escapeHtml(String(counts.review))}</strong></div>
      <div><span>观察</span><strong>${escapeHtml(String(counts.watch))}</strong></div>
    </div>
  `;
}

function renderActionCard(action) {
  const key = actionDetailKey(action);
  const status = String(action.status || "").toLowerCase();
  const statusClass = status === "review" ? "review" : status === "ready" ? "ready" : "watch";
  return `
    <article class="action-card ${statusClass}">
      <div class="action-card-header">
        <div>
          <strong>${escapeHtml(actionSymbol(action))}</strong>
          <span>${escapeHtml(actionSourceContext(action))}</span>
        </div>
        <span class="badge">${escapeHtml(actionCardStatusLabel(action))}</span>
      </div>
      <div class="action-card-metrics">
        <div><span>限价</span><strong>${escapeHtml(formatDisplayNumber(firstPresent(action.limit_price, action.last_price)))}</strong></div>
        <div><span>数量</span><strong>${escapeHtml(formatDisplayNumber(action.suggested_quantity))}</strong></div>
        <div><span>金额</span><strong>${escapeHtml(actionNotionalText(action))}</strong></div>
      </div>
      <div class="action-card-reason">
        <span>短触发理由</span>
        <p>${escapeHtml(shortActionReason(action))}</p>
      </div>
      <button class="raw-toggle action-detail-button" type="button" data-action-detail="${escapeHtml(key)}">查看完整策略</button>
    </article>
  `;
}

function renderConnectionPanel() {
  const snapshot = state.accountSnapshot || {};
  const quoteSource = snapshot.sources?.quotes || {};
  const quoteStatus = String(quoteSource.status || "unknown").trim().toLowerCase();
  const quoteHealthy = ["healthy", "ok", "fresh"].includes(quoteStatus);
  const quoteAsOf = firstPresent(snapshot.quote_as_of, quoteSource.as_of, "-");
  const transport = state.accountError ? "账户快照请求失败" : "账户快照请求正常";
  const quoteLabel = quoteHealthy ? "行情正常" : quoteStatus === "stale" ? "行情已过期" : "行情不可用";
  setElementText(
    "connection-status",
    snapshot.status === "healthy" && !state.accountError && quoteHealthy ? "账户与行情正常" : "账户或行情不可用",
  );
  setElementText("connection-success", quoteAsOf);
  setElementText("connection-poll", `${transport} · ${quoteLabel}`);
  setElementText(
    "connection-task",
    `${accountSnapshotStatusText()} · 最近接受快照 ${formatPlain(snapshot.generated_at || "-")}`,
  );
}

function renderLoadError(error) {
  state.dashboard = null;
  state.dashboardError = error;
  renderHeaderSummary();
  renderSourceStatusListIntoHeader();
  renderKellyLab();
  renderHoldings();
}

function setElementText(id, text) {
  if (elements[id]) {
    elements[id].textContent = text;
  }
}

function renderDashboardErrorState() {
  const container = elements["account-holdings"] || elements["holdings-body"];
  setAccountHoldingsFallbackLabel("账户持仓不可用");
  container.innerHTML = '<div class="empty-state">看板数据加载失败</div>';
}

function filteredHoldings() {
  return getHoldings().filter((holding) => {
    const market = String(holding.market || "").toUpperCase();
    const brokers = rowBrokers(holding);
    const marketMatches = state.marketFilter === "ALL" || market === state.marketFilter;
    const brokerMatches = brokers.includes(state.brokerFilter);
    return marketMatches && brokerMatches;
  });
}

function renderUsdMarketValue(holding) {
  const currency = String(holding && holding.currency || "").trim().toUpperCase();
  if (currency !== "USD") {
    return "-";
  }
  return formatMoney(holding.market_value, "USD");
}

function getHoldings() {
  return (state.dashboard && Array.isArray(state.dashboard.holding_enrichment))
    ? state.dashboard.holding_enrichment
    : [];
}

function accountHoldingGroups() {
  const legacyHoldings = Array.isArray(state.dashboard?.holding_enrichment) ? state.dashboard.holding_enrichment : [];
  const groups = Object.entries(ACCOUNT_STRATEGY_PROFILES).map(([broker, profile]) => {
    const summary = brokerSummaries().find((item) => brokerKey(item) === broker) || {broker};
    const rows = (Array.isArray(state.accountSnapshot?.positions)
      ? state.accountSnapshot.positions : [])
      .filter((position) => brokerKey(position) === broker && isAccountHoldingPosition(position))
      .map((position, index) => {
        const matches = position.instrument_id
          ? legacyHoldings.filter((holding) => holding.instrument_id === position.instrument_id)
          : [];
        const enrichment = matches.length === 1 ? matches[0] : {};
        const holding = {
          ...enrichment, ...position, brokers: broker,
          enrichment_status: matches.length === 1 ? "" : "unavailable",
        };
        return {
          key: String(position.position_id || ""), broker, holding,
          display: accountPositionDisplay(position), index,
        };
      });
    return {broker, profile, summary, rows};
  });
  return groups;
}

function numericValue(value) {
  if (!hasValue(value)) {
    return null;
  }
  const raw = String(value).trim();
  const validNumber = raw.includes(",")
    ? /^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$/.test(raw)
    : /^[+-]?(?:\d+|\d*\.\d+)$/.test(raw);
  if (!validNumber) {
    return null;
  }
  const parsed = Number(raw.replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}


function brokerSummaries() {
  return (state.accountSnapshot && Array.isArray(state.accountSnapshot.broker_summaries))
    ? state.accountSnapshot.broker_summaries
    : [];
}

function brokerSyncStatus(broker) {
  const source = state.accountSnapshot?.sources?.account?.brokers?.[broker];
  const rawStatus = String(source?.status || "unknown").trim().toLowerCase();
  const status = rawStatus === "healthy" ? "ok" : rawStatus;
  const dataAsOf = formatPlain(source?.data_as_of || source?.as_of);
  const fallback = status === "ok" ? "同步正常"
    : status === "failed" ? `同步失败${dataAsOf !== "-" ? ` · 数据截至 ${dataAsOf}` : ""}`
    : status === "stale" ? `数据已过期${dataAsOf !== "-" ? ` · 数据截至 ${dataAsOf}` : ""}`
    : "同步状态未知 · 数据未验证";
  return {
    status,
    display: formatPlain(source?.display || fallback),
    unsafe: !accountActionsEnabled() || ["failed", "stale", "unknown"].includes(status),
  };
}

function brokerSourceTime(broker, source) {
  const live = ["futu", "tiger"].includes(broker);
  const raw = String(firstPresent(
    source?.data_as_of,
    live ? source?.last_success_at : "",
  ) || "");
  if (live) {
    const match = raw.match(/(?:T|\s|^)(\d{2}:\d{2})(?::\d{2})?/);
    return match ? match[1] : "";
  }
  const match = raw.match(/\b\d{4}-(\d{2}-\d{2})\b/);
  return match ? match[1] : "";
}

function brokerSourceStatus(broker) {
  const sync = brokerSyncStatus(broker);
  const source = state.accountSnapshot?.sources?.account?.brokers?.[broker] || {};
  const live = ["futu", "tiger"].includes(broker);
  const time = brokerSourceTime(broker, source);
  const suffix = time ? ` · ${time}` : "";
  const display = sync.status === "ok"
    ? (live ? `同步正常${suffix}` : (time ? `数据截至${suffix}` : "同步正常"))
    : sync.status === "failed"
      ? `同步失败${time ? ` · ${live ? "上次 " : "数据截至 "}${time}` : ""}`
      : sync.status === "stale"
        ? `数据已过期${time ? ` · 截至 ${time}` : ""}`
        : "同步状态未知 · 数据未验证";
  return {...sync, display};
}

function renderBrokerCards() {
  elements["broker-summary-cards"].innerHTML = renderBrokerSummaryCards();
}

function renderBrokerSummaryCards() {
  const summaries = brokerSummaries();
  return ACCOUNT_BROKERS.map((broker) => {
    const summary = summaries.find((item) => brokerKey(item) === broker) || {broker};
    const profile = ACCOUNT_STRATEGY_PROFILES[broker] || {horizon: "-", strategy: "-"};
    return `<button class="broker-summary-card ${escapeHtml(sourceStatusClass(brokerSyncStatus(broker).status))}" type="button" data-broker="${escapeHtml(broker)}">
      <span class="summary-label">${escapeHtml(brokerDisplayName(summary))}</span>
      <span class="account-horizon-label">${escapeHtml(profile.horizon)} · ${escapeHtml(profile.strategy)}</span>
      <span class="broker-account-alias">账户 ${escapeHtml(formatPlain(brokerAccountAlias(broker, summary)))}</span>
      <strong>${escapeHtml(formatMoney(summary.portfolio_value_hkd, "HKD"))}</strong>
      <span class="summary-note">持仓 ${escapeHtml(formatDisplayNumber(summary.holding_count))} · ${escapeHtml(brokerSummarySourceText(summary))}</span>
    </button>`;
  }).join("");
}

function brokerAccountAlias(broker, summary = {}) {
  const cash = getCashRows().find((row) => brokerKey(row) === broker) || {};
  const position = (state.accountSnapshot?.positions || []).find((row) => brokerKey(row) === broker) || {};
  return firstPresent(summary.account_alias, summary.accounts, cash.account_alias, cash.accounts, position.account_alias,
    position.accounts, "-");
}

function brokerSummarySourceText(summary) {
  return brokerSyncStatus(brokerKey(summary)).display;
}

function renderSourceStatusListIntoHeader() {
  elements["source-status-list"].innerHTML = renderSourceStatusList();
}

function renderSourceStatusList() {
  return ACCOUNT_SOURCE_GROUPS.map((group) => `
    <div class="source-status-group">${escapeHtml(group.label)}</div>
    ${group.brokers.map((broker) => {
      const sync = brokerSourceStatus(broker);
      return `
        <div class="source-status-row ${escapeHtml(sourceStatusClass(sync.status))}" data-broker="${escapeHtml(broker)}">
          <strong>${escapeHtml(brokerDisplayName(broker))}账户</strong>
          <span>${escapeHtml(sync.display)}</span>
        </div>
      `;
    }).join("")}
  `).join("");
}

function sourceStatusClass(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "ok" || normalized === "healthy" || normalized === "real_time" || normalized === "fresh") {
    return "status-ok";
  }
  if (normalized === "non_realtime" || normalized === "statement") {
    return "status-partial";
  }
  if (normalized === "stale") {
    return "status-stale";
  }
  if (normalized === "missing" || normalized === "failed" || normalized === "error") {
    return "status-failed";
  }
  return "status-muted";
}

function getCashRows() {
  return (state.accountSnapshot && Array.isArray(state.accountSnapshot.cash_balances))
    ? state.accountSnapshot.cash_balances
    : [];
}

function accountActionsEnabled() {
  return Boolean(state.accountSnapshot)
    && !state.accountError
    && state.accountSnapshot.status === "healthy"
    && !state.accountSnapshot.stale;
}

function accountSnapshotStatusText() {
  if (state.accountError) return "账户快照不可用，已冻结上次数据";
  if (!state.accountSnapshot) return "账户快照不可用";
  if (!accountActionsEnabled()) return "账户快照已过期，操作已禁用";
  return "账户快照正常";
}

function sourceKindText(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "live_account" || normalized === "real_time" || normalized === "ok" || normalized === "fresh") {
    return "实时";
  }
  if (normalized === "quote_and_live_account") {
    return "行情与账户";
  }
  if (normalized === "statement" || normalized === "non_realtime") {
    return "非实时";
  }
  if (normalized === "missing") {
    return "暂无数据";
  }
  return formatPlain(value);
}

function rowBrokers(row) {
  if (!row || typeof row !== "object") {
    return [];
  }
  const brokers = splitList(row.brokers);
  const broker = brokerKey(row);
  if (broker && !brokers.includes(broker)) {
    brokers.push(broker);
  }
  return brokers;
}

function brokerKey(value) {
  const raw = typeof value === "object" && value !== null
    ? firstPresent(value.broker, value.broker_key)
    : value;
  const normalized = String(raw || "").trim().toLowerCase();
  if (normalized === "phillip") {
    return "phillips";
  }
  return normalized;
}

function brokerDisplayName(value) {
  if (typeof value === "object" && value !== null) {
    const label = firstPresent(value.label, value.display_name);
    if (hasValue(label)) {
      return label;
    }
  }
  const key = brokerKey(value);
  const labels = {
    eastmoney: "东方财富",
    futu: "富途",
    tiger: "老虎",
    phillips: "辉立",
  };
  return labels[key] || formatPlain(value);
}

function quoteNotApplicable(holding) {
  const market = String(holding.market || "").toUpperCase();
  const assetClass = String(holding.asset_class || "").toLowerCase();
  return market === "CASH" || assetClass === "cash" || assetClass === "money_market_fund";
}

function isAccountHoldingPosition(position) {
  const quantity = numericValue(firstPresent(position.total_quantity, position.quantity));
  return !quoteNotApplicable(position) && quantity !== 0;
}

function detailLivePrice(holding, quote) {
  if (quoteNotApplicable(holding)) {
    return "-";
  }
  return quote && hasValue(quote.last_price) ? quote.last_price : "缺行情";
}

function renderQuotePrice(holding, quote) {
  if (quoteNotApplicable(holding)) {
    return escapeHtml("-");
  }
  if (!quote || !hasValue(quote.last_price)) {
    return `<span class="missing-text">缺行情</span>`;
  }
  const sessionKey = String(quote.price_session || "");
  const session = String(holding && holding.market || "").toUpperCase() === "US"
    ? sessionQuoteLabel(sessionKey) : "";
  if (!session) return escapeHtml(formatDisplayNumber(quote.last_price));
  const detail = quote.current_session_quote
    ? quoteTimeEt(quote.price_time)
    : "上一有效价";
  return `<span class="session-quote"><span class="session-quote-label" data-session="${escapeHtml(sessionKey)}">${escapeHtml(session)}</span><strong class="session-quote-price">${escapeHtml(formatDisplayNumber(quote.last_price))}</strong>${detail ? `<span class="session-quote-time">· ${escapeHtml(detail)}</span>` : ""}</span>`;
}

function renderAccountHoldingPrice(display) {
  if (!hasValue(display?.last_price)) return '<span class="missing-text">缺行情</span>';
  const kind = String(display.price_kind || "");
  const label = ({overnight: "夜盘", pre_market: "盘前", live: "盘中", after_hours: "盘后", statement: "结单", account_snapshot: "账户快照"})[kind] || "";
  const isUs = String(display.market || "").toUpperCase() === "US";
  if (!label || (!isUs && kind !== "statement" && kind !== "account_snapshot")) {
    return escapeHtml(formatDisplayNumber(display.last_price));
  }
  const detail = display.price_as_of ? `· ${escapeHtml(quoteTimeEt(display.price_as_of))}` : "";
  return `<span class="session-quote ${kind === "statement" ? "statement-quote" : ""}"><span class="session-quote-label" data-session="${escapeHtml(kind)}">${escapeHtml(label)}</span><strong class="session-quote-price">${escapeHtml(formatDisplayNumber(display.last_price))}</strong>${detail ? `<span class="session-quote-time">${detail}</span>` : ""}</span>`;
}

function sessionQuoteLabel(value) {
  return ({overnight: "夜盘", pre_market: "盘前", regular: "盘中", after_hours: "盘后"})[value] || "";
}

function quoteTimeEt(value) {
  const match = String(value || "").match(/\b\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})/);
  return match ? `${match[1]} ET` : "";
}

function renderActionBadge(action, status) {
  const actionText = formatAction(action);
  const statusText = formatActionStatus(status);
  if (actionText === "-" && statusText === "-") {
    return `<span class="badge">-</span>`;
  }
  return `<span class="badge">${escapeHtml(actionText)}${statusText !== "-" ? ` · ${escapeHtml(statusText)}` : ""}</span>`;
}

function quoteStatusLabel(status) {
  if (status === "ok") {
    return "行情正常";
  }
  if (status === "partial") {
    return "部分缺行情";
  }
  if (status === "failed") {
    return "刷新失败";
  }
  return "等待行情";
}

function quoteStatusClass(status) {
  if (status === "ok") {
    return "status-ok";
  }
  if (status === "partial") {
    return "status-partial";
  }
  if (status === "failed") {
    return "status-failed";
  }
  return "status-muted";
}

function formatDiagnostic(payload) {
  if (!payload || !payload.status) {
    return "-";
  }
  const diagnostic = payload.diagnostic || {};
  if (diagnostic.reason) {
    return formatDiagnosticMessage(diagnostic.reason);
  }
  if (diagnostic.message) {
    return formatDiagnosticMessage(diagnostic.message);
  }
  if (diagnostic.next_step) {
    return formatDiagnosticMessage(diagnostic.next_step);
  }
  return quoteStatusLabel(payload.status);
}

function formatAction(action) {
  return labelFromMap(ACTION_LABELS, action);
}

function formatActionStatus(status) {
  return labelFromMap(ACTION_STATUS_LABELS, status);
}

function formatPriority(priority) {
  return labelFromMap(PRIORITY_LABELS, priority);
}

function formatTriggerStatus(status) {
  return labelFromMap(TRIGGER_STATUS_LABELS, status);
}

function formatActionReason(reason) {
  return labelFromMap(REASON_LABELS, reason);
}

function formatDiagnosticMessage(message) {
  return labelFromMap(REASON_LABELS, message);
}

function labelFromMap(map, value) {
  const raw = formatPlain(value);
  if (raw === "-") {
    return raw;
  }
  return map[raw] || map[raw.toLowerCase()] || raw;
}

function setActiveFilter(container, activeButton) {
  container.querySelectorAll(".filter-button").forEach((button) => {
    button.classList.toggle("active", button === activeButton);
  });
}

function splitList(value) {
  return String(value || "")
    .split(";")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatDisplayNumber(value) {
  const raw = formatPlain(value).trim();
  const match = raw.match(/^([+-]?)(\d+)(?:\.(\d+))?$/);
  if (!match) return raw;
  const [, sign, rawInteger, rawFraction = ""] = match;
  const rounded = (BigInt(`${rawInteger}${rawFraction.slice(0, 2).padEnd(2, "0")}`)
    + ((rawFraction[2] || "0") >= "5" ? 1n : 0n)).toString().padStart(rawInteger.length + 2, "0");
  const integer = rounded.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = rounded.slice(-2).replace(/0+$/, "");
  return `${sign}${integer}${fraction ? `.${fraction}` : ""}`;
}

function formatDecisionTarget(value) {
  const raw = safePrimaryValue(value);
  const range = raw.match(/^([+-]?\d+(?:\.\d+)?)(\s+-\s+)([+-]?\d+(?:\.\d+)?)$/);
  if (range) return `${formatDisplayNumber(range[1])}${range[2]}${formatDisplayNumber(range[3])}`;
  const threshold = raw.match(/^([<>]=?\s*)([+-]?\d+(?:\.\d+)?)$/);
  if (threshold) return `${threshold[1]}${formatDisplayNumber(threshold[2])}`;
  return formatDisplayNumber(raw);
}

function formatMoney(value, currency) {
  if (!hasValue(value)) return "-";
  return `${currency} ${formatDisplayNumber(value)}`;
}

function formatSignedPnl(value) {
  const raw = formatPlain(value).trim();
  const suffix = raw.endsWith("%") ? "%" : "";
  const numberText = suffix ? raw.slice(0, -1) : raw;
  const number = numericValue(numberText);
  if (number === null) return raw;
  const digits = formatDisplayNumber(numberText.replace(/^[+-]/, "").replace(/,/g, ""));
  const sign = number > 0 ? "+" : number < 0 ? "-" : "";
  return `${sign}${digits}${suffix}`;
}

function formatSignedMoney(value, currency) {
  return hasValue(value) ? `${currency} ${formatSignedPnl(value)}` : "-";
}

function pnlClass(value) {
  const number = numericValue(String(value || "").replace("%", ""));
  return number > 0 ? "pnl-profit" : number < 0 ? "pnl-loss" : "";
}

function formatPlain(value) {
  return hasValue(value) ? String(value) : "-";
}

function hasValue(value) {
  return value !== null && value !== undefined && String(value).trim() !== "";
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
