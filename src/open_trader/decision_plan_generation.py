from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from .akshare_quote import AkShareDailyKlineProvider
from .backtest_prices import DailyKlineProvider, ensure_resolved_backtest_price_range
from .decision_plan import build_decision_plan, publish_decision_plans
from .futu_quote import FutuQuoteClient
from .standard_strategies import build_current_strategy_snapshot, strategy_catalog
from .strategy_backtest import StandardBacktestRequest, run_standard_backtest


@dataclass(frozen=True)
class DecisionPlansBuildResult:
    run_path: Path
    latest_path: Path
    records: int


def generate_daily_decision_plans(
    *,
    portfolio_path: Path,
    technical_facts_path: Path,
    tradingagents_summary_path: Path,
    data_dir: Path,
    reports_dir: Path,
    run_date: str,
    market: str,
    futu_host: str,
    futu_port: int,
    update_latest: bool,
    price_provider: DailyKlineProvider | None = None,
) -> DecisionPlansBuildResult:
    if not technical_facts_path.exists() or not tradingagents_summary_path.exists():
        raise ValueError("每日计划缺少技术事实或 TradingAgents 摘要")
    normalized_market = market.strip().upper()
    owned_provider = price_provider is None
    provider = price_provider or (
        AkShareDailyKlineProvider()
        if normalized_market == "CN"
        else FutuQuoteClient(host=futu_host, port=futu_port)
    )
    provider_source = "akshare" if normalized_market == "CN" else "futu"
    try:
        rows = _portfolio_rows(portfolio_path)
        nav_hkd = sum(
            (Decimal(row["market_value_hkd"]) for row in rows if row.get("market_value_hkd")),
            Decimal("0"),
        )
        summaries = _summary_by_symbol(tradingagents_summary_path)
        plans: list[dict[str, object]] = []
        for row in rows:
            if not _eligible(row, normalized_market):
                continue
            symbol = (row.get("analysis_symbol") or row.get("symbol") or "").strip().upper()
            resolved = ensure_resolved_backtest_price_range(
                data_dir=data_dir, market=normalized_market, symbol=symbol,
                preset="5Y", custom_start=None, custom_end=date.fromisoformat(run_date),
                provider=provider,
            )
            bars = tuple(resolved.price_range.bars)
            if not bars:
                raise ValueError(f"{normalized_market}.{symbol} 没有可用日线")
            snapshots = [
                build_current_strategy_snapshot(
                    definition.strategy_id, bars, Decimal("0.10"),
                )
                for definition in strategy_catalog()
            ]
            evidence: list[dict[str, object]] = []
            available_days = (bars[-1].date - bars[0].date).days
            for range_name, minimum_days in (("6M", 150), ("1Y", 330), ("5Y", 1650)):
                if available_days < minimum_days:
                    continue
                for definition in strategy_catalog():
                    result = run_standard_backtest(
                        StandardBacktestRequest(
                            data_dir=data_dir, reports_dir=reports_dir,
                            market=normalized_market, symbol=symbol,
                            strategy_id=definition.strategy_id,
                            range_preset=range_name, custom_start=None, custom_end=None,
                            initial_cash=Decimal("100000"),
                            max_strategy_weight=Decimal("0.10"),
                            commission_bps=Decimal("10"), slippage_bps=Decimal("5"),
                        ),
                        price_provider=provider,
                    )
                    payload = result.to_dict()
                    payload["range"] = range_name
                    payload["source_provider"] = provider_source
                    evidence.append(payload)
            fx = Decimal(row.get("fx_to_hkd") or "1")
            effective_at, expires_at = _market_session(run_date, normalized_market)
            plan = build_decision_plan(
                run_date=run_date, market=normalized_market, symbol=symbol,
                position={
                    "quantity": row.get("total_quantity") or "0",
                    "weight": str(Decimal((row.get("portfolio_weight_hkd") or "0").rstrip("%")) / Decimal("100")),
                    "nav": str(nav_hkd / fx),
                    "price": str(bars[-1].close),
                },
                strategy_snapshots=snapshots, backtests=evidence,
                technical_facts=snapshots[0]["facts"],
                tradingagents_summary=summaries.get(symbol, {}),
                effective_at=effective_at, expires_at=expires_at,
            )
            plan["market_data_source"] = provider_source
            plans.append(plan)
        run_path, latest_path = publish_decision_plans(
            data_dir=data_dir, run_date=run_date, market=normalized_market,
            records=plans, update_latest=update_latest,
        )
        return DecisionPlansBuildResult(run_path, latest_path, len(plans))
    finally:
        if owned_provider and hasattr(provider, "close"):
            provider.close()  # type: ignore[attr-defined]


def _portfolio_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _eligible(row: Mapping[str, str], market: str) -> bool:
    return (
        (row.get("market") or "").strip().upper() == market
        and (row.get("asset_class") or "").strip().lower() in {"stock", "etf"}
        and (row.get("ai_eligible") or "").strip().lower() == "true"
    )


def _summary_by_symbol(path: Path) -> dict[str, dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", []) if isinstance(payload, Mapping) else []
    return {
        str(record.get("symbol") or "").strip().upper(): dict(record)
        for record in records
        if isinstance(record, Mapping) and record.get("symbol")
    }


def _market_session(run_date: str, market: str) -> tuple[str, str]:
    zone = ZoneInfo("America/New_York" if market == "US" else "Asia/Shanghai" if market == "CN" else "Asia/Hong_Kong")
    day = date.fromisoformat(run_date)
    return (
        datetime.combine(day, time(9, 30), zone).isoformat(),
        datetime.combine(day, time(16, 0), zone).isoformat(),
    )
