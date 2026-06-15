from __future__ import annotations

import sys
from pathlib import Path

import pytest

from open_trader.advice.models import PortfolioInputRow
from open_trader.advice.tradingagents_adapter import TradingAgentsAdapter


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def propagate(
        self, symbol: str, run_date: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        self.calls.append((symbol, run_date))
        return {"symbol": symbol}, {
            "action": "hold",
            "summary": f"Hold {symbol}",
        }


def portfolio_row(symbol: str = "VIXY") -> PortfolioInputRow:
    return PortfolioInputRow(
        symbol=symbol,
        market="US",
        asset_class="etf",
        name="Volatility ETF",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        analysis_symbol=symbol,
    )


def test_adapter_calls_graph_and_normalizes_success() -> None:
    graph = FakeGraph()
    adapter = TradingAgentsAdapter.from_graph(graph)

    advice = adapter.analyze(portfolio_row("VIXY"), "2026-06-16")

    assert graph.calls == [("VIXY", "2026-06-16")]
    assert advice.symbol == "VIXY"
    assert advice.source == "tradingagents"
    assert advice.advice_action == "hold"
    assert advice.advice_summary == "Hold VIXY"
    assert advice.status == "ok"
    assert '"action": "hold"' in advice.raw_decision


def test_adapter_records_symbol_failure_as_error() -> None:
    class FailingGraph:
        def propagate(
            self, symbol: str, run_date: str
        ) -> tuple[dict[str, str], dict[str, str]]:
            raise RuntimeError("network unavailable")

    adapter = TradingAgentsAdapter.from_graph(FailingGraph())

    advice = adapter.analyze(portfolio_row("QQQ"), "2026-06-16")

    assert advice.symbol == "QQQ"
    assert advice.status == "error"
    assert advice.error == "network unavailable"
    assert advice.raw_decision == ""


def test_adapter_preserves_real_tradingagents_detail_shape() -> None:
    class RealShapeGraph:
        def propagate(self, symbol: str, run_date: str) -> tuple[dict[str, str], str]:
            return {"final_trade_decision": "Detailed PM rationale"}, "Hold"

    adapter = TradingAgentsAdapter.from_graph(RealShapeGraph())

    advice = adapter.analyze(portfolio_row("AAPL"), "2026-06-16")

    assert advice.status == "ok"
    assert advice.advice_action == "Hold"
    assert advice.advice_summary == "Detailed PM rationale"
    assert "final_trade_decision" in advice.raw_decision


def test_adapter_stringifies_non_json_values_without_raising() -> None:
    non_json_value = object()

    class NonJsonGraph:
        def propagate(
            self, symbol: str, run_date: str
        ) -> tuple[dict[str, object], dict[str, object]]:
            return {
                "final_trade_decision": non_json_value,
            }, {
                "action": "hold",
                "summary": non_json_value,
            }

    adapter = TradingAgentsAdapter.from_graph(NonJsonGraph())

    advice = adapter.analyze(portfolio_row("MSFT"), "2026-06-16")

    assert advice.status == "ok"
    assert advice.error == ""
    assert advice.advice_action == "hold"
    assert advice.advice_summary == str(non_json_value)
    assert str(non_json_value) in advice.raw_decision


def test_adapter_rejects_missing_tradingagents_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        TradingAgentsAdapter.from_project_path(tmp_path / "missing")


def test_adapter_removes_project_path_after_graph_construction_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_path = tmp_path / "TradingAgents"
    project_path.mkdir()
    package_path = project_path / "tradingagents"
    graph_path = package_path / "graph"
    graph_path.mkdir(parents=True)
    (package_path / "__init__.py").write_text("", encoding="utf-8")
    (graph_path / "__init__.py").write_text("", encoding="utf-8")
    (package_path / "default_config.py").write_text(
        "DEFAULT_CONFIG = {'provider': 'fake'}\n", encoding="utf-8"
    )
    (graph_path / "trading_graph.py").write_text(
        "class TradingAgentsGraph:\n"
        "    def __init__(self, debug, config):\n"
        "        raise RuntimeError('graph construction failed')\n",
        encoding="utf-8",
    )
    project_path_str = str(project_path.resolve())

    for module_name in list(sys.modules):
        if module_name == "tradingagents" or module_name.startswith("tradingagents."):
            monkeypatch.delitem(sys.modules, module_name)

    with pytest.raises(RuntimeError, match="graph construction failed"):
        TradingAgentsAdapter.from_project_path(project_path)

    assert project_path_str not in sys.path


def test_from_project_path_merges_config_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_path = tmp_path / "TradingAgents"
    project_path.mkdir()
    package_path = project_path / "tradingagents"
    graph_path = package_path / "graph"
    graph_path.mkdir(parents=True)
    (package_path / "__init__.py").write_text("", encoding="utf-8")
    (graph_path / "__init__.py").write_text("", encoding="utf-8")
    (package_path / "default_config.py").write_text(
        "DEFAULT_CONFIG = {\n"
        "    'llm_provider': 'openai',\n"
        "    'deep_think_llm': 'gpt-5.4',\n"
        "    'quick_think_llm': 'gpt-5.4-mini',\n"
        "}\n",
        encoding="utf-8",
    )
    (graph_path / "trading_graph.py").write_text(
        "CAPTURED_CONFIG = None\n"
        "class TradingAgentsGraph:\n"
        "    def __init__(self, debug, config):\n"
        "        global CAPTURED_CONFIG\n"
        "        CAPTURED_CONFIG = config\n"
        "    def propagate(self, symbol, run_date):\n"
        "        return {'final_trade_decision': 'Hold'}, 'Hold'\n",
        encoding="utf-8",
    )

    for module_name in list(sys.modules):
        if module_name == "tradingagents" or module_name.startswith("tradingagents."):
            monkeypatch.delitem(sys.modules, module_name)

    TradingAgentsAdapter.from_project_path(
        project_path,
        config_overrides={
            "llm_provider": "deepseek",
            "deep_think_llm": "deepseek-v4-pro",
            "quick_think_llm": "deepseek-v4-flash",
        },
    )

    from tradingagents.graph import trading_graph

    assert trading_graph.CAPTURED_CONFIG == {
        "llm_provider": "deepseek",
        "deep_think_llm": "deepseek-v4-pro",
        "quick_think_llm": "deepseek-v4-flash",
    }
