from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import date
from typing import Any

import pytest

from open_trader.dashboard_quotes import QuoteRefreshResult
from open_trader.dashboard_web import STATIC_DIR
from open_trader.portfolio import PORTFOLIO_FIELDNAMES
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES

from tests.test_dashboard import dashboard_config, portfolio_rows, write_csv


def test_dashboard_static_keeps_existing_columns_and_adds_cn() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert holdings_table_header_labels(html) == [
        "明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值",
        "港元市值", "持仓占总资产的占比", "盈亏",
    ]
    assert 'data-market="CN">A 股</button>' in html
    assert 'label: "A 股正股"' in js
    assert 'market === "CN"' in js
    for forbidden_id in ("a-share-panel", "a-share-card", "cn-panel", "cn-card"):
        assert f'id="{forbidden_id}"' not in html


def test_dashboard_command_center_theme_preserves_the_data_contract() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert holdings_table_header_labels(html) == [
        "明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值",
        "港元市值", "持仓占总资产的占比", "盈亏",
    ]
    for element_id in (
        "open-standard-backtest", "header-market-filters",
        "header-broker-filters", "current-view-value",
        "broker-summary-cards", "quote-status", "refresh-quotes",
        "source-status-list", "last-refresh", "kelly-lab-panel",
        "holdings-body", "cash-detail-panel", "symbol-detail-panel",
        "standard-backtest-workspace", "research-chat-layer",
    ):
        assert f'id="{element_id}"' in html
    assert "今日结论" not in html
    assert 'id="trade-actions"' not in html
    assert "--bg: #f5f7fa;" in css
    assert "--text: #101828;" in css
    assert "--accent: #2563eb;" in css
    assert "--primary: #101828;" in css
    assert "font-variant-numeric: tabular-nums;" in css


def test_dashboard_command_center_css_keeps_accessible_responsive_states() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert "button:focus-visible" in css
    assert "outline: 3px solid rgba(37, 99, 235, 0.32);" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transition-duration: 0.01ms !important;" in css
    mobile = css.split("@media (max-width: 760px) {", 1)[1]
    assert "min-height: 44px;" in mobile
    assert 'grid-template-areas: "brand" "assets" "source";' in mobile


def test_dashboard_renders_validated_and_fallback_decision_plans() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
const validatedPlan = {
  available: true,
  mode: "validated_plan",
  status: "waiting",
  run_date: "2026-07-13",
  action_summary: "继续持有，等待条件触发",
  next_condition_id: "trend-exit",
  current_quantity: "400",
  current_weight: "0.078",
  max_weight: "0.10",
  risk_status: "within_limit",
  strategy: {id: "trend_pullback/v1", name_zh: "趋势回调"},
  conditions: [{
    condition_id: "trend-exit", priority: "risk", operator: "<=",
    calculated_value: "57", target_weight: "0", target_quantity: "0",
    suggested_action: "退出", formula: "min(sma50, active_stop)",
    inputs: {sma50: "58", active_stop: "57"}, source_date: "2026-07-10",
    trigger_count: 2,
  }],
  backtests: [{
    range: "1Y", gate: {passed: true},
    strategy: {total_return_pct: "8", max_drawdown_pct: "6", sharpe_ratio: "1.1", calmar_ratio: "1.3"},
    market_benchmark: {symbol: "SPY", total_return_pct: "5.5"},
    market_excess_return_pct: "2.5",
  }],
  previous_review: {run_date: "2026-07-10", status: "triggered", trigger_count: 1, starting_quantity: "400", closing_quantity: "400"},
};
const validated = renderDecisionPlan({decision_plan: validatedPlan});
for (const text of ["今日交易计划", "下一条件", "目标仓位", "回测闸门", "最大回撤", "夏普比率", "卡玛比率", "参数来源", "上期复盘"]) {
  if (!validated.includes(text)) throw new Error("missing " + text + ": " + validated);
}
if (!validated.includes("data-plan-condition")) throw new Error("validated plan has no condition cards");
if (!validated.includes("<dt>卡玛比率</dt><dd>1.30</dd>")) throw new Error("calmar ratio is not readable: " + validated);

const fallbackPlan = {
  available: true,
  mode: "fallback_advice",
  status: "waiting",
  run_date: "2026-07-13",
  max_weight: "0.10",
  fallback: {
    label: "非执行型建议", reason: "没有策略通过当前回测闸门", recommendation: "禁止加仓",
    max_weight: "0.10", tradingagents: {current_action: "观察", core_reason: "等待趋势确认"},
    facts: [
      {key: "ma20_distance_pct", calculated_value: "-3.2", formula: "(close/sma20-1)*100", inputs: {close: "47"}, source_date: "2026-07-10"},
      {key: "rsi14", calculated_value: "31", formula: "RSI(14)", inputs: {period: "14"}, source_date: "2026-07-10"},
      {key: "bollinger_position", calculated_value: "below_lower", formula: "compare bands", inputs: {close: "47"}, source_date: "2026-07-10"},
    ],
  },
};
const fallback = renderDecisionPlan({decision_plan: fallbackPlan});
for (const text of ["非执行型建议", "禁止加仓", "RSI", "布林带", "为什么没有可执行计划"]) {
  if (!fallback.includes(text)) throw new Error("missing " + text + ": " + fallback);
}
if (fallback.includes("data-plan-condition")) throw new Error("fallback rendered executable condition");
`, sandbox);
'''
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_final_tab_uses_plan_contract_and_deep_link_helpers() -> None:
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    final_view = js.split("final: {", 1)[1].split("},", 1)[0]
    assert "holding.decision_plan" in final_view
    assert "renderDecisionPlan(holding)" in final_view
    assert "holding.agent_report" not in final_view
    assert "restoreDecisionDeepLink" in js
    assert "syncDecisionDeepLink" in js
    assert "history.replaceState" in js


def test_backtest_options_payload_exposes_fixed_catalog_and_defaults(tmp_path) -> None:
    from open_trader.dashboard_web import build_standard_backtest_options_payload

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    payload = build_standard_backtest_options_payload(config)

    assert [item["id"] for item in payload["strategies"]] == [
        "trend_pullback/v1", "breakout_momentum/v1", "range_mean_reversion/v1",
    ]
    assert payload["ranges"] == ["6M", "1Y", "3Y", "5Y", "CUSTOM"]
    assert payload["defaults"] == {
        "range": "1Y", "initial_cash": "100000", "max_strategy_weight": "0.10",
        "commission_bps": "10", "slippage_bps": "5",
    }
    assert payload["benchmarks"]["CN"] == "000300"


def test_cn_standard_backtest_owns_akshare_provider(tmp_path, monkeypatch) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({"market": "CN", "symbol": "600025", "asset_class": "stock"})
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])
    provider = object()
    monkeypatch.setattr(dashboard_web, "AkShareDailyKlineProvider", lambda: provider)
    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: pytest.fail("Futu must not serve CN"))
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", lambda request, *, price_provider: type("Result", (), {"to_dict": lambda self: {"provider": price_provider}})())

    result = dashboard_web.build_standard_backtest_run_payload(config, {
        "market": "CN", "symbol": "600025", "strategy_id": "trend_pullback/v1",
    })
    assert result["provider"] is provider


def test_standard_backtest_run_rejects_adapter_choice(tmp_path) -> None:
    from open_trader.dashboard_web import build_standard_backtest_run_payload

    config = dashboard_config(tmp_path)
    with pytest.raises(ValueError, match="不支持从界面选择回测执行工具"):
        build_standard_backtest_run_payload(config, {"adapter": "simple"})


def test_standard_backtest_request_parses_percent_and_normalizes_hk_symbol(tmp_path) -> None:
    from decimal import Decimal
    from open_trader.dashboard_web import parse_standard_backtest_request

    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({"market": "HK", "symbol": "700", "asset_class": "stock"})
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])

    parsed = parse_standard_backtest_request(config, {
        "market": "hk", "symbol": "00700", "strategy_id": "trend_pullback/v1",
        "range_preset": "CUSTOM", "custom_start": "2025-01-01",
        "custom_end": "2026-01-01", "max_strategy_weight": "10%",
    })

    assert parsed.market == "HK"
    assert parsed.symbol == "00700"
    assert parsed.max_strategy_weight == Decimal("0.10")
    assert parsed.custom_start == date(2025, 1, 1)


def test_standard_backtest_request_allows_custom_range_without_end_date(tmp_path) -> None:
    from open_trader.dashboard_web import parse_standard_backtest_request

    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({"market": "US", "symbol": "MSFT", "asset_class": "stock"})
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])
    parsed = parse_standard_backtest_request(config, {
        "market": "US", "symbol": "MSFT", "strategy_id": "trend_pullback/v1",
        "range_preset": "CUSTOM", "custom_start": "2025-01-01",
    })
    assert parsed.custom_start == date(2025, 1, 1)
    assert parsed.custom_end is None


@pytest.mark.parametrize("symbol", ["../../outside", "..", "BAD/S", "BAD\\S", "BAD:S", "BAD S"])
def test_standard_backtest_request_rejects_unsafe_symbol_grammar(tmp_path, symbol) -> None:
    from open_trader.dashboard_web import parse_standard_backtest_request

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    with pytest.raises(ValueError, match="标的代码格式无效"):
        parse_standard_backtest_request(config, {
            "market": "US", "symbol": symbol,
            "strategy_id": "trend_pullback/v1", "range_preset": "1Y",
        })


def test_standard_backtest_http_routes_expose_options_and_map_validation_to_400(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        options = read_json(f"http://{host}:{port}/api/backtests/options")
        assert options["defaults"]["range"] == "1Y"
        status, _, payload = post_error_json(
            f"http://{host}:{port}/api/backtests/standard/run",
            json.dumps({"adapter": "simple"}).encode(),
        )
        assert status == 400
        assert payload["message"] == "不支持从界面选择回测执行工具"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("body", [b"{bad json", b"[]"])
def test_standard_backtest_http_rejects_invalid_json_objects_with_chinese_400(
    tmp_path, body
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        status, _, payload = post_error_json(
            f"http://{host}:{port}/api/backtests/standard/run", body
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert status == 400
    assert payload["message"] == "请求正文必须是有效的 JSON 对象"


@pytest.mark.parametrize(
    ("content_length", "expected_status", "expected_message"),
    [
        ("invalid", 400, "Content-Length 必须是非负整数"),
        ("-1", 400, "Content-Length 必须是非负整数"),
        (str(1024 * 1024 + 1), 413, "请求正文不能超过 1 MiB"),
    ],
)
def test_dashboard_http_rejects_invalid_or_oversized_content_length_before_read(
    tmp_path, content_length, expected_status, expected_message
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    server = create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.putrequest("POST", "/api/backtests/standard/run")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", content_length)
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert response.status == expected_status
    assert payload["message"] == expected_message


def test_owned_backtest_provider_close_failure_is_execution_error(tmp_path, monkeypatch) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    class Provider:
        def close(self) -> None:
            raise RuntimeError("close boom")

    class Result:
        def to_dict(self) -> dict[str, str]:
            return {"status": "ok"}

    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: Provider())
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", lambda *_, **__: Result())
    request = {"market": "US", "symbol": "VIXY", "strategy_id": "trend_pullback/v1"}

    with pytest.raises(dashboard_web.StandardBacktestExecutionError, match="关闭.*close boom"):
        dashboard_web.build_standard_backtest_run_payload(config, request)


def test_owned_backtest_provider_close_failure_does_not_mask_run_failure(tmp_path, monkeypatch) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    class Provider:
        def close(self) -> None:
            raise RuntimeError("close boom")

    def fail(*args, **kwargs):
        raise RuntimeError("run boom")

    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: Provider())
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", fail)
    request = {"market": "US", "symbol": "VIXY", "strategy_id": "trend_pullback/v1"}

    with pytest.raises(dashboard_web.StandardBacktestExecutionError, match="run boom") as error:
        dashboard_web.build_standard_backtest_run_payload(config, request)
    assert "close boom" not in str(error.value)


@pytest.mark.parametrize(
    ("run_error", "expected"),
    [(None, "行情服务关闭失败：close boom"), ("run boom", "标准策略回测执行失败：run boom")],
)
def test_standard_backtest_http_maps_owned_provider_lifecycle_errors_to_502(
    tmp_path, monkeypatch, run_error, expected
) -> None:
    import open_trader.dashboard_web as dashboard_web

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    class Provider:
        def close(self) -> None:
            raise RuntimeError("close boom")

    class Result:
        def to_dict(self) -> dict[str, str]:
            return {"status": "ok"}

    def run(*args, **kwargs):
        if run_error:
            raise RuntimeError(run_error)
        return Result()

    monkeypatch.setattr(dashboard_web, "FutuQuoteClient", lambda **_: Provider())
    monkeypatch.setattr(dashboard_web, "run_standard_backtest", run)
    server = dashboard_web.create_dashboard_server(
        config, "127.0.0.1", 0, quote_service=FakeQuoteService(quote_result())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        status, _, payload = post_error_json(
            f"http://{host}:{port}/api/backtests/standard/run",
            json.dumps({
                "market": "US", "symbol": "VIXY",
                "strategy_id": "trend_pullback/v1",
            }).encode(),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert status == 502
    assert payload["message"] == expected


def test_dashboard_static_removes_legacy_holding_backtest_ui() -> None:
    source = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "查看回测" not in source
    assert 'data-detail-mode="backtest"' not in source
    assert 'fetch("/api/backtests/run"' not in source
    assert "header-backtest-filters" not in html
    assert "backtest-price-sync-status" not in html


def test_dashboard_has_one_global_backtest_entry_and_no_row_entry() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert html.count('id="open-standard-backtest"') == 1
    assert 'id="standard-backtest-workspace"' in html
    assert 'id="header-backtest-filters"' not in html
    assert 'data-detail-mode="backtest"' not in js
    assert "查看回测" not in js


def test_standard_backtest_workspace_builds_request_without_adapter() -> None:
    output = run_dashboard_js(
        r"""
state.standardBacktest.symbolKey = "US:MSFT";
state.standardBacktest.strategyId = "trend_pullback/v1";
state.standardBacktest.rangePreset = "3Y";
state.standardBacktest.initialCash = "250000";
state.standardBacktest.maxWeight = "10%";
const request = buildStandardBacktestRequest();
if (request.market !== "US" || request.symbol !== "MSFT") throw new Error(JSON.stringify(request));
if (request.strategy_id !== "trend_pullback/v1" || request.range_preset !== "3Y") throw new Error(JSON.stringify(request));
if (request.adapter !== undefined) throw new Error("adapter leaked to UI");
if (request.initial_cash !== "250000") throw new Error(JSON.stringify(request));
if (request.max_strategy_weight !== "10%" || request.commission_bps !== "10") throw new Error(JSON.stringify(request));
console.log("ok");
"""
    )
    assert "ok" in output


def test_standard_backtest_custom_dates_and_safe_error_contract() -> None:
    output = run_dashboard_js(
        r"""
state.standardBacktest.rangePreset = "CUSTOM";
state.standardBacktest.customStart = "";
state.standardBacktest.customEnd = "";
if (validateStandardBacktestDates() !== "自定义区间必须填写开始日期。") throw new Error("missing start");
state.standardBacktest.customStart = "2026-01-02";
state.standardBacktest.customEnd = "2026-01-02";
if (validateStandardBacktestDates() !== "开始日期必须早于结束日期。") throw new Error("equal dates");
state.standardBacktest.customEnd = "";
if (validateStandardBacktestDates() !== "") throw new Error("optional end rejected");
if (safeBacktestErrorMessage({message: "参数有误"}) !== "参数有误") throw new Error("Chinese message lost");
if (safeBacktestErrorMessage({message: "Internal Server Error"}) !== "回测请求失败，请稍后重试。") throw new Error("English leaked");
if (safeBacktestErrorMessage({message: "参数 invalid: Internal Server Error"}) !== "回测请求失败，请稍后重试。") throw new Error("mixed English leaked");
if (safeBacktestErrorMessage({message: "参数 X 无效"}) !== "回测请求失败，请稍后重试。") throw new Error("single Latin leaked");
if (safeBacktestErrorMessage({message: "错误 E"}) !== "回测请求失败，请稍后重试。") throw new Error("Latin code leaked");
if (safeBacktestErrorMessage(null) !== "回测请求失败，请稍后重试。") throw new Error("fallback missing");
console.log("ok");
"""
    )
    assert "ok" in output


def test_standard_backtest_workspace_accessibility_and_hidden_results_contract() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert 'id="backtest-initial-cash"' in html
    assert 'role="group"' in html
    assert "aria-pressed" in js
    assert 'elements["standard-backtest-results"].hidden = false' not in js
    assert 'elements["standard-backtest-results"].innerHTML' not in js


def test_standard_backtest_dom_click_and_submit_flow() -> None:
    output = run_dashboard_js(r"""
class E {
  constructor(){this.dataset={};this.value="";this.hidden=false;this.disabled=false;this.required=false;this.innerHTML="";this.textContent="";this.listeners={};this.classList={add(){},remove(){},toggle(){}};}
  addEventListener(n,f){this.listeners[n]=f;} click(target=this){return this.listeners.click&&this.listeners.click({target,preventDefault(){}});} submit(){return this.listeners.submit({preventDefault(){}});}
  closest(s){if(s==="[data-backtest-source]"&&this.dataset.backtestSource)return this;if(s==="[data-strategy-id]"&&this.dataset.strategyId)return this;if(s==="[data-range-preset]"&&this.dataset.rangePreset)return this;return null;} querySelector(){return null;}
}
const nodes={}; document.getElementById=(id)=>nodes[id]||(nodes[id]=new E()); document.querySelector=()=>new E(); document.getElementById("standard-backtest-results").hidden=true;
const posts=[]; fetch=async(url,init={})=>{
 if(url==="/api/backtests/options")return{ok:true,json:async()=>({strategies:[{id:"trend_pullback/v1",name_zh:"趋势回调",description_zh:"说明"},{id:"breakout_momentum/v1",name_zh:"突破动量",description_zh:"说明"},{id:"range_mean_reversion/v1",name_zh:"区间均值回归",description_zh:"说明"}],ranges:["1Y","3Y","CUSTOM"],defaults:{range:"1Y",initial_cash:"100000",max_strategy_weight:"0.10",commission_bps:"10",slippage_bps:"5"},universe:{holdings:[{market:"US",symbol:"MSFT",name:"微软"}],watchlist:[{market:"HK",symbol:"00700",name:"腾讯"}]}})};
 posts.push({url,body:JSON.parse(init.body)});if(posts.length===2)return{ok:false,json:async()=>{throw new Error("html")}};return{ok:true,json:async()=>({status:"ok"})};};
bindElements();bindEvents();await elements["open-standard-backtest"].click();
if(elements["standard-backtest-workspace"].hidden||state.standardBacktest.symbolKey!=="US:MSFT")throw new Error("open failed");
const watch=new E();watch.dataset.backtestSource="watchlist";elements["backtest-symbol-source"].click(watch);
const range=new E();range.dataset.rangePreset="3Y";elements["backtest-range-controls"].click(range);
elements["backtest-initial-cash"].value="250000";elements["backtest-max-weight"].value="12%";elements["backtest-commission"].value="8";elements["backtest-slippage"].value="3";
await elements["standard-backtest-form"].submit();
if(posts.length!==1||posts[0].url!=="/api/backtests/standard/run"||posts[0].body.adapter!==undefined||posts[0].body.initial_cash!=="250000")throw new Error(JSON.stringify(posts));
if(elements["standard-backtest-results"].hidden||!elements["standard-backtest-results"].innerHTML.includes("回测对比"))throw new Error("results missing");
await elements["standard-backtest-form"].submit();if(elements["standard-backtest-status"].textContent!=="回测请求失败，请稍后重试。")throw new Error("unsafe fallback");
const custom=new E();custom.dataset.rangePreset="CUSTOM";elements["backtest-range-controls"].click(custom);if(!elements["backtest-custom-start"].required||elements["backtest-custom-end"].required)throw new Error("required mismatch");
elements["backtest-custom-start"].value="";await elements["standard-backtest-form"].submit();if(posts.length!==2||elements["standard-backtest-status"].textContent!=="自定义区间必须填写开始日期。")throw new Error("missing start fetched");
elements["backtest-custom-start"].value="2026-01-02";elements["backtest-custom-end"].value="2026-01-02";await elements["standard-backtest-form"].submit();if(posts.length!==2||elements["standard-backtest-status"].textContent!=="开始日期必须早于结束日期。")throw new Error("date order fetched");
elements["close-standard-backtest"].click();await elements["open-standard-backtest"].click();if(state.standardBacktest.initialCash!=="250000"||state.standardBacktest.source!=="watchlist")throw new Error("state lost");
console.log("ok");
""")
    assert "ok" in output


def test_standard_backtest_result_renders_normalized_comparisons_and_details(tmp_path) -> None:
    from tests.test_strategy_backtest import fixture_provider, standard_request
    from open_trader.strategy_backtest import run_standard_backtest

    fixture_result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=fixture_provider("breakout_next_open"),
    ).to_dict()
    fixture_result.update({
        "benchmark_symbol": "<SPY>", "run_id": "<run>",
        "requested_start": "<2025-01-01>", "manifest_path": "data/<manifest>.json",
    })
    fixture_result["strategy"]["trades"][0]["reason"] = "<规则触发>"
    output = run_dashboard_js('''
const target={innerHTML:"",hidden:true}; document.getElementById=(id)=>id==="standard-backtest-results"?target:null;
const fixtureResult=''' + json.dumps(fixture_result, ensure_ascii=False) + r''';
renderStandardBacktestResult(fixtureResult);
for(const expected of ["策略收益","买入持有","&lt;SPY&gt;","相对买入持有","相对市场指数","最大回撤","交易次数","胜率","BUY","EXIT","请求范围","实际数据","breakout_momentum/v1","交易假设","初始资金","最大策略仓位","佣金","滑点","固定参数","突破周期","HOLD（观察）","结果文件"]){if(!target.innerHTML.includes(expected))throw new Error(`missing ${expected}`)}
for(const hostile of ["data/<manifest>","<规则触发>","<2025-01-01>","<run>"]){if(target.innerHTML.includes(hostile))throw new Error("dynamic value not escaped: "+hostile)}
if(target.hidden)throw new Error("result remains hidden"); console.log("ok");
''')
    assert "ok" in output


def test_generated_standard_backtest_payload_renders_finite_price_path_and_marker(tmp_path) -> None:
    from tests.test_strategy_backtest import fixture_provider, standard_request
    from open_trader.strategy_backtest import run_standard_backtest

    fixture_result = run_standard_backtest(
        standard_request(tmp_path, strategy_id="breakout_momentum/v1"),
        price_provider=fixture_provider("breakout_next_open"),
    ).to_dict()
    output = run_dashboard_js('''
const result=''' + json.dumps(fixture_result, ensure_ascii=False) + r''';
const chart=renderPriceActionChart(result.strategy.equity_curve,result.strategy.trades);
const path=(chart.match(/class="backtest-price-line" d="([^"]+)"/)||[])[1]||"";
if(!path.includes("M")||!path.includes("L")||/NaN|Infinity/.test(path))throw new Error(`invalid price path: ${path}`);
const marker=(chart.match(/<circle cx="([^"]+)" cy="([^"]+)" r="5"><\/circle>/)||[]);
if(!marker.length||!Number.isFinite(Number(marker[1]))||!Number.isFinite(Number(marker[2])))throw new Error(`invalid marker: ${chart}`);
console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_treats_zero_trades_as_success() -> None:
    output = run_dashboard_js(r'''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const result={strategy:{trades:[],equity_curve:[],total_return_pct:"0",max_drawdown_pct:"0",win_rate_pct:"0",initial_cash:"100",initial_allocated_notional:"10"},buy_hold:{equity_curve:[],total_return_pct:"0"},market_benchmark:{equity_curve:[],total_return_pct:"0"},benchmark_symbol:"SPY"};
renderStandardBacktestResult(result); if(!target.innerHTML.includes("所选区间内没有触发交易")||target.innerHTML.includes("error"))throw new Error(target.innerHTML); console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_isolates_missing_market_benchmark(tmp_path) -> None:
    from tests.test_strategy_backtest import fixture_provider, standard_request
    from open_trader.strategy_backtest import run_standard_backtest

    fixture_result = run_standard_backtest(
        standard_request(tmp_path), price_provider=fixture_provider("missing_benchmark"),
    ).to_dict()
    output = run_dashboard_js('''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const result=''' + json.dumps(fixture_result, ensure_ascii=False) + r''';
renderStandardBacktestResult(result); if(!target.innerHTML.includes("策略收益")||!target.innerHTML.includes("基准行情缺失，无法比较"))throw new Error(target.innerHTML); console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_bounds_large_and_invalid_chart_data() -> None:
    output = run_dashboard_js(r'''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const rows=Array.from({length:50000},(_,i)=>({date:`2025-${String(1+(i%12)).padStart(2,"0")}-${String(1+(i%28)).padStart(2,"0")}-${i}`,equity:String(100000+i),close:String(100+i/100)}));
rows[10].equity="NaN"; rows[11].equity="Infinity"; rows[12].close="bad";
const trades=Array.from({length:700},(_,i)=>({execution_date:rows[i*50].date,action:i%2?"BUY":"HOLD",quantity:"1",raw_price:i===2?"Infinity":rows[i*50].close,execution_price:"100",fees:"1",reason:"记录"}));
const result={strategy:{trades,equity_curve:rows,total_return_pct:"1",max_drawdown_pct:"-1",win_rate_pct:"1"},buy_hold:{equity_curve:rows,total_return_pct:"1"},market_benchmark:{equity_curve:rows,total_return_pct:"1"},benchmark_symbol:"SPY",signals:[],assumptions:{},strategy_definition:{parameters:{}}};
renderStandardBacktestResult(result);
if(/NaN|Infinity/.test(target.innerHTML))throw new Error("non-finite SVG output");
if((target.innerHTML.match(/<tr>/g)||[]).length!==501)throw new Error("trade rows not bounded");
if(!target.innerHTML.includes("仅显示前 500 笔，共 700 笔"))throw new Error("missing trade limit notice");
for(const d of [...target.innerHTML.matchAll(/ d="([^"]*)"/g)].map(x=>x[1]))if((d.match(/[ML]/g)||[]).length>600)throw new Error("chart not downsampled");
console.log("ok");
''')
    assert "ok" in output


def test_standard_backtest_result_aggregates_and_bounds_action_markers() -> None:
    output = run_dashboard_js(r'''
const target={innerHTML:"",hidden:true}; document.getElementById=()=>target;
const rows=Array.from({length:1000},(_,i)=>({date:`d${i}`,equity:String(100000+i),close:String(100+i/100)}));
const actions=["BUY","ADD","REDUCE","EXIT"];
const trades=Array.from({length:50000},(_,i)=>({execution_date:rows[i%rows.length].date,action:actions[i%4],quantity:"1",raw_price:i===49999?"Infinity":rows[i%rows.length].close,execution_price:"100",fees:"1",reason:"大量记录"}));
const result={strategy:{trades,equity_curve:rows,total_return_pct:"1",max_drawdown_pct:"-1",win_rate_pct:"1"},buy_hold:{equity_curve:rows,total_return_pct:"1"},market_benchmark:{equity_curve:rows,total_return_pct:"1"},benchmark_symbol:"SPY",signals:[],assumptions:{},strategy_definition:{parameters:{}}};
const chart=renderPriceActionChart(rows,trades);
const markerCount=(chart.match(/<g class="backtest-action-marker/g)||[]).length;
if(markerCount>600)throw new Error(`unbounded markers ${markerCount}`);
if(!chart.includes("×"))throw new Error("aggregated count missing");
if(!chart.includes("另有 ")||!chart.includes("组交易标记未显示"))throw new Error("omitted notice missing");
const aria=(chart.match(/aria-label="([^"]*)"/)||[])[1]||"";
if(aria.length>50000)throw new Error(`unbounded aria ${aria.length}`);
if(/NaN|Infinity/.test(chart))throw new Error("invalid numeric output");
console.log("ok");
''')
    assert "ok" in output


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


class FakeBacktestPriceProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[object]:
        from open_trader.kline_technical_facts import DailyKlineBar

        self.requests.append({"futu_symbol": futu_symbol, "start": start, "end": end})
        return [
            DailyKlineBar(
                date="2026-06-19",
                open=41.0,
                high=43.0,
                low=40.0,
                close=42.0,
                volume=1000.0,
            )
        ]


class RaisingBacktestPriceProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[object]:
        self.requests.append({"futu_symbol": futu_symbol, "start": start, "end": end})
        raise RuntimeError("kline unavailable")


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


def run_dashboard_js(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    runner = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} }, console };
(async () => {
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  await vm.runInContext(`(async () => {${process.argv[2]}})()`, sandbox);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", runner, str(js_path), script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_dashboard_trading_decision_tabs() -> None:
    output = run_dashboard_js(
        r'''
function assertOrdered(html, labels) {
  let cursor = -1;
  for (const label of labels) {
    const next = html.indexOf(label, cursor + 1);
    if (next <= cursor) throw new Error("tab order mismatch: " + html);
    cursor = next;
  }
}
const holding = {
  market: "US",
  symbol: "NVDA",
  name: "英伟达",
  total_quantity: "10",
  agent_report: { available: true, error: "" },
  decision_plan: {
    available: true,
    mode: "validated_plan",
    status: "waiting",
    run_date: "2026-07-13",
    action_summary: "继续持有，等待条件触发",
    max_weight: "0.10",
    strategy: {id: "trend_pullback/v1", name_zh: "趋势回调"},
    conditions: [],
    backtests: [],
  },
  tradingagents_summary: {
    available: true,
    error: "",
    ta_view: "偏多",
    current_action: "持有",
    core_reason: "趋势仍在",
  },
  decision_facts: {
    kline: { available: true, fields: { trend: "上涨" } },
    news_sentiment: { available: false, error: "新闻任务失败" },
  },
  futu_skill_facts: {},
};
state.selectedDecisionTab = "final";
let html = renderTradingDecisionTabs(holding);
assertOrdered(html, ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]);
if ((html.match(/role="tabpanel"/g) || []).length !== 1) throw new Error(html);
if (!html.includes('data-decision-tab="news"') || !html.includes("decision-tab-failed")) throw new Error(html);
if (!html.includes("今日交易计划") || html.includes("大模型决策模板") || html.includes("<h4>TradingAgents</h4>")) throw new Error(html);
state.selectedDecisionTab = "tradingagents";
html = renderTradingDecisionTabs(holding);
if (!html.includes("<h4>TradingAgents</h4>") || html.includes("今日交易计划")) throw new Error(html);
const missingSummary = {
  ...holding,
  tradingagents_summary: {
    available: false,
    error: "TradingAgents summary is unavailable for current advice",
    ta_view: "低配",
    current_action: "持有",
    core_reason: "缺失",
  },
};
state.selectedDecisionTab = "tradingagents";
html = renderTradingDecisionTabs(missingSummary);
const tradingagentsTab = html.match(/<button[^>]*data-decision-tab="tradingagents"[^>]*>/)[0];
if (!tradingagentsTab.includes("decision-tab-failed") || !html.includes("status-failed") || !html.includes("TradingAgents summary is unavailable for current advice")) throw new Error(html);
state.selectedDecisionTab = "news";
html = renderTradingDecisionTabs(holding);
if ((html.match(/role="tabpanel"/g) || []).length !== 1 || !html.includes("新闻任务失败")) throw new Error(html);
state.selectedDecisionTab = "futu";
html = renderTradingDecisionTabs(holding);
if ((html.match(/role="tabpanel"/g) || []).length !== 1 || !html.includes("数据未生成")) throw new Error(html);

const technicalHolding = {
  ...holding,
  decision_facts: {},
  technical_facts: {
    available: true,
    status: "usable",
    facts: { timeframes: [{ timeframe_label: "日线" }] },
  },
};
state.selectedDecisionTab = "kline";
html = renderTradingDecisionTabs(technicalHolding);
if (html.includes("decision-tab-empty") || !html.includes("趋势 / K 线")) throw new Error(html);

const staleTechnicalHolding = {
  ...holding,
  decision_facts: { kline: { available: false, error: "" } },
  technical_facts: {
    available: false,
    status: "stale_run_date",
    error: "technical facts run date does not match latest advice",
  },
};
state.selectedDecisionTab = "kline";
html = renderTradingDecisionTabs(staleTechnicalHolding);
if (!html.includes("status-failed") || !html.includes("technical facts run date does not match latest advice") || html.includes("数据未生成")) throw new Error(html);

let renders = 0;
renderHoldings = () => { renders += 1; };
handleSymbolDetailClick({ target: { closest: (selector) => selector === "[data-decision-tab]" ? { dataset: { decisionTab: "kline" } } : null } });
if (state.selectedDecisionTab !== "kline" || renders !== 1) throw new Error("tab click did not render");
state.selectedDecisionTab = "news";
showSymbolDetail("US|NVDA", "decision");
if (state.selectedDecisionTab !== "final") throw new Error("new holding did not reset tab");
console.log("ok");
'''
    )

    assert "ok" in output


def test_dashboard_news_tab_uses_futu_skill_news_sentiment() -> None:
    output = run_dashboard_js(
        r'''
const holding = {
  decision_facts: {},
  futu_skill_facts: {
    news_sentiment: {
      available: true,
      domestic_discussion: { summary: "国内投资者关注存储链联动" },
    },
  },
};
state.selectedDecisionTab = "news";
const html = renderTradingDecisionTabs(holding);
const tab = html.match(/<button[^>]*data-decision-tab="news"[^>]*>/)[0];
if (tab.includes("decision-tab-failed")) throw new Error(tab);
if (!html.includes("富途社区 / 国内讨论") || !html.includes("国内投资者关注存储链联动")) throw new Error(html);
console.log("ok");
'''
    )

    assert "ok" in output


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
    assert "renderBacktestPriceSyncStatus" not in js
    assert "全部市场" in html
    assert "symbol-detail-panel" in html
    assert "dashboard-header" in html
    assert "header-market-filters" in html
    assert "header-broker-filters" in html
    assert "header-backtest-filters" not in html
    assert "backtest-price-sync-status" not in html
    assert "data-backtest=\"READY\"" not in html
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
    assert "futuAnomalySignalsPlugin" in js
    assert "translateFutuSignalValue" in js
    assert ".futu-signal-module-grid" in css

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
    decision_plugin_card_css = css.split(".decision-plugin-card {", 1)[1].split("}", 1)[0]
    assert "align-content: start;" in decision_plugin_card_css
    kelly_experiment_card_css = css.split(".kelly-experiment-card {", 1)[1].split("}", 1)[0]
    assert "align-content: start;" in kelly_experiment_card_css
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
    assert "基于已接入的交易决策与市场事实数据展示" in js
    assert "大模型决策模板" in js
    assert 'selectedDecisionTab: "final"' in js
    assert "const DECISION_TABS" in js
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
    assert ".market-section-row" in css
    assert ".market-section-us-stock" in css
    assert ".market-section-us-option" in css
    assert ".market-section-hk-stock" in css
    assert ".market-section-hk-option" in css
    assert ".symbol-cell" in css
    scoped_table_selector = ".holdings-panel > .table-wrap > table"
    assert scoped_table_selector in css
    global_table_css = css.split(scoped_table_selector, 1)[0]
    assert "table-layout: fixed;" not in global_table_css
    assert "min-width: 1120px;" in css
    assert "table-layout: fixed;" in css
    symbol_column_selector = (
        ".holdings-panel > .table-wrap > table > thead > tr > th:nth-child(3) {"
    )
    assert symbol_column_selector in css
    assert ".holdings-panel > .table-wrap > table th:nth-child(3) {" not in css
    assert ".holdings-panel > .table-wrap > table > thead > tr > th:nth-child(1) {" in css
    assert ".holdings-panel > .table-wrap > table > thead > tr > th:nth-child(10) {" in css
    symbol_column_css = css.split(symbol_column_selector, 1)[1].split("}", 1)[0]
    assert "width: 170px;" in symbol_column_css
    number_cell_css = css.split(".number-cell {", 1)[1].split("}", 1)[0]
    assert "text-align: right;" in number_cell_css
    market_section_other_css = css.split(".market-section-other td {", 1)[1].split("}", 1)[0]
    assert "border-bottom-color: var(--line);" in market_section_other_css
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


def test_trading_decision_tab_css() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert ".decision-tab-list" in css
    assert "overflow-x: auto" in css
    assert "flex-wrap: nowrap" in css
    assert ".decision-tab.active" in css
    assert ".decision-tab-failed" in css
    assert ".decision-tab-panel" in css


def test_dashboard_static_contains_kelly_lab_panel_mount() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="kelly-lab-panel"' in html


def test_dashboard_js_renders_kelly_lab_panel() -> None:
    run_dashboard_js(
        """
state.dashboard = {
  kelly_lab: {
    available: true,
    experiment_count: 1,
    experiments: [{
      experiment_id: "trend_pullback_20d_exp_20260707",
      experiment_name: "趋势回调 20D 第一批",
      market: "US",
      status: "running",
      locked: true,
      experiment_budget: "30000",
      budget_currency: "USD",
      market_capital_pool: {currency: "USD", amount: "30000"},
      capital_utilization_pct: "50",
      order_sync: {
        status: "success",
        environment: "SIMULATE",
        last_synced_at: "2026-07-08 10:08",
        order_count: 7,
        fill_count: 5,
        message: "富途模拟盘订单已同步。",
        next_action: "可以继续扫描入场与退出信号。",
        orders: [
          {
            market: "US",
            symbol: "RAM",
            side: "buy",
            submitted_at: "2026-07-08 10:01",
            order_price: "12.34",
            order_qty: "800",
            filled_qty: "800",
            avg_fill_price: "12.34",
            status: "filled",
            order_id: "SIM-10001"
          },
          {
            market: "HK",
            symbol: "02840",
            side: "sell",
            submitted_at: "2026-07-08 10:03",
            order_price: "218.80",
            order_qty: "100",
            filled_qty: "0",
            avg_fill_price: "-",
            status: "submitted",
            order_id: "SIM-10002"
          }
        ]
      },
      order_execution: {
        status: "partial",
        environment: "DRY_RUN",
        source: "dry_run",
        last_executed_at: "2026-07-10 13:32",
        execution_count: 2,
        submitted_count: 0,
        dry_run_count: 1,
        skipped_count: 1,
        failed_count: 0,
        message: "Kelly 订单执行存在失败或跳过项。",
        executions: [
          {
            intent_id: "trend_pullback_20d_exp_20260707:US:RAM:entry",
            market: "US",
            symbol: "RAM",
            futu_code: "US.RAM",
            side: "buy",
            order_type: "NORMAL",
            price: "12.50",
            qty: "80",
            planned_notional: "400",
            budget_currency: "USD",
            execution_status: "dry_run",
            futu_order_id: "",
            executed_at: "2026-07-10 13:32",
            error: ""
          },
          {
            intent_id: "trend_pullback_20d_exp_20260707:HK:02840:exit",
            market: "HK",
            symbol: "02840",
            futu_code: "HK.02840",
            side: "sell",
            order_type: "NORMAL",
            price: "3000",
            qty: "",
            planned_notional: "",
            budget_currency: "USD",
            execution_status: "skipped",
            futu_order_id: "",
            executed_at: "2026-07-10 13:32",
            error: "missing order quantity"
          }
        ]
      },
      lifecycle_states: [
        {
          status: "watching",
          market: "US",
          symbol: "DRAM",
          reason: "价格距离 MA20 仍有 2.4%，入场规则未满足。",
          updated_at: "2026-07-08 10:00"
        },
        {
          status: "pending_entry_order",
          market: "US",
          symbol: "RAM",
          reason: "入场规则触发，仓位计算与风控检查待执行。",
          action: "等待仓位计算与风控检查",
          updated_at: "2026-07-08 10:01"
        },
        {
          status: "holding",
          market: "US",
          symbol: "SOXX",
          reason: "模拟盘买入已成交，当前监控退出规则。",
          action: "继续检查止盈、止损、移动止盈、时间退出",
          updated_at: "2026-07-08 10:02"
        },
        {
          status: "pending_exit_order",
          market: "HK",
          symbol: "02840",
          reason: "止盈触发，价格达到入场价 + 2R。",
          action: "准备卖出 50%",
          updated_at: "2026-07-08 10:03"
        }
      ],
      template: {
        strategy_id: "trend_pullback_20d",
        strategy_name: "趋势回调 20D",
        strategy_version: "v1",
        entry_rule_description: "结构化规则生成入场。",
        exit_rule_description: "目标价、止损或 20 个交易日到期。",
        rules: {
          entry: {
            type: "pullback_to_moving_average",
            ma_days: 20,
            tolerance_pct: 1,
            trend_filter: {type: "moving_average_slope", ma_days: 50, direction: "up"}
          },
          stop_loss: {
            type: "any_of",
            rules: [
              {type: "pct_below_moving_average", ma_days: 20, pct: 3},
              {type: "recent_swing_low_break", lookback_days: 20}
            ]
          },
          take_profit: {type: "risk_multiple", trigger_r: 2, sell_pct: 50},
          trailing_stop: {type: "close_below_moving_average", ma_days: 10, apply_to_remaining_position: true},
          time_exit: {type: "max_holding_days", days: 20, exit_if: "no_take_profit_or_stop_loss"}
        }
      },
      participants: [
        {market: "US", symbol: "DRAM", name: "Roundhill Memory ETF", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"},
        {market: "US", symbol: "RAM", name: "2倍做多DRAM ETF-T-REX", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"},
        {market: "US", symbol: "SOXX", name: "iShares费城交易所半导体ETF", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"},
        {market: "HK", symbol: "02840", name: "SPDR金", source: "holding", per_symbol_budget: "25000", budget_currency: "USD"}
      ],
      stats: {
        completed_samples: 18,
        open_samples: 4,
        observed_win_rate: "56%",
        sample_stage: "insufficient",
        winning_samples: 10,
        losing_samples: 8,
        raw_win_rate: "56%",
        adjusted_win_rate: "52%",
        avg_net_win_pct: "4.8%",
        avg_net_loss_pct: "2.9%",
        payoff_ratio: "1.66",
        full_kelly_pct: "23.1%",
        fractional_kelly_pct: "5.8%",
        suggested_position_pct: "4%",
        sample_adjustment: "样本少于 200，向 50% 收缩",
        last_sample_closed_at: "2026-07-07 15:30",
        last_recomputed_at: "2026-07-07 15:31"
      }
    },
    {
      experiment_id: "breakout_10d_mock_20260707",
      experiment_name: "突破 10D Mock 第一批",
      market: "US",
      status: "running",
      locked: true,
      experiment_budget: "30000",
      budget_currency: "USD",
      market_capital_pool: {currency: "USD", amount: "30000"},
      capital_utilization_pct: "40",
      order_sync: {
        status: "failed",
        environment: "SIMULATE",
        last_synced_at: "2026-07-08 10:09",
        order_count: 3,
        fill_count: 2,
        message: "模拟盘订单同步失败：OpenD 不可用。",
        next_action: "本轮不下单，保留现有订单状态。",
        orders: [
          {
            market: "US",
            symbol: "MSFT",
            side: "buy",
            submitted_at: "2026-07-08 10:05",
            order_price: "505.10",
            order_qty: "20",
            filled_qty: "0",
            avg_fill_price: "-",
            status: "rejected",
            order_id: "SIM-20001"
          }
        ]
      },
      order_execution: {
        status: "failed",
        environment: "SIMULATE",
        source: "futu_simulate_order_execution_client",
        last_executed_at: "2026-07-10 13:35",
        execution_count: 1,
        submitted_count: 0,
        dry_run_count: 0,
        skipped_count: 0,
        failed_count: 1,
        message: "Kelly 订单执行存在失败或跳过项。",
        executions: [
          {
            intent_id: "breakout_10d_mock_20260707:US:MSFT:entry",
            market: "US",
            symbol: "MSFT",
            futu_code: "US.MSFT",
            side: "buy",
            order_type: "NORMAL",
            price: "505.10",
            qty: "1",
            planned_notional: "505.10",
            budget_currency: "USD",
            execution_status: "failed",
            futu_order_id: "",
            executed_at: "2026-07-10 13:35",
            error: "OpenD disconnected"
          }
        ]
      },
      template: {
        strategy_id: "breakout_10d",
        strategy_name: "突破 10D",
        strategy_version: "v1",
        entry_rule_description: "结构化规则生成入场。",
        exit_rule_description: "目标价、止损或 10 个交易日到期。",
        rules: {
          entry: {
            type: "volume_breakout_high",
            lookback_days: 10,
            volume_multiple: 1.5
          },
          stop_loss: {
            type: "any_of",
            rules: [
              {type: "pct_below_reference_price", reference: "breakout_price", pct: 2},
              {type: "atr_below_entry", atr_multiple: 1.5}
            ]
          },
          take_profit: {type: "risk_multiple", trigger_r: 2, sell_pct: 50},
          trailing_stop: {type: "close_below_recent_low", lookback_days: 5, apply_to_remaining_position: true},
          time_exit: {type: "max_holding_days", days: 10, exit_if: "minimum_unrealized_r_not_reached", min_unrealized_r: 1}
        }
      },
      participants: [
        {market: "US", symbol: "MSFT", name: "微软", source: "watchlist", per_symbol_budget: "15000", budget_currency: "USD"},
        {market: "US", symbol: "TSM", name: "台积电", source: "holding", per_symbol_budget: "15000", budget_currency: "USD"},
        {market: "HK", symbol: "06951", name: "三环集团", source: "holding", per_symbol_budget: "15000", budget_currency: "USD"}
      ],
      stats: {
        completed_samples: 42,
        open_samples: 3,
        observed_win_rate: "52%",
        sample_stage: "open",
        winning_samples: 22,
        losing_samples: 20,
        raw_win_rate: "52%",
        adjusted_win_rate: "51%",
        avg_net_win_pct: "6.1%",
        avg_net_loss_pct: "3.4%",
        payoff_ratio: "1.79",
        full_kelly_pct: "24.2%",
        fractional_kelly_pct: "6.1%",
        suggested_position_pct: "4%",
        sample_adjustment: "样本少于 200，向 50% 收缩",
        last_sample_closed_at: "2026-07-07 15:45",
        last_recomputed_at: "2026-07-07 15:46"
      }
    }]
  }
};
state.workspaceView = "portfolio";
const entryHtml = renderKellyLabPanel();
if (!entryHtml.includes("凯利实验室") || !entryHtml.includes("data-workspace-view=\\\"kelly_lab\\\"")) {
  throw new Error("kelly lab entry missing: " + entryHtml);
}
if (entryHtml.includes("趋势回调 20D 第一批") || entryHtml.includes("US.AAPL")) {
  throw new Error("kelly lab entry leaked experiment details: " + entryHtml);
}
state.workspaceView = "kelly_lab";
const html = renderKellyLabPanel();
if (!html.includes("模拟盘策略实验室") || !html.includes("趋势回调 20D 第一批")) {
  throw new Error("kelly lab panel missing experiment identity: " + html);
}
if (!html.includes("role=\\\"tablist\\\"") || !html.includes("data-kelly-experiment=\\\"trend_pullback_20d_exp_20260707\\\"") || !html.includes("data-kelly-experiment=\\\"breakout_10d_mock_20260707\\\"")) {
  throw new Error("kelly lab strategy tabs missing: " + html);
}
const breakoutNameCount = html.split("突破 10D Mock 第一批").length - 1;
if (breakoutNameCount !== 1) {
  throw new Error("kelly lab should only render active strategy detail: " + html);
}
if (!html.includes("样本不足") || !html.includes("US.DRAM")) {
  throw new Error("kelly lab panel missing sample stage or participant: " + html);
}
function expectMetric(html, label, value, description) {
  const pattern = new RegExp("<div>\\\\s*<dt>" + label + "</dt>\\\\s*<dd>" + value + "</dd>\\\\s*</div>");
  if (!pattern.test(html)) {
    throw new Error(description + ": " + html);
  }
}
expectMetric(html, "市场", "US", "kelly lab panel missing market metric");
expectMetric(html, "模拟资金池", "USD 30000", "kelly lab panel missing capital pool metric");
for (const forbidden of ["US.MSFT", "US.TSM", "HK.06951"]) {
  if (html.includes(forbidden)) {
    throw new Error("kelly first tab leaked another strategy symbol " + forbidden + ": " + html);
  }
}
if (html.includes("实验参与标的") || html.includes("kelly-participant-row")) {
  throw new Error("kelly lab should use symbol states as the only symbol list: " + html);
}
for (const required of [
  "标的状态",
  "订单执行",
  "部分执行",
  "Kelly 订单执行存在失败或跳过项。",
  "DRY_RUN",
  "2026-07-10 13:32",
  "执行",
  "2",
  "预演",
  "1",
  "提交",
  "0",
  "跳过",
  "1",
  "计划金额",
  "富途订单",
  "错误",
  "400",
  "预演",
  "已跳过",
  "missing order quantity",
  "订单同步",
  "同步成功",
  "富途模拟盘订单已同步。",
  "SIMULATE",
  "2026-07-08 10:08",
  "订单",
  "7",
  "成交",
  "5",
  "可以继续扫描入场与退出信号。",
  "标的",
  "方向",
  "下单时间",
  "订单价",
  "订单数量",
  "成交数量",
  "成交均价",
  "状态",
  "US.RAM",
  "SIM-10001",
  "买入",
  "2026-07-08 10:01",
  "12.34",
  "800",
  "已成交",
  "HK.02840",
  "SIM-10002",
  "卖出",
  "218.80",
  "100",
  "0",
  "待成交",
  "观察中 → 待下单 → 持仓中 → 待退出 → 已完成",
  "观察中",
  "该标的在策略监控范围内，但当前没有入场信号，也没有持仓。",
  "待下单",
  "入场规则触发，仓位计算与风控检查待执行。",
  "持仓中",
  "模拟盘买入已成交，这笔策略样本正在进行中。",
  "待退出",
  "这笔持仓已经触发退出规则，但卖出还没有完成。",
  "US.SOXX",
  "HK.02840",
  "US.RAM",
  "US.DRAM",
  "策略详情",
  "入场",
  "价格回调到 20 日均线 ±1% 内，且 50 日均线斜率向上。",
  "止损",
  "跌破 20 日均线 3% 或跌破最近波段低点。",
  "止盈",
  "价格达到入场价 + 2R 时卖出 50%。",
  "移动止盈",
  "剩余仓位收盘跌破 10 日均线时退出。",
  "时间退出",
  "持有满 20 个交易日仍未触发止盈或止损则退出。",
  "参数推导",
  "原始胜率",
  "10 赢 / 8 亏",
  "修正胜率",
  "52%",
  "盈亏比 b",
  "1.66",
  "Full Kelly",
  "23.1%",
  "建议仓位",
  "4%",
  "样本少于 200，向 50% 收缩",
  "2026-07-07 15:31"
]) {
  if (!html.includes(required)) {
    throw new Error("kelly derivation missing " + required + ": " + html);
  }
}
if (html.includes("Mock 状态样本") || html.includes("状态说明")) {
  throw new Error("kelly lifecycle should be scoped inside strategy card, not global: " + html);
}
if (html.includes("风控通过") || html.includes("Kelly 建议单标的仓位 4%")) {
  throw new Error("pending entry narrative claims pre-risk approval: " + html);
}
if (html.includes("第一目标") || html.includes("延续")) {
  throw new Error("kelly strategy rules contain vague terms: " + html);
}
if (!html.includes("data-workspace-view=\\\"portfolio\\\"")) {
  throw new Error("kelly lab panel missing return button: " + html);
}
const fallbackHtml = renderKellyExperimentCard({
  experiment_name: "无状态样本策略",
  market: "US",
  status: "running",
  experiment_budget: "25000",
  budget_currency: "USD",
  order_sync: {
    status: "success",
    environment: "SIMULATE",
    last_synced_at: "2026-07-08 10:10",
    order_count: 0,
    fill_count: 0,
    message: "富途模拟盘订单已同步。",
    next_action: "等待下一次信号。"
  },
  participants: [{market: "US", symbol: "IBM", name: "IBM", source: "watchlist"}],
  template: {strategy_id: "fallback_strategy", strategy_name: "Fallback"},
  stats: {}
});
if (!fallbackHtml.includes("标的状态") || !fallbackHtml.includes("US.IBM") || !fallbackHtml.includes("等待该策略下一次入场信号。")) {
  throw new Error("kelly participant fallback lifecycle missing: " + fallbackHtml);
}
expectMetric(fallbackHtml, "市场", "US", "kelly fallback market metric missing");
expectMetric(fallbackHtml, "模拟资金池", "USD 25000", "kelly fallback capital pool missing");
const disabledPoolHtml = renderKellyExperimentCard({
  experiment_name: "禁用市场资金池策略",
  market: "CN",
  status: "running",
  experiment_budget: "150000",
  budget_currency: "CNY",
  market_capital_pool: {market: "CN", currency: "CNY", amount: "150000", enabled: false},
  participants: [{market: "CN", symbol: "600000", name: "浦发银行", source: "watchlist"}],
  template: {strategy_id: "disabled_pool_strategy", strategy_name: "Disabled Pool"},
  stats: {}
});
expectMetric(disabledPoolHtml, "市场", "CN", "kelly disabled pool market metric missing");
expectMetric(disabledPoolHtml, "模拟资金池", "未启用", "kelly disabled pool should show unavailable metric");
if (/<div>\\s*<dt>模拟资金池<\\/dt>\\s*<dd>CNY 150000<\\/dd>\\s*<\\/div>/.test(disabledPoolHtml)) {
  throw new Error("kelly disabled pool rendered active capital amount: " + disabledPoolHtml);
}
if (fallbackHtml.includes("实验参与标的") || fallbackHtml.includes("kelly-participant-row")) {
  throw new Error("kelly fallback should not render duplicate participant chips: " + fallbackHtml);
}
if (!fallbackHtml.includes("暂无同步订单明细。")) {
  throw new Error("kelly order sync empty detail missing: " + fallbackHtml);
}
state.selectedKellyExperimentId = "breakout_10d_mock_20260707";
const secondHtml = renderKellyLabPanel();
const trendNameCount = secondHtml.split("趋势回调 20D 第一批").length - 1;
if (!secondHtml.includes("突破 10D Mock 第一批") || trendNameCount !== 1) {
  throw new Error("kelly lab tab selection did not isolate active strategy: " + secondHtml);
}
if (!secondHtml.includes("价格放量突破近 10 个交易日高点，成交量不低于 1.5 倍均量。") || !secondHtml.includes("US.MSFT") || !secondHtml.includes("US.TSM") || !secondHtml.includes("HK.06951")) {
  throw new Error("kelly lab second tab content missing: " + secondHtml);
}
for (const required of ["订单同步", "同步失败", "模拟盘订单同步失败：OpenD 不可用。", "本轮不下单，保留现有订单状态。", "US.MSFT", "SIM-20001", "买入", "505.10", "20", "拒单"]) {
  if (!secondHtml.includes(required)) {
    throw new Error("kelly second tab order sync missing " + required + ": " + secondHtml);
  }
}
for (const required of ["订单执行", "执行失败", "Kelly 订单执行存在失败或跳过项。", "SIMULATE", "2026-07-10 13:35", "OpenD disconnected", "执行失败"]) {
  if (!secondHtml.includes(required)) {
    throw new Error("kelly second tab order execution missing " + required + ": " + secondHtml);
  }
}
for (const forbidden of ["US.DRAM", "US.RAM", "US.SOXX", "HK.02840"]) {
  if (secondHtml.includes(forbidden)) {
    throw new Error("kelly second tab leaked another strategy symbol " + forbidden + ": " + secondHtml);
  }
}
"""
    )


def test_dashboard_js_renders_kelly_parameter_source() -> None:
    html = run_dashboard_js(
        """
const html = renderKellyParameterDerivation({
  completed_samples: 2,
  open_samples: 1,
  observed_win_rate: "50%",
  sample_stage: "insufficient",
  raw_win_rate: "50%",
  adjusted_win_rate: "50%",
  avg_net_win_pct: "10%",
  avg_net_loss_pct: "5%",
  payoff_ratio: "2",
  full_kelly_pct: "25%",
  fractional_kelly_pct: "6.25%",
  suggested_position_pct: "4%",
  sample_adjustment: "样本少于 200，向 50% 收缩",
  source_trade_samples_generated_at: "2026-07-12 09:59",
  last_sample_closed_at: "2026-07-12 10:00",
  last_recomputed_at: "2026-07-12 10:01",
  parameter_source: "futu_paper_order_samples",
  skipped_order_count: 3
});
console.log(html);
"""
    )

    assert "样本状态" in html
    assert "样本不足" in html
    assert "已完成样本" in html
    assert "2" in html
    assert "进行中样本" in html
    assert "1" in html
    assert "参数来源" in html
    assert "富途模拟盘订单样本" in html
    assert "跳过订单" in html
    assert "3" in html
    assert "来源样本时间" in html
    assert "2026-07-12 09:59" in html
    assert "最近完成样本" in html
    assert "2026-07-12 10:00" in html
    assert "最近计算" in html
    assert "2026-07-12 10:01" in html


def test_dashboard_js_renders_kelly_unavailable_strategy_stats_error() -> None:
    html = run_dashboard_js(
        """
state.workspaceView = "kelly_lab";
state.dashboard = {
  kelly_lab: {
    available: false,
    error: "kelly_strategy_stats.json stale: source trade sample timestamp does not match"
  }
};
console.log(renderKellyLabPanel());
"""
    )

    assert "不可用" in html
    assert "kelly_strategy_stats.json" in html


def test_dashboard_renders_kelly_strategy_capital_panel() -> None:
    output = run_dashboard_js(
        """
state.dashboard = {
  kelly_lab: {
    available: true,
    experiments: [{
      experiment_id: "trend_pullback_20d_us_mock_20260707",
      experiment_name: "趋势回调 20D Mock US 第一批",
      market: "US",
      experiment_budget: "30000",
      budget_currency: "USD",
      status: "running",
      template: {
        strategy_id: "trend_pullback_20d",
        strategy_name: "趋势回调 20D",
        entry_rule_description: "价格回调到 20 日均线附近。"
      },
      stats: {},
      capital: {
        currency: "USD",
        budget: 30000,
        occupied_notional: 8460,
        position_notional: 6200,
        reserved_order_notional: 2260,
        available_notional: 21540,
        utilization_pct: 28.2,
        open_buy_order_count: 2,
        realized_pnl: 420,
        updated_at: "2026-07-10 13:45",
        symbol_occupancy: [
          {symbol: "US.RAM", occupied_notional: 8460}
        ],
        next_order_impact: {
          symbol: "US.RAM",
          estimated_notional: 1500,
          available_after_order: 20040,
          risk_status: "approved",
          reason: "订单提交后仍保留充足可用资金。"
        }
      }
    }]
  }
};
state.workspaceView = "kelly_lab";
const html = renderKellyLabPanel();
for (const required of [
  "策略资金",
  "总资金",
  "USD 30,000",
  "可用资金",
  "USD 21,540",
  "已占用",
  "USD 8,460",
  "下一笔下单影响",
  "US.RAM",
  "资金足够"
]) {
  if (!html.includes(required)) {
    throw new Error("kelly capital panel missing " + required + ": " + html);
  }
}
"""
    )
    assert output == ""


def test_dashboard_renders_kelly_strategy_capital_unavailable_fallback() -> None:
    output = run_dashboard_js(
        """
const baseExperiment = {
  experiment_name: "资金缺失策略",
  market: "US",
  experiment_budget: "30000",
  budget_currency: "USD",
  status: "running",
  template: {
    strategy_id: "trend_pullback_20d",
    strategy_name: "趋势回调 20D",
    entry_rule_description: "价格回调到 20 日均线附近。"
  },
  stats: {}
};
const missingHtml = renderKellyExperimentCard(baseExperiment);
const disabledHtml = renderKellyExperimentCard({
  ...baseExperiment,
  capital: {available: false}
});
for (const html of [missingHtml, disabledHtml]) {
  for (const required of ["策略资金", "策略资金数据暂不可用。"]) {
    if (!html.includes(required)) {
      throw new Error("kelly capital fallback missing " + required + ": " + html);
    }
  }
}
"""
    )
    assert output == ""


def test_dashboard_bounds_kelly_strategy_capital_utilization_widths() -> None:
    output = run_dashboard_js(
        """
const overflowingHtml = renderKellyExperimentCard({
  experiment_name: "资金超限策略",
  market: "US",
  budget_currency: "USD",
  status: "running",
  template: {strategy_id: "overflow", strategy_name: "Overflow"},
  stats: {},
  capital: {
    currency: "USD",
    budget: 100,
    occupied_notional: 250,
    position_notional: 140,
    reserved_order_notional: 90,
    available_notional: 0,
    utilization_pct: 250,
    open_buy_order_count: 1,
    realized_pnl: 0
  }
});
const invalidHtml = renderKellyExperimentCard({
  experiment_name: "资金异常策略",
  market: "US",
  budget_currency: "USD",
  status: "running",
  template: {strategy_id: "invalid", strategy_name: "Invalid"},
  stats: {},
  capital: {
    currency: "USD",
    budget: "not-a-number",
    occupied_notional: "",
    position_notional: "bad",
    reserved_order_notional: -25,
    available_notional: 0,
    utilization_pct: "bad",
    open_buy_order_count: 0,
    realized_pnl: 0
  }
});
for (const html of [overflowingHtml, invalidHtml]) {
  if (html.includes("NaN%") || /width:\\s*-/.test(html)) {
    throw new Error("kelly capital utilization emitted invalid width: " + html);
  }
  const widths = [...html.matchAll(/width:\\s*([0-9.]+)%/g)].map((match) => Number.parseFloat(match[1]));
  if (widths.length !== 2) {
    throw new Error("kelly capital utilization width count mismatch: " + html);
  }
  for (const width of widths) {
    if (!Number.isFinite(width) || width < 0 || width > 100) {
      throw new Error("kelly capital utilization width out of bounds " + width + ": " + html);
    }
  }
}
if (!overflowingHtml.includes('style="width: 100%"></span>') || !overflowingHtml.includes('style="width: 0%"></span>')) {
  throw new Error("kelly capital overflowing widths should clamp to 100 and 0: " + overflowingHtml);
}
"""
    )
    assert output == ""


def test_dashboard_renders_kelly_capital_producer_symbol_shape() -> None:
    output = run_dashboard_js(
        """
const html = renderKellyExperimentCard({
  experiment_name: "真实资金形状策略",
  market: "US",
  budget_currency: "USD",
  status: "running",
  template: {strategy_id: "producer", strategy_name: "Producer"},
  stats: {},
  capital: {
    currency: "USD",
    budget: 10000,
    occupied_notional: 3720,
    position_notional: 3720,
    reserved_order_notional: 0,
    available_notional: 6280,
    utilization_pct: 37.2,
    open_buy_order_count: 0,
    realized_pnl: 0,
    symbol_occupancy: [
      {market: "US", symbol: "RAM", notional: "3720"},
      {market: "US", symbol: "US.DRAM", notional: "500"}
    ],
    next_order_impact: {
      market: "US",
      symbol: "US.RAM",
      estimated_notional: 500,
      available_after_order: 5780,
      risk_status: "approved"
    }
  }
});
if (!html.includes("US.RAM") || !html.includes("USD 3,720")) {
  throw new Error("kelly producer symbol shape missing rendered symbol: " + html);
}
if (html.includes("US.US.RAM") || html.includes("US.US.DRAM")) {
  throw new Error("kelly producer symbol duplicated market prefix: " + html);
}
"""
    )
    assert output == ""
def obsolete_dashboard_backtest_filter_limits_holdings_and_ignores_cash_view() -> None:
    output = run_dashboard_js(
        r"""
state.dashboard = {
  holdings: [
    {
      market: "US",
      symbol: "READY",
      name: "Ready",
      brokers: "futu",
      backtest_readiness: { status: "ready", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOPRICE",
      name: "No Price",
      brokers: "futu",
      backtest_readiness: { status: "missing_prices", prices_missing: true, missing_fields: [] },
    },
    {
      market: "HK",
      symbol: "NOFIELD",
      name: "No Field",
      brokers: "phillips",
      backtest_readiness: { status: "missing_fields", prices_missing: false, missing_fields: ["target_1"] },
    },
    {
      market: "US",
      symbol: "UNSUPPORTED",
      name: "Unsupported",
      brokers: "tiger",
      backtest_readiness: { status: "unsupported_strategy", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOREADINESS",
      name: "No Readiness",
      brokers: "futu",
    },
  ],
  cash_rows: [
    { market: "CASH", symbol: "HKD_CASH", brokers: "futu", market_value_hkd: "100" },
  ],
};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.backtestFilter = "READY";
let symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "READY") {
  throw new Error("READY filter mismatch: " + symbols);
}
state.backtestFilter = "MISSING_PRICES";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "NOPRICE") {
  throw new Error("MISSING_PRICES filter mismatch: " + symbols);
}
state.backtestFilter = "MISSING_FIELDS";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "NOFIELD") {
  throw new Error("MISSING_FIELDS filter mismatch: " + symbols);
}
state.backtestFilter = "UNSUPPORTED";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "UNSUPPORTED") {
  throw new Error("UNSUPPORTED filter mismatch: " + symbols);
}
state.backtestFilter = "ALL";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "READY,NOPRICE,NOFIELD,UNSUPPORTED,NOREADINESS") {
  throw new Error("ALL filter mismatch: " + symbols);
}
state.marketFilter = "US";
state.backtestFilter = "READY";
symbols = filteredHoldings().map((holding) => holding.symbol).join(",");
if (symbols !== "READY") {
  throw new Error("combined market/backtest filter mismatch: " + symbols);
}
state.marketFilter = "CASH";
state.brokerFilter = "futu";
state.backtestFilter = "READY";
const cashRows = filteredCashRows();
if (cashRows.length !== 1 || cashRows[0].symbol !== "HKD_CASH") {
  throw new Error("backtest filter should not affect cash view: " + JSON.stringify(cashRows));
}
console.log("ok");
"""
    )

    assert "ok" in output


def obsolete_dashboard_backtest_filter_buttons_show_current_scope_counts() -> None:
    output = run_dashboard_js(
        r"""
state.dashboard = {
  holdings: [
    {
      market: "US",
      symbol: "READY",
      brokers: "futu",
      backtest_readiness: { status: "ready", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOPRICE",
      brokers: "futu",
      backtest_readiness: { status: "missing_prices", prices_missing: true, missing_fields: [] },
    },
    {
      market: "HK",
      symbol: "NOFIELD",
      brokers: "phillips",
      backtest_readiness: { status: "missing_fields", prices_missing: false, missing_fields: ["target_1"] },
    },
    {
      market: "US",
      symbol: "UNSUPPORTED",
      brokers: "tiger",
      backtest_readiness: { status: "unsupported_strategy", prices_missing: false, missing_fields: [] },
    },
    {
      market: "US",
      symbol: "NOREADINESS",
      brokers: "futu",
    },
  ],
};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.backtestFilter = "READY";
let html = renderBacktestFilterButtons();
for (const expected of ["全部回测 5", "可运行 1", "缺价格 1", "缺字段 1", "暂不支持 1"]) {
  if (!html.includes(expected)) {
    throw new Error("missing global count " + expected + ": " + html);
  }
}
if (!html.includes('data-backtest="READY"') || !html.includes("active")) {
  throw new Error("active backtest filter should remain selected: " + html);
}
state.marketFilter = "US";
state.brokerFilter = "futu";
state.backtestFilter = "ALL";
html = renderBacktestFilterButtons();
for (const expected of ["全部回测 3", "可运行 1", "缺价格 1", "缺字段 0", "暂不支持 0"]) {
  if (!html.includes(expected)) {
    throw new Error("missing scoped count " + expected + ": " + html);
  }
}
console.log("ok");
"""
    )

    assert "ok" in output


def obsolete_dashboard_renders_backtest_price_auto_sync_status() -> None:
    output = run_dashboard_js(
        r"""
let rendered = "";
elements["backtest-price-sync-status"] = {
  textContent: "",
  className: "",
};
state.dashboard = {
  backtest_price_sync: {
    status: "ok",
    attempted: 2,
    succeeded: 2,
    failed: 0,
    errors: [],
  },
};
renderBacktestPriceSyncStatus();
rendered = elements["backtest-price-sync-status"].textContent;
if (rendered !== "已自动补齐 2 个回测价格文件") {
  throw new Error("success sync status mismatch: " + rendered);
}
if (!elements["backtest-price-sync-status"].className.includes("status-ok")) {
  throw new Error("success sync status should use ok tone: " + elements["backtest-price-sync-status"].className);
}
state.dashboard = {
  backtest_price_sync: {
    status: "failed",
    attempted: 1,
    succeeded: 0,
    failed: 1,
    errors: [{ market: "US", symbol: "VIXY", message: "kline unavailable" }],
  },
};
renderBacktestPriceSyncStatus();
rendered = elements["backtest-price-sync-status"].textContent;
if (rendered !== "自动补齐失败 1 个：US.VIXY") {
  throw new Error("failed sync status mismatch: " + rendered);
}
if (!elements["backtest-price-sync-status"].className.includes("status-warning")) {
  throw new Error("failed sync status should use warning tone: " + elements["backtest-price-sync-status"].className);
}
state.dashboard = { backtest_price_sync: { status: "skipped", attempted: 0, succeeded: 0, failed: 0, errors: [] } };
renderBacktestPriceSyncStatus();
if (elements["backtest-price-sync-status"].textContent !== "") {
  throw new Error("skipped sync status should stay empty: " + elements["backtest-price-sync-status"].textContent);
}
console.log("ok");
"""
    )

    assert "ok" in output


def test_dashboard_renders_futu_anomaly_signal_card_in_chinese() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  name: "英伟达",
  portfolio_weight_hkd: "8.2%",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: true,
      status: "ok",
      signal: "supportive",
      confidence: "medium",
      suggested_constraint: "",
      window_days: 7,
      summary: "技术信号支持趋势。",
      categories: [
        {name: "MACD", state: "anomaly", direction: "bullish", detail: "金叉后继续放大。", evidence_date: "2026-07-01"},
        {name: "RSI", state: "anomaly", direction: "risk_up", detail: "接近超买区。", evidence_date: "2026-07-02"},
        {name: "K线形态", state: "none", direction: "", detail: "窗口内无异常。", evidence_date: ""}
      ]
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "mixed",
      confidence: "medium",
      suggested_constraint: "no_add",
      window_days: 7,
      summary: "资金流向与加仓动作存在分歧。",
      categories: [
        {name: "资金流向", state: "anomaly", direction: "bearish", detail: "主力资金连续净流出。", evidence_date: "2026-07-02"},
        {name: "卖空情况", state: "none", direction: "", detail: "窗口内无异常。", evidence_date: ""}
      ]
    },
    derivatives_anomaly: {
      available: true,
      status: "partial",
      signal: "risk_up",
      confidence: "low",
      suggested_constraint: "no_add",
      window_days: 7,
      summary: "期权波动率偏高。",
      categories: [
        {name: "期权波动率", state: "anomaly", direction: "risk_up", detail: "IV 位于高位。", evidence_date: "2026-07-02"},
        {name: "期权大单", state: "anomaly", direction: "bullish", detail: "出现看涨大单。", evidence_date: "2026-07-01"}
      ]
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
const end = html.length;
if (start < 0 || start >= end) {
  throw new Error("Futu signal card boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    for required in [
        "市场信号 · 富途异动信号",
        "技术异动",
        "资金异动",
        "衍生品异动",
        "支持",
        "不加仓",
        "部分可用",
        "偏多",
        "偏空",
        "风险上升",
        "无异常",
    ]:
        assert required in output

    for forbidden in [
        "supportive",
        "no_add",
        "partial",
        "risk_up",
        "bullish",
        "bearish",
        "schema",
    ]:
        assert forbidden not in output


def test_dashboard_futu_anomaly_opposing_signal_affects_overall() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: true,
      status: "ok",
      signal: "opposing",
      confidence: "medium",
      suggested_constraint: "",
      summary: "技术信号反对追高。",
      categories: [
        {name: "MACD", state: "anomaly", direction: "bearish", detail: "动能转弱。", evidence_date: "2026-07-02"}
      ]
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "资金无明显方向。",
      categories: []
    },
    derivatives_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "衍生品无明显方向。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf('<div class="futu-signal-overall">');
const end = html.indexOf('<div class="futu-signal-module-grid">');
if (start < 0 || end < 0 || start >= end) {
  throw new Error("Futu signal overall boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    assert "反对" in output
    assert "市场信号反对当前交易方向" in output
    assert "中性" not in output


def test_dashboard_futu_anomaly_missing_modules_do_not_render_neutral_direction() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: false,
      status: "missing",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "缺少富途技术异动数据。",
      categories: []
    },
    capital_anomaly: {
      available: false,
      status: "error",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途资金异动查询失败。",
      categories: []
    },
    derivatives_anomaly: {
      available: false,
      status: "stale",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途衍生品异动数据已过期。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf('<div class="futu-signal-module-grid">');
const end = html.indexOf('<p class="condition-box">');
if (start < 0 || end < 0 || start >= end) {
  throw new Error("Futu signal module boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    for required in ["<strong>缺失</strong>", "<strong>错误</strong>", "<strong>已过期</strong>"]:
        assert required in output
    assert "<strong>中性</strong>" not in output


def test_dashboard_futu_anomaly_unavailable_modules_do_not_render_neutral_overall() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: false,
      status: "missing",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "缺少富途技术异动数据。",
      categories: []
    },
    capital_anomaly: {
      available: false,
      status: "error",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途资金异动查询失败。",
      categories: []
    },
    derivatives_anomaly: {
      available: false,
      status: "stale",
      signal: "neutral",
      confidence: "low",
      suggested_constraint: "",
      summary: "富途衍生品异动数据已过期。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf('<div class="futu-signal-overall">');
const end = html.indexOf('<div class="futu-signal-module-grid">');
if (start < 0 || end < 0 || start >= end) {
  throw new Error("Futu signal overall boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    assert "需复核" in output
    assert "市场信号数据不可用" in output
    assert "窗口内未发现明显异动" not in output
    assert "<strong>中性</strong>" not in output


def test_dashboard_futu_anomaly_unknown_enums_render_safe_chinese_fallback() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: true,
      status: "schema",
      signal: "schema_break",
      confidence: "very_high",
      suggested_constraint: "unsafe_add",
      summary: "异常字段测试。",
      categories: [
        {name: "MACD", state: "invalid_state", direction: "strange_direction", detail: "未知枚举测试。", evidence_date: "2026-07-02"}
      ]
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "正常模块。",
      categories: []
    },
    derivatives_anomaly: {
      available: true,
      status: "ok",
      signal: "neutral",
      confidence: "medium",
      suggested_constraint: "",
      summary: "正常模块。",
      categories: []
    }
  }
};
const html = futuAnomalySignalsPlugin(holding);
const start = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
const end = html.length;
if (start < 0 || start >= end) {
  throw new Error("Futu signal card boundary missing: " + html);
}
console.log(html.slice(start, end));
"""
    )

    assert "未知" in output
    assert "MACD" in output
    for forbidden in [
        "schema",
        "schema_break",
        "very_high",
        "unsafe_add",
        "invalid_state",
        "strange_direction",
    ]:
        assert forbidden not in output


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
  const nextStart = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
  if (klineStart < 0 || newsStart < 0 || nextStart < 0 || !(klineStart < newsStart && newsStart < nextStart)) {
    throw new Error("fixed decision fact card boundaries missing: " + html);
  }
  return html.slice(klineStart, nextStart);
}
function renderDecisionFactCards(holding) {
  return renderDecisionPluginCard(klineDecisionFactsPlugin(holding))
    + renderDecisionPluginCard(newsSentimentPlugin(holding))
    + futuAnomalySignalsPlugin(holding);
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
const cards = fixedDecisionFactCards(renderDecisionFactCards(holding));
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
  const nextStart = html.indexOf("<h4>市场信号 · 富途异动信号</h4>");
  if (klineStart < 0 || newsStart < 0 || nextStart < 0 || !(klineStart < newsStart && newsStart < nextStart)) {
    throw new Error("fixed decision fact card boundaries missing: " + html);
  }
  return html.slice(klineStart, nextStart);
}
function renderDecisionFactCards(holding) {
  return renderDecisionPluginCard(klineDecisionFactsPlugin(holding))
    + renderDecisionPluginCard(newsSentimentPlugin(holding))
    + futuAnomalySignalsPlugin(holding);
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
const completeCards = fixedDecisionFactCards(renderDecisionFactCards({
  ...baseHolding,
  decision_facts: {
    kline: {available: true, fields: {trend: "过热拉升", position: "显著高于均线", momentum: "RSI 高位", key_levels: "支撑 580", risk: "超买风险"}},
    news_sentiment: {available: true, fields: {direction: "偏多", change: "较上次转强", catalyst: "AI 基建需求", risk: "估值过高", attention: "关注度升高"}}
  }
}));
assertStatus(cardBefore(completeCards, "<h4>新闻 / 舆论</h4>"), "可用", "ok");
assertStatus(cardFrom(completeCards, "<h4>新闻 / 舆论</h4>"), "可用", "ok");
const partialCards = fixedDecisionFactCards(renderDecisionFactCards({
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
const missingCards = fixedDecisionFactCards(renderDecisionFactCards({
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
  if (start < 0) {
    throw new Error("TradingAgents card boundaries missing: " + html);
  }
  return html.slice(start, end < 0 ? html.length : end);
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
const html = renderTradingAgentsSummaryCard({
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
const missingCard = tradingAgentsCard(renderTradingAgentsSummaryCard({
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


def test_dashboard_renders_kline_technical_card_without_duplicate_fact_grid() -> None:
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
          bollinger: {
            upper: "430.00",
            middle: "405.00",
            lower: "380.00",
            position: "middle_range",
            status: "neutral",
            reference_band: "",
            distance_pct: "",
            summary_zh: "当前价格位于日线布林带区间内",
            detail_zh: "价格未贴近上轨或下轨，布林带事实仅作背景展示。",
          },
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
  "日线布林带",
  "中性区间",
  "当前价格位于日线布林带区间内",
  "下轨 380.00",
  "中轨 405.00",
  "上轨 430.00"
]) {
  if (!card.includes(required)) {
    throw new Error("missing K-line bollinger fact " + required + ": " + card);
  }
}
for (const duplicate of ["日线 当前价", "日线 RSI", "日线 MACD", "周线 当前价", "条件："]) {
  if (card.includes(duplicate)) {
    throw new Error("duplicate K-line fact grid rendered " + duplicate + ": " + card);
  }
}
if (card.includes("待接入") || card.includes("占位") || card.includes("rsi:")) {
  throw new Error("usable technical facts rendered as placeholder/raw field: " + card);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def test_dashboard_renders_fixed_bollinger_card_without_internal_enums() -> None:
    script = r'''
const holding = {
  technical_facts: {
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {message: "日线数据截至 2026-07-03"},
    facts: {
      timeframes: [{
        timeframe: "daily",
        timeframe_label: "日线",
        current_price: "466.20",
        bollinger: {
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "above_upper",
          status: "upper_risk",
          reference_band: "upper",
          reference_value: "459.13",
          distance_pct: "1.5%",
          summary_zh: "当前价格已超过日线布林带上轨",
          detail_zh: "价格处在布林带上沿之外，说明短线偏热。",
        },
        rsi: {value: "56.88"},
        macd: {crossover: "金叉后延续"},
        moving_averages: {summary: "价格在主要均线上方"},
      }],
    },
  },
};
const html = renderDecisionPluginCard(klineTechnicalFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "布林带" in html
    assert "回调风险升高" in html
    assert "当前价格已超过日线布林带上轨" in html
    assert "当前价" in html
    assert "上轨" in html
    assert "偏离幅度" in html
    assert "technical-bollinger-card upper-risk" in html
    assert "upper_risk" not in html
    assert "above_upper" not in html


def test_dashboard_renders_bollinger_card_in_current_kline_plugin_path() -> None:
    script = r'''
const holding = {
  market: "US",
  symbol: "MSFT",
  portfolio_weight_hkd: "10.00%",
  decision_facts: {
    kline: {available: false, fields: {}},
    news_sentiment: {available: false, fields: {}},
  },
  technical_facts: {
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {message: "日线数据截至 2026-07-03"},
    facts: {
      timeframes: [{
        timeframe: "daily",
        timeframe_label: "日线",
        current_price: "466.20",
        bollinger: {
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "above_upper",
          status: "upper_risk",
          reference_band: "upper",
          reference_value: "459.13",
          distance_pct: "1.5%",
          summary_zh: "当前价格已超过日线布林带上轨",
          detail_zh: "价格处在布林带上沿之外，说明短线偏热。",
        },
      }],
    },
  },
};
const html = renderDecisionPluginCard(klineDecisionFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "<h4>趋势 / K 线</h4>" in html
    assert "technical-bollinger-card upper-risk" in html
    assert "回调风险升高" in html
    assert "当前价格已超过日线布林带上轨" in html
    assert "status-pill status-ok\">可用" in html
    assert "趋势</span>" not in html
    assert "upper_risk" not in html
    assert "above_upper" not in html


def test_dashboard_renders_kline_extraction_error_without_decision_field_noise() -> None:
    script = r'''
const holding = {
  market: "US",
  symbol: "RAM",
  portfolio_weight_hkd: "2.95%",
  decision_facts: {
    kline: {available: false, fields: {}},
    news_sentiment: {available: false, fields: {}},
  },
  technical_facts: {
    available: false,
    status: "extraction_error",
    data_date: "2026-07-02",
    run_date: "2026-07-04",
    error: "日线不足 20 根，无法计算布林带",
    freshness: {message: "指标周期缺失，需复核"},
    facts: {},
  },
};
const html = renderDecisionPluginCard(klineDecisionFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "<h4>趋势 / K 线</h4>" in html
    assert "不可用" in html
    assert "抽取失败" in html
    assert "日线不足 20 根，无法计算布林带" in html
    assert "趋势</span>" not in html
    assert "undefined" not in html


@pytest.mark.parametrize(
    ("status", "expected_label", "expected_class"),
    [
        ("lower_opportunity", "低位机会区域", "lower-opportunity"),
        ("neutral", "中性区间", "middle-range"),
        ("unknown", "布林带数据缺失", "missing"),
    ],
)
def test_dashboard_renders_bollinger_status_variants(
    status: str,
    expected_label: str,
    expected_class: str,
) -> None:
    script = f'''
const holding = {{
  technical_facts: {{
    available: true,
    status: "usable",
    data_date: "2026-07-03",
    run_date: "2026-07-04",
    freshness: {{message: "日线数据截至 2026-07-03"}},
    facts: {{
      timeframes: [{{
        timeframe: "daily",
        timeframe_label: "日线",
        current_price: "388.20",
        bollinger: {{
          upper: "459.13",
          middle: "399.62",
          lower: "340.11",
          position: "middle_range",
          status: "{status}",
          reference_band: "",
          reference_value: "",
          distance_pct: "",
          summary_zh: "",
          detail_zh: "",
        }},
      }}],
    }},
  }},
}};
const html = renderDecisionPluginCard(klineTechnicalFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert expected_label in html
    assert f"technical-bollinger-card {expected_class}" in html
    assert status not in html


def test_dashboard_omits_bollinger_when_technical_facts_unusable() -> None:
    script = r'''
const holding = {
  market: "US",
  symbol: "MSFT",
  portfolio_weight_hkd: "10.00%",
  decision_facts: {
    kline: {available: true, fields: {trend: "长期看涨，短期动能减弱"}},
    news_sentiment: {available: false, fields: {}},
  },
  technical_facts: {
    available: false,
    status: "extraction_error",
    error: "technical facts status is missing",
  },
};
const html = renderDecisionPluginCard(klineDecisionFactsPlugin(holding));
console.log(html);
'''
    html = run_dashboard_js(script)

    assert "长期看涨，短期动能减弱" in html
    assert "technical-bollinger-card" not in html
    assert "布林带数据缺失" not in html
    assert "undefined" not in html
    assert "参考轨道" not in html
    assert "缺失" in html


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


def test_dashboard_header_filters_and_cash_view_helpers() -> None:
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
      market: "HK",
      symbol: "00700",
      name: "Tencent",
      brokers: "phillips",
      currency: "HKD",
      total_quantity: "100",
      avg_cost_price: "150.00",
      market_value: "15982.00",
      market_value_hkd: "15982.00",
      portfolio_weight_hkd: "3.25%",
      unrealized_pnl_pct: "2.00%",
    },
    {
      market: "US",
      symbol: "VIXY",
      name: "ProShares VIX Short-Term Futures ETF",
      brokers: "futu;tiger",
      currency: "USD",
      total_quantity: "10",
      avg_cost_price: "12.34",
      market_value: "6250.00",
      market_value_hkd: "49062.50",
      portfolio_weight_hkd: "7.50%",
      unrealized_pnl_pct: "5.00%",
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
      t_signal: {
        schema_version: "open_trader.t_signal.v1",
        run_date: "2026-07-02",
        market: "US",
        symbol: "VIXY",
        futu_symbol: "US.VIXY",
        name: "ProShares VIX Short-Term Futures ETF",
        session_phase: "regular",
        updated_at: "2026-07-02T22:32:00+08:00",
        action: "BUY_T",
        suggested_ratio: "15",
        current_status: "BUY_T 条件满足，等待执行确认。",
        signal_summary_zh: "低吸做T信号成立，确定比例 15%。",
        price: {
          last_price: "48.50",
          day_change_pct: "-1.20",
          vwap: "49.10",
          ma_1m: "48.55",
          ma_5m: "48.85",
          day_low: "48.00",
          day_high: "50.20",
        },
        liquidity: {
          bid: "48.49",
          ask: "48.50",
          spread_pct: "0.02",
          bid_depth: "5000",
          ask_depth: "4700",
          depth_status: "pass",
        },
        technical: {
          rsi_5m: "34",
          volume_ratio_5m: "1.30",
          price_position: "below_vwap_reclaim",
          trend_state: "range_rebound",
        },
        hard_gates: [
          {
            name: "session_phase",
            status: "pass",
            message_zh: "当前处于盘中交易时段。",
          },
        ],
        evidence: [
          {
            name: "vwap_reclaim",
            direction: "buy",
            strength: "medium",
            message_zh: "价格低于 VWAP 后回收，出现低吸做T信号。",
          },
          {
            name: "rsi_low",
            direction: "buy",
            strength: "medium",
            message_zh: "5分钟 RSI 偏低。",
          },
        ],
        timeline: [
          {
            event_at: "2026-07-02T22:32:00+08:00",
            event_type: "signal_created",
            action: "BUY_T",
            suggested_ratio: "15",
            message_zh: "生成 BUY_T 信号，建议比例 15%。",
          },
          {
            event_at: "2026-07-02T22:32:00+08:00",
            event_type: "notification_sent",
            action: "BUY_T",
            suggested_ratio: "15",
            message_zh: "已发送 BUY_T 通知。",
          },
        ],
        notification: {
          should_notify: false,
          notified: true,
          dedupe_key: "2026-07-02|US.VIXY|BUY_T|15",
          last_notified_at: "2026-07-02T22:32:00+08:00",
          last_notified_dedupe_key: "2026-07-02|US.VIXY|BUY_T|15",
          last_attempted_dedupe_key: "2026-07-02|US.VIXY|BUY_T|15",
        },
        status: "ok",
        error: "",
      },
    },
    {
      market: "US",
      symbol: "BND",
      name: "Vanguard Total Bond Market ETF",
      brokers: "tiger",
      currency: "HKD",
      total_quantity: "2",
      avg_cost_price: "50.00",
      market_value: "100.00",
      market_value_hkd: "100.00",
      portfolio_weight_hkd: "2.50%",
      unrealized_pnl_pct: "-1.00%",
    },
    {
      market: "US",
      symbol: "VIXY260821C22000",
      name: "VIXY 260821 22.00C",
      brokers: "futu",
      currency: "USD",
      total_quantity: "1",
      avg_cost_price: "2.10",
      market_value: "168.00",
      market_value_hkd: "300.00",
      portfolio_weight_hkd: "0.50%",
      unrealized_pnl_pct: "-20.00%",
    },
    {
      market: "HK",
      symbol: "HKOPT",
      name: "腾讯 260730 400.00C",
      asset_class: "option",
      brokers: "futu",
      currency: "HKD",
      total_quantity: "1",
      avg_cost_price: "1.00",
      market_value: "200.00",
      market_value_hkd: "200.00",
      portfolio_weight_hkd: "0.40%",
      unrealized_pnl_pct: "1.00%",
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
if (summary.portfolio_value_hkd !== "15432.00") {
  throw new Error("unexpected portfolio value: " + JSON.stringify(summary));
}
if (summary.holding_value_hkd !== "15432.00") {
  throw new Error("unexpected holding value: " + JSON.stringify(summary));
}
if (summary.cash_like_value_hkd !== "") {
  throw new Error("unexpected cash value: " + JSON.stringify(summary));
}
if (summary.holding_weight_hkd !== "100.00%") {
  throw new Error("unexpected holding weight: " + JSON.stringify(summary));
}
if (summary.holding_count !== 2) {
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
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[1], 1);
renderHoldings();
if (elements["holdings-table-wrap"].classList.contains("hidden")) {
  throw new Error("trading decision should keep holdings table visible");
}
if (!elements["symbol-detail-panel"].classList.contains("hidden")) {
  throw new Error("trading decision should keep bottom symbol detail panel hidden");
}
if (!elements["holdings-body"].innerHTML.includes("交易决策") || !elements["holdings-body"].innerHTML.includes(">做T<") || elements["holdings-body"].innerHTML.includes(">凯利<") || elements["holdings-body"].innerHTML.includes(">详情<")) {
  throw new Error("holdings row should expose trading decision entry: " + elements["holdings-body"].innerHTML);
}
if (!elements["holdings-body"].innerHTML.includes('data-detail-market="US"') || !elements["holdings-body"].innerHTML.includes('data-detail-symbol="VIXY"')) {
  throw new Error("trading decision entry should expose exact holding identity: " + elements["holdings-body"].innerHTML);
}
if (!elements["holdings-body"].innerHTML.includes("t-signal-button-active")) {
  throw new Error("active BUY_T/SELL_T signals should pulse the t signal button: " + elements["holdings-body"].innerHTML);
}
state.dashboard.holdings[1].t_signal.session_phase = "closed";
renderHoldings();
if (elements["holdings-body"].innerHTML.includes("t-signal-button-active")) {
  throw new Error("non-regular t signals should not pulse the t signal button: " + elements["holdings-body"].innerHTML);
}
state.dashboard.holdings[1].t_signal.session_phase = "regular";
renderHoldings();
const renderedHoldings = elements["holdings-body"].innerHTML;
const usStockSectionIndex = renderedHoldings.indexOf("美股正股");
const usOptionSectionIndex = renderedHoldings.indexOf("美股期权");
const hkStockSectionIndex = renderedHoldings.indexOf("港股正股");
const hkOptionSectionIndex = renderedHoldings.indexOf("港股期权");
if (
  usStockSectionIndex === -1
  || usOptionSectionIndex === -1
  || hkStockSectionIndex === -1
  || hkOptionSectionIndex === -1
  || usStockSectionIndex > usOptionSectionIndex
  || usOptionSectionIndex > hkStockSectionIndex
  || hkStockSectionIndex > hkOptionSectionIndex
) {
  throw new Error("holdings should render stock/option market sections in requested order: " + renderedHoldings);
}
if (!renderedHoldings.includes("2 个标的 · 港元市值 HKD 49162.50 · 权重 10.00%")) {
  throw new Error("US stock section should render count, HKD subtotal, and weight subtotal: " + renderedHoldings);
}
if (!renderedHoldings.includes("1 个标的 · 港元市值 HKD 15982.00 · 权重 3.25%")) {
  throw new Error("HK stock section should render count, HKD subtotal, and weight subtotal: " + renderedHoldings);
}
if (!renderedHoldings.includes("1 个标的 · 港元市值 HKD 300.00 · 权重 0.50%")) {
  throw new Error("US option section should render count, HKD subtotal, and weight subtotal: " + renderedHoldings);
}
if (!renderedHoldings.includes("1 个标的 · 港元市值 HKD 200.00 · 权重 0.40%")) {
  throw new Error("HK option section should render count, HKD subtotal, and weight subtotal: " + renderedHoldings);
}
if (renderedHoldings.includes("其他市场持仓")) {
  throw new Error("OTHER section should not render without an OTHER-market holding: " + renderedHoldings);
}
for (const required of ["成本价", "美元市值", "港元市值", "持仓占总资产的占比"]) {
  if (renderedHoldings.includes("<th>" + required + "</th>")) {
    throw new Error("body should not render table headers inside market sections: " + renderedHoldings);
  }
}
if (!renderedHoldings.includes("USD 6250.00")) {
  throw new Error("USD holding should show original USD market value: " + renderedHoldings);
}
if (!renderedHoldings.includes("HKD 49062.50")) {
  throw new Error("HKD converted market value should remain visible: " + renderedHoldings);
}
if (!renderedHoldings.includes("<td class=\\"number-cell\\">-</td>")) {
  throw new Error("non-USD holding should show dash in USD market value column: " + renderedHoldings);
}
const holdingRows = Array.from(renderedHoldings.matchAll(/<tr class="[^"]*">\\s*<td><button class="expand-button"[\\s\\S]*?<\\/tr>/g)).map((match) => match[0]);
if (holdingRows.length !== 5) {
  throw new Error("main holdings table should render exactly 5 holding rows: " + renderedHoldings);
}
for (const row of holdingRows) {
  const cellCount = (row.match(/<td(?:\\s|>)/g) || []).length;
  if (cellCount !== 10) {
    throw new Error("holding row should render exactly 10 cells: " + row);
  }
}
for (const unexpected of ["<td>futu;tiger</td>", "<td>phillips</td>", "<td>futu</td>", "<td>tiger</td>", "<span class=\\"badge\\">"]) {
  if (renderedHoldings.includes(unexpected)) {
    throw new Error("main holdings table should not render broker/action cell " + unexpected + ": " + renderedHoldings);
  }
}
if (renderedHoldings.includes("观察 ·") || renderedHoldings.includes("人工复核 ·")) {
  throw new Error("main holdings table should not render action badges: " + renderedHoldings);
}
const sortedSections = groupedHoldingsByMarketSection([
  { market: "US", symbol: "LOW", portfolio_weight_hkd: "1.00%" },
  { market: "US", symbol: "HIGH", portfolio_weight_hkd: "9.00%" },
  { market: "US", symbol: "MISSING", portfolio_weight_hkd: "" },
]);
const sortedSymbols = sortedSections[0].rows.map((row) => row.holding.symbol).join(",");
if (sortedSymbols !== "HIGH,LOW,MISSING") {
  throw new Error("holdings should sort by portfolio weight descending within market section: " + sortedSymbols);
}
const emptyOptionSections = groupedHoldingsByMarketSection([
  { market: "US", symbol: "US_STOCK", portfolio_weight_hkd: "2.00%" },
  { market: "HK", symbol: "HK_STOCK", portfolio_weight_hkd: "1.00%" },
]);
const emptyOptionKeys = emptyOptionSections.map((section) => section.market).join(",");
if (emptyOptionKeys !== "US_STOCK,US_OPTION,HK_STOCK,HK_OPTION") {
  throw new Error("stock/option sections should render in fixed order, including empty option sections: " + emptyOptionKeys);
}
const emptyOptionRow = renderMarketSectionRow(emptyOptionSections[3]);
if (!emptyOptionRow.includes("0 个标的 · 港元市值 HKD 0.00 · 权重 0.00%")) {
  throw new Error("empty option section should render explicit zero totals: " + emptyOptionRow);
}
const malformedSection = renderMarketSectionRow({
  market: "OTHER",
  label: "其他市场持仓",
  className: "market-section-other",
  rows: [
    { holding: { market_value_hkd: "bad", portfolio_weight_hkd: "1.00%" }, index: 99 },
    { holding: { market_value_hkd: "300.00", portfolio_weight_hkd: "" }, index: 100 },
  ],
});
if (!malformedSection.includes("2 个标的 · 港元市值 - · 权重 -") || malformedSection.includes("HKD 0.00")) {
  throw new Error("malformed section subtotal data should render dash instead of zero: " + malformedSection);
}
if (!elements["holdings-body"].innerHTML.includes("decision-detail-row") || !elements["holdings-body"].innerHTML.includes("inline-symbol-detail")) {
  throw new Error("trading decision should render directly below selected holding row: " + elements["holdings-body"].innerHTML);
}
for (const required of ["交易决策 ·", "最终决策", "趋势 / K 线", "新闻 / 舆论", "富途异动", "数据未生成"]) {
  if (!elements["holdings-body"].innerHTML.includes(required)) {
    throw new Error("trading decision detail missing " + required + ": " + elements["holdings-body"].innerHTML);
  }
}
for (const unexpected of ["插件管理", "策略阈值"]) {
  if (elements["holdings-body"].innerHTML.includes(unexpected)) {
    throw new Error("trading decision detail should not render extra panel " + unexpected);
  }
}
state.selectedHoldingDetail = "t_signal";
renderHoldings();
for (const required of ["做T信号 ·", "买入做T", "确定比例", "15%", "信号依据", "价格低于 VWAP 后回收", "前置条件", "t-signal-checkmark", "交易时段", "详细信息", "消息 timeline", "已发送 BUY_T 通知。", "已发起提醒 · 2026-07-02T22:32:00+08:00"]) {
  if (!elements["holdings-body"].innerHTML.includes(required)) {
    throw new Error("t signal detail missing " + required + ": " + elements["holdings-body"].innerHTML);
  }
}
for (const unexpected of ["小T", "大T", "状态机", ">session_phase<", "已提醒 ·"]) {
  if (elements["holdings-body"].innerHTML.includes(unexpected)) {
    throw new Error("t signal detail should not render ambiguous wording " + unexpected);
  }
}
state.selectedHoldingDetail = "decision";
state.dashboard.holdings.push({
  market: "JP",
  symbol: "7203",
  name: "Toyota",
  brokers: "phillips",
  currency: "JPY",
  total_quantity: "1",
  avg_cost_price: "3000",
  market_value: "300.00",
  market_value_hkd: "300.00",
  portfolio_weight_hkd: "1.50%",
  unrealized_pnl_pct: "0.00%",
});
state.selectedHoldingKey = "";
renderHoldings();
const renderedWithOther = elements["holdings-body"].innerHTML;
if (!renderedWithOther.includes("其他市场持仓") || !renderedWithOther.includes("1 个标的 · 港元市值 HKD 300.00 · 权重 1.50%")) {
  throw new Error("OTHER section should render only when an OTHER-market holding exists: " + renderedWithOther);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)


def obsolete_dashboard_renders_backtest_entry_and_detail_only_after_selection() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
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
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingKey = "";
state.selectedHoldingDetail = "decision";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: {
      available: true,
      run_id: "2026-06-18-US-VIXY-trading-plan",
      run_date: "2026-06-18",
      market: "US",
      symbol: "VIXY",
      strategy: "trading_plan",
      adapter: "backtrader",
      metrics: {
        total_return_pct: "1.17",
        win_rate_pct: "50.00",
        max_drawdown_pct: "-3.40",
        trade_count: "2",
      },
      trades: [
        {
          date: "2026-06-19",
          side: "BUY",
          price: "40.2000",
          quantity: "621",
          fees: "24.96",
          cash_after: "75010.84",
          reason: "entry_zone",
        },
        {
          date: "2026-06-20",
          side: "SELL",
          price: "47.9760",
          quantity: "621",
          fees: "29.79",
          cash_after: "104774.15",
          reason: "target_1",
        },
      ],
      equity_curve: [
        { date: "2026-06-18", close: "45.0000", equity: "100000.00", drawdown_pct: "0.00" },
        { date: "2026-06-19", close: "42.0000", equity: "101092.84", drawdown_pct: "0.00" },
        { date: "2026-06-20", close: "48.0000", equity: "104774.15", drawdown_pct: "0.00" },
      ],
      report_path: "reports/backtests/2026-06-18-US-VIXY-trading-plan.md",
      trades_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/trades.csv",
      equity_curve_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/equity_curve.csv",
      status: "ok",
      error: "",
    },
  }],
};
renderHoldings();
let html = elements["holdings-body"].innerHTML;
if (!html.includes(">查看回测<") || !html.includes('data-detail-mode="backtest"')) {
  throw new Error("holding row should expose backtest entry: " + html);
}
if (html.includes("总收益") || html.includes("1.17%") || html.includes("回测详情 ·")) {
  throw new Error("main holdings table should not show backtest metrics before selection: " + html);
}
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
state.selectedHoldingDetail = "backtest";
renderHoldings();
html = elements["holdings-body"].innerHTML;
for (const required of ["回测详情 · US.VIXY", "Backtrader", "总收益", "1.17%", "胜率", "50.00%", "最大回撤", "-3.40%", "交易次数", "2", "权益曲线", "价格走势与买卖点", "交易明细", "<svg", "BUY", "SELL", "entry_zone", "target_1", "reports/backtests/2026-06-18-US-VIXY-trading-plan.md"]) {
  if (!html.includes(required)) {
    throw new Error("backtest detail missing " + required + ": " + html);
  }
}
if ((html.match(/回测准备/g) || []).length !== 1) {
  throw new Error("backtest readiness should render once: " + html);
}
console.log(html);
"""
    )

    assert "回测详情 · US.VIXY" in html


def obsolete_dashboard_backtest_detail_runs_from_button_and_refreshes() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    disabled: false,
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
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
    querySelectorAll() { return []; },
    addEventListener() {},
  };
}
(async () => {
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
let html = elements["holdings-body"].innerHTML;
if (!html.includes(">运行回测<") || !html.includes('data-run-backtest="US:VIXY:ProShares VIX Short-Term Futures ETF:0"')) {
  throw new Error("backtest detail should expose run button: " + html);
}
let posted = null;
let loadCount = 0;
globalThis.fetch = async (url, options) => {
  posted = { url, body: JSON.parse(options.body) };
  return {
    ok: true,
    json: async () => ({
      status: "ok",
      backtest: {
        available: true,
        run_id: "2026-06-18-US-VIXY-trading-plan",
        metrics: { total_return_pct: "1.17" },
      },
    }),
  };
};
loadDashboard = async () => {
  loadCount += 1;
  state.dashboard.holdings[0].backtest = {
    available: true,
    run_id: "2026-06-18-US-VIXY-trading-plan",
    run_date: "2026-06-18",
    market: "US",
    symbol: "VIXY",
    strategy: "trading_plan",
    adapter: "backtrader",
    metrics: {
      total_return_pct: "1.17",
      win_rate_pct: "50.00",
      max_drawdown_pct: "-3.40",
      trade_count: "2",
    },
    report_path: "reports/backtests/2026-06-18-US-VIXY-trading-plan.md",
    trades_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/trades.csv",
    equity_curve_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/equity_curve.csv",
    metrics_path: "data/backtests/2026-06-18-US-VIXY-trading-plan/metrics.json",
  };
};
await runBacktestForHolding(state.selectedHoldingKey);
if (!posted || posted.url !== "/api/backtests/run") {
  throw new Error("backtest run should post to API: " + JSON.stringify(posted));
}
if (posted.body.market !== "US" || posted.body.symbol !== "VIXY" || posted.body.initial_position_quantity !== "10") {
  throw new Error("backtest run body should identify holding: " + JSON.stringify(posted.body));
}
if (loadCount !== 1) {
  throw new Error("backtest run should reload dashboard once: " + loadCount);
}
html = elements["holdings-body"].innerHTML;
if (!html.includes("回测详情 · US.VIXY") || !html.includes("1.17%")) {
  throw new Error("backtest detail should refresh after run: " + html);
}
console.log(html);
})();
"""
    )

    assert "回测详情 · US.VIXY" in html


def obsolete_dashboard_backtest_detail_renders_readiness_gaps() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
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
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
    backtest_readiness: {
      available: false,
      status: "missing_fields",
      run_date: "2026-06-18",
      plan_path: "data/latest/US/trading_plan.csv",
      prices_path: "data/prices/US/VIXY.csv",
      prices_missing: true,
      missing_fields: ["entry_zone_high", "max_weight"],
      error: "missing backtest field(s): entry_zone_high, max_weight",
    },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
const html = elements["holdings-body"].innerHTML;
for (const required of ["回测准备", "缺少计划字段", "entry_zone_high", "max_weight", "data/latest/US/trading_plan.csv", "data/prices/US/VIXY.csv"]) {
  if (!html.includes(required)) {
    throw new Error("backtest readiness missing " + required + ": " + html);
  }
}
console.log(html);
"""
    )

    assert "缺少计划字段" in html


def obsolete_dashboard_backtest_detail_renders_unsupported_strategy() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
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
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
    backtest_readiness: {
      available: false,
      status: "unsupported_strategy",
      run_date: "2026-06-18",
      plan_path: "data/latest/US/trading_plan.csv",
      prices_path: "data/prices/US/VIXY.csv",
      prices_missing: false,
      missing_fields: [],
      error: "unsupported backtest strategy rating",
    },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
const html = elements["holdings-body"].innerHTML;
for (const required of ["回测准备", "暂不支持该策略", "第一版回测支持买入、加仓和减仓类交易计划；其他策略暂不支持。"]) {
  if (!html.includes(required)) {
    throw new Error("unsupported strategy readiness missing " + required + ": " + html);
  }
}
if (html.includes(">运行回测<")) {
  throw new Error("unsupported strategy should not expose run button: " + html);
}
console.log(html);
"""
    )

    assert "暂不支持该策略" in html


def obsolete_dashboard_backtest_detail_hides_manual_missing_price_fetch_button() -> None:
    html = run_dashboard_js(
        r"""
function makeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
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
    querySelectorAll() { return []; },
  };
}
elements["visible-count"] = makeElement();
elements["workspace-grid"] = makeElement();
elements["holdings-table-wrap"] = makeElement();
elements["symbol-detail-panel"] = makeElement();
elements["cash-detail-panel"] = makeElement();
elements["holdings-body"] = makeElement();
state.dashboardError = null;
state.quotes = {};
state.marketFilter = "ALL";
state.brokerFilter = "ALL";
state.selectedHoldingDetail = "backtest";
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    name: "ProShares VIX Short-Term Futures ETF",
    brokers: "futu",
    currency: "USD",
    total_quantity: "10",
    avg_cost_price: "12.34",
    market_value: "6250.00",
    market_value_hkd: "49062.50",
    portfolio_weight_hkd: "7.50%",
    unrealized_pnl_pct: "5.00%",
    backtest: { available: false, error: "" },
    backtest_readiness: {
      available: false,
      status: "missing_fields",
      run_date: "2026-06-18",
      plan_path: "data/latest/US/trading_plan.csv",
      prices_path: "data/prices/US/VIXY.csv",
      prices_missing: true,
      missing_fields: ["max_weight"],
      error: "missing backtest field(s): max_weight",
    },
  }],
};
state.selectedHoldingKey = holdingKey(state.dashboard.holdings[0], 0);
renderHoldings();
const html = elements["holdings-body"].innerHTML;
if (!html.includes("缺少计划字段") || !html.includes("missing backtest field(s): max_weight")) {
  throw new Error("missing price readiness should still show diagnostic state: " + html);
}
if (html.includes(">拉取价格数据<") || html.includes("data-fetch-backtest-prices")) {
  throw new Error("missing price readiness should not expose manual fetch button: " + html);
}
console.log(html);
"""
    )

    assert "缺少计划字段" in html


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


def test_dashboard_server_runs_backtest_api_and_refreshes_payload(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "target_1": "48",
            "stop_loss": "36",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    write_csv(
        config.data_dir / "prices" / "US" / "VIXY.csv",
        ["date", "open", "high", "low", "close"],
        [
            {"date": "2026-06-18", "open": "45", "high": "46", "low": "44", "close": "45"},
            {"date": "2026-06-19", "open": "41", "high": "43", "low": "40", "close": "42"},
            {"date": "2026-06-20", "open": "47", "high": "49", "low": "46", "close": "48"},
        ],
    )
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        payload = post_json(
            f"http://{host}:{port}/api/backtests/run",
            {"market": "US", "symbol": "VIXY"},
        )
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert payload["status"] == "ok"
    assert payload["backtest"]["run_id"] == "2026-06-18-US-VIXY-trading-plan"
    assert payload["backtest"]["adapter"] == "backtrader"
    assert payload["backtest"]["metrics"]["trade_count"] == "2"
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert "backtest" not in vixy
    assert "backtest_readiness" not in vixy


def test_dashboard_server_runs_sell_side_backtest_from_current_position(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Underweight",
            "entry_zone_low": "",
            "entry_zone_high": "",
            "target_1": "40",
            "stop_loss": "",
            "max_weight": "",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    write_csv(
        config.data_dir / "prices" / "US" / "VIXY.csv",
        ["date", "open", "high", "low", "close"],
        [
            {"date": "2026-06-18", "open": "45", "high": "46", "low": "44", "close": "45"},
            {"date": "2026-06-19", "open": "41", "high": "43", "low": "39", "close": "40"},
        ],
    )
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        payload = post_json(
            f"http://{host}:{port}/api/backtests/run",
            {"market": "US", "symbol": "VIXY", "initial_position_quantity": "10"},
        )
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert payload["status"] == "ok"
    assert payload["backtest"]["metrics"]["trade_count"] == "1"
    assert payload["backtest"]["trades"][0]["side"] == "SELL"
    assert payload["backtest"]["trades"][0]["reason"] == "target_1"
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert "backtest" not in vixy
    assert "backtest_readiness" not in vixy


def obsolete_dashboard_server_fetches_backtest_prices_api(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    provider = FakeBacktestPriceProvider()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        backtest_price_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        payload = post_json(
            f"http://{host}:{port}/api/backtests/prices",
            {"market": "US", "symbol": "VIXY", "end": "2026-07-10"},
        )
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert payload["status"] == "ok"
    assert payload["records"] == 1
    assert payload["prices_path"] == str(config.data_dir / "prices" / "US" / "VIXY.csv")
    assert payload["backtest_readiness"]["status"] == "ready"
    assert provider.requests == [
        {
            "futu_symbol": "US.VIXY",
            "start": "2026-06-18",
            "end": "2026-07-10",
        }
    ]
    assert (config.data_dir / "prices" / "US" / "VIXY.csv").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "date,open,high,low,close",
        "2026-06-19,41.0,43.0,40.0,42.0",
    ]
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"]["status"] == "ready"


def obsolete_dashboard_server_auto_fetches_missing_backtest_prices_on_dashboard_load(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    provider = FakeBacktestPriceProvider()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        backtest_price_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert provider.requests == [
        {
            "futu_symbol": "US.VIXY",
            "start": "2026-06-18",
            "end": date.today().isoformat(),
        }
    ]
    assert (config.data_dir / "prices" / "US" / "VIXY.csv").is_file()
    assert dashboard_payload["backtest_price_sync"] == {
        "status": "ok",
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "errors": [],
    }
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"]["status"] == "ready"


def obsolete_dashboard_server_keeps_payload_when_auto_backtest_price_fetch_fails(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "42",
            "max_weight": "25%",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    provider = RaisingBacktestPriceProvider()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        backtest_price_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert provider.requests == [
        {
            "futu_symbol": "US.VIXY",
            "start": "2026-06-18",
            "end": date.today().isoformat(),
        }
    ]
    assert not (config.data_dir / "prices" / "US" / "VIXY.csv").exists()
    assert dashboard_payload["backtest_price_sync"] == {
        "status": "failed",
        "attempted": 1,
        "succeeded": 0,
        "failed": 1,
        "errors": [
            {
                "market": "US",
                "symbol": "VIXY",
                "message": "kline unavailable",
            }
        ],
    }
    vixy = next(row for row in dashboard_payload["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"]["status"] == "missing_prices"


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
    ("body", "error_type", "expected_status", "expected_message"),
    [
        (b"", "ResearchChatError", 500, "market and symbol are required"),
        (b"{bad json", "ValueError", 400, "请求正文必须是有效的 JSON 对象"),
        (b'["not", "object"]', "ValueError", 400, "请求正文必须是有效的 JSON 对象"),
        (b'"not object"', "ValueError", 400, "请求正文必须是有效的 JSON 对象"),
    ],
)
def test_dashboard_server_returns_json_error_for_bad_research_chat_create_body(
    tmp_path,
    body: bytes,
    error_type: str,
    expected_status: int,
    expected_message: str,
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

    assert status == expected_status
    assert content_type == "application/json; charset=utf-8"
    assert payload["status"] == "error"
    assert payload["error_type"] == error_type
    assert payload["message"] == expected_message
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

    def raise_runtime_error(config, **kwargs: Any) -> dict[str, Any]:
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
