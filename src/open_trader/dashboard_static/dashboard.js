"use strict";

const state = {
  dashboard: null,
  dashboardError: null,
  quotes: {},
  quotePayload: null,
  marketFilter: "ALL",
  brokerFilter: "ALL",
  expanded: new Set(),
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
};

const ACTION_STATUS_LABELS = {
  ready: "待确认",
  review: "需复核",
  watch: "观察中",
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
    "summary-value",
    "summary-holding-count",
    "summary-refresh-status",
    "summary-refresh-note",
    "summary-brokers",
    "summary-detail-month",
    "summary-health",
    "summary-health-note",
    "market-filters",
    "broker-filters",
    "visible-count",
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
}

function bindEvents() {
  elements["refresh-quotes"].addEventListener("click", refreshQuotes);
  elements["market-filters"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-market]");
    if (!button) {
      return;
    }
    state.marketFilter = button.dataset.market || "ALL";
    setActiveFilter(elements["market-filters"], button);
    renderHoldings();
  });
  elements["broker-filters"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-broker]");
    if (!button) {
      return;
    }
    state.brokerFilter = button.dataset.broker || "ALL";
    setActiveFilter(elements["broker-filters"], button);
    renderHoldings();
  });
  elements["holdings-body"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-expand-key]");
    if (!button) {
      return;
    }
    const key = button.dataset.expandKey;
    if (state.expanded.has(key)) {
      state.expanded.delete(key);
    } else {
      state.expanded.add(key);
    }
    renderHoldings();
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
  renderSummary();
  renderBrokerFilters();
  renderHoldings();
  renderTradeActions();
  renderConnectionPanel();
}

function renderSummary() {
  const dashboard = state.dashboard || {};
  const summary = dashboard.summary || {};
  elements["summary-value"].textContent = formatMoney(summary.portfolio_value_hkd, "HKD");
  elements["summary-holding-count"].textContent = `持仓 ${formatPlain(summary.holding_count)}`;
  elements["summary-brokers"].textContent = `${formatPlain(summary.broker_count)} 个`;
  elements["summary-detail-month"].textContent = dashboard.broker_detail_month
    ? `明细月份 ${dashboard.broker_detail_month}`
    : "暂无券商明细";
  elements["summary-health"].textContent = dashboard.detail_available ? "明细可用" : "仅组合汇总";
  elements["summary-health-note"].textContent = dashboard.portfolio_path || "-";
}

function renderBrokerFilters() {
  const brokers = new Set();
  for (const holding of getHoldings()) {
    for (const broker of splitList(holding.brokers)) {
      brokers.add(broker);
    }
  }
  const buttons = [
    `<button class="filter-button active" type="button" data-broker="ALL">全部券商</button>`,
  ];
  for (const broker of [...brokers].sort()) {
    buttons.push(
      `<button class="filter-button" type="button" data-broker="${escapeHtml(broker)}">${escapeHtml(broker)}</button>`,
    );
  }
  elements["broker-filters"].innerHTML = buttons.join("");
}

function renderHoldings() {
  const holdings = filteredHoldings();
  elements["visible-count"].textContent = `${holdings.length} 条`;
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
    const rowKey = `${holding.market || ""}:${holding.symbol || ""}:${index}`;
    const details = Array.isArray(holding.broker_details) ? holding.broker_details : [];
    const expanded = state.expanded.has(rowKey);
    const quote = quoteForHolding(holding);
    const action = holding.trade_action || {};
    const actionText = action.action ? action.action : "-";
    rows.push(`
      <tr>
        <td>${renderExpandButton(rowKey, details.length, expanded)}</td>
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
    if (expanded) {
      rows.push(renderDetailRow(details));
    }
  });
  elements["holdings-body"].innerHTML = rows.join("");
}

function renderExpandButton(rowKey, count, expanded) {
  if (!count) {
    return `<span class="meta-text">-</span>`;
  }
  const label = expanded ? "收起" : "展开";
  return `<button class="expand-button" type="button" data-expand-key="${escapeHtml(rowKey)}">${label}</button>`;
}

function renderDetailRow(details) {
  const detailRows = details.map((detail) => `
    <tr>
      <td>${escapeHtml(formatPlain(detail.broker))}</td>
      <td>${escapeHtml(formatPlain(detail.account_alias))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.quantity))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.cost_price))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.last_price))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.market_value))}</td>
      <td class="number-cell">${escapeHtml(formatPlain(detail.unrealized_pnl))}</td>
    </tr>
  `);
  return `
    <tr class="detail-row">
      <td colspan="10">
        <table class="detail-table">
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
          <tbody>${detailRows.join("")}</tbody>
        </table>
      </td>
    </tr>
  `;
}

function renderTradeActions() {
  const actions = (state.dashboard && state.dashboard.trade_actions) || [];
  elements["action-count"].textContent = `${actions.length} 条`;
  if (!actions.length) {
    elements["trade-actions"].innerHTML = `<div class="empty-state">暂无交易动作</div>`;
    return;
  }
  elements["trade-actions"].innerHTML = actions.map((action) => `
    <article class="action-item">
      <strong>${escapeHtml(formatPlain(action.market))}.${escapeHtml(formatPlain(action.symbol))}</strong>
      <span class="meta-text">${escapeHtml(formatActionReason(action.reason))}</span>
      <div class="action-meta">
        ${renderActionBadge(action.action, action.status)}
        <span class="badge">${escapeHtml(formatPriority(action.priority))}</span>
        <span class="badge">${escapeHtml(formatTriggerStatus(action.trigger_status))}</span>
      </div>
    </article>
  `).join("");
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
  elements["summary-refresh-status"].textContent = label;
  elements["summary-refresh-note"].textContent = stale && payload.last_success_at
    ? `数据已过期 · ${payload.last_success_at}`
    : formatDiagnostic(payload);
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
  elements["summary-health"].textContent = "加载失败";
  elements["summary-health-note"].textContent = error.message || "无法读取看板数据";
  elements["connection-poll"].textContent = "-";
  renderDashboardErrorState();
}

function renderDashboardErrorState() {
  elements["holdings-body"].innerHTML = `<tr><td colspan="10" class="empty-state">看板数据加载失败</td></tr>`;
}

function filteredHoldings() {
  return getHoldings().filter((holding) => {
    const market = String(holding.market || "").toUpperCase();
    const brokers = splitList(holding.brokers);
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

function quoteForHolding(holding) {
  const key = futuSymbolForHolding(holding);
  if (!key) {
    return null;
  }
  return state.quotes[key] || null;
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
  if (String(holding.market || "").toUpperCase() === "CASH") {
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
