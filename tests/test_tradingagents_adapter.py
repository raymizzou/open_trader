from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from open_trader.advice.models import PortfolioInputRow
from open_trader.advice.tradingagents_adapter import (
    TradingAgentsAdapter,
    TradingAgentsSubprocessRunner,
)


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


def test_adapter_uses_hk_futu_symbol_and_records_hk_context() -> None:
    graph = FakeGraph()
    adapter = TradingAgentsAdapter.from_graph(graph)
    row = PortfolioInputRow(
        symbol="00700",
        market="HK",
        asset_class="stock",
        name="Tencent",
        portfolio_weight_hkd="2.00%",
        risk_flag="normal",
        analysis_symbol="00700",
    )

    advice = adapter.analyze(row, "2026-06-19")
    raw = json.loads(advice.raw_decision)

    assert graph.calls == [("HK.00700", "2026-06-19")]
    assert raw["market_context"] == {
        "market": "HK",
        "market_name": "Hong Kong / HKEX",
        "currency": "HKD",
        "portfolio_symbol": "00700",
        "tradingagents_symbol": "HK.00700",
        "futu_symbol": "HK.00700",
    }


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


def test_adapter_formats_trader_decision_with_standard_template() -> None:
    class TemplateShapeGraph:
        def propagate(self, symbol: str, run_date: str) -> tuple[dict[str, str], str]:
            return {
                "final_trade_decision": (
                    "**Rating**: Overweight\n\n"
                    "**Executive Summary**: 在380-400美元区间分3-4次买入目标仓位的60%，"
                    "350美元附近加仓剩余40%。统一停损线设在340美元。"
                    "10月底财报为关键催化剂。\n\n"
                    "**Investment Thesis**: 微软AI商业化路径清晰，Azure和Copilot支撑中期增长。\n\n"
                    "**Price Target**: 450.0\n\n"
                    "**Time Horizon**: 3-6个月"
                )
            }, "Overweight"

    adapter = TradingAgentsAdapter.from_graph(TemplateShapeGraph())

    advice = adapter.analyze(portfolio_row("MSFT"), "2026-06-16")

    assert advice.advice_action == "Overweight"
    assert advice.advice_summary == "\n".join(
        [
            "评级：Overweight",
            "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。统一停损线设在340美元。10月底财报为关键催化剂。",
            "风控：统一停损线设在340美元。",
            "仓位：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
            "催化剂：10月底财报为关键催化剂。",
            "目标价：450.0",
            "时间窗口：3-6个月",
            "理由：微软AI商业化路径清晰，Azure和Copilot支撑中期增长。",
        ]
    )


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


def test_subprocess_runner_reads_worker_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: float | None,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "run_date": "2026-06-16",
                    "symbol": "VIXY",
                    "market": "US",
                    "asset_class": "etf",
                    "portfolio_weight_hkd": "3.05%",
                    "risk_flag": "normal",
                    "source": "tradingagents",
                    "advice_action": "hold",
                    "advice_summary": "Hold VIXY",
                    "raw_decision": "{}",
                    "status": "ok",
                    "error": "",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="noise", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = TradingAgentsSubprocessRunner(
        project_path=tmp_path / "TradingAgents",
        config_overrides={"llm_provider": "deepseek"},
        timeout_seconds=45.0,
        python_executable="/python",
    )

    advice = runner.analyze(portfolio_row("VIXY"), "2026-06-16")

    assert advice.status == "ok"
    assert advice.symbol == "VIXY"
    assert advice.advice_action == "hold"
    assert captured["cwd"] == Path.cwd()
    assert captured["timeout"] == 45.0
    assert "open_trader.advice.tradingagents_worker" in captured["command"]


def test_subprocess_runner_returns_error_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["worker"], timeout=30.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = TradingAgentsSubprocessRunner(
        project_path=tmp_path / "TradingAgents",
        config_overrides={},
        timeout_seconds=30.0,
        python_executable="/python",
    )

    advice = runner.analyze(portfolio_row("QQQ"), "2026-06-16")

    assert advice.status == "error"
    assert advice.symbol == "QQQ"
    assert "timed out after 30.0 seconds" in advice.error


def test_subprocess_runner_can_run_without_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: float | None,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = timeout
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "run_date": "2026-06-16",
                    "symbol": "MSFT",
                    "market": "US",
                    "asset_class": "stock",
                    "portfolio_weight_hkd": "1.13%",
                    "risk_flag": "normal",
                    "source": "tradingagents",
                    "advice_action": "Overweight",
                    "advice_summary": "评级：Overweight",
                    "raw_decision": "{}",
                    "status": "ok",
                    "error": "",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = TradingAgentsSubprocessRunner(
        project_path=tmp_path / "TradingAgents",
        config_overrides={},
        timeout_seconds=None,
        python_executable="/python",
    )

    advice = runner.analyze(portfolio_row("MSFT"), "2026-06-16")

    assert advice.status == "ok"
    assert captured["timeout"] is None
    assert advice.source == "tradingagents"
    assert advice.error == ""
