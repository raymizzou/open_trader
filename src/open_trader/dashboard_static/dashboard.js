"use strict";

const state = {
  dashboard: null,
  dashboardError: null,
  quotes: {},
  quotePayload: null,
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
  refreshActive: false,
  quoteIntervalId: null,
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
};

const elements = {};

const WORKSPACE_VIEWS = new Set(["portfolio", "kelly_lab", "standard_backtest", "trend_report"]);

const ACCOUNT_STRATEGY_PROFILES = {
  futu: {horizon: "期权增强", strategy: "跨市场期权关注"},
  tiger: {horizon: "趋势", strategy: "美股趋势交易"},
  phillips: {horizon: "趋势", strategy: "港股趋势交易"},
  eastmoney: {horizon: "偏短线", strategy: "趋势交易"},
};

const ACCOUNT_BROKERS = Object.keys(ACCOUNT_STRATEGY_PROFILES);
const TREND_ACCOUNT_BROKERS = ["tiger", "phillips", "eastmoney"];
const ACCOUNT_VIEW_KEYS = ["real", "simulate", "report", "review"];

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
  refreshQuotes();
});

function bindElements() {
  [
    "quote-status",
    "last-refresh",
    "refresh-quotes",
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
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    window.matchMedia("(max-width: 760px)").addEventListener?.(
      "change",
      syncCnTrendBuyAccessibility,
    );
  }
  elements["refresh-quotes"].addEventListener("click", refreshQuotes);
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
  elements["account-holdings"].addEventListener("keydown", handleAccountViewTabKeydown);
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
    if (event.target.closest("[data-close-trend-report]")) returnToPortfolio();
  });
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
  const universe = (options.universe && options.universe[backtest.source]) || [];
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
  return Number.isFinite(number) ? `${number * 100}%` : fallback;
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
    scheduleQuotePolling(state.dashboard.poll_seconds);
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

function scheduleQuotePolling(pollSeconds) {
  if (state.quoteIntervalId !== null) {
    window.clearInterval(state.quoteIntervalId);
    state.quoteIntervalId = null;
  }

  const seconds = Number(pollSeconds);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    setElementText("connection-poll", "-");
    return;
  }

  const intervalMs = Math.max(1000, seconds * 1000);
  setElementText("connection-poll", `${intervalMs / 1000} 秒`);
  state.quoteIntervalId = window.setInterval(refreshQuotes, intervalMs);
}

async function refreshQuotes() {
  if (state.refreshActive) {
    return;
  }
  state.refreshActive = true;
  elements["refresh-quotes"].disabled = true;
  elements["refresh-quotes"].textContent = "刷新中";
  try {
    const response = await fetch("/api/quotes", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`quotes ${response.status}`);
    }
    const payload = await response.json();
    state.quotePayload = payload;
    state.quotes = payload.quotes || {};
    if (accountSyncReloadNeeded(payload.account_sync)) {
      await loadDashboard();
    }
    renderQuoteStatus(payload);
    renderHoldings();
  } catch (error) {
    state.quotePayload = {
      status: "failed",
      stale: true,
      last_success_at: "",
      diagnostic: { message: error.message },
      quotes: state.quotes,
    };
    renderQuoteStatus(state.quotePayload);
    renderHoldings();
  } finally {
    state.refreshActive = false;
    elements["refresh-quotes"].disabled = false;
    elements["refresh-quotes"].textContent = "刷新账户与行情";
  }
}

function accountSyncReloadNeeded(accountSync) {
  if (!accountSync || typeof accountSync !== "object") {
    return false;
  }
  const status = String(accountSync.status || "").trim();
  return Boolean(status) && status !== "skipped";
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
}

function renderWorkspaceChrome() {
  const view = state.workspaceView;
  const toolView = view !== "portfolio";
  elements["dashboard-shell"].classList.toggle("tool-workspace-view", toolView);
  elements["return-to-portfolio"].hidden = !toolView;
  elements["return-to-portfolio"].classList.toggle("hidden", !toolView);
  elements["workspace-grid"].classList.toggle("hidden", view === "standard_backtest" || view === "trend_report");
  elements["holdings-panel"].classList.toggle("hidden", view !== "portfolio");
  elements["kelly-lab-panel"].classList.toggle("hidden", view !== "kelly_lab");
  elements["standard-backtest-workspace"].hidden = view !== "standard_backtest";
  elements["standard-backtest-workspace"].classList.toggle("hidden", view !== "standard_backtest");
  elements["trend-report-workspace"].hidden = view !== "trend_report";
  elements["trend-report-workspace"].classList.toggle("hidden", view !== "trend_report");
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
  renderAccountHoldings();
}

const TREND_REASON_LABELS = {
  protection_line_already_triggered: "活动保护线已触发",
  danger_signal: "危险信号触发",
  left_trend_right_side: "右侧趋势已结束",
  holding_signal_unknown: "趋势信号不完整",
  holding_kline_unavailable: "持仓日线数据不可用",
  trend_intact: "趋势保持完好",
  temperature_changed_to_flat: "趋势温度转平",
  a_share_only: "仅限 A 股股票",
  temperature_missing: "个股趋势温度缺失",
  temperature_transition_not_entry: "不是温转热或温转沸",
  filter_price_missing: "筛选价缺失",
  filter_price_above_200: "筛选价高于 200 元",
  strength_missing: "趋势强度缺失",
  strength_below_95: "趋势强度低于 95",
  industry_id_missing: "行业 ID 缺失",
  industry_temperature_missing: "行业温度缺失",
  industry_temperature_not_hot: "行业温度未达到热或沸",
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
};

function renderTrendReportEntry(broker) {
  if (!ACCOUNT_BROKERS.includes(broker)) return "";
  const report = state.dashboard?.trend_reports?.[broker] || {};
  const label = broker === "futu" ? "期权关注" : "当天趋势报告";
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
    ? broker === "futu"
      ? `<span>${escapeHtml(formatPlain(report.status_text || "期权关注"))}</span>`
      : `<span>${escapeHtml(formatPlain(report.status_text || "今日已更新"))}</span><span>报告日期 ${escapeHtml(formatPlain(report.report_date))}</span><span>数据截至 ${escapeHtml(formatPlain(report.data_date))}</span>`
    : `<span>${escapeHtml(formatPlain(report.status_text || "今日暂无趋势报告"))}</span>`;
  const reviewStatus = review && !review.available
    ? `<span>${escapeHtml(formatPlain(review.status_text || "暂无复盘数据"))}</span>`
    : "";
  return `<div class="trend-report-entry${report.available ? "" : " trend-report-entry-empty"}">
    <div class="trend-entry-buttons">${reportButton}${reviewButton}</div>
    <div class="trend-entry-details">${details}${reviewStatus}</div>
  </div>`;
}

const TREND_REVIEW_SERIES = [
  {key:"discipline", label:"纪律模拟", className:"discipline"},
  {key:"actual", label:"实际执行", className:"actual"},
  {key:"benchmark", label:"市场基准", className:"benchmark"},
];

function formatTrendReviewValue(cell, percent) {
  const value = numericValue(cell?.value);
  if (value === null) return formatPlain(cell?.reason || "数据不足");
  const formatted = value.toLocaleString("zh-CN", {maximumFractionDigits:2});
  return percent ? `${formatted}%` : formatted;
}

function renderTrendReviewMetric(review, key, label, percent) {
  const values = TREND_REVIEW_SERIES.map((series) => numericValue(review.metrics?.[key]?.[series.key]?.value));
  const maximum = Math.max(...values.filter((value) => value !== null).map(Math.abs), 0);
  const rows = TREND_REVIEW_SERIES.map((series, index) => {
    const cell = review.metrics?.[key]?.[series.key] || {};
    const value = values[index];
    const width = value === null || maximum === 0 ? 0 : Math.round(Math.abs(value) / maximum * 50);
    const direction = value !== null && value < 0 ? " negative" : " positive";
    const unavailable = value === null ? " unavailable" : "";
    return `<div class="trend-review-series${unavailable}">
      <span>${escapeHtml(series.label)}</span>
      <span class="trend-review-track" aria-hidden="true"><i class="trend-review-bar ${series.className}${direction}" style="--trend-review-width:${width}%"></i></span>
      <strong>${escapeHtml(formatTrendReviewValue(cell, percent))}</strong>
    </div>`;
  }).join("");
  return `<section class="trend-review-metric"><h3>${escapeHtml(label)}</h3>${rows}</section>`;
}

function renderTrendReviewChart(review, title, definitions) {
  return `<figure class="trend-review-chart"><figcaption>${escapeHtml(title)}</figcaption>
    <div class="trend-review-legend">${TREND_REVIEW_SERIES.map((series) => `<span class="${series.className}">${escapeHtml(series.label)}</span>`).join("")}</div>
    ${definitions.map(([key,label,percent]) => renderTrendReviewMetric(review,key,label,percent)).join("")}
  </figure>`;
}

function renderTrendReviewWorkspace(review, embedded = false) {
  const snapshot = review.strategy_snapshot || {};
  const rows = Array.isArray(snapshot.parameter_rows) ? snapshot.parameter_rows : [];
  const root = embedded ? "div" : "main";
  return `<${root} class="trend-review">
    <header class="trend-review-header">
      <div><p>${escapeHtml(`${formatPlain(review.broker_label)}｜${formatPlain(review.market_label)}`)}</p>
      <h1>${escapeHtml(`${formatPlain(review.market_label)}趋势复盘`)}</h1>
      <span>${escapeHtml(formatPlain(snapshot.strategy_name))}｜版本 ${escapeHtml(formatPlain(snapshot.strategy_version))}</span></div>
      ${embedded ? "" : '<button type="button" data-close-trend-report>返回持仓看板</button>'}
    </header>
    <section class="trend-review-parameters"><h2>当前策略参数</h2>
      <div class="trend-review-parameter-list trend-review-parameter-table">${rows.map((row) => `<div><span>${escapeHtml(formatPlain(row.group))}</span><strong>${escapeHtml(formatPlain(row.name))}</strong><p>${escapeHtml(formatPlain(row.value))}</p></div>`).join("")}</div>
    </section>
    <div class="trend-review-charts">
      ${renderTrendReviewChart(review,"收益与回撤",[["period_net_return","期间净收益率",true],["market_excess_return","相对市场超额收益",true],["max_drawdown","最大回撤",true]])}
      ${renderTrendReviewChart(review,"风险调整收益",[["calmar","卡玛比率",false],["sharpe","夏普比率",false]])}
    </div>
  </${root}>`;
}

function renderTrendAction(item, kind) {
  const identity = [item.symbol, item.name].filter(Boolean).map(formatPlain).join(" ");
  const reason = TREND_REASON_LABELS[item.reason] || "未知动作或原因，需人工确认";
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

function renderTrendStage(title, items, kind) {
  const rows = Array.isArray(items)
    ? items.filter((item) => item && typeof item === "object" && !Array.isArray(item))
    : [];
  return `<section class="trend-stage">
    <h2>${escapeHtml(title)}</h2>
    ${rows.length ? `<ol>${rows.map((item) => renderTrendAction(item, kind)).join("")}</ol>` : "<p>无</p>"}
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

function renderCnTrendCell(label, value, ariaLabel = "") {
  const display = hasValue(value) ? formatPlain(value) : "—";
  return `<td data-label="${escapeHtml(label)}"${ariaLabel ? ` aria-label="${escapeHtml(ariaLabel)}"` : ""}>${escapeHtml(display)}</td>`;
}

function cnTrendIdentity(item) {
  return [item.symbol, item.name].filter(hasValue).map(formatPlain).join(" ") || "-";
}

function cnTrendTemperature(item) {
  return `${formatPlain(item.temperature_prev)} → ${formatPlain(item.temperature_curr)}`;
}

function cnTrendHints(item) {
  return Array.isArray(item.entry_hints) && item.entry_hints.length
    ? item.entry_hints.map(formatPlain).join("；")
    : "数据不可用";
}

function renderTrendExecutionRow(item, columnCount) {
  const execution = item.execution && typeof item.execution === "object" ? item.execution : {};
  const status = String(execution.status || "pending");
  const statusLabel = {
    pending: "待执行", submitted: "已提交", partially_filled: "部分成交",
    filled: "全部成交", failed: "失败", blocked: "受阻", missed: "错过",
    incomplete: "未完成", early_revision_executed: "早期版本已执行",
  }[status] || "待执行";
  const details = [statusLabel];
  if (hasValue(execution.filled_qty) || hasValue(execution.target_qty)) {
    details.push(`成交 ${formatPlain(execution.filled_qty)} / ${formatPlain(execution.target_qty)}`);
  }
  if (hasValue(execution.avg_fill_price)) details.push(`均价 ${formatPlain(execution.avg_fill_price)}`);
  if (Array.isArray(execution.order_ids) && execution.order_ids.length) {
    details.push(`订单 ${execution.order_ids.map(formatPlain).join("、")}`);
  }
  if (hasValue(execution.updated_at)) details.push(formatPlain(execution.updated_at));
  if (hasValue(execution.reason)) details.push(`原因 ${formatPlain(execution.reason)}`);
  return `<tr class="cn-trend-execution"><td colspan="${columnCount}">${details.map((detail) => `<span>${escapeHtml(detail)}</span>`).join("")}</td></tr>`;
}

function formatCnTrendPrice(value) {
  const number = numericValue(value);
  return number === null
    ? formatPlain(value)
    : number.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
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

function renderCnSellOrHoldStage(title, items, kind) {
  const action = { sell: "全部卖出", review: "人工复核" }[kind] || "继续持有";
  const reasonHeading = kind === "sell" ? "触发原因" : kind === "review" ? "复核原因" : "当前判断";
  const headings = [
    "标的", "动作", "执行参考价（Futu 前复权）", "温度变化", "强度",
    reasonHeading, "活动保护线", "持仓提示",
  ];
  const rows = cnTrendRows(items).map((item) => `<tr class="cn-trend-card">
    ${renderCnTrendCell("标的", cnTrendIdentity(item))}
    ${renderCnTrendCell("动作", action)}
    ${renderCnTrendCell("执行参考价（Futu 前复权）", item.close)}
    ${renderCnTrendCell("温度变化", cnTrendTemperature(item))}
    ${renderCnTrendCell("强度", item.strength)}
    ${renderCnTrendCell(headings[5], TREND_REASON_LABELS[item.reason] || "未知动作或原因，需人工确认")}
    ${renderCnTrendCell("活动保护线", formatCnTrendPrice(item.active_line))}
    ${renderCnTrendCell("持仓提示", cnTrendHints(item))}
  </tr>${kind === "sell" ? renderTrendExecutionRow(item, headings.length) : ""}`);
  return renderCnTrendTable(title, kind, headings, rows);
}

function renderMarketSellOrHoldStage(title, items, kind) {
  const action = { sell: "全部卖出", review: "人工复核" }[kind] || "继续持有";
  const reasonHeading = kind === "sell" ? "触发原因" : kind === "review" ? "复核原因" : "当前判断";
  const headings = ["标的", "动作", "执行参考价", "强度", reasonHeading, "活动保护线", "持仓提示"];
  const rows = cnTrendRows(items).map((item) => `<tr class="cn-trend-card">
    ${renderCnTrendCell("标的", cnTrendIdentity(item))}
    ${renderCnTrendCell("动作", action)}
    ${renderCnTrendCell("执行参考价", item.close)}
    ${renderCnTrendCell("强度", item.strength)}
    ${renderCnTrendCell(reasonHeading, TREND_REASON_LABELS[item.reason] || "未知动作或原因，需人工确认")}
    ${renderCnTrendCell("活动保护线", item.active_line)}
    ${renderCnTrendCell("持仓提示", Array.isArray(item.entry_hints) && item.entry_hints.length ? item.entry_hints.map(formatPlain).join("；") : "—")}
  </tr>${kind === "sell" ? renderTrendExecutionRow(item, headings.length) : ""}`);
  return renderCnTrendTable(title, kind, headings, rows);
}

function renderMarketBuyStage(report) {
  const headings = ["标的", "动作", "执行参考价", "强度", "行业", "目标仓位", "金额上限", "预计数量", "预计保护线"];
  const rows = cnTrendRows(report.buy_actions).map((item) => {
    const targetWeight = decimalAsPercent(item.target_weight, "—");
    return `<tr class="cn-trend-card">
      ${renderCnTrendCell("标的", cnTrendIdentity(item))}
      ${renderCnTrendCell("动作", "正式买入")}
      ${renderCnTrendCell("执行参考价", item.close)}
      ${renderCnTrendCell("强度", item.strength)}
      ${renderCnTrendCell("行业", item.industry)}
      ${renderCnTrendCell("目标仓位", targetWeight, `目标仓位 ${targetWeight}`)}
      ${renderCnTrendCell("金额上限", hasValue(item.target_amount) ? formatDisplayNumber(item.target_amount) : "—")}
      ${renderCnTrendCell("预计数量", hasValue(item.estimated_shares) ? `${formatDisplayNumber(item.estimated_shares)} 股` : "—")}
      ${renderCnTrendCell("预计保护线", hasValue(item.estimated_initial_line) ? formatDisplayNumber(item.estimated_initial_line) : "—")}
    </tr>${renderTrendExecutionRow(item, headings.length)}`;
  });
  return renderCnTrendTable(`${formatPlain(report.buy_window)} · 正式买入计划`, "buy", headings, rows);
}

function renderCnBuyStage(report) {
  const headings = [
    "标的", "动作", "筛选价（Trend Animals）", "执行参考价（Futu 前复权）",
    "温度变化", "节气", "强度", "行业", "行业温度", "市值（亿元）",
    "日成交额（亿元）", "目标仓位", "目标金额", "预计数量", "预计保护线",
  ];
  const rows = cnTrendRows(report.buy_actions).map((item) => {
    const targetWeight = decimalAsPercent(item.target_weight, "-");
    return `<tr class="cn-trend-card">
      ${renderCnTrendCell("标的", cnTrendIdentity(item))}
      ${renderCnTrendCell("动作", "正式买入")}
      ${renderCnTrendCell("筛选价（Trend Animals）", item.filter_price)}
      ${renderCnTrendCell("执行参考价（Futu 前复权）", item.close)}
      ${renderCnTrendCell("温度变化", cnTrendTemperature(item))}
      ${renderCnTrendCell("节气", item.phase)}
      ${renderCnTrendCell("强度", item.strength)}
      ${renderCnTrendCell("行业", item.industry)}
      ${renderCnTrendCell("行业温度", item.industry_temperature)}
      ${renderCnTrendCell("市值（亿元）", item.market_cap)}
      ${renderCnTrendCell("日成交额（亿元）", item.amount)}
      ${renderCnTrendCell("目标仓位", targetWeight, `目标仓位 ${targetWeight}`)}
      ${renderCnTrendCell("目标金额", item.target_amount)}
      ${renderCnTrendCell("预计数量", `${formatPlain(item.estimated_shares)} 股`)}
      ${renderCnTrendCell("预计保护线", formatCnTrendPrice(item.estimated_initial_line))}
    </tr>${renderTrendExecutionRow(item, headings.length)}`;
  });
  return renderCnTrendTable(
    `${formatPlain(report.buy_window)} · 正式买入计划`, "buy", headings, rows,
    "价格口径：筛选价（Trend Animals）｜执行参考价（Futu 前复权）",
  );
}

function renderCnTrendDisciplines() {
  const desktopOpen = typeof window === "undefined"
    || typeof window.matchMedia !== "function"
    || !window.matchMedia("(max-width: 760px)").matches;
  const open = desktopOpen ? " open" : "";
  return `<section class="cn-trend-disciplines">
    <details class="trend-discipline"${open}><summary>买入纪律</summary><ol>
      <li>仅限 A 股股票，排除基金、北交所、ST 与退市标的</li>
      <li>个股必须由温转热或温转沸；热目标仓位 4%，沸目标仓位 2%</li>
      <li>筛选价不高于 200 元，强度不低于 95</li>
      <li>行业温度为热或沸，节气不晚于夏至</li>
      <li>市值不低于 100 亿元，日成交额不低于 2 亿元</li>
      <li>当前可交易、未持有、处于右侧、无危险信号，执行价与 ATR 可用</li>
      <li>正式计划还须通过现金与最多 10 个持仓席位约束</li>
    </ol></details>
    <details class="trend-discipline"${open}><summary>卖出纪律</summary><ol>
      <li>活动保护线触发时全部卖出</li>
      <li>危险信号触发时全部卖出</li>
      <li>离开右侧趋势时全部卖出</li>
      <li>温、热或沸转为平时全部卖出</li>
      <li>沸腾或开香槟只上移保护线，不减仓</li>
    </ol></details>
  </section>`;
}

function renderCnTrendAudit(audit) {
  const candidates = cnTrendRows(audit.candidates);
  const excluded = audit.excluded && typeof audit.excluded === "object" && !Array.isArray(audit.excluded) ? audit.excluded : {};
  const industries = Array.isArray(audit.industry_concentration) ? audit.industry_concentration.filter(Array.isArray) : [];
  const dataSources = Array.isArray(audit.data_sources) ? audit.data_sources : [];
  const candidateRows = candidates.map((item) => {
    const reasons = Array.isArray(item.excluded_reasons)
      ? item.excluded_reasons.map((reason) => TREND_REASON_LABELS[reason] || "未知原因")
      : [];
    const result = item.eligible === true ? "通过策略纪律" : item.eligible === false ? "已排除" : "未知";
    return `<li>${escapeHtml([
      cnTrendIdentity(item), `最终排名 ${formatPlain(item.rank)}`, `结论 ${result}`,
      `排除原因 ${reasons.join("、") || "无"}`,
      `筛选价（Trend Animals） ${formatPlain(item.filter_price)}`,
      `执行参考价（Futu 前复权） ${formatPlain(item.close)}`,
      `温度 ${cnTrendTemperature(item)}`, `节气 ${formatPlain(item.phase)}`,
      `强度 ${formatPlain(item.strength)}`, `行业 ${formatPlain(item.industry)}`,
      `行业 ID ${formatPlain(item.industry_tm_id)}`,
      `行业温度 ${formatPlain(item.industry_temperature)}`,
      `市值 ${formatPlain(item.market_cap)}`, `日成交额 ${formatPlain(item.amount)}`,
      `ATR14 ${formatPlain(item.atr)}`, `危险信号 ${formatPlain(item.danger)}`,
    ].join("｜"))}</li>`;
  }).join("");
  return `<details class="trend-audit"><summary>审计详情</summary>
    <section><h3>完整候选审计</h3><ol>${candidateRows || "<li>无</li>"}</ol></section>
    <section><h3>排除项</h3><ul>${Object.entries(excluded).length
      ? Object.entries(excluded).map(([symbol, reasons]) => `<li>${escapeHtml(formatPlain(symbol))}｜${escapeHtml((Array.isArray(reasons) ? reasons : []).map((reason) => TREND_REASON_LABELS[reason] || "未知原因").join("、"))}</li>`).join("")
      : "<li>无</li>"}</ul></section>
    <section><h3>行业集中度</h3><ul>${industries.length
      ? industries.map((item) => `<li>${escapeHtml(item.map(formatPlain).join("｜"))}</li>`).join("")
      : "<li>无</li>"}</ul></section>
    <p>数据来源：${escapeHtml(dataSources.map(formatPlain).join("、") || "无")}</p>
    <p>API 成本：${escapeHtml(formatPlain(audit.actual_api_cost ?? audit.estimated_api_cost ?? "未知"))}</p>
  </details>`;
}

function renderCnTrendReportWorkspace(report, embedded = false, historical = false) {
  const counts = report.counts || {};
  const audit = report.audit || {};
  const isCn = String(report.market || "").toUpperCase() === "CN";
  const sellOrHold = isCn ? renderCnSellOrHoldStage : renderMarketSellOrHoldStage;
  const buyStage = isCn ? renderCnBuyStage(report) : renderMarketBuyStage(report);
  const root = embedded ? "div" : "main";
  const identity = report.artifact && report.report_sha256 && report.strategy_version
    ? ` data-report-artifact="${escapeHtml(formatPlain(report.artifact))}" data-report-sha256="${escapeHtml(formatPlain(report.report_sha256))}" data-strategy-version="${escapeHtml(formatPlain(report.strategy_version))}"`
    : "";
  const strategyVersion = report.strategy_version
    ? `<span>版本 ${escapeHtml(formatPlain(report.strategy_version))}</span>`
    : "";
  return `<${root} class="cn-trend-report"${identity}>
    <header class="trend-report-header">
      <div><p>${escapeHtml(`${formatPlain(report.broker_label)}｜${formatPlain(report.market_label)}`)}</p><h1>当天趋势报告</h1>${strategyVersion}</div>
      ${embedded
        ? historical
          ? `<button class="trend-history-button" type="button" data-current-trend-report="${escapeHtml(report.broker)}">返回当前报告</button>`
          : `<button class="trend-history-button" type="button" data-report-history="${escapeHtml(report.broker)}">历史报告</button>`
        : '<button type="button" data-close-trend-report>返回持仓看板</button>'}
      <dl>
        <div><dt>报告日期</dt><dd>${escapeHtml(formatPlain(report.report_date))}</dd></div>
        <div><dt>数据截至</dt><dd>${escapeHtml(formatPlain(report.data_date))}</dd></div>
        <div><dt>生成时间</dt><dd>${escapeHtml(formatPlain(report.generated_at))}</dd></div>
        <div><dt>账户状态</dt><dd>${escapeHtml(formatPlain(report.account_status))}</dd></div>
      </dl>
      <div class="trend-report-metrics cn-trend-counts">
        <span>正式买入 ${escapeHtml(formatDisplayNumber(counts.buy || 0))}</span>
        <span>全部卖出 ${escapeHtml(formatDisplayNumber(counts.sell || 0))}</span>
        <span>继续持有 ${escapeHtml(formatDisplayNumber(counts.hold || 0))}</span>
        <span>人工复核 ${escapeHtml(formatDisplayNumber(counts.review || 0))}</span>
      </div>
    </header>
    <div class="cn-trend-actions">
      ${sellOrHold("优先处理 · 卖出触发", report.sell_actions, "sell")}
      ${sellOrHold("需要确认 · 人工复核", report.review_actions, "review")}
      ${buyStage}
      ${sellOrHold("盘中持续 · 已有持仓", report.hold_actions, "hold")}
    </div>
    ${isCn ? renderCnTrendDisciplines() : ""}
    ${isCn ? renderCnTrendAudit(audit) : renderTrendAudit(audit)}
  </${root}>`;
}

const OPTION_ATTENTION_COLUMNS = [
  {label: "标的", content: (item) => [item.symbol, item.name].map(optionAttentionValue).map(escapeHtml).join(" ")},
  {label: "分类", content: (item) => escapeHtml(optionAttentionValue(item.category))},
  {label: "右侧状态", content: (item) => renderOptionAttentionTransition(item.right_side)},
  {label: "趋势温度", content: (item) => renderOptionAttentionTransition(item.temperature)},
  {label: "趋势节气", content: (item) => renderOptionAttentionTransition(item.phase)},
  {label: "本地 / 全球强度", content: (item) => [item.local_strength, item.global_strength].map(optionAttentionValue).map(escapeHtml).join(" / ")},
  {label: "上周 / 上月", content: (item) => `${[item.strength_prev_week, item.strength_prev_month].map(optionAttentionValue).map(escapeHtml).join(" / ")}<br>${renderOptionAttentionTransition(item.strength_change)}`},
  {label: "右侧天数 / 累计涨幅", content: (item) => [item.days, item.gain_since_entry].map(optionAttentionValue).map(escapeHtml).join(" / ")},
  {label: "危险 / 沸腾 / 开香槟", content: (item) => [item.danger, item.boiling, item.champagne].map(renderOptionAttentionTransition).join(" / ")},
  {label: "来源动作", content: (item) => [optionAttentionValue(item.source_broker), optionAttentionAction(item.source_action)].map(escapeHtml).join(" / ")},
];

function optionAttentionValue(value) {
  if (value === null || value === undefined || typeof value === "string" && !value.trim()) {
    return "未提供";
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  return formatPlain(value);
}

function renderOptionAttentionTransition(transition) {
  const value = transition && typeof transition === "object" && !Array.isArray(transition)
    ? transition
    : {};
  const text = `${optionAttentionValue(value.previous)} → ${optionAttentionValue(value.current)}`;
  const changed = value.changed === true ? ' class="option-attention-changed"' : "";
  return `<span${changed}>${escapeHtml(text)}</span>`;
}

function optionAttentionAction(action) {
  if (action === "BUY") return "允许买入";
  if (action === "SELL_ALL") return "卖出复核";
  if (action === "HOLD") return "继续持有";
  return "观察";
}

function optionAttentionMarketStatus(market) {
  if (hasValue(market.status_text)) return optionAttentionValue(market.status_text);
  if (market.data_status === "current") return "今日已更新";
  if (market.data_status === "stale") {
    return `数据截至 ${optionAttentionValue(market.data_date)}；今日未更新`;
  }
  return "暂时不可用";
}

function renderOptionAttentionRow(item) {
  return `<tr class="option-attention-row">${OPTION_ATTENTION_COLUMNS.map(({label, content}) => `<td data-label="${escapeHtml(label)}">${content(item)}</td>`).join("")}</tr>`;
}

function renderOptionAttentionWorkspace(report) {
  const order = {US: 0, HK: 1};
  const markets = (Array.isArray(report.attention_markets) ? report.attention_markets : [])
    .filter((market) => market && typeof market === "object" && !Array.isArray(market))
    .sort((left, right) => (order[String(left.market).toUpperCase()] ?? 2) - (order[String(right.market).toUpperCase()] ?? 2));
  const rowgroups = markets.map((market) => {
    const items = Array.isArray(market.items)
      ? market.items.filter((item) => item && typeof item === "object" && !Array.isArray(item))
      : [];
    return `<tbody><tr class="option-attention-market"><th colspan="${OPTION_ATTENTION_COLUMNS.length}" scope="rowgroup"><div class="option-attention-market-content"><span>${escapeHtml(optionAttentionValue(market.market_label))}</span><span>${escapeHtml(optionAttentionMarketStatus(market))}</span></div></th></tr>${items.map(renderOptionAttentionRow).join("")}</tbody>`;
  }).join("");
  return `<main class="option-attention-workspace">
    <header class="option-attention-header"><h1>期权关注</h1><button type="button" data-close-trend-report>返回持仓看板</button></header>
    <table class="option-attention-table"><thead><tr>${OPTION_ATTENTION_COLUMNS.map(({label}) => `<th scope="col">${escapeHtml(label)}</th>`).join("")}</tr></thead>${rowgroups}</table>
  </main>`;
}

function renderTrendReportWorkspace(report, embedded = false, historical = false) {
  return String(report && report.broker || "").toLowerCase() === "futu"
    ? renderOptionAttentionWorkspace(report || {})
    : renderCnTrendReportWorkspace(report || {}, embedded, historical);
}

function renderHeaderSummary() {
  const summary = state.dashboard?.summary || {};
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
  const dashboard = state.dashboard || {};
  const summary = dashboard.summary || {};
  elements["summary-value"].textContent = formatMoney(summary.portfolio_value_hkd, "HKD");
  elements["summary-holding-value"].textContent = `持仓资产 ${formatMoney(summary.holding_value_hkd, "HKD")}`;
  elements["summary-holding-weight"].textContent = formatPlain(summary.holding_weight_hkd);
  elements["summary-cash-note"].textContent = `现金类资产 ${formatMoney(summary.cash_like_value_hkd, "HKD")} · ${formatPlain(summary.cash_like_weight_hkd)} · 持仓 ${formatDisplayNumber(summary.holding_count)}`;
  elements["summary-holding-bar"].style.width = percentBarWidth(summary.holding_weight_hkd);
  elements["summary-brokers"].textContent = `${formatDisplayNumber(summary.broker_count)} 个`;
  elements["summary-detail-month"].textContent = dashboard.broker_detail_month
    ? `明细月份 ${dashboard.broker_detail_month}`
    : "暂无券商明细";
  elements["summary-health"].textContent = dashboard.detail_available ? "明细可用" : "仅组合汇总";
  elements["summary-health-note"].textContent = dashboard.portfolio_path || "-";
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
  if (view === "report") return "趋势报告";
  return `${{tiger: "美股", phillips: "港股", eastmoney: "A股"}[broker] || "市场"}复盘`;
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
  if (view === "simulate" && !Object.hasOwn(state.trendSimulatePositions, broker)) {
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
  if (elements["visible-count"]) {
    elements["visible-count"].textContent = `${formatDisplayNumber(visibleRows.length)} 条`;
  }
  panel.innerHTML = renderAccountViewPanel({...group, rows});
  panel.setAttribute("aria-labelledby", `account-${broker}-view-${view}`);
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
  if (state.dashboardError) {
    renderDashboardErrorState();
    return;
  }
  if (!state.dashboard) {
    setAccountHoldingsFallbackLabel("账户持仓加载中");
    elements["visible-count"].textContent = "0 条";
    container.innerHTML = '<div class="empty-state">加载中</div>';
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
  elements["visible-count"].textContent = `${formatDisplayNumber(visibleRows.length)} 条`;
  container.innerHTML = active
    ? renderAccountSection({...active, rows})
    : '<div class="empty-state">暂无券商账户</div>';
  if (active?.broker === focusedBroker && focusedView) {
    container.querySelector(`[data-account-view="${focusedView}"]`)?.focus();
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
  const source = firstPresent(brokerSummarySourceText(group.summary), "-");
  const sourceTime = firstPresent(
    group.summary.generated_at, group.summary.as_of, state.dashboard?.broker_detail_month, "-",
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
      ${group.broker === "futu" ? renderTrendReportEntry(group.broker) : ""}
      <div class="account-section-actions">
        <strong>${escapeHtml(formatMoney(group.summary.portfolio_value_hkd, "HKD"))}</strong>
        ${renderStatementUpload(group.broker)}
      </div>
      ${TREND_ACCOUNT_BROKERS.includes(group.broker) ? renderAccountViewTabs(group.broker) : ""}
    </header>
    ${TREND_ACCOUNT_BROKERS.includes(group.broker)
      ? `<div id="account-${escapeHtml(group.broker)}-view-panel" class="account-view-panel" role="tabpanel" aria-labelledby="account-${escapeHtml(group.broker)}-view-${escapeHtml(state.accountViews[group.broker] || "real")}">${renderAccountViewPanel(group)}</div>`
      : rows.length ? renderAccountTable(rows) : '<p class="account-empty">当前筛选下没有持仓</p>'}
  </section>`;
}

function renderAccountViewPanel(group) {
  const view = state.accountViews[group.broker] || "real";
  if (view === "simulate") return renderSimulatedAccountView(group.broker);
  if (view === "report") return renderEmbeddedTrendReport(group.broker);
  if (view === "review") {
    const review = state.dashboard?.trend_reviews?.[group.broker] || {};
    return review.available
      ? renderTrendReviewWorkspace(review, true)
      : `<p class="account-empty">${escapeHtml(formatPlain(review.status_text || "暂无复盘数据"))}</p>`;
  }
  return group.rows.length
    ? renderAccountTable(group.rows)
    : '<p class="account-empty">当前筛选下没有持仓</p>';
}

function simulatedAccountRows(broker) {
  const payload = state.trendSimulatePositions[broker] || {};
  const positions = Array.isArray(payload.positions) ? payload.positions : [];
  return positions.map((position, index) => {
    const display = {
      ...position,
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
  return report.available
    ? renderTrendReportWorkspace(report, true)
    : `<p class="account-empty">${escapeHtml(formatPlain(report.status_text || "今日暂无趋势报告"))}</p>`;
}

function renderTrendReportHistory(broker, history) {
  const rows = Array.isArray(history.rows) ? history.rows : [];
  const content = history.loading
    ? '<p class="account-empty">历史报告加载中</p>'
    : history.error
      ? `<p class="missing-text">${escapeHtml(history.error)}</p>`
      : rows.length
        ? `<ul class="trend-history-list">${rows.map((row) => row.available
          ? `<li><button type="button" data-history-broker="${escapeHtml(broker)}" data-history-artifact="${escapeHtml(row.artifact)}"><strong>报告 ${escapeHtml(formatPlain(row.execution_date))} · ${escapeHtml(formatPlain(row.strategy_version))}</strong><span>${escapeHtml(row.artifact)}</span></button></li>`
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
    <button class="secondary-button" type="button" data-statement-upload="${escapeHtml(broker)}" ${busy ? "disabled" : ""}>${busy ? "上传中…" : "上传结单"}</button>
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
  const response = await fetch(`/api/statements/${encodeURIComponent(broker)}`, {
    method: "POST",
    headers: {"Content-Type": "application/pdf"},
    body: file,
  });
  const payload = await response.json();
  if (!response.ok || payload.status === "error") {
    throw new Error(payload.message || `上传失败 (${response.status})`);
  }
  await loadDashboard();
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
      message: `已导入 ${payload.statement_date} · 持仓 ${payload.positions}`,
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
  const isSelected = !simulated && row.key === state.selectedHoldingKey;
  const selectedDetail = isSelected ? normalizeHoldingDetailMode(state.selectedHoldingDetail) : "";
  const pnlTone = pnlClass(display.unrealized_pnl_pct);
  const detailActions = simulated
    ? ""
    : `<button class="expand-button" type="button" data-detail-key="${escapeHtml(row.key)}" data-detail-mode="decision" data-detail-market="${escapeHtml(display.market)}" data-detail-symbol="${escapeHtml(display.symbol)}">交易决策</button><button class="${escapeHtml(tSignalButtonClass(holding))}" type="button" data-detail-key="${escapeHtml(row.key)}" data-detail-mode="t_signal">做T</button>`;
  const attribution = simulated ? renderSimulationAttribution(holding, row.broker) : "";
  const quote = simulated && hasValue(display.last_price)
    ? {last_price: display.last_price}
    : quoteForHolding(display);
  const cells = `<tr class="account-holding-row ${isSelected ? "active-row" : ""}">
    <td class="account-holding-actions"><span class="account-mobile-label">明细</span>${detailActions}</td>
    <td class="account-holding-market"><span class="account-mobile-label">市场</span>${escapeHtml(formatPlain(display.market))}</td>
    <td class="symbol-cell account-holding-symbol"><span class="account-mobile-label">标的</span><strong>${escapeHtml(formatPlain(display.symbol))}</strong><span class="meta-text">${escapeHtml(formatPlain(display.name))}</span>${attribution}</td>
    <td class="number-cell account-holding-quantity"><span class="account-mobile-label">数量</span>${escapeHtml(formatDisplayNumber(display.total_quantity))}</td>
    <td class="number-cell account-holding-cost"><span class="account-mobile-label">成本价</span>${escapeHtml(formatDisplayNumber(display.avg_cost_price))}</td>
    <td class="number-cell account-holding-price"><span class="account-mobile-label">实时价</span>${renderQuotePrice(display, quote)}</td>
    <td class="number-cell account-holding-usd-value"><span class="account-mobile-label">美元市值</span>${escapeHtml(renderUsdMarketValue(display))}</td>
    <td class="number-cell account-holding-market-value"><span class="account-mobile-label">港元市值</span>${escapeHtml(formatMoney(display.market_value_hkd, "HKD"))}</td>
    <td class="number-cell account-holding-account-weight"><span class="account-mobile-label">账户权重</span>${escapeHtml(formatPlain(display.account_weight))}</td>
    <td class="number-cell account-holding-portfolio-weight"><span class="account-mobile-label">组合权重</span>${escapeHtml(formatPlain(display.portfolio_weight))}</td>
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

function normalizeHoldingDetailMode(mode) {
  return mode === "t_signal" ? mode : "decision";
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
  return holdingByKeyFromRows(filteredHoldings(), detailKey)
    || (state.dashboard && Array.isArray(state.dashboard.holdings)
      ? holdingByKeyFromRows(state.dashboard.holdings, detailKey)
      : null);
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

function renderQuoteStatus(payload) {
  const label = quoteStatusText(payload);
  const stale = Boolean(payload.stale);
  const statusClass = stale ? "status-stale" : quoteStatusClass(payload.status);
  elements["quote-status"].className = `status-pill ${statusClass}`;
  elements["quote-status"].textContent = stale && payload.last_success_at
    ? "数据已过期"
    : label;
  elements["last-refresh"].textContent = quoteRefreshText(payload);
  renderSourceStatusListIntoHeader();
  renderConnectionPanel();
}

function renderConnectionPanel() {
  const payload = state.quotePayload || {};
  setElementText(
    "connection-status",
    payload.status ? quoteStatusLabel(payload.status) : "等待行情",
  );
  setElementText("connection-success", payload.last_success_at || "-");
  setElementText(
    "connection-task",
    payload.stale && payload.last_success_at ? "数据已过期" : formatDiagnostic(payload),
  );
}

function renderLoadError(error) {
  state.dashboard = null;
  state.dashboardError = error;
  if (state.quoteIntervalId !== null) {
    window.clearInterval(state.quoteIntervalId);
    state.quoteIntervalId = null;
  }
  elements["last-refresh"].textContent = error.message
    ? `看板加载失败：${error.message}`
    : "看板加载失败";
  setElementText("connection-poll", "-");
  renderHeaderSummary();
  renderSourceStatusListIntoHeader();
  renderKellyLab();
  renderDashboardErrorState();
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
  const holdings = (state.dashboard && Array.isArray(state.dashboard.holdings))
    ? state.dashboard.holdings
    : [];
  const adjusted = holdings.map((holding) => ({
    ...quoteAdjustedHolding(holding, quoteForHolding(holding)),
    snapshot_market_value_hkd: holding.market_value_hkd,
  }));
  const values = [...adjusted, ...getCashRows()].map(
    (row) => numericValue(row.market_value_hkd),
  );
  if (values.some((value) => value === null)) {
    return adjusted;
  }
  const total = values.reduce((sum, value) => sum + value, 0);
  if (total <= 0) {
    return adjusted;
  }
  return adjusted.map((holding) => ({
    ...holding,
    portfolio_weight_hkd: percentValue(
      numericValue(holding.market_value_hkd),
      total,
    ),
  }));
}

function accountHoldingGroups() {
  const portfolioTotal = state.dashboard?.summary?.portfolio_value_hkd;
  const groups = Object.entries(ACCOUNT_STRATEGY_PROFILES).map(([broker, profile]) => {
    const summary = brokerSummaries().find((item) => brokerKey(item) === broker) || {broker};
    const rows = [];
    getHoldings().forEach((holding, index) => {
      const details = (Array.isArray(holding.broker_details) ? holding.broker_details : [])
        .filter((detail) => brokerKey(detail) === broker);
      details.forEach((detail) => rows.push({
        key: accountHoldingKey(broker, holding, index), broker, holding,
        display: accountDisplayRow(holding, detail, summary, portfolioTotal),
        snapshot_market_value_hkd: detail.market_value_hkd, index,
      }));
      if (!details.length && rowBrokers(holding).length === 1 && rowBrokers(holding)[0] === broker) {
        rows.push({key: accountHoldingKey(broker, holding, index), broker, holding,
          display: accountDisplayRow(holding, null, summary, portfolioTotal),
          snapshot_market_value_hkd: holding.snapshot_market_value_hkd, index});
      }
    });
    return {broker, profile, summary, rows};
  });
  const livePortfolioTotal = quoteAdjustedTotal(
    state.dashboard?.summary?.portfolio_value_hkd,
    groups.flatMap((group) => group.rows),
  );
  groups.forEach((group) => {
    const liveAccountTotal = quoteAdjustedTotal(group.summary.portfolio_value_hkd, group.rows);
    group.rows.forEach((row) => {
      const marketValue = numericValue(row.display.market_value_hkd);
      row.display.account_weight = percentValue(marketValue, liveAccountTotal);
      row.display.portfolio_weight = percentValue(marketValue, livePortfolioTotal);
    });
  });
  return groups;
}

function quoteAdjustedTotal(snapshotTotal, rows) {
  let total = numericValue(snapshotTotal);
  if (total === null) return null;
  for (const row of rows) {
    const liveValue = numericValue(row.display.market_value_hkd);
    const snapshotValue = numericValue(row.snapshot_market_value_hkd);
    if (liveValue === null || snapshotValue === null) return null;
    total += liveValue - snapshotValue;
  }
  return total;
}

function accountDisplayRow(holding, detail, summary, portfolioTotal) {
  const quote = quoteForHolding(holding);
  const hasQuotePrice = numericValue(quote && quote.last_price) > 0;
  const display = quoteAdjustedHolding({
    ...holding,
    ...(detail || {}),
    total_quantity: detail ? detail.quantity : holding.total_quantity,
  }, quote);
  const marketValue = numericValue(display.market_value_hkd);
  return {
    ...display,
    total_quantity: formatPlain(detail ? detail.quantity : holding.total_quantity),
    avg_cost_price: formatPlain(detail ? detail.cost_price : holding.avg_cost_price),
    account_weight: percentValue(marketValue, numericValue(summary.portfolio_value_hkd)),
    portfolio_weight: percentValue(marketValue, numericValue(portfolioTotal)),
    unrealized_pnl_pct: detail && !hasQuotePrice
      ? percentValue(numericValue(display.unrealized_pnl), numericValue(display.cost_value))
      : formatPlain(display.unrealized_pnl_pct),
  };
}

function accountHoldingKey(broker, holding, index) {
  return [broker, holding.market || "", holding.symbol || "", index]
    .map((part) => String(part)).join(":");
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

function percentValue(numerator, denominator) {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator <= 0) {
    return "-";
  }
  return `${((numerator / denominator) * 100).toFixed(2)}%`;
}

function brokerSummaries() {
  return (state.dashboard && Array.isArray(state.dashboard.broker_summaries))
    ? state.dashboard.broker_summaries
    : [];
}

function renderBrokerCards() {
  elements["broker-summary-cards"].innerHTML = renderBrokerSummaryCards();
}

function renderBrokerSummaryCards() {
  const summaries = brokerSummaries();
  return ACCOUNT_BROKERS.map((broker) => {
    const summary = summaries.find((item) => brokerKey(item) === broker) || {broker};
    const profile = ACCOUNT_STRATEGY_PROFILES[broker] || {horizon: "-", strategy: "-"};
    return `<button class="broker-summary-card" type="button" data-broker="${escapeHtml(broker)}">
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
  const detail = (state.dashboard?.holdings || []).flatMap((holding) => (
    Array.isArray(holding.broker_details) ? holding.broker_details : []
  )).find((row) => brokerKey(row) === broker) || {};
  return firstPresent(summary.account_alias, summary.accounts, cash.account_alias, cash.accounts,
    detail.account_alias, detail.accounts, "-");
}

function brokerSummarySourceText(summary) {
  const source = sourceStatuses().find((row) => brokerKey(row) === brokerKey(summary));
  if (source) {
    return sourceDisplayText(source);
  }
  return sourceKindText(summary.source_kind || summary.source_status);
}

function renderSourceStatusListIntoHeader() {
  elements["source-status-list"].innerHTML = renderSourceStatusList();
}

function renderSourceStatusList() {
  const rows = sourceStatuses();
  if (!rows.length) {
    return `<div class="source-status-row status-muted"><strong>数据来源</strong><span>-</span></div>`;
  }
  return rows.map((row) => {
    const status = sourceStatusValue(row);
    return `
      <div class="source-status-row ${escapeHtml(sourceStatusClass(row.status))}" data-broker="${escapeHtml(brokerKey(row))}">
        <strong>${escapeHtml(sourceStatusLabel(row))}</strong>
        <span>${escapeHtml(status)}</span>
      </div>
    `;
  }).join("");
}

function sourceStatuses() {
  return (state.dashboard && Array.isArray(state.dashboard.source_statuses))
    ? state.dashboard.source_statuses
    : [];
}

function sourceStatusLabel(row) {
  return brokerDisplayName(row);
}

function sourceStatusValue(row) {
  if (brokerKey(row) === "futu" && quoteDiagnosticActive()) {
    const diagnostic = formatDiagnostic(state.quotePayload);
    if (state.quotePayload && state.quotePayload.stale && diagnostic === quoteStatusLabel(state.quotePayload.status)) {
      return state.quotePayload.last_success_at
        ? `数据已过期 · ${state.quotePayload.last_success_at}`
        : "数据已过期";
    }
    return diagnostic;
  }
  return sourceDisplayText(row);
}

function sourceDisplayText(row) {
  return firstPresent(row.display_text, row.value, sourceKindText(row.status));
}

function sourceStatusClass(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "ok" || normalized === "real_time" || normalized === "fresh") {
    return "status-ok";
  }
  if (normalized === "non_realtime" || normalized === "statement") {
    return "status-partial";
  }
  if (normalized === "missing" || normalized === "failed" || normalized === "error") {
    return "status-failed";
  }
  return "status-muted";
}

function getCashRows() {
  return (state.dashboard && Array.isArray(state.dashboard.cash_rows))
    ? state.dashboard.cash_rows
    : [];
}

function quoteDiagnosticActive() {
  const payload = state.quotePayload || {};
  const diagnostic = payload.diagnostic || {};
  return payload.status === "failed"
    || payload.status === "partial"
    || Boolean(payload.stale)
    || hasValue(diagnostic.message)
    || hasValue(diagnostic.reason)
    || hasValue(diagnostic.next_step);
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

function quoteForHolding(holding) {
  const market = String(holding && holding.market || "").trim().toUpperCase();
  const symbol = String(holding && holding.symbol || "").trim().toUpperCase();
  return Object.values(state.quotes).find((quote) => (
    String(quote && quote.market || "").trim().toUpperCase() === market
    && String(quote && quote.symbol || "").trim().toUpperCase() === symbol
  )) || null;
}

function quoteAdjustedHolding(holding, quote) {
  const price = numericValue(quote && quote.last_price);
  const quantity = numericValue(holding && holding.total_quantity);
  const cost = numericValue(holding && holding.cost_value);
  const fx = numericValue(holding && holding.fx_to_hkd);
  const isStandardUsOption = String(holding.market || "").toUpperCase() === "US"
    && String(holding.asset_class || "").toLowerCase() === "option";
  if (price === null || price <= 0 || quantity === null
      || (isStandardUsOption ? quantity === 0 : quantity <= 0) || cost === null
      || (isStandardUsOption ? cost === 0 : cost <= 0) || fx === null || fx <= 0) {
    return holding;
  }
  // ponytail: standard US contracts only; use a feed multiplier for adjusted contracts.
  const multiplier = isStandardUsOption ? 100 : 1;
  const marketValue = price * quantity * multiplier;
  const costBasis = cost * multiplier;
  const unrealizedPnl = marketValue - costBasis;
  return {
    ...holding,
    last_price: String(price),
    market_value: marketValue.toFixed(2),
    market_value_hkd: (marketValue * fx).toFixed(2),
    unrealized_pnl: unrealizedPnl.toFixed(2),
    unrealized_pnl_pct: `${((unrealizedPnl / Math.abs(costBasis)) * 100).toFixed(2)}%`,
  };
}

function quoteNotApplicable(holding) {
  const market = String(holding.market || "").toUpperCase();
  const assetClass = String(holding.asset_class || "").toLowerCase();
  return market === "CASH" || assetClass === "cash" || assetClass === "money_market_fund";
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

function sessionQuoteLabel(value) {
  return ({overnight: "夜盘", pre_market: "盘前", regular: "盘中", after_hours: "盘后"})[value] || "";
}

function quoteTimeEt(value) {
  const match = String(value || "").match(/\b\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})/);
  return match ? `${match[1]} ET` : "";
}

function quoteRefreshText(payload) {
  const stale = Boolean(payload && payload.stale);
  const raw = stale ? payload.last_success_at : (payload.fetched_at || payload.last_success_at);
  if (!hasValue(raw)) return stale ? "尚无成功行情" : "尚未刷新";
  const text = String(raw).replace("T", " ").replace(/[+-]\d{2}:\d{2}$/, "");
  return `${stale ? "上次成功" : "刷新于"} ${text} CST`;
}

function quoteStatusText(payload) {
  if (payload && payload.fallback_count > 0 && payload.missing_count === 0) {
    return "部分标的当前时段无报价";
  }
  if (payload && payload.status === "ok" && payload.us_session_status === "closed") {
    return "美股休市";
  }
  return quoteStatusLabel(payload && payload.status);
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
  const match = raw.match(/^([+-]?)(\d+)(\.\d+)?$/);
  if (!match) return raw;
  const [, sign, integer, fraction = ""] = match;
  return `${sign}${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}`;
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
