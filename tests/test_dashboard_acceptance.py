from decimal import Decimal
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest

from open_trader import dashboard_acceptance
from open_trader.dashboard_acceptance import (
    REQUIRED_SOURCE_PATHS,
    _is_actionable_console_error,
    classify_result,
    dashboard_signature,
    validate_dashboard_payload,
)


def test_make_acceptance_allows_an_isolated_dashboard_url_and_log() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert 'DASHBOARD_URL ?= http://127.0.0.1:8766' in makefile
    assert 'DASHBOARD_LOG ?= /tmp/open_trader_dashboard_8766.log' in makefile
    assert '--url "$(DASHBOARD_URL)"' in makefile
    assert '--log "$(DASHBOARD_LOG)"' in makefile


def test_browser_ignores_chrome_unattributed_404_but_not_app_errors() -> None:
    assert not _is_actionable_console_error(
        "Failed to load resource: the server responded with a status of 404 (Not Found)"
    )
    assert _is_actionable_console_error("Uncaught TypeError: failed")


def valid_payload() -> dict[str, object]:
    cn = [
        {
            "market": "CN",
            "symbol": str(index),
            "portfolio_weight_hkd": "10.00%",
            "agent_report": {"available": False},
        }
        for index in range(5)
    ]
    other = [{
        "market": "US",
        "symbol": "MSFT",
        "portfolio_weight_hkd": "50.00%",
        "agent_report": {"available": True},
        "tradingagents_summary": {"available": True},
        "technical_facts": {"available": True},
        "decision_facts": {
            "kline": {"available": True},
            "news_sentiment": {"available": True},
        },
        "futu_skill_facts": {
            "news_sentiment": {"available": True},
            "technical_anomaly": {"available": True},
            "capital_anomaly": {"available": True},
            "derivatives_anomaly": {"available": True},
        },
    }]
    return {
        "holdings": cn + other,
        "cash_rows": [],
        "backtest_universe": {"holdings": [
            {"market": "CN", "symbol": row["symbol"]} for row in cn
        ]},
    }


def nested_get(row: dict[str, object], path: tuple[str, ...]) -> dict[str, object]:
    value: object = row
    for key in path:
        value = value[key]  # type: ignore[index]
    return value  # type: ignore[return-value]


@pytest.mark.parametrize("path", REQUIRED_SOURCE_PATHS)
def test_validate_dashboard_payload_rejects_each_missing_current_source(
    path: tuple[str, ...],
) -> None:
    payload = valid_payload()
    source = nested_get(payload["holdings"][-1], path)  # type: ignore[index]
    source["available"] = False
    source["status"] = "stale_source_hash"

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert any("US.MSFT" in error and path[-1] in error for error in errors)


def test_validate_dashboard_payload_ignores_missing_sources_without_current_advice() -> None:
    payload = valid_payload()
    payload["holdings"][0]["tradingagents_summary"] = {  # type: ignore[index]
        "available": False,
        "status": "stale_source_hash",
    }

    assert validate_dashboard_payload(payload, expected_cn=5) == []


def test_validate_dashboard_payload_accepts_explicitly_unsupported_source() -> None:
    payload = valid_payload()
    source = payload["holdings"][-1]["futu_skill_facts"]["technical_anomaly"]  # type: ignore[index]
    source.update(
        available=False,
        unsupported=True,
        status="error",
        summary="富途接口不支持技术异动：US.MSFT",
    )

    assert validate_dashboard_payload(payload, expected_cn=5) == []


def test_first_in_scope_holding_returns_exact_market_and_symbol() -> None:
    assert dashboard_acceptance._first_in_scope_holding(valid_payload()) == ("US", "MSFT")


def test_first_in_scope_holding_rejects_payload_without_current_advice() -> None:
    payload = valid_payload()
    payload["holdings"][-1]["agent_report"]["available"] = False  # type: ignore[index]

    with pytest.raises(AssertionError, match="advice-backed holding"):
        dashboard_acceptance._first_in_scope_holding(payload)


def test_check_decision_tabs_uses_exact_holding_and_checks_every_panel() -> None:
    selectors: list[str] = []
    clicks: list[str] = []

    class Locator:
        def __init__(self, kind: str, index: int = 0) -> None:
            self.kind = kind
            self.index = index

        def count(self) -> int:
            return {"button": 1, "tabs": 5, "failed": 0, "panel": 1}[self.kind]

        def click(self) -> None:
            clicks.append(self.kind)

        def all_inner_texts(self) -> list[str]:
            return ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]

        def nth(self, index: int) -> "Locator":
            return Locator("tab", index)

        def get_attribute(self, name: str) -> str:
            assert name == "aria-controls"
            return f"decision-panel-{self.index}"

        def inner_text(self) -> str:
            if self.index == 0:
                return "回测闸门 夏普比率 1.2 卡玛比率 0.8"
            if self.index == 2:
                return "当前价 710.55"
            return "source data"

    class Page:
        def locator(self, selector: str) -> Locator:
            selectors.append(selector)
            if selector.startswith('button[data-detail-mode="decision"]'):
                return Locator("button")
            if selector == ".decision-tab-list [data-decision-tab]":
                return Locator("tabs")
            if selector == ".decision-tab-list .decision-tab-failed":
                return Locator("failed")
            match = re.search(r"decision-panel-(\d+)", selector)
            return Locator("panel", int(match.group(1)) if match else 0)

    dashboard_acceptance._check_decision_tabs(Page(), "US", "MSFT")

    assert selectors[0] == (
        'button[data-detail-mode="decision"]'
        '[data-detail-market="US"]'
        '[data-detail-symbol="MSFT"]'
    )
    assert clicks == ["button", "tab", "tab", "tab", "tab", "tab"]


def test_check_decision_tabs_rejects_stale_initial_panel_after_tab_click() -> None:
    class Locator:
        def __init__(self, kind: str, index: int = 0) -> None:
            self.kind = kind
            self.index = index

        def count(self) -> int:
            if self.kind in {"button", "initial-panel"}:
                return 1
            if self.kind == "tabs":
                return 5
            return 0

        def click(self) -> None:
            pass

        def all_inner_texts(self) -> list[str]:
            return ["最终决策", "TradingAgents", "趋势 / K 线", "新闻 / 舆论", "富途异动"]

        def nth(self, index: int) -> "Locator":
            return Locator("tab", index)

        def get_attribute(self, name: str) -> str:
            assert name == "aria-controls"
            return f"decision-panel-{self.index}"

        def inner_text(self) -> str:
            return "source data 夏普比率 1.2 卡玛比率 0.8"

    class Page:
        def locator(self, selector: str) -> Locator:
            if selector.startswith('button[data-detail-mode="decision"]'):
                return Locator("button")
            if selector == ".decision-tab-list [data-decision-tab]":
                return Locator("tabs")
            if selector == ".decision-tab-panel:visible":
                return Locator("initial-panel")
            if selector == "#decision-panel-0:visible":
                return Locator("initial-panel")
            return Locator("missing")

    with pytest.raises(AssertionError, match="TradingAgents"):
        dashboard_acceptance._check_decision_tabs(Page(), "US", "MSFT")


def test_browser_check_treats_page_error_as_desktop_failure_and_runs_mobile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited: list[str] = []
    selectors: list[tuple[str, str]] = []
    state = {"fail_desktop_navigation": True}

    class Locator:
        @property
        def first(self) -> "Locator":
            return self

        def locator(self, _selector: str) -> "Locator":
            return self

        def click(self) -> None:
            pass

        def inner_text(self) -> str:
            return "5 条"

    class Page:
        def __init__(self, name: str) -> None:
            self.name = name

        def on(self, *_args: object) -> None:
            pass

        def goto(self, *_args: object, **_kwargs: object) -> None:
            visited.append(self.name)
            if self.name == "desktop" and state["fail_desktop_navigation"]:
                raise RuntimeError("navigation failed")

        def locator(self, selector: str) -> Locator:
            selectors.append((self.name, selector))
            return Locator()

        def wait_for_timeout(self, _milliseconds: int) -> None:
            pass

        def close(self) -> None:
            pass

    class Browser:
        pages = 0

        def new_page(self, **_kwargs: object) -> Page:
            self.pages += 1
            return Page("desktop" if self.pages == 1 else "mobile")

        def close(self) -> None:
            pass

    class Playwright:
        chromium = type("Chromium", (), {"launch": lambda *_args, **_kwargs: Browser()})()

    class Context:
        def __enter__(self) -> Playwright:
            return Playwright()

        def __exit__(self, *_args: object) -> None:
            pass

    module = ModuleType("playwright.sync_api")
    module.sync_playwright = Context  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_decision_tabs",
        lambda *_args: None,
    )

    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, valid_payload()
    )

    assert errors == ["desktop：RuntimeError: navigation failed"]
    assert blocker is None
    assert visited == ["desktop", "mobile"]

    state["fail_desktop_navigation"] = False
    visited.clear()
    selectors.clear()
    monkeypatch.setattr(
        dashboard_acceptance,
        "_check_decision_tabs",
        lambda *_args: (_ for _ in ()).throw(AssertionError("decision failed")),
    )

    errors, blocker = dashboard_acceptance._browser_check(
        "http://dashboard", 5, valid_payload()
    )

    assert errors == [
        "desktop：AssertionError: decision failed",
        "mobile：AssertionError: decision failed",
    ]
    assert blocker is None
    for viewport in ("desktop", "mobile"):
        assert (viewport, '#broker-summary-cards [data-broker="phillips"]') in selectors
        assert (viewport, '[data-market="CN"]') in selectors
        assert (viewport, 'button[data-broker="eastmoney"]') in selectors


def test_validate_dashboard_payload_accepts_real_contract() -> None:
    assert validate_dashboard_payload(valid_payload(), expected_cn=5) == []


def test_validate_dashboard_payload_rejects_bad_counts_and_weights() -> None:
    payload = valid_payload()
    payload["holdings"][0]["portfolio_weight_hkd"] = "9.99%"  # type: ignore[index]
    payload["backtest_universe"] = {"holdings": []}

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert "组合权重合计不是 100.00%：99.99%" in errors
    assert "A 股回测标的数量不是 5：0" in errors


def test_validate_dashboard_payload_checks_eastmoney_statement_total_assets() -> None:
    payload = valid_payload()
    for row in payload["holdings"][:5]:  # type: ignore[index]
        row.update({"brokers": "eastmoney", "currency": "CNY", "market_value": "10"})
    payload["cash_rows"] = [{
        "market": "CASH", "symbol": "CNY_CASH", "brokers": "eastmoney",
        "currency": "CNY", "market_value": "50", "portfolio_weight_hkd": "0.00%",
    }]

    assert validate_dashboard_payload(
        payload, expected_cn=5, expected_eastmoney_cny=Decimal("100")
    ) == []

    errors = validate_dashboard_payload(
        payload, expected_cn=5, expected_eastmoney_cny=Decimal("101")
    )
    assert "东方财富总资产不匹配：100 != 101 CNY" in errors


def test_acceptance_parser_does_not_hardcode_mark_to_market_eastmoney_total() -> None:
    from open_trader.dashboard_acceptance import build_parser

    args = build_parser().parse_args([])

    assert args.expected_eastmoney_cny is None


def test_validate_dashboard_payload_checks_latest_phillips_statement() -> None:
    payload = valid_payload()
    payload["broker_summaries"] = [{
        "broker": "phillips", "detail_available": True,
        "portfolio_value_hkd": "628554.05",
    }]
    payload["source_statuses"] = [{
        "broker": "phillips", "display_text": "2026-07 月结单导入"
    }]

    errors = validate_dashboard_payload(
        payload, expected_cn=5,
        expected_phillips_total=Decimal("628554.06"),
        expected_phillips_period="2026-07",
    )

    assert "辉立总资产不匹配：628554.05 != 628554.06 HKD" in errors
    assert not any("行数" in error for error in errors)


def test_latest_phillips_expectation_uses_newest_archived_pdf(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / "statements/phillips/2026-06-30/statement.pdf"
    latest = tmp_path / "statements/phillips/2026-07-10/statement.pdf"
    old.parent.mkdir(parents=True)
    latest.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    latest.write_bytes(b"latest")

    def parse(_self, path, _month):
        assert path == latest
        return SimpleNamespace(
            positions=[SimpleNamespace(currency="HKD", market_value=Decimal("100"))],
            cash_balances=[SimpleNamespace(currency="HKD", cash_balance=Decimal("20"))],
        )

    monkeypatch.setattr("open_trader.parsers.phillips.PhillipsStatementParser.parse", parse)

    assert dashboard_acceptance._latest_phillips_expectation(tmp_path) == (
        Decimal("120"), "2026-07",
    )


def test_validate_dashboard_payload_rejects_empty_phillips_account_card() -> None:
    payload = valid_payload()
    payload["broker_summaries"] = [{
        "broker": "phillips", "detail_available": False, "portfolio_value_hkd": ""
    }]
    payload["source_statuses"] = [{
        "broker": "phillips", "display_text": "暂无月结单明细"
    }]

    errors = validate_dashboard_payload(
        payload, expected_cn=5, expected_phillips_total=Decimal("628554.06")
    )

    assert "辉立账户卡没有可用月结单资产" in errors


def test_classify_result_has_only_three_states() -> None:
    assert classify_result([], browser_blocker=None) == "PASS"
    assert classify_result(["API failed"], browser_blocker=None) == "FAIL"
    assert classify_result([], browser_blocker="Chrome unavailable") == "BLOCKED"
    assert classify_result(["API failed"], browser_blocker="Chrome unavailable") == "FAIL"


def test_dashboard_signature_ignores_live_values_but_detects_structural_change() -> None:
    first = valid_payload()
    second = valid_payload()
    first["last_refresh"] = "one"
    second["last_refresh"] = "two"
    second["holdings"][0]["market_value_hkd"] = "123.45"  # type: ignore[index]
    second["holdings"][0]["portfolio_weight_hkd"] = "9.99%"  # type: ignore[index]
    assert dashboard_signature(first) == dashboard_signature(second)

    second["holdings"][0]["brokers"] = "changed"  # type: ignore[index]
    assert dashboard_signature(first) != dashboard_signature(second)
