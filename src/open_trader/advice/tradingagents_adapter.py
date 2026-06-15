from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .models import PortfolioInputRow, TradingAdvice


class TradingAgentsAdapter:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @classmethod
    def from_graph(cls, graph: Any) -> TradingAgentsAdapter:
        return cls(graph)

    @classmethod
    def from_project_path(cls, project_path: Path) -> TradingAgentsAdapter:
        resolved_project_path = project_path.resolve()
        if not resolved_project_path.exists():
            raise FileNotFoundError(project_path)

        project_path_str = str(resolved_project_path)
        inserted_path = False
        if project_path_str not in _resolved_sys_path_strings():
            sys.path.insert(0, project_path_str)
            inserted_path = True

        try:
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())
        except Exception:
            if inserted_path:
                sys.path.remove(project_path_str)
            raise
        return cls(graph)

    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        try:
            state, decision = self._graph.propagate(row.analysis_symbol, run_date)
            return TradingAdvice(
                run_date=run_date,
                symbol=row.symbol,
                market=row.market,
                asset_class=row.asset_class,
                portfolio_weight_hkd=row.portfolio_weight_hkd,
                risk_flag=row.risk_flag,
                source="tradingagents",
                advice_action=_extract_action(decision),
                advice_summary=_extract_summary(state, decision),
                raw_decision=json.dumps(
                    {"state": state, "decision": decision},
                    ensure_ascii=False,
                    default=str,
                ),
                status="ok",
                error="",
            )
        except Exception as exc:
            return TradingAdvice(
                run_date=run_date,
                symbol=row.symbol,
                market=row.market,
                asset_class=row.asset_class,
                portfolio_weight_hkd=row.portfolio_weight_hkd,
                risk_flag=row.risk_flag,
                source="tradingagents",
                advice_action="",
                advice_summary="",
                raw_decision="",
                status="error",
                error=str(exc),
            )


def _extract_action(decision: Any) -> str:
    if isinstance(decision, str):
        return decision
    if isinstance(decision, dict):
        for key in ("action", "decision", "recommendation", "signal"):
            value = decision.get(key)
            if value:
                return str(value)
    return ""


def _extract_summary(state: Any, decision: Any) -> str:
    if isinstance(state, dict):
        value = state.get("final_trade_decision")
        if value:
            return str(value)
    if isinstance(decision, dict):
        for key in ("summary", "reasoning", "rationale", "analysis"):
            value = decision.get(key)
            if value:
                return str(value)
    return str(decision)


def _resolved_sys_path_strings() -> set[str]:
    return {str(Path(path_entry).resolve()) for path_entry in sys.path}
