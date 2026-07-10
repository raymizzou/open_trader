"use strict";

const state = {
  dashboard: null,
  dashboardError: null,
  quotes: {},
  quotePayload: null,
  marketFilter: "ALL",
  brokerFilter: "ALL",
  backtestFilter: "ALL",
  selectedHoldingKey: "",
  selectedHoldingDetail: "decision",
  detailLanguage: "zh",
  refreshActive: false,
  quoteIntervalId: null,
  backtestRun: {
    detailKey: "",
    busy: false,
    error: "",
  },
  backtestPrices: {
    detailKey: "",
    busy: false,
    error: "",
  },
  researchChat: {
    holdingKey: "",
    sessionId: "",
    busy: false,
    messageCount: 0,
    messages: [],
  },
};

const elements = {};

const HOLDINGS_TABLE_COLUMN_COUNT = 10;

const MARKET_SECTION_CONFIGS = [
  { market: "US_STOCK", marketGroup: "US", label: "美股正股", className: "market-section-us-stock" },
  { market: "US_OPTION", marketGroup: "US", label: "美股期权", className: "market-section-us-option" },
  { market: "HK_STOCK", marketGroup: "HK", label: "港股正股", className: "market-section-hk-stock" },
  { market: "HK_OPTION", marketGroup: "HK", label: "港股期权", className: "market-section-hk-option" },
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

const BACKTEST_FILTER_OPTIONS = [
  { value: "ALL", label: "全部回测" },
  { value: "READY", label: "可运行" },
  { value: "MISSING_PRICES", label: "缺价格" },
  { value: "MISSING_FIELDS", label: "缺字段" },
  { value: "UNSUPPORTED", label: "暂不支持" },
];

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
    "header-backtest-filters",
    "current-view-label",
    "current-view-value",
    "current-view-holding-value",
    "current-view-holding-weight",
    "current-view-cash-note",
    "broker-summary-cards",
    "source-status-list",
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
  ].forEach((id) => {
    elements[id] = document.getElementById(id);
  });
  elements["holdings-table-wrap"] = document.querySelector(".table-wrap");
  elements["workspace-grid"] = document.querySelector(".workspace-grid");
}

function bindEvents() {
  elements["refresh-quotes"].addEventListener("click", refreshQuotes);
  elements["header-market-filters"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-market]");
    if (!button) {
      return;
    }
    state.marketFilter = button.dataset.market || "ALL";
    state.selectedHoldingKey = "";
    state.selectedHoldingDetail = "decision";
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
    setActiveFilter(elements["header-broker-filters"], button);
    renderDashboardViews();
  });
  elements["header-backtest-filters"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-backtest]");
    if (!button) {
      return;
    }
    state.backtestFilter = button.dataset.backtest || "ALL";
    state.selectedHoldingKey = "";
    state.selectedHoldingDetail = "decision";
    setActiveFilter(elements["header-backtest-filters"], button);
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
}

function handleSymbolDetailClick(event) {
  const backButton = event.target.closest("[data-back-to-holdings]");
  if (backButton) {
    state.selectedHoldingKey = "";
    state.selectedHoldingDetail = "decision";
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
  const backtestButton = event.target.closest("[data-run-backtest]");
  if (backtestButton) {
    runBacktestForHolding(backtestButton.dataset.runBacktest || "");
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
  renderBacktestFilters();
  renderBrokerCards();
  renderSourceStatusListIntoHeader();
  renderDashboardViews();
  renderTradeActions();
  renderConnectionPanel();
}

function renderDashboardViews() {
  renderBacktestFilters();
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
    && state.backtestFilter === "ALL"
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
  const marketLabel = state.marketFilter === "ALL" ? "全部市场" : state.marketFilter === "CASH" ? "现金" : state.marketFilter;
  const brokerLabel = state.brokerFilter === "ALL" ? "全部券商" : brokerDisplayName(state.brokerFilter);
  const backtestLabel = state.backtestFilter === "ALL" || state.marketFilter === "CASH"
    ? ""
    : ` · ${backtestFilterLabel(state.backtestFilter)}`;
  return `当前视图：${marketLabel} · ${brokerLabel}${backtestLabel} · ${formatPlain(count)} 条`;
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

function renderBacktestFilters() {
  if (!elements["header-backtest-filters"]) {
    return;
  }
  elements["header-backtest-filters"].innerHTML = renderBacktestFilterButtons();
}

function renderBacktestFilterButtons() {
  const counts = backtestFilterCounts();
  return BACKTEST_FILTER_OPTIONS.map((option) => {
    const activeClass = state.backtestFilter === option.value ? " active" : "";
    const count = counts[option.value] || 0;
    return `<button class="filter-button${activeClass}" type="button" data-backtest="${escapeHtml(option.value)}">${escapeHtml(option.label)} ${formatPlain(count)}</button>`;
  }).join("");
}

function backtestFilterCounts() {
  const counts = {
    ALL: 0,
    READY: 0,
    MISSING_PRICES: 0,
    MISSING_FIELDS: 0,
    UNSUPPORTED: 0,
  };
  for (const holding of backtestFilterScopeHoldings()) {
    counts.ALL += 1;
    const bucket = backtestFilterBucket(holding);
    if (bucket && Object.prototype.hasOwnProperty.call(counts, bucket)) {
      counts[bucket] += 1;
    }
  }
  return counts;
}

function backtestFilterScopeHoldings() {
  return getHoldings().filter((holding) => {
    const market = String(holding.market || "").toUpperCase();
    const brokers = rowBrokers(holding);
    const marketMatches = state.marketFilter === "ALL" || state.marketFilter === "CASH" || market === state.marketFilter;
    const brokerMatches = state.brokerFilter === "ALL" || brokers.includes(state.brokerFilter);
    return marketMatches && brokerMatches;
  });
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
          <td><button class="expand-button" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="decision">交易决策</button><button class="${escapeHtml(tSignalClass)}" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="t_signal">做T</button><button class="expand-button backtest-button" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="backtest">查看回测</button></td>
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
                  : selectedDetail === "backtest"
                    ? renderBacktestDetail(selected.holding)
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
  renderHoldings();
}

function normalizeHoldingDetailMode(mode) {
  if (mode === "t_signal" || mode === "backtest") {
    return mode;
  }
  return "decision";
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
      renderDashboardViews();
      return;
    }
  }
}

function resetHoldingFilters() {
  state.marketFilter = "ALL";
  state.brokerFilter = "ALL";
  state.backtestFilter = "ALL";
  setFilterActiveByDataset(elements["header-market-filters"], "market", "ALL");
  setFilterActiveByDataset(elements["header-broker-filters"], "broker", "ALL");
  setFilterActiveByDataset(elements["header-backtest-filters"], "backtest", "ALL");
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
      ${renderTradingDecisionPlugins(holding)}
      ${renderLLMDecisionTemplate(holding)}
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

function renderBacktestDetail(holding) {
  const title = `${formatPlain(holding.market)}.${formatPlain(holding.symbol)}`;
  const runControls = renderBacktestRunControls(holding);
  const backtest = holding && holding.backtest && typeof holding.backtest === "object"
    ? holding.backtest
    : null;
  if (!backtest || backtest.available === false) {
    const message = backtest && backtest.error ? backtest.error : "暂无回测结果。";
    return `
      <div class="detail-header trading-decision-header">
        <div>
          <button class="raw-toggle" type="button" data-back-to-holdings>返回持仓列表</button>
          <h2>回测详情 · ${escapeHtml(title)}</h2>
          <p>${escapeHtml(message)}</p>
          ${runControls}
        </div>
        <button class="raw-toggle" type="button" data-back-to-holdings>收起</button>
      </div>
      <section class="detail-section backtest-section">
        <h3>当前状态</h3>
        <p class="muted-copy">该标的尚未生成交易计划回测结果。</p>
      </section>
      ${renderBacktestReadiness(holding)}
    `;
  }
  const metrics = backtest.metrics && typeof backtest.metrics === "object" ? backtest.metrics : {};
  return `
    <div class="detail-header trading-decision-header">
      <div>
        <button class="raw-toggle" type="button" data-back-to-holdings>返回持仓列表</button>
        <h2>回测详情 · ${escapeHtml(title)}</h2>
        <p>${escapeHtml(backtestSummaryText(backtest))}</p>
        ${runControls}
      </div>
      <button class="raw-toggle" type="button" data-back-to-holdings>收起</button>
    </div>
    <div class="backtest-layout">
      <section class="detail-section backtest-section">
        <h3>结果概览</h3>
        <div class="detail-metric-grid backtest-metric-grid">
          ${renderBacktestMetric("总收益", percentMetricText(metrics.total_return_pct))}
          ${renderBacktestMetric("胜率", percentMetricText(metrics.win_rate_pct))}
          ${renderBacktestMetric("最大回撤", percentMetricText(metrics.max_drawdown_pct))}
          ${renderBacktestMetric("交易次数", metrics.trade_count)}
        </div>
      </section>
      ${renderBacktestVisualReport(backtest)}
      <section class="detail-section backtest-section">
        <h3>输出文件</h3>
        <dl class="detail-dl backtest-output-list">
          ${renderRequiredTerm("报告", backtest.report_path)}
          ${renderRequiredTerm("交易明细", backtest.trades_path)}
          ${renderRequiredTerm("权益曲线", backtest.equity_curve_path)}
          ${renderRequiredTerm("指标 JSON", backtest.metrics_path)}
        </dl>
      </section>
      ${renderBacktestReadiness(holding)}
    </div>
  `;
}

function renderBacktestVisualReport(backtest) {
  const equityRows = Array.isArray(backtest.equity_curve) ? backtest.equity_curve : [];
  const trades = Array.isArray(backtest.trades) ? backtest.trades : [];
  if (!equityRows.length && !trades.length) {
    return `
      <section class="detail-section backtest-section">
        <h3>可视化报告</h3>
        <p class="muted-copy">暂无可视化数据。生成 trades.csv 和 equity_curve.csv 后会显示权益曲线、价格走势和交易明细。</p>
      </section>
    `;
  }
  return `
    <div class="backtest-visual-grid">
      <section class="detail-section backtest-section backtest-chart-section">
        <div class="backtest-chart-header">
          <div>
            <h3>权益曲线</h3>
            <p class="muted-copy">从初始资金到最终权益，叠加 BUY/SELL 标记。</p>
          </div>
        </div>
        ${renderBacktestEquityChart(equityRows, trades)}
      </section>
      <section class="detail-section backtest-section backtest-chart-section">
        <div class="backtest-chart-header">
          <div>
            <h3>价格走势与买卖点</h3>
            <p class="muted-copy">使用回测权益曲线里的 close 字段定位价格走势。</p>
          </div>
        </div>
        ${renderBacktestPriceChart(equityRows, trades)}
      </section>
    </div>
    ${renderBacktestTradesTable(trades)}
  `;
}

function renderBacktestEquityChart(rows, trades) {
  const points = backtestChartPoints(rows, "equity", 720, 260);
  if (!points.length) {
    return `<p class="muted-copy">暂无权益曲线数据。</p>`;
  }
  return `
    <div class="backtest-chart-wrap">
      <svg viewBox="0 0 720 260" role="img" aria-label="权益曲线">
        ${renderBacktestGrid()}
        <path class="backtest-area-line" d="${escapeHtml(backtestAreaPath(points, 224))}"></path>
        <path class="backtest-equity-line" d="${escapeHtml(backtestLinePath(points))}"></path>
        ${backtestTradeMarkers(points, trades, "equity")}
        ${renderBacktestAxisLabels(points, "equity")}
      </svg>
    </div>
  `;
}

function renderBacktestPriceChart(rows, trades) {
  const points = backtestChartPoints(rows, "close", 720, 260);
  if (!points.length) {
    return `<p class="muted-copy">暂无价格走势数据。</p>`;
  }
  return `
    <div class="backtest-chart-wrap">
      <svg viewBox="0 0 720 260" role="img" aria-label="价格走势与买卖点">
        ${renderBacktestGrid()}
        <path class="backtest-price-line" d="${escapeHtml(backtestLinePath(points))}"></path>
        ${backtestTradeMarkers(points, trades, "price")}
        ${renderBacktestAxisLabels(points, "close")}
      </svg>
    </div>
  `;
}

function renderBacktestGrid() {
  return `
    <line class="backtest-grid-line" x1="58" y1="34" x2="684" y2="34"></line>
    <line class="backtest-grid-line" x1="58" y1="98" x2="684" y2="98"></line>
    <line class="backtest-grid-line" x1="58" y1="162" x2="684" y2="162"></line>
    <line class="backtest-axis-line" x1="58" y1="224" x2="684" y2="224"></line>
    <line class="backtest-axis-line" x1="58" y1="24" x2="58" y2="224"></line>
  `;
}

function backtestChartPoints(rows, field, width, height) {
  const values = rows
    .map((row) => ({
      date: formatPlain(row.date),
      value: numericMetric(row[field]),
    }))
    .filter((row) => hasValue(row.date) && Number.isFinite(row.value));
  if (!values.length) {
    return [];
  }
  const left = 58;
  const right = width - 36;
  const top = 24;
  const bottom = height - 36;
  const minValue = Math.min(...values.map((row) => row.value));
  const maxValue = Math.max(...values.map((row) => row.value));
  const spread = maxValue - minValue || Math.max(Math.abs(maxValue), 1);
  return values.map((row, index) => {
    const ratioX = values.length === 1 ? 0.5 : index / (values.length - 1);
    const ratioY = (row.value - minValue) / spread;
    return {
      ...row,
      x: left + ratioX * (right - left),
      y: bottom - ratioY * (bottom - top),
      label: row.date.slice(5) || row.date,
    };
  });
}

function numericMetric(value) {
  if (value === null || value === undefined) {
    return NaN;
  }
  const parsed = Number(String(value).replace(/[%,$]/g, "").trim());
  return Number.isFinite(parsed) ? parsed : NaN;
}

function backtestLinePath(points) {
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"}${numberForSvg(point.x)} ${numberForSvg(point.y)}`)
    .join(" ");
}

function backtestAreaPath(points, baseline) {
  if (!points.length) {
    return "";
  }
  return `${backtestLinePath(points)} L${numberForSvg(points[points.length - 1].x)} ${baseline} L${numberForSvg(points[0].x)} ${baseline} Z`;
}

function backtestTradeMarkers(points, trades, mode) {
  if (!points.length || !Array.isArray(trades)) {
    return "";
  }
  const pointsByDate = new Map(points.map((point) => [point.date, point]));
  return trades
    .map((trade) => {
      const point = pointsByDate.get(formatPlain(trade.date));
      if (!point) {
        return "";
      }
      const side = String(trade.side || "").toUpperCase();
      const klass = side === "SELL" ? "backtest-marker-sell" : "backtest-marker-buy";
      const label = mode === "price" ? formatPlain(trade.price) : side;
      return `
        <circle class="${klass}" cx="${numberForSvg(point.x)}" cy="${numberForSvg(point.y)}" r="7"></circle>
        <text class="backtest-chart-label" x="${numberForSvg(Math.min(point.x + 10, 610))}" y="${numberForSvg(Math.max(point.y - 10, 20))}">${escapeHtml(side)} ${escapeHtml(label)}</text>
      `;
    })
    .join("");
}

function renderBacktestAxisLabels(points, field) {
  if (!points.length) {
    return "";
  }
  const first = points[0];
  const last = points[points.length - 1];
  const high = points.reduce((current, point) => (point.value > current.value ? point : current), first);
  return `
    <text class="backtest-axis-label" x="62" y="246">${escapeHtml(first.label)}</text>
    <text class="backtest-axis-label" x="${numberForSvg(Math.max(last.x - 34, 62))}" y="246">${escapeHtml(last.label)}</text>
    <text class="backtest-axis-label" x="64" y="42">${escapeHtml(backtestCompactNumber(high.value, field))}</text>
  `;
}

function renderBacktestTradesTable(trades) {
  if (!Array.isArray(trades) || !trades.length) {
    return `
      <section class="detail-section backtest-section">
        <h3>交易明细</h3>
        <p class="muted-copy">暂无交易明细。</p>
      </section>
    `;
  }
  return `
    <section class="detail-section backtest-section">
      <div class="backtest-chart-header">
        <div>
          <h3>交易明细</h3>
          <p class="muted-copy">trades.csv 内容直接展示，便于审计成交假设。</p>
        </div>
        <span class="status-pill status-ok">${escapeHtml(String(trades.length))} rows</span>
      </div>
      <div class="backtest-table-scroll">
        <table class="backtest-trades-table">
          <thead>
            <tr>
              <th>日期</th>
              <th>方向</th>
              <th>价格</th>
              <th>数量</th>
              <th>手续费</th>
              <th>现金</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            ${trades.map(renderBacktestTradeRow).join("")}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function renderBacktestTradeRow(trade) {
  const side = String(trade.side || "").toUpperCase();
  const tone = side === "SELL" ? "status-partial" : "status-ok";
  return `
    <tr>
      <td>${escapeHtml(formatPlain(trade.date))}</td>
      <td><span class="status-pill ${tone}">${escapeHtml(side || "-")}</span></td>
      <td>${escapeHtml(formatPlain(trade.price))}</td>
      <td>${escapeHtml(formatPlain(trade.quantity))}</td>
      <td>${escapeHtml(formatPlain(trade.fees))}</td>
      <td>${escapeHtml(formatPlain(trade.cash_after))}</td>
      <td>${escapeHtml(formatPlain(trade.reason))}</td>
    </tr>
  `;
}

function numberForSvg(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(2) : "0";
}

function backtestCompactNumber(value, field) {
  if (!Number.isFinite(value)) {
    return "-";
  }
  if (field === "equity" && Math.abs(value) >= 1000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return value.toFixed(2);
}

function renderBacktestReadiness(holding) {
  const readiness = holding && holding.backtest_readiness && typeof holding.backtest_readiness === "object"
    ? holding.backtest_readiness
    : {};
  return `
    <section class="detail-section backtest-section">
      <h3>回测准备</h3>
      <div class="t-signal-status-row">
        <p class="muted-copy">${escapeHtml(backtestReadinessMessage(readiness))}</p>
        <span class="status-pill ${escapeHtml(backtestReadinessTone(readiness.status))}">${escapeHtml(backtestReadinessLabel(readiness.status))}</span>
      </div>
      <dl class="detail-dl backtest-output-list">
        ${renderRequiredTerm("计划日期", readiness.run_date)}
        ${renderRequiredTerm("计划文件", readiness.plan_path)}
        ${renderRequiredTerm("价格文件", readiness.prices_path)}
        ${renderRequiredTerm("缺少字段", backtestMissingFieldsText(readiness.missing_fields))}
      </dl>
    </section>
  `;
}

function backtestReadinessLabel(status) {
  const labels = {
    ready: "已就绪",
    missing_fields: "缺少计划字段",
    missing_prices: "缺少价格文件",
    missing_plan: "缺少交易计划",
    unsupported_strategy: "暂不支持该策略",
  };
  return labels[status] || "未就绪";
}

function backtestReadinessTone(status) {
  return status === "ready" ? "status-ok" : "status-partial";
}

function backtestReadinessMessage(readiness) {
  if (!readiness || typeof readiness !== "object") {
    return "暂无回测准备信息。";
  }
  if (readiness.status === "ready") {
    return "交易计划字段和价格 CSV 已就绪，可以运行只读回测。";
  }
  if (readiness.status === "unsupported_strategy") {
    return "第一版回测支持买入、加仓和减仓类交易计划；其他策略暂不支持。";
  }
  if (readiness.error) {
    return readiness.error;
  }
  return "回测运行前需要补齐交易计划字段和价格 CSV。";
}

function backtestMissingFieldsText(fields) {
  return Array.isArray(fields) && fields.length ? fields.join(", ") : "-";
}

function renderBacktestRunControls(holding) {
  const readiness = holding && holding.backtest_readiness && typeof holding.backtest_readiness === "object"
    ? holding.backtest_readiness
    : {};
  if (readiness.status === "unsupported_strategy") {
    return "";
  }
  const detailKey = state.selectedHoldingKey || holdingKey(holding, 0);
  const runState = state.backtestRun || {};
  const busy = runState.busy === true && runState.detailKey === detailKey;
  const error = runState.detailKey === detailKey ? runState.error : "";
  return `
    <div class="backtest-run-controls">
      <button class="raw-toggle" type="button" data-run-backtest="${escapeHtml(detailKey)}"${busy ? " disabled" : ""}>${busy ? "运行中" : "运行回测"}</button>
      ${error ? `<span class="detail-warning">${escapeHtml(error)}</span>` : ""}
    </div>
  `;
}

async function runBacktestForHolding(detailKey) {
  const holding = holdingByKey(detailKey);
  if (!holding) {
    return;
  }
  state.backtestRun = { detailKey, busy: true, error: "" };
  state.selectedHoldingKey = detailKey;
  state.selectedHoldingDetail = "backtest";
  renderHoldings();
  try {
    const response = await fetch("/api/backtests/run", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        market: holding.market || "",
        symbol: holding.symbol || "",
        initial_position_quantity: formatPlain(holding.total_quantity || "0"),
      }),
    });
    const payload = await response.json();
    if (!response.ok || payload.status === "error") {
      throw new Error(payload.message || `backtest ${response.status}`);
    }
    await loadDashboard();
    state.backtestRun = { detailKey, busy: false, error: "" };
    state.selectedHoldingKey = detailKey;
    state.selectedHoldingDetail = "backtest";
    renderHoldings();
  } catch (error) {
    state.backtestRun = {
      detailKey,
      busy: false,
      error: error.message || "回测运行失败",
    };
    renderHoldings();
  }
}

function backtestSummaryText(backtest) {
  const runDate = formatPlain(backtest.run_date);
  const strategy = backtestStrategyLabel(backtest.strategy);
  const adapter = backtestAdapterLabel(backtest.adapter);
  return [runDate, strategy, adapter, formatPlain(backtest.run_id)]
    .filter((part) => hasValue(part) && part !== "-")
    .join(" · ") || "交易计划回测结果";
}

function backtestStrategyLabel(strategy) {
  return strategy === "trading_plan" ? "交易计划回测" : formatPlain(strategy);
}

function backtestAdapterLabel(adapter) {
  if (adapter === "backtrader") {
    return "Backtrader";
  }
  if (adapter === "simple") {
    return "Simple";
  }
  if (adapter === "legacy") {
    return "Legacy";
  }
  return formatPlain(adapter);
}

function renderBacktestMetric(label, value) {
  return `
    <div class="detail-metric backtest-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatPlain(value))}</strong>
    </div>
  `;
}

function percentMetricText(value) {
  if (!hasValue(value)) {
    return "-";
  }
  const text = formatPlain(value);
  return text.endsWith("%") ? text : `${text}%`;
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

function renderTradingDecisionPlugins(holding) {
  const plugins = [
    klineDecisionFactsPlugin(holding),
    newsSentimentPlugin(holding),
    futuAnomalySignalsPlugin(holding),
    {
      title: "公司行动",
      status: "占位",
      tone: "muted",
      score: "-",
      headline: "待接入",
      detail: "未来确认分红、拆股、增发、回购、停牌等事件。",
      condition: "事实确认：是否有会改变交易计划的公司行动公告。",
    },
    {
      title: "基本面",
      status: "占位",
      tone: "muted",
      score: "-",
      headline: "待接入",
      detail: "未来确认估值、增长假设和业务趋势是否支持继续持仓。",
      condition: "条件：基本面证据是否足以支持当前仓位或需要降低风险。",
    },
    renderTradingAgentsSummaryCard(holding),
    {
      title: "财报",
      status: "占位",
      tone: "muted",
      score: "-",
      headline: "待接入",
      detail: "未来确认财报发布日期、业绩预期和财报后复评要求。",
      condition: "事实确认：财报是否临近，以及是否必须等财报后再执行。",
    },
    {
      title: "大盘 / 行业",
      status: "占位",
      tone: "muted",
      score: "-",
      headline: "待接入",
      detail: "未来确认大盘和半导体行业环境是否支持继续持仓。",
      condition: "条件：大盘与行业趋势是否对当前仓位形成顺风或逆风。",
    },
    {
      title: "组合风险",
      status: "占位",
      tone: "muted",
      score: "-",
      headline: `当前权重 ${formatPlain(holding.portfolio_weight_hkd || "-")}`,
      detail: "这里只展示现有字段，尚未接入独立组合风险插件。",
      condition: "条件：单一标的权重是否过高、波动是否需要降仓。",
    },
  ];
  return `
    <section class="detail-section trading-decision-section">
      <div class="trading-decision-section-header">
        <div>
          <h3>插件模块</h3>
          <p>每个模块说明条件是否达成，或正在确认的事实；趋势 / K 线、新闻 / 舆论与富途异动信号读取固定决策事实，其余插件仍为占位。</p>
        </div>
      </div>
      <div class="decision-plugin-grid">
        ${plugins.map((plugin) => typeof plugin === "string" ? plugin : renderDecisionPluginCard(plugin)).join("")}
      </div>
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
    const backtestMatches = backtestFilterMatches(holding);
    return marketMatches && brokerMatches && backtestMatches;
  });
}

function backtestFilterMatches(holding) {
  if (state.marketFilter === "CASH" || state.backtestFilter === "ALL") {
    return true;
  }
  return backtestFilterBucket(holding) === state.backtestFilter;
}

function backtestFilterBucket(holding) {
  const readiness = holding && holding.backtest_readiness && typeof holding.backtest_readiness === "object"
    ? holding.backtest_readiness
    : {};
  const status = String(readiness.status || "").trim();
  if (status === "ready") {
    return "READY";
  }
  if (status === "missing_prices" || readiness.prices_missing === true) {
    return "MISSING_PRICES";
  }
  if (status === "missing_fields") {
    return "MISSING_FIELDS";
  }
  if (status === "unsupported_strategy") {
    return "UNSUPPORTED";
  }
  return "";
}

function backtestFilterLabel(value) {
  const labels = {
    READY: "回测可运行",
    MISSING_PRICES: "回测缺价格",
    MISSING_FIELDS: "回测缺字段",
    UNSUPPORTED: "回测暂不支持",
  };
  return labels[value] || "全部回测";
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
