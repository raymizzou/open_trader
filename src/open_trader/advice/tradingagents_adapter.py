from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .models import PortfolioInputRow, TradingAdvice
from .trader_template import format_trader_template


class TradingAgentsAdapter:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @classmethod
    def from_graph(cls, graph: Any) -> TradingAgentsAdapter:
        return cls(graph)

    @classmethod
    def from_project_path(
        cls,
        project_path: Path,
        *,
        config_overrides: Mapping[str, object] | None = None,
    ) -> TradingAgentsAdapter:
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

            config = DEFAULT_CONFIG.copy()
            if config_overrides is not None:
                config.update(
                    {
                        key: value
                        for key, value in config_overrides.items()
                        if value is not None
                    }
                )
            graph = TradingAgentsGraph(debug=False, config=config)
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
                advice_summary=_extract_summary(
                    state,
                    decision,
                    action=_extract_action(decision),
                ),
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


def _extract_summary(state: Any, decision: Any, *, action: str = "") -> str:
    if isinstance(state, dict):
        value = state.get("final_trade_decision")
        if value:
            return format_trader_template(value, action)
    if isinstance(decision, dict):
        for key in ("summary", "reasoning", "rationale", "analysis"):
            value = decision.get(key)
            if value:
                return str(value)
    return str(decision)


def _resolved_sys_path_strings() -> set[str]:
    return {str(Path(path_entry).resolve()) for path_entry in sys.path}


class TradingAgentsSubprocessRunner:
    def __init__(
        self,
        *,
        project_path: Path,
        config_overrides: Mapping[str, object],
        timeout_seconds: float | None,
        python_executable: str | None = None,
    ) -> None:
        self._project_path = project_path
        self._config_overrides = dict(config_overrides)
        self._timeout_seconds = timeout_seconds
        self._python_executable = python_executable or sys.executable

    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".json",
            prefix=f"open-trader-{row.symbol}-",
            delete=False,
        ) as handle:
            output_path = Path(handle.name)

        command = [
            self._python_executable,
            "-m",
            "open_trader.advice.tradingagents_worker",
            "--project-path",
            str(self._project_path),
            "--run-date",
            run_date,
            "--row-json",
            json.dumps(dataclasses.asdict(row), ensure_ascii=False),
            "--config-json",
            json.dumps(self._config_overrides, ensure_ascii=False),
            "--output",
            str(output_path),
        ]

        try:
            subprocess.run(
                command,
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=True,
            )
            data = json.loads(output_path.read_text(encoding="utf-8"))
            return TradingAdvice(**data)
        except subprocess.TimeoutExpired:
            timeout_label = (
                "disabled"
                if self._timeout_seconds is None
                else f"{self._timeout_seconds} seconds"
            )
            return _error_advice(
                row=row,
                run_date=run_date,
                error=f"TradingAgents timed out after {timeout_label}",
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            return _error_advice(row=row, run_date=run_date, error=detail)
        except Exception as exc:
            return _error_advice(row=row, run_date=run_date, error=str(exc))
        finally:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass


def _error_advice(
    *,
    row: PortfolioInputRow,
    run_date: str,
    error: str,
) -> TradingAdvice:
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
        error=error,
    )
