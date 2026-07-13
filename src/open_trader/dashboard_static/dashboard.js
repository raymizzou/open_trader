"use strict";

const state = {
  dashboard: null,
  dashboardError: null,
  quotes: {},
  quotePayload: null,
  marketFilter: "ALL",
  brokerFilter: "ALL",
  workspaceView: "portfolio",
  selectedKellyExperimentId: "",
  selectedHoldingKey: "",
  selectedHoldingDetail: "decision",
  selectedDecisionTab: "final",
  detailLanguage: "zh",
  refreshActive: false,
  quoteIntervalId: null,
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

const HOLDINGS_TABLE_COLUMN_COUNT = 10;

const DECISION_TABS = [
  { key: "final", label: "最终决策" },
  { key: "kline", label: "趋势 / K 线" },
  { key: "news", label: "新闻 / 舆论" },
  { key: "futu", label: "富途异动" },
];

const MARKET_SECTION_CONFIGS = [
  { market: "US_STOCK", marketGroup: "US", label: "美股正股", className: "market-section-us-stock" },
  { market: "US_OPTION", marketGroup: "US", label: "美股期权", className: "market-section-us-option" },
  { market: "HK_STOCK", marketGroup: "HK", label: "港股正股", className: "market-section-hk-stock" },
  { market: "HK_OPTION", marketGroup: "HK", label: "港股期权", className: "market-section-hk-option" },
  { market: "CN_STOCK", marketGroup: "CN", label: "A 股正股", className: "market-section-cn-stock" },
  { market: "OTHER", marketGroup: "OTHER", label: "其他市场持仓", className: "market-section-other" },
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
    "header-broker-filters",
    "current-view-label",
    "current-view-value",
    "current-view-holding-value",
    "current-view-holding-weight",
    "current-view-cash-note",
    "broker-summary-cards",
    "source-status-list",
    "kelly-lab-panel",
    "cash-detail-panel",
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
    "holdings-body",
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
    "close-standard-backtest",
    "standard-backtest-workspace",
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
  elements["holdings-table-wrap"] = document.querySelector(".table-wrap");
  elements["workspace-grid"] = document.querySelector(".workspace-grid");
}

function bindEvents() {
  elements["refresh-quotes"].addEventListener("click", refreshQuotes);
  if (elements["kelly-lab-panel"]) {
    elements["kelly-lab-panel"].addEventListener("click", (event) => {
      const strategyTab = event.target.closest("[data-kelly-experiment]");
      if (strategyTab) {
        state.selectedKellyExperimentId = strategyTab.dataset.kellyExperiment || "";
        renderKellyLab();
        return;
      }
      const viewButton = event.target.closest("[data-workspace-view]");
      if (viewButton) {
        setWorkspaceView(viewButton.dataset.workspaceView || "portfolio");
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
    setActiveFilter(elements["header-market-filters"], button);
    renderDashboardViews();
  });
  elements["header-broker-filters"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-broker]");
    if (!button) {
      return;
    }
    state.brokerFilter = button.dataset.broker || "ALL";
    state.selectedHoldingKey = "";
    state.selectedHoldingDetail = "decision";
    state.selectedDecisionTab = "final";
    setActiveFilter(elements["header-broker-filters"], button);
    renderDashboardViews();
  });
  elements["holdings-body"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-detail-key]");
    if (button) {
      showSymbolDetail(button.dataset.detailKey || "", button.dataset.detailMode || "decision");
      return;
    }
    handleSymbolDetailClick(event);
  });
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
  elements["open-standard-backtest"].addEventListener("click", openStandardBacktest);
  elements["close-standard-backtest"].addEventListener("click", closeStandardBacktest);
  elements["backtest-symbol-source"].addEventListener("click", handleBacktestChoice);
  elements["backtest-strategy-cards"].addEventListener("click", handleBacktestChoice);
  elements["backtest-range-controls"].addEventListener("click", handleBacktestChoice);
  elements["backtest-symbol"].addEventListener("change", (event) => {
    state.standardBacktest.symbolKey = event.target.value;
  });
  elements["standard-backtest-form"].addEventListener("submit", submitStandardBacktest);
}

async function openStandardBacktest() {
  elements["workspace-grid"].classList.add("hidden");
  elements["standard-backtest-workspace"].hidden = false;
  elements["standard-backtest-workspace"].classList.remove("hidden");
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

function closeStandardBacktest() {
  syncStandardBacktestInputs();
  elements["standard-backtest-workspace"].hidden = true;
  elements["standard-backtest-workspace"].classList.add("hidden");
  elements["workspace-grid"].classList.remove("hidden");
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
    ["策略收益", strategy.total_return_pct],
    ["买入持有", buyHold.total_return_pct],
    [benchmarkLabel, benchmark && benchmark.total_return_pct],
    ["相对买入持有", result.strategy_excess_return_pct],
    ["相对市场指数", benchmark && result.market_excess_return_pct],
    ["最大回撤", strategy.max_drawdown_pct],
    ["交易次数", Array.isArray(strategy.trades) ? strategy.trades.filter((trade) => Number(trade.quantity) !== 0).length : 0, "count"],
    ["胜率", strategy.win_rate_pct],
  ];
  return `<section class="backtest-result-section" aria-labelledby="backtest-comparison-title"><h3 id="backtest-comparison-title">回测对比</h3><div class="backtest-comparison-grid">${rows.map(([label, value, kind]) => {
    const unavailable = (label === benchmarkLabel || label === "相对市场指数") && !benchmark;
    const display = unavailable ? "基准行情缺失，无法比较" : kind === "count" ? String(value) : backtestPercent(value);
    return `<article class="backtest-metric-card${unavailable ? " benchmark-unavailable" : ""}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(display)}</strong></article>`;
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
    const count = group.count > 1 ? ` ×${group.count}` : "";
    return `<g class="backtest-action-marker action-${group.action.toLowerCase()}"><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5"></circle><text x="${x.toFixed(1)}" y="${(y - 9).toFixed(1)}">${group.action}${count}</text></g>`;
  }).join("");
  const summary = displayedGroups.map((group) => `${group.execution_date} ${group.action}（${explanations[group.action]}）${group.count > 1 ? `共 ${group.count} 笔` : ""}`).join("；");
  const omittedNotice = omittedGroups ? `另有 ${omittedGroups} 组交易标记未显示` : "";
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
  const notice = trades.length > visible.length ? `<p>仅显示前 500 笔，共 ${trades.length} 笔</p>` : "";
  return `<section class="backtest-result-section"><h3>交易记录</h3>${notice}<div class="backtest-table-wrap"><table class="backtest-trades-table"><thead><tr><th>执行日期</th><th>动作</th><th>数量</th><th>成交价</th><th>费用</th><th>原因</th></tr></thead><tbody>${visible.map((trade) => `<tr><td>${escapeHtml(trade.execution_date)}</td><td>${escapeHtml(trade.action)}</td><td>${escapeHtml(trade.quantity)}</td><td>${escapeHtml(trade.execution_price)}</td><td>${escapeHtml(trade.fees)}</td><td>${escapeHtml(trade.reason)}</td></tr>`).join("")}</tbody></table></div></section>`;
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
  return `<section class="backtest-result-section"><h3>运行详情</h3><dl class="backtest-run-details"><dt>请求范围</dt><dd>${escapeHtml(result.requested_start || "-")} 至 ${escapeHtml(result.requested_end || "-")}</dd><dt>实际数据</dt><dd>${escapeHtml(result.actual_start || "-")} 至 ${escapeHtml(result.actual_end || "-")}</dd><dt>策略版本</dt><dd>${escapeHtml(result.strategy_id || "-")}</dd><dt>策略名称</dt><dd>${escapeHtml(definition.name_zh || "-")} · ${escapeHtml(definition.description_zh || "-")}</dd><dt>执行器版本</dt><dd>${escapeHtml(result.adapter_version || "-")}</dd><dt>运行编号</dt><dd>${escapeHtml(result.run_id || "-")}</dd></dl><h4>交易假设</h4><dl class="backtest-run-details"><dt>初始资金</dt><dd>${escapeHtml(assumptions.initial_cash || "-")}</dd><dt>最大策略仓位</dt><dd>${backtestPercent(Number(assumptions.max_strategy_weight) * 100)}</dd><dt>佣金</dt><dd>${escapeHtml(assumptions.commission_bps || "-")} 基点</dd><dt>滑点</dt><dd>${escapeHtml(assumptions.slippage_bps || "-")} 基点</dd><dt>已实现交易费用</dt><dd>${escapeHtml(totalFees.toFixed(2))}</dd></dl><h4>固定参数</h4><dl class="backtest-run-details">${parameters.map(([key, value]) => `<dt>${escapeHtml(parameterLabels[key] || key)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl><p class="backtest-signal-summary">HOLD（观察）信号 ${holdSignals.length} 次${holdSignals.length ? `；${escapeHtml(holdSignals.slice(0, 10).map((signal) => signal.decision_date).join("、"))}` : ""}</p><h4>结果文件</h4><ul class="backtest-artifacts">${artifacts.filter(([key]) => result[key]).map(([key, label]) => `<li><span>${label}</span><code>${escapeHtml(result[key])}</code></li>`).join("")}</ul></section>`;
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
    renderHoldings();
    return;
  }
  const backButton = event.target.closest("[data-back-to-holdings]");
  if (backButton) {
    state.selectedHoldingKey = "";
    state.selectedHoldingDetail = "decision";
    state.selectedDecisionTab = "final";
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
    scheduleQuotePolling(state.dashboard.poll_seconds);
    renderDashboard();
  } catch (error) {
    renderLoadError(error);
  }
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
  renderBrokerFilters();
  renderBrokerCards();
  renderSourceStatusListIntoHeader();
  renderWorkspaceChrome();
  renderKellyLab();
  renderDashboardViews();
  renderTradeActions();
  renderConnectionPanel();
}

function setWorkspaceView(view) {
  state.workspaceView = view === "kelly_lab" ? "kelly_lab" : "portfolio";
  renderWorkspaceChrome();
  renderKellyLab();
}

function renderWorkspaceChrome() {
  if (!elements["workspace-grid"]) {
    return;
  }
  elements["workspace-grid"].classList.toggle("kelly-lab-view", state.workspaceView === "kelly_lab");
}

function renderKellyLab() {
  if (!elements["kelly-lab-panel"]) {
    return;
  }
  elements["kelly-lab-panel"].innerHTML = renderKellyLabPanel();
}

function renderKellyLabPanel() {
  if (state.workspaceView !== "kelly_lab") {
    return renderKellyLabEntry();
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
          <button class="secondary-button" type="button" data-workspace-view="portfolio">返回主页</button>
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
          <button class="secondary-button" type="button" data-workspace-view="portfolio">返回主页</button>
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
          <button class="secondary-button" type="button" data-workspace-view="portfolio">返回主页</button>
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
        <button class="secondary-button" type="button" data-workspace-view="portfolio">返回主页</button>
        <span class="count-pill">${escapeHtml(formatPlain(count))} 个实验</span>
      </div>
    </div>
    ${renderKellyStrategyTabs(experiments, activeExperimentId)}
    <div class="kelly-experiment-grid single">
      ${cards}
    </div>
  `;
}

function renderKellyLabEntry() {
  const dashboard = state.dashboard || {};
  const lab = dashboard.kelly_lab;
  const experiments = lab && typeof lab === "object" && Array.isArray(lab.experiments)
    ? lab.experiments
    : [];
  const count = lab && typeof lab === "object" && hasValue(lab.experiment_count)
    ? lab.experiment_count
    : experiments.length;
  const statusText = state.dashboardError
    ? "不可用"
    : !state.dashboard
      ? "加载中"
      : lab && typeof lab === "object" && lab.available
        ? `${formatPlain(count)} 个实验`
        : "未就绪";
  return `
    <div class="kelly-lab-entry">
      <div>
        <h2>凯利实验室</h2>
        <p>模拟盘策略实验结果单独查看。</p>
      </div>
      <div class="kelly-lab-entry-actions">
        <span class="count-pill">${escapeHtml(statusText)}</span>
        <button class="primary-button" type="button" data-workspace-view="kelly_lab">凯利实验室</button>
      </div>
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
    ["订单", sync.order_count],
    ["成交", sync.fill_count],
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
    escapeHtml(formatPlain(item.order_price || "-")),
    escapeHtml(formatPlain(item.order_qty || "-")),
    escapeHtml(formatPlain(item.filled_qty || "-")),
    escapeHtml(formatPlain(item.avg_fill_price || "-")),
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
    ["执行", execution.execution_count],
    ["预演", execution.dry_run_count],
    ["提交", execution.submitted_count],
    ["跳过", execution.skipped_count],
    ["失败", execution.failed_count],
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
    escapeHtml(formatPlain(item.price || "-")),
    escapeHtml(formatPlain(item.qty || "-")),
    escapeHtml(formatPlain(item.planned_notional || "-")),
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
    ["未完成买单", capital.open_buy_order_count, ""],
    ["已实现盈亏", formatCapitalMoney(capital.realized_pnl, currency), ""],
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
          <div class="${escapeHtml(className)}">
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
  const parsed = Number.parseFloat(String(value).replace(/,/g, ""));
  const amount = Number.isFinite(parsed)
    ? parsed.toLocaleString("en-US", { maximumFractionDigits: 2 })
    : formatPlain(value);
  return formatMoney(amount, currency);
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
    ["已完成", stats.completed_samples],
    ["进行中", stats.open_samples],
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
  return hasValue(currency) && hasValue(amount) ? `${formatPlain(currency)} ${formatPlain(amount)}` : "";
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
    ? `${formatPlain(item.winning_samples)} 赢 / ${formatPlain(item.losing_samples)} 亏`
    : "";
  const payoffDetail = [item.avg_net_win_pct, item.avg_net_loss_pct]
    .filter(hasValue)
    .map(formatPlain)
    .join(" / ");
  const sourceLabel = item.parameter_source === "futu_paper_order_samples"
    ? "富途模拟盘订单样本"
    : item.parameter_source;
  const rows = [
    ["样本状态", sampleStageLabel],
    ["已完成样本", item.completed_samples],
    ["进行中样本", item.open_samples],
    ["原始胜率", [item.raw_win_rate, winLossCount].filter(hasValue).map(formatPlain).join(" · ")],
    ["修正胜率", [item.adjusted_win_rate, item.sample_adjustment].filter(hasValue).map(formatPlain).join(" · ")],
    ["盈亏比 b", [item.payoff_ratio, payoffDetail].filter(hasValue).map(formatPlain).join(" · ")],
    ["Full Kelly", item.full_kelly_pct],
    ["保守 Kelly", item.fractional_kelly_pct],
    ["建议仓位", item.suggested_position_pct],
    ["参数来源", sourceLabel],
    ["跳过订单", item.skipped_order_count],
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
  renderHoldings();
}

function renderHeaderSummary() {
  const summary = currentViewSummary();
  elements["current-view-value"].textContent = formatMoney(summary.portfolio_value_hkd, "HKD");
  elements["current-view-holding-value"].textContent = `持仓资产 ${formatMoney(summary.holding_value_hkd, "HKD")}`;
  elements["current-view-holding-weight"].textContent = summary.holding_weight_hkd;
  elements["current-view-cash-note"].textContent = `现金类资产 ${formatMoney(summary.cash_like_value_hkd, "HKD")} · 持仓 ${formatPlain(summary.holding_count)}`;
  elements["current-view-label"].textContent = currentViewLabel(summary.holding_count);
}

function currentViewSummary() {
  if (state.marketFilter === "CASH") {
    const cashRows = filteredCashRows();
    const cashTotal = sumMoneyValues(cashRows);
    const hasCashTotal = cashTotal.complete && cashTotal.hasValue;
    return {
      portfolio_value_hkd: hasCashTotal ? cashTotal.text : "",
      holding_value_hkd: "",
      cash_like_value_hkd: hasCashTotal ? cashTotal.text : "",
      holding_weight_hkd: hasCashTotal ? percentValue(0, cashTotal.value) : "-",
      holding_count: cashRows.length,
    };
  }
  if (
    state.marketFilter === "ALL"
    && state.brokerFilter !== "ALL"
  ) {
    const summary = currentBrokerSummary();
    if (summary) {
      return {
        portfolio_value_hkd: firstPresent(summary.portfolio_value_hkd, ""),
        holding_value_hkd: firstPresent(summary.holding_value_hkd, ""),
        cash_like_value_hkd: firstPresent(summary.cash_like_value_hkd, ""),
        holding_weight_hkd: brokerSummaryHoldingWeight(summary),
        holding_count: numericValue(summary.holding_count) === null
          ? firstPresent(summary.holding_count, 0)
          : Number(summary.holding_count),
      };
    }
  }
  const holdingRows = filteredHoldings();
  const holdingTotal = sumHoldingValues(holdingRows);
  const cashTotal = state.marketFilter === "ALL"
    ? sumMoneyValues(filteredCashRows())
    : emptyMoneySummary(true);
  const totalsComplete = holdingTotal.complete && cashTotal.complete;
  const hasTotal = totalsComplete && (holdingTotal.hasValue || cashTotal.hasValue);
  const portfolioTotal = holdingTotal.value + cashTotal.value;
  return {
    portfolio_value_hkd: hasTotal ? moneyValue(portfolioTotal) : "",
    holding_value_hkd: holdingTotal.text,
    cash_like_value_hkd: cashTotal.text,
    holding_weight_hkd: hasTotal ? percentValue(holdingTotal.value, portfolioTotal) : "-",
    holding_count: holdingRows.length,
  };
}

function currentBrokerSummary() {
  return brokerSummaries().find((summary) => brokerKey(summary) === state.brokerFilter) || null;
}

function brokerSummaryHoldingWeight(summary) {
  const holdingValue = numericValue(summary.holding_value_hkd);
  const portfolioValue = numericValue(summary.portfolio_value_hkd);
  if (holdingValue === null || portfolioValue === null) {
    return "-";
  }
  return percentValue(holdingValue, portfolioValue);
}

function sumHoldingValues(rows) {
  if (state.brokerFilter === "ALL") {
    return sumMoneyValues(rows);
  }

  let validValueCount = 0;
  let total = 0;
  let complete = true;
  for (const row of rows) {
    const value = brokerHoldingValue(row);
    if (!value.complete) {
      complete = false;
      continue;
    }
    if (!value.hasValue) {
      continue;
    }
    validValueCount += 1;
    total += value.value;
  }

  return {
    value: total,
    hasValue: validValueCount > 0,
    complete,
    text: complete && validValueCount > 0 ? moneyValue(total) : "",
  };
}

function brokerHoldingValue(holding) {
  const brokers = rowBrokers(holding);
  const details = brokerDetailRowsForCurrentFilter(holding);
  if (details.length) {
    const detailValue = sumMoneyValues(details);
    if (detailValue.complete && detailValue.hasValue) {
      return detailValue;
    }
    if (brokers.length > 1) {
      return detailValue.complete ? emptyMoneySummary(false) : detailValue;
    }
  }
  if (brokers.length > 1) {
    return emptyMoneySummary(false);
  }
  return sumMoneyValues([holding]);
}

function brokerDetailRowsForCurrentFilter(holding) {
  const details = Array.isArray(holding.broker_details) ? holding.broker_details : [];
  return details.filter((detail) => {
    if (brokerKey(detail) !== state.brokerFilter) {
      return false;
    }
    const detailMarket = String(detail.market || "").trim().toUpperCase();
    const holdingMarket = String(holding.market || "").trim().toUpperCase();
    if (state.marketFilter !== "ALL" && detailMarket !== state.marketFilter) {
      return false;
    }
    return !detailMarket || !holdingMarket || detailMarket === holdingMarket;
  });
}

function sumMoneyValues(rows) {
  let validValueCount = 0;
  let complete = true;
  const total = rows.reduce((sum, row) => {
    const value = numericValue(row.market_value_hkd);
    if (value === null) {
      complete = false;
      return sum;
    }
    validValueCount += 1;
    return sum + value;
  }, 0);
  return {
    value: total,
    hasValue: validValueCount > 0,
    complete,
    text: complete && validValueCount > 0 ? moneyValue(total) : "",
  };
}

function emptyMoneySummary(complete) {
  return {
    value: 0,
    hasValue: false,
    complete,
    text: "",
  };
}

function currentViewLabel(count) {
  const marketLabel = state.marketFilter === "ALL" ? "全部市场" : state.marketFilter === "CASH" ? "现金" : state.marketFilter === "CN" ? "A 股" : state.marketFilter;
  const brokerLabel = state.brokerFilter === "ALL" ? "全部券商" : brokerDisplayName(state.brokerFilter);
  return `当前视图：${marketLabel} · ${brokerLabel} · ${formatPlain(count)} 条`;
}

function renderSummary() {
  const dashboard = state.dashboard || {};
  const summary = dashboard.summary || {};
  elements["summary-value"].textContent = formatMoney(summary.portfolio_value_hkd, "HKD");
  elements["summary-holding-value"].textContent = `持仓资产 ${formatMoney(summary.holding_value_hkd, "HKD")}`;
  elements["summary-holding-weight"].textContent = formatPlain(summary.holding_weight_hkd);
  elements["summary-cash-note"].textContent = `现金类资产 ${formatMoney(summary.cash_like_value_hkd, "HKD")} · ${formatPlain(summary.cash_like_weight_hkd)} · 持仓 ${formatPlain(summary.holding_count)}`;
  elements["summary-holding-bar"].style.width = percentBarWidth(summary.holding_weight_hkd);
  elements["summary-brokers"].textContent = `${formatPlain(summary.broker_count)} 个`;
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

function renderBrokerFilters() {
  const brokers = new Map();
  for (const holding of getHoldings()) {
    for (const broker of splitList(holding.brokers)) {
      brokers.set(broker, brokerDisplayName(broker));
    }
  }
  for (const row of getCashRows()) {
    for (const broker of rowBrokers(row)) {
      brokers.set(broker, brokerDisplayName(broker));
    }
  }
  for (const summary of brokerSummaries()) {
    const broker = brokerKey(summary);
    if (broker) {
      brokers.set(broker, brokerDisplayName(summary));
    }
  }
  for (const status of sourceStatuses()) {
    const broker = brokerKey(status);
    if (broker) {
      brokers.set(broker, brokerDisplayName(status));
    }
  }
  const buttons = [
    `<button class="filter-button active" type="button" data-broker="ALL">全部券商</button>`,
  ];
  for (const [broker, label] of [...brokers.entries()].sort((left, right) => left[1].localeCompare(right[1]))) {
    buttons.push(
      `<button class="filter-button" type="button" data-broker="${escapeHtml(broker)}">${escapeHtml(label)}</button>`,
    );
  }
  elements["header-broker-filters"].innerHTML = buttons.join("");
  setFilterActiveByDataset(elements["header-broker-filters"], "broker", state.brokerFilter);
}

function renderHoldings() {
  if (state.marketFilter === "CASH") {
    const cashRows = filteredCashRows();
    elements["visible-count"].textContent = `${cashRows.length} 条`;
    elements["workspace-grid"].classList.remove("detail-mode");
    elements["holdings-table-wrap"].classList.add("hidden");
    elements["symbol-detail-panel"].classList.add("hidden");
    elements["symbol-detail-panel"].innerHTML = "";
    elements["cash-detail-panel"].classList.remove("hidden");
    renderCashDetailPanel(cashRows);
    return;
  }
  elements["cash-detail-panel"].classList.add("hidden");
  elements["cash-detail-panel"].innerHTML = "";
  const holdings = filteredHoldings();
  elements["visible-count"].textContent = `${holdings.length} 条`;
  const selected = selectedHolding(holdings);
  elements["workspace-grid"].classList.remove("detail-mode");
  elements["holdings-table-wrap"].classList.remove("hidden");
  elements["symbol-detail-panel"].classList.add("hidden");
  elements["symbol-detail-panel"].innerHTML = "";
  if (state.dashboardError) {
    renderDashboardErrorState();
    return;
  }
  if (!state.dashboard) {
    elements["holdings-body"].innerHTML = holdingsEmptyRow("加载中");
    return;
  }
  if (holdings.length === 0) {
    elements["holdings-body"].innerHTML = holdingsEmptyRow("没有匹配的持仓");
    return;
  }

  const rows = [];
  groupedHoldingsByMarketSection(holdings).forEach((section) => {
    rows.push(renderMarketSectionRow(section));
    section.rows.forEach((entry) => {
      const holding = entry.holding;
      const index = entry.index;
      const rowKey = holdingKey(holding, index);
      const selectedClass = selected && rowKey === state.selectedHoldingKey ? "active-row" : "";
      const quote = quoteForHolding(holding);
      const selectedDetail = selected && rowKey === state.selectedHoldingKey
        ? normalizeHoldingDetailMode(state.selectedHoldingDetail)
        : "";
      const tSignalClass = tSignalButtonClass(holding);
      rows.push(`
        <tr class="${selectedClass}">
          <td><button class="expand-button" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="decision">交易决策</button><button class="${escapeHtml(tSignalClass)}" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="t_signal">做T</button></td>
          <td>${escapeHtml(formatPlain(holding.market))}</td>
          <td class="symbol-cell">
            <strong>${escapeHtml(formatPlain(holding.symbol))}</strong>
            <span class="meta-text">${escapeHtml(formatPlain(holding.name))}</span>
          </td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.total_quantity))}</td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.avg_cost_price))}</td>
          <td class="number-cell">${renderQuotePrice(holding, quote)}</td>
          <td class="number-cell">${escapeHtml(renderUsdMarketValue(holding))}</td>
          <td class="number-cell">${escapeHtml(formatMoney(holding.market_value_hkd, "HKD"))}</td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.portfolio_weight_hkd))}</td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.unrealized_pnl_pct))}</td>
        </tr>
      `);
      if (selected && rowKey === state.selectedHoldingKey) {
        rows.push(`
          <tr class="decision-detail-row">
            <td colspan="${HOLDINGS_TABLE_COLUMN_COUNT}">
              <div class="symbol-detail-panel inline-symbol-detail">
                ${selectedDetail === "t_signal"
                  ? renderTSignalDetail(selected.holding)
                  : renderSymbolDetail(selected.holding, selected.index)}
              </div>
            </td>
          </tr>
        `);
      }
    });
  });
  elements["holdings-body"].innerHTML = rows.join("");
}

function holdingKey(holding, index) {
  return [
    holding.market || "",
    holding.symbol || "",
    holding.name || "",
    index,
  ].map((part) => String(part)).join(":");
}

function selectedHolding(holdings = filteredHoldings()) {
  if (!state.selectedHoldingKey) {
    return null;
  }
  for (let index = 0; index < holdings.length; index += 1) {
    if (holdingKey(holdings[index], index) === state.selectedHoldingKey) {
      return { holding: holdings[index], index };
    }
  }
  return null;
}

function showSymbolDetail(detailKey, detailMode = "decision") {
  state.selectedHoldingKey = detailKey;
  state.selectedHoldingDetail = normalizeHoldingDetailMode(detailMode);
  state.selectedDecisionTab = "final";
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
  const holdings = getHoldings();
  for (let index = 0; index < holdings.length; index += 1) {
    const holding = holdings[index];
    if (holdingActionKeys(holding).includes(normalizedActionKey)) {
      resetHoldingFilters();
      state.selectedHoldingKey = holdingKey(holding, index);
      state.selectedHoldingDetail = "decision";
      state.selectedDecisionTab = "final";
      renderDashboardViews();
      return;
    }
  }
}

function resetHoldingFilters() {
  state.marketFilter = "ALL";
  state.brokerFilter = "ALL";
  setFilterActiveByDataset(elements["header-market-filters"], "market", "ALL");
  setFilterActiveByDataset(elements["header-broker-filters"], "broker", "ALL");
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
        <p>${escapeHtml(formatPlain(holding.name))} · 基于现有持仓数据展示；除 TradingAgents 外，插件模块目前仅为 UI 占位。</p>
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
            { label: "最新价", value: nestedValue(signal.price, "last_price") },
            { label: "日内涨跌", value: percentText(nestedValue(signal.price, "day_change_pct")) },
            { label: "VWAP", value: nestedValue(signal.price, "vwap") },
            { label: "日内区间", value: rangeText(nestedValue(signal.price, "day_low"), nestedValue(signal.price, "day_high")) },
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
  return hasValue(value) ? `${value}%` : "-";
}

function rangeText(low, high) {
  return hasValue(low) || hasValue(high) ? `${formatPlain(low)} / ${formatPlain(high)}` : "-";
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
  const definitions = {
    final: {
      available: [summary.ta_view, summary.current_action, summary.core_reason].some(hasValue),
      error: summary.error,
      html: `${renderLLMDecisionTemplate(holding)}${renderTradingAgentsSummaryCard(holding)}`,
    },
    kline: {
      available: Boolean(facts.kline && facts.kline.available === true) || technicalFactsUsable(technicalFacts),
      error: facts.kline && facts.kline.error,
      html: renderDecisionPluginCard(klineDecisionFactsPlugin(holding)),
    },
    news: {
      available: facts.news_sentiment && facts.news_sentiment.available === true,
      error: facts.news_sentiment && facts.news_sentiment.error,
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
    bodyHtml: `${timeframes.length ? renderBollingerSection(timeframes) : ""}${plugin.bodyHtml}`,
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
  const signal = value.available === true && !["missing", "error", "stale"].includes(status) && hasValue(value.signal)
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
  if (signals.includes("error") || signals.includes("missing") || signals.includes("stale")) {
    return {
      tone: "warn",
      label: "需复核",
      signal: signals.includes("error") ? "error" : (signals.includes("stale") ? "stale" : "missing"),
      constraint: constraint || "review",
      headline: "市场信号数据不可用，不能视为中性。",
      detail: "缺失、错误或过期模块会保留数据质量状态，不会自动改写成交易方向。",
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
  if (status === "stale") return "stale";
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
          <em>${escapeHtml(formatPlain(item.count))}</em>
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
    const bollingerHtml = renderBollingerSection(timeframes);
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

function renderBollingerSection(timeframes) {
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
  return renderBollingerCard(bollinger, preferred.current_price, timeframeLabel(preferred));
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
        <span>下轨 ${escapeHtml(formatPlain(lower || "缺失"))}</span>
        <span>中轨 ${escapeHtml(formatPlain(middle || "缺失"))}</span>
        <span>上轨 ${escapeHtml(formatPlain(upper || "缺失"))}</span>
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
  return hasValue(value) ? formatPlain(value) : "缺失";
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
    rows.push({ label, value: formatPlain(value) });
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

function macdValue(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  const macdLine = firstPresent(value.macd, value.value);
  const parts = [
    hasValue(macdLine) ? `MACD ${macdLine}` : "",
    hasValue(value.signal) ? `Signal ${value.signal}` : "",
    hasValue(value.histogram) ? `Hist ${value.histogram}` : "",
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
    indicatorValue(value.value),
    indicatorValue(value.percent_of_price),
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
      .map((item) => indicatorValue(item))
      .filter((item) => hasValue(item))
      .map((item) => formatPlain(item))
      .join(" · ");
  }
  return indicatorValue(value);
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
      .map(([key, value]) => `${key.toUpperCase()} ${formatPlain(value)}`);
    if (parts.length) {
      return parts.join(" · ");
    }
  }
  const parts = [
    hasValue(timeframe.ma20) ? `MA20 ${timeframe.ma20}` : "",
    hasValue(timeframe.ma50) ? `MA50 ${timeframe.ma50}` : "",
    hasValue(timeframe.ma200) ? `MA200 ${timeframe.ma200}` : "",
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
          ${renderCompactKv("限价", firstSafePrimaryValue(action.limit_price, action.last_price))}
          ${renderCompactKv("数量", firstSafePrimaryValue(action.suggested_quantity, action.target_quantity, action.quantity))}
          ${renderCompactKv("金额", safeActionNotionalText(action))}
          ${renderCompactKv("止损", firstSafePrimaryValue(action.stop_price))}
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
    ["当前数量", firstSafePrimaryValue(action.current_quantity, holding.total_quantity)],
    ["交易后数量", firstSafePrimaryValue(action.post_trade_quantity)],
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
  return [
    ["观点", analystViewText(holding)],
    ["目标价", safeRangeText(strategy.target_1, strategy.target_2) || safePrimaryValue(strategy.target_range)],
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
  const rows = details.map((detail) => `
    <tr>
      <td>${escapeHtml(formatPlain(detail.broker))}</td>
      <td>${escapeHtml(formatPlain(detail.account_alias))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.quantity))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.cost_price))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.last_price))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.market_value))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.unrealized_pnl))}</td>
    </tr>
  `).join("");
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
    return currency === "-" ? action.suggested_notional : `${currency} ${action.suggested_notional}`;
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
    return currency ? `${currency} ${notional}` : notional;
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
        <div><span>限价</span><strong>${escapeHtml(formatPlain(firstPresent(action.limit_price, action.last_price)))}</strong></div>
        <div><span>数量</span><strong>${escapeHtml(formatPlain(action.suggested_quantity))}</strong></div>
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
  const label = quoteStatusLabel(payload.status);
  const stale = Boolean(payload.stale);
  const statusClass = stale ? "status-stale" : quoteStatusClass(payload.status);
  elements["quote-status"].className = `status-pill ${statusClass}`;
  elements["quote-status"].textContent = stale && payload.last_success_at
    ? "数据已过期"
    : label;
  elements["last-refresh"].textContent = payload.last_success_at
    ? `上次成功 ${payload.last_success_at}`
    : "尚无成功行情";
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
  elements["holdings-body"].innerHTML = holdingsEmptyRow("看板数据加载失败");
}

function filteredHoldings() {
  return getHoldings().filter((holding) => {
    const market = String(holding.market || "").toUpperCase();
    const brokers = rowBrokers(holding);
    const marketMatches = state.marketFilter === "ALL" || market === state.marketFilter;
    const brokerMatches = state.brokerFilter === "ALL" || brokers.includes(state.brokerFilter);
    return marketMatches && brokerMatches;
  });
}

function holdingsEmptyRow(message) {
  return `<tr><td colspan="${HOLDINGS_TABLE_COLUMN_COUNT}" class="empty-state">${escapeHtml(message)}</td></tr>`;
}

function marketSectionKey(holding) {
  const market = String(holding && holding.market || "").trim().toUpperCase();
  if (market === "US") {
    return isOptionHolding(holding) ? "US_OPTION" : "US_STOCK";
  }
  if (market === "HK") {
    return isOptionHolding(holding) ? "HK_OPTION" : "HK_STOCK";
  }
  if (market === "CN") {
    return "CN_STOCK";
  }
  return "OTHER";
}

function isOptionHolding(holding) {
  const optionFields = [
    holding && holding.asset_class,
    holding && holding.security_type,
    holding && holding.sec_type,
    holding && holding.instrument_type,
    holding && holding.product_type,
  ];
  if (optionFields.some((value) => isOptionText(value))) {
    return true;
  }
  const symbol = String(holding && holding.symbol || "").trim().toUpperCase();
  if (/^[A-Z]{1,8}\d{6}[CP]\d{5,8}$/.test(symbol) || /^[A-Z]{1,8}\s+\d{6}[CP]\d{5,8}$/.test(symbol)) {
    return true;
  }
  const name = String(holding && holding.name || "").trim();
  const fundLike = /ETF|基金|FUND/i.test(name);
  return !fundLike && /(?:CALL|PUT|OPTION|期权|期權|\d{6}\s+\d+(?:\.\d+)?[CP])/.test(name);
}

function isOptionText(value) {
  if (!hasValue(value)) {
    return false;
  }
  const text = String(value).trim();
  return /^(option|options)$/i.test(text) || /(?:期权|期權)/.test(text);
}

function groupedHoldingsByMarketSection(holdings) {
  const sections = MARKET_SECTION_CONFIGS.map((config) => ({
    ...config,
    rows: [],
  }));
  const sectionByMarket = new Map(sections.map((section) => [section.market, section]));
  const presentMarketGroups = new Set();
  holdings.forEach((holding, index) => {
    const sectionKey = marketSectionKey(holding);
    const section = sectionByMarket.get(sectionKey) || sectionByMarket.get("OTHER");
    presentMarketGroups.add(section.marketGroup);
    section.rows.push({ holding, index });
  });
  sections.forEach((section) => {
    section.rows.sort(compareRowsByPortfolioWeight);
  });
  return sections.filter((section) => {
    if (section.rows.length > 0) {
      return true;
    }
    return section.marketGroup !== "OTHER" && presentMarketGroups.has(section.marketGroup);
  });
}

function sectionRowHolding(row) {
  return row && row.holding ? row.holding : row;
}

function compareRowsByPortfolioWeight(left, right) {
  const leftWeight = numericPercentValue(sectionRowHolding(left).portfolio_weight_hkd);
  const rightWeight = numericPercentValue(sectionRowHolding(right).portfolio_weight_hkd);
  if (leftWeight === null && rightWeight === null) {
    return left.index - right.index;
  }
  if (leftWeight === null) {
    return 1;
  }
  if (rightWeight === null) {
    return -1;
  }
  if (rightWeight !== leftWeight) {
    return rightWeight - leftWeight;
  }
  return left.index - right.index;
}

function sumNumericField(rows, fieldName) {
  if (rows.length === 0) {
    return 0;
  }
  let total = 0;
  for (const row of rows) {
    const value = numericValue(sectionRowHolding(row)[fieldName]);
    if (value === null) {
      return null;
    }
    total += value;
  }
  return rows.length ? total : null;
}

function sumPercentField(rows, fieldName) {
  if (rows.length === 0) {
    return 0;
  }
  let total = 0;
  for (const row of rows) {
    const parsed = numericPercentValue(sectionRowHolding(row)[fieldName]);
    if (parsed === null) {
      return null;
    }
    total += parsed;
  }
  return rows.length ? total : null;
}

function numericPercentValue(value) {
  if (!hasValue(value)) {
    return null;
  }
  const raw = String(value).trim();
  if (!/^[+-]?(?:\d+|\d*\.\d+)%$/.test(raw)) {
    return null;
  }
  const parsed = Number(raw.slice(0, -1));
  return Number.isFinite(parsed) ? parsed : null;
}

function renderMarketSectionRow(section) {
  const hkdTotal = sumNumericField(section.rows, "market_value_hkd");
  const weightTotal = sumPercentField(section.rows, "portfolio_weight_hkd");
  const hkdText = hkdTotal === null ? "-" : formatMoney(moneyValue(hkdTotal), "HKD");
  const weightText = weightTotal === null ? "-" : `${weightTotal.toFixed(2)}%`;
  return `
    <tr class="market-section-row ${escapeHtml(section.className)}">
      <td colspan="${HOLDINGS_TABLE_COLUMN_COUNT}">
        <strong>${escapeHtml(section.label)}</strong>
        <span class="meta-text">${escapeHtml(`${section.rows.length} 个标的 · 港元市值 ${hkdText} · 权重 ${weightText}`)}</span>
      </td>
    </tr>
  `;
}

function renderUsdMarketValue(holding) {
  const currency = String(holding && holding.currency || "").trim().toUpperCase();
  if (currency !== "USD") {
    return "-";
  }
  return formatMoney(holding.market_value, "USD");
}

function getHoldings() {
  return (state.dashboard && Array.isArray(state.dashboard.holdings))
    ? state.dashboard.holdings
    : [];
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

function moneyValue(value) {
  return Number.isFinite(value) ? value.toFixed(2) : "";
}

function percentValue(numerator, denominator) {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator <= 0) {
    return "-";
  }
  return `${((numerator / denominator) * 100).toFixed(2)}%`;
}

function isCashLikeRow(row) {
  if (!row || typeof row !== "object") {
    return false;
  }
  const market = String(row.market || "").trim().toUpperCase();
  const assetClass = String(row.asset_class || "").trim().toLowerCase();
  const symbol = String(row.symbol || "").trim().toUpperCase();
  return market === "CASH"
    || assetClass === "cash"
    || assetClass === "money_market_fund"
    || symbol.endsWith("_CASH");
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
  if (!summaries.length) {
    return `<article class="broker-summary-card"><span class="summary-label">券商暂无数据</span><strong>-</strong></article>`;
  }
  return summaries.map((summary) => `
    <article class="broker-summary-card" data-broker="${escapeHtml(brokerKey(summary))}">
      <span class="summary-label">${escapeHtml(brokerDisplayName(summary))}</span>
      <strong>${escapeHtml(formatMoney(summary.portfolio_value_hkd, "HKD"))}</strong>
      <span class="summary-note">持仓 ${escapeHtml(formatPlain(summary.holding_count))} · ${escapeHtml(brokerSummarySourceText(summary))}</span>
    </article>
  `).join("");
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

function filteredCashRows() {
  if (state.brokerFilter !== "ALL") {
    const detailRows = brokerCashDetailRows();
    if (detailRows.length) {
      return detailRows;
    }
  }
  return getCashRows().filter((row) => {
    if (!isCashLikeRow(row)) {
      return false;
    }
    const brokers = rowBrokers(row);
    return state.brokerFilter === "ALL" || brokers.includes(state.brokerFilter);
  });
}

function getCashRows() {
  return (state.dashboard && Array.isArray(state.dashboard.cash_rows))
    ? state.dashboard.cash_rows
    : [];
}

function brokerCashDetailRows() {
  return getCashDetails().filter((row) => {
    if (!isCashLikeRow(row)) {
      return false;
    }
    return brokerKey(row) === state.brokerFilter;
  });
}

function getCashDetails() {
  return (state.dashboard && Array.isArray(state.dashboard.cash_details))
    ? state.dashboard.cash_details
    : [];
}

function renderCashDetailPanel(rows) {
  if (state.dashboardError) {
    elements["cash-detail-panel"].innerHTML = `<div class="empty-state">看板数据加载失败</div>`;
    return;
  }
  if (!state.dashboard) {
    elements["cash-detail-panel"].innerHTML = `<div class="empty-state">加载中</div>`;
    return;
  }
  if (!rows.length) {
    elements["cash-detail-panel"].innerHTML = `<div class="empty-state">没有匹配的现金资产</div>`;
    return;
  }
  elements["cash-detail-panel"].innerHTML = `
    <h2>现金明细</h2>
    <div class="compact-detail-table">
      <table>
        <thead>
          <tr>
            <th>券商</th>
            <th>币种</th>
            <th>标的</th>
            <th>名称</th>
            <th>港元市值</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(rowBrokers(row).map(brokerDisplayName).join("; ") || "-")}</td>
              <td>${escapeHtml(formatPlain(row.currency))}</td>
              <td>${escapeHtml(formatPlain(row.symbol))}</td>
              <td>${escapeHtml(formatPlain(row.name))}</td>
              <td class="number-cell">${escapeHtml(formatMoney(row.market_value_hkd, "HKD"))}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
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
    futu: "富途",
    tiger: "老虎",
    phillips: "辉立",
  };
  return labels[key] || formatPlain(value);
}

function quoteForHolding(holding) {
  const key = futuSymbolForHolding(holding);
  if (!key) {
    return null;
  }
  return state.quotes[key] || null;
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

function futuSymbolForHolding(holding) {
  const market = String(holding.market || "").trim().toUpperCase();
  let symbol = String(holding.symbol || "").trim().toUpperCase();
  if (!market || !symbol || market === "CASH") {
    return "";
  }
  if (market === "HK" && /^\d+$/.test(symbol)) {
    symbol = symbol.padStart(5, "0");
  }
  return `${market}.${symbol}`;
}

function renderQuotePrice(holding, quote) {
  if (quoteNotApplicable(holding)) {
    return escapeHtml("-");
  }
  if (!quote || !hasValue(quote.last_price)) {
    return `<span class="missing-text">缺行情</span>`;
  }
  return escapeHtml(String(quote.last_price));
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

function formatMoney(value, currency) {
  if (!hasValue(value)) {
    return "-";
  }
  return `${currency} ${value}`;
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
