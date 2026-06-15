from __future__ import annotations

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


def test_adapter_rejects_missing_tradingagents_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        TradingAgentsAdapter.from_project_path(tmp_path / "missing")
