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
  ].forEach((id) => {
    elements[id] = document.getElementById(id);
  });
  elements["holdings-table-wrap"] = document.querySelector(".table-wrap");
  elements["workspace-grid"] = document.querySelector(".workspace-grid");
  elements["right-rail"] = document.querySelector(".right-rail");
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
    if (!button) {
      return;
    }
    showSymbolDetail(button.dataset.detailKey || "");
  });
  elements["trade-actions"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-action-detail]");
    if (!button) {
      return;
    }
    openTradeActionDetail(button.dataset.actionDetail || "");
  });
  elements["symbol-detail-panel"].addEventListener("click", (event) => {
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
  });
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
    elements["connection-poll"].textContent = "-";
    return;
  }

  const intervalMs = Math.max(1000, seconds * 1000);
  elements["connection-poll"].textContent = `${intervalMs / 1000} 秒`;
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
  const cashRows = filteredCashRows();
  if (state.marketFilter === "CASH") {
    const cashTotal = sumMoneyValues(cashRows);
    return {
      portfolio_value_hkd: cashTotal.text,
      holding_value_hkd: "",
      cash_like_value_hkd: cashTotal.text,
      holding_weight_hkd: percentValue(0, cashTotal.value),
      holding_count: cashRows.length,
    };
  }
  const holdingRows = filteredHoldings();
  const holdingTotal = sumMoneyValues(holdingRows);
  const cashTotal = sumMoneyValues(cashRows);
  const hasTotal = holdingTotal.hasValue || cashTotal.hasValue;
  const portfolioTotal = holdingTotal.value + cashTotal.value;
  return {
    portfolio_value_hkd: hasTotal ? moneyValue(portfolioTotal) : "",
    holding_value_hkd: holdingTotal.text,
    cash_like_value_hkd: cashTotal.text,
    holding_weight_hkd: hasTotal ? percentValue(holdingTotal.value, portfolioTotal) : "-",
    holding_count: holdingRows.length,
  };
}

function sumMoneyValues(rows) {
  let validValueCount = 0;
  const total = rows.reduce((sum, row) => {
    const value = numericValue(row.market_value_hkd);
    if (value === null) {
      return sum;
    }
    validValueCount += 1;
    return sum + value;
  }, 0);
  return {
    value: total,
    hasValue: validValueCount > 0,
    text: validValueCount > 0 ? moneyValue(total) : "",
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
    elements["right-rail"].classList.remove("hidden");
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
  if (selected) {
    elements["workspace-grid"].classList.add("detail-mode");
    elements["right-rail"].classList.add("hidden");
    elements["holdings-table-wrap"].classList.add("hidden");
    elements["symbol-detail-panel"].classList.remove("hidden");
    elements["symbol-detail-panel"].innerHTML = renderSymbolDetail(selected.holding, selected.index);
    return;
  }
  elements["workspace-grid"].classList.remove("detail-mode");
  elements["right-rail"].classList.remove("hidden");
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
    const quote = quoteForHolding(holding);
    const action = holding.trade_action || {};
    const actionText = action.action ? action.action : "-";
    rows.push(`
      <tr>
        <td><button class="expand-button" type="button" data-detail-key="${escapeHtml(rowKey)}">详情</button></td>
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
  const quote = quoteForHolding(holding);
  const livePrice = detailLivePrice(holding, quote);
  const title = `${formatPlain(holding.market)}.${formatPlain(holding.symbol)}`;
  return `
    <div class="detail-header">
      <div>
        <button class="raw-toggle" type="button" data-back-to-holdings>返回持仓列表</button>
        <h2>${escapeHtml(title)}</h2>
        <p>${escapeHtml(formatPlain(holding.name))}</p>
      </div>
      <div class="detail-header-actions">
        ${renderLanguageToggle()}
        <button class="disabled-button" type="button" disabled>重新分析 · 未启用</button>
      </div>
    </div>
    <section class="detail-metric-grid" aria-label="标的概览">
      ${renderMetric("数量", holding.total_quantity)}
      ${renderMetric("成本价", holding.avg_cost_price)}
      ${renderMetric("实时价", livePrice)}
      ${renderMetric("港元市值", formatMoney(holding.market_value_hkd, "HKD"))}
      ${renderMetric("盈亏", holding.unrealized_pnl_pct)}
      ${renderMetric("组合权重", holding.portfolio_weight_hkd)}
      ${renderMetric("数据健康", dataHealthText(holding))}
    </section>
    <div class="detail-grid">
      ${renderAgentReportSection(holding.agent_report, holding)}
      ${renderStrategySection(holding.strategy, holding)}
      ${renderTradeActionSection(holding)}
      ${renderBrokerDetailSection(holding.broker_details)}
    </div>
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
    renderRequiredTerm("观点", formatAction(report.rating || report.advice_action)),
    renderRequiredTerm("报告状态", formatActionStatus(report.status)),
    renderRequiredTerm("生成时间", report.generated_at || report.run_date),
    renderChineseTerm("交易动作", action.action),
    renderChineseTerm("动作状态", action.status),
    renderChineseTerm("触发状态", action.trigger_status),
    renderChineseTerm("核心理由", reason),
    renderChineseTerm("目标价", joinRange(strategy.target_1, strategy.target_2) || strategy.target_range),
    renderChineseTerm("止损价", strategy.stop_loss || action.stop_price),
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
    renderRequiredTerm("观点", formatAction(strategy.view || strategy.stance || strategy.signal || strategy.rating)),
    renderChineseTerm("买入区间", joinRange(strategy.entry_min, strategy.entry_max) || joinRange(strategy.entry_zone_low, strategy.entry_zone_high) || strategy.entry_range),
    renderChineseTerm("加仓价", strategy.add_price),
    renderChineseTerm("止损价", strategy.stop_loss || action.stop_price),
    renderChineseTerm("目标价", joinRange(strategy.target_1, strategy.target_2) || strategy.target_range),
    renderChineseTerm("仓位上限", strategy.target_weight || strategy.target_position || strategy.max_weight),
    renderChineseTerm("时间周期", strategy.time_horizon),
    renderChineseTerm("催化因素", strategy.catalyst),
    renderChineseTerm("风险", strategy.risk_level || strategy.risk),
    renderChineseTerm("当前动作", action.action),
    renderChineseTerm("触发状态", action.trigger_status),
    renderChineseTerm("说明", action.agent_reason || strategy.agent_reason || strategy.notes),
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

function renderTradeActionSection(holding) {
  const premarketAction = holding.premarket_action || {};
  const tradeAction = holding.trade_action || {};
  if (!sectionAvailable(tradeAction) && !sectionAvailable(premarketAction)) {
    return renderDetailSection("当前交易动作", renderStatusMessage("暂无触发中的交易动作", tradeAction));
  }
  const action = sectionAvailable(tradeAction) ? tradeAction : premarketAction;
  const body = `
    ${renderStatusWarning(action)}
    ${renderTradeDecisionBand(action, holding)}
    ${renderTradeImpactGrid(action, holding)}
    ${typeof renderRationaleDialogue === "function" ? renderRationaleDialogue(holding) : ""}
  `;
  return renderDetailSection("当前交易动作", body);
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
          ${renderCompactKv("动作", actionCardStatusLabel(action))}
          ${renderCompactKv("限价", firstPresent(action.limit_price, action.last_price))}
          ${renderCompactKv("数量", firstPresent(action.suggested_quantity, action.target_quantity, action.quantity))}
          ${renderCompactKv("金额", actionNotionalText(action))}
          ${renderCompactKv("止损", action.stop_price)}
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
    ["当前数量", firstPresent(action.current_quantity, holding.total_quantity)],
    ["交易后数量", action.post_trade_quantity],
    ["建议金额", actionNotionalText(action)],
    ["交易后权重", action.post_trade_weight],
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
      return hasValue(row.text) && row.text !== "-" && !hasRawEnglishProse(row.text);
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
  const actionText = formatAction(action.action || action.suggested_action);
  if (actionText === "-") {
    return `${symbol} 交易策略`;
  }
  return `${actionText} ${symbol}`;
}

function strategySubline(action, holding) {
  const strategy = holding.strategy || {};
  const view = formatAction(strategy.view || strategy.stance || strategy.signal || strategy.rating);
  const status = formatActionStatus(action.status);
  const parts = [view, status].filter((part) => part && part !== "-");
  if (parts.length) {
    return `${parts.join(" · ")}；执行前保持人工确认。`;
  }
  return "执行前保持人工确认。";
}

function nextTriggerText(action, holding) {
  const watchTrigger = safeChineseDisplayText(
    firstAvailableText(action.watch_trigger_zh, action.watch_trigger),
  ) || firstMappedLabel(TRIGGER_STATUS_LABELS, action.watch_trigger)
    || firstMappedLabel(REASON_LABELS, action.watch_trigger);
  if (watchTrigger) {
    return watchTrigger;
  }
  const strategy = holding.strategy || {};
  const targetText = joinRange(strategy.target_1, strategy.target_2) || strategy.target_range;
  if (hasValue(targetText)) {
    return `目标价 ${targetText}`;
  }
  const planText = safeChineseDisplayText(
    firstAvailableText(strategy.plan_text_zh, strategy.rationale_zh, strategy.plan_text),
  );
  if (planText) {
    return compactSentence(planText, 48);
  }
  return "";
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
    return renderDetailSection("券商账户明细", renderStatusMessage("暂无券商账户明细"));
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
  `);
}

function renderDetailSection(title, body) {
  return `
    <section class="detail-section">
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
  return safeChineseDisplayText(firstAvailableText(
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
  )) || firstMappedLabel(
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
    .replace(/\b[A-Z]{2,8}\b/g, "")
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
  const translatedReason = firstChineseText(
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
  elements["connection-status"].textContent = payload.status
    ? quoteStatusLabel(payload.status)
    : "等待行情";
  elements["connection-success"].textContent = payload.last_success_at || "-";
  elements["connection-task"].textContent = payload.stale && payload.last_success_at
    ? "数据已过期"
    : formatDiagnostic(payload);
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
  elements["connection-poll"].textContent = "-";
  renderHeaderSummary();
  renderSourceStatusListIntoHeader();
  renderDashboardErrorState();
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
      <span class="summary-note">持仓 ${escapeHtml(formatPlain(summary.holding_count))} · ${escapeHtml(sourceKindText(summary.source_kind || summary.source_status))}</span>
    </article>
  `).join("");
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
  return payload.status === "failed" || Boolean(payload.stale);
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
