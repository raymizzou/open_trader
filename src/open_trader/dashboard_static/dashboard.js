"use strict";

const state = {
  dashboard: null,
  dashboardError: null,
  quotes: {},
  quotePayload: null,
  marketFilter: "ALL",
  brokerFilter: "ALL",
  selectedHoldingKey: "",
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
};

const elements = {};

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
    setActiveFilter(elements["header-broker-filters"], button);
    renderDashboardViews();
  });
  elements["holdings-body"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-detail-key]");
    if (button) {
      showSymbolDetail(button.dataset.detailKey || "");
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
    elements["refresh-quotes"].textContent = "刷新行情";
  }
}

function renderDashboard() {
  renderBrokerFilters();
  renderBrokerCards();
  renderSourceStatusListIntoHeader();
  renderDashboardViews();
  renderTradeActions();
  renderConnectionPanel();
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
  if (state.marketFilter === "ALL" && state.brokerFilter !== "ALL") {
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
    elements["holdings-body"].innerHTML = `<tr><td colspan="10" class="empty-state">加载中</td></tr>`;
    return;
  }
  if (holdings.length === 0) {
    elements["holdings-body"].innerHTML = `<tr><td colspan="10" class="empty-state">没有匹配的持仓</td></tr>`;
    return;
  }

  const rows = [];
  holdings.forEach((holding, index) => {
    const rowKey = holdingKey(holding, index);
    const selectedClass = selected && rowKey === state.selectedHoldingKey ? "active-row" : "";
    const quote = quoteForHolding(holding);
    const action = holding.trade_action || {};
    const actionText = action.action ? action.action : "-";
    rows.push(`
      <tr class="${selectedClass}">
        <td><button class="expand-button" type="button" data-detail-key="${escapeHtml(rowKey)}">交易决策</button></td>
        <td>${escapeHtml(formatPlain(holding.market))}</td>
        <td class="symbol-cell">
          <strong>${escapeHtml(formatPlain(holding.symbol))}</strong>
          <span class="meta-text">${escapeHtml(formatPlain(holding.name))}</span>
        </td>
        <td>${escapeHtml(formatPlain(holding.brokers))}</td>
        <td class="number-cell">${escapeHtml(formatPlain(holding.total_quantity))}</td>
        <td class="number-cell">${escapeHtml(formatPlain(holding.last_price))}</td>
        <td class="number-cell">${renderQuotePrice(holding, quote)}</td>
        <td class="number-cell">${escapeHtml(formatMoney(holding.market_value_hkd, "HKD"))}</td>
        <td class="number-cell">${escapeHtml(formatPlain(holding.unrealized_pnl_pct))}</td>
        <td>${renderActionBadge(actionText, action.status)}</td>
      </tr>
    `);
    if (selected && rowKey === state.selectedHoldingKey) {
      rows.push(`
        <tr class="decision-detail-row">
          <td colspan="10">
            <div class="symbol-detail-panel inline-symbol-detail">
              ${renderSymbolDetail(selected.holding, selected.index)}
            </div>
          </td>
        </tr>
      `);
    }
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

function showSymbolDetail(detailKey) {
  state.selectedHoldingKey = detailKey;
  renderHoldings();
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
      ${renderTradingDecisionPlugins(holding)}
      ${renderLLMDecisionTemplate(holding)}
    </div>
  `;
}

function renderTradingDecisionPlugins(holding) {
  const action = currentDecisionAction(holding);
  const hasTradingAgents = sectionAvailable(holding.agent_report)
    || sectionAvailable(holding.strategy)
    || sectionAvailable(action);
  const plugins = [
    decisionFactsPlugin(holding, {
      title: "趋势 / K 线",
      moduleKey: "kline",
      fieldOrder: [
        ["trend", "趋势"],
        ["position", "位置"],
        ["momentum", "动能"],
        ["key_levels", "关键位"],
        ["risk", "风险"],
      ],
      score: "K线",
    }),
    decisionFactsPlugin(holding, {
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
    }),
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
    {
      title: "TradingAgents",
      status: hasTradingAgents ? "已接入" : "缺失",
      tone: hasTradingAgents ? "ok" : "partial",
      score: hasTradingAgents ? "TA" : "-",
      headline: finalConclusionText(holding),
      detail: decisionSubline(holding),
      condition: "条件：TradingAgents 结论与当前交易动作是否支持下一步策略。",
    },
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
          <p>每个模块说明条件是否达成，或正在确认的事实；趋势 / K 线与新闻 / 舆论读取固定决策事实，其余插件仍为占位。</p>
        </div>
      </div>
      <div class="decision-plugin-grid">
        ${plugins.map((plugin) => renderDecisionPluginCard(plugin)).join("")}
      </div>
    </section>
  `;
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
    const rows = technicalFactRows(detail.facts);
    const dateText = technicalFactsDateText(detail);
    return {
      title: "趋势 / K 线",
      status: "可用",
      tone: "ok",
      score: "K线",
      headline: dateText || "当前可用",
      detail: technicalFactsFreshnessText(detail) || "技术面事实已按最新 TradingAgents 来源校验。",
      bodyHtml: renderTechnicalFactRows(rows),
      condition: technicalFactsRunText(detail) || "条件：技术面事实与最新报告来源一致。",
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
  return filteredHoldings().find((holding) => holdingKey(holding) === detailKey)
    || (state.dashboard && Array.isArray(state.dashboard.holdings)
      ? state.dashboard.holdings.find((holding) => holdingKey(holding) === detailKey)
      : null);
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
  elements["holdings-body"].innerHTML = `<tr><td colspan="10" class="empty-state">看板数据加载失败</td></tr>`;
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
