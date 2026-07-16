from __future__ import annotations

import argparse
import json
from getpass import getpass
import re
import sys
import time
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .advice.change_classifier import ChangeClassifier, OpenAIClassifierClient
from .advice.premarket import run_premarket
from .advice.tradingagents_adapter import TradingAgentsSubprocessRunner
from .a_share_trend import run_a_share_trend_report
from .a_share_trend_watch import watch_a_share_protection
from .market_trend import market_paths, run_market_trend_report
from .market_trend_watch import watch_market_protection
from .backtest import run_backtest
from .daily_premarket import (
    DailyPremarketRunner,
    RunLock,
    _read_env_file,
    build_notifier,
    load_env_config,
    refresh_live_portfolio,
    send_notification_with_results,
)
from .dashboard import DashboardConfig
from .dashboard_web import serve_dashboard
from .decision_facts import LLMDecisionFactsExtractor, generate_decision_facts
from .decision_plan import load_decision_plans
from .decision_plan_watch import run_decision_plan_watch
from .futu_account import FutuAccountClient, FutuAccountError, sync_futu_portfolio
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_skill_facts import FutuSkillFactsExtractor, generate_futu_skill_facts
from .kelly_paper_order_sync import (
    FakeFutuPaperOrderClient,
    FutuPaperOrderSyncError,
    FutuSimulatePaperOrderClient,
    MultiMarketPaperOrderClient,
    build_kelly_paper_order_sync_report,
    default_fake_kelly_paper_orders,
    load_kelly_experiment_symbol_index_details,
    load_kelly_order_links,
    sync_kelly_paper_orders,
    write_kelly_paper_order_sync_report,
)
from .kelly_order_intents import (
    build_kelly_order_intents,
    write_kelly_order_intents,
)
from .kelly_order_risk import (
    build_kelly_order_risk_checks,
    write_kelly_order_risk_checks,
)
from .kelly_strategy_capital import (
    build_kelly_strategy_capital_payload,
    load_kelly_strategy_capital,
    write_kelly_strategy_capital,
)
from .kelly_strategy_stats import (
    build_kelly_strategy_stats_payload,
    write_kelly_strategy_stats,
)
from .kelly_trade_samples import (
    build_kelly_trade_samples_payload,
    load_kelly_trade_samples,
    write_kelly_trade_samples,
)
from .kelly_lab import load_kelly_lab_state
from .kelly_order_execution import (
    FutuOrderExecutionError,
    FutuSimulateOrderExecutionClient,
    MarketRoutingOrderExecutionClient,
    execute_kelly_orders,
    write_kelly_order_links_from_executions,
    write_kelly_order_executions,
)
from .t_signal import TSignalInterpreter
from .t_signal_futu import FutuTSignalMarketDataClient
from .t_signal_runner import run_t_signal_watch_once
from .futu_universe import load_futu_quote_universe
from .futu_watch import run_futu_watch
from .fx import StaticMonthEndFxProvider
from .market_scope import parse_market_scope
from .notifications import NullNotifier
from .parsers.phillips import PhillipsStatementParser
from .parsers.eastmoney import EastmoneyStatementParser
from .pipeline import run_import, validate_month
from .report_translation import DeepSeekReportTranslator, translate_agent_report_files
from .tiger_account import (
    TigerAccountClient,
    TigerAccountError,
    TigerPortfolioSyncResult,
    load_tiger_account_config,
    mask_account_id,
    sync_tiger_portfolio,
)
from .technical_facts import LLMTechnicalFactsExtractor, generate_technical_facts
from .trade_actions import generate_trade_actions
from .tradingagents_summary import (
    LLMTradingAgentsSummaryExtractor,
    generate_tradingagents_summary,
)
from .trading_plan import (
    TradingPlanRow,
    build_trading_plan,
    evaluate_plan_quote,
    load_trading_plan_rows,
)
from .watchlist import build_watchlist


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def positive_decimal(value: str) -> Decimal:
    try:
        rate = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"invalid positive decimal value: {value}"
        ) from exc

    if not rate.is_finite() or rate <= Decimal("0"):
        raise argparse.ArgumentTypeError(
            f"invalid positive decimal value: {value}"
        )
    return rate


def non_negative_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"invalid non-negative decimal value: {value}"
        ) from exc

    if not parsed.is_finite() or parsed < Decimal("0"):
        raise argparse.ArgumentTypeError(
            f"invalid non-negative decimal value: {value}"
        )
    return parsed


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid positive integer: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"invalid positive integer: {value}")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid positive float: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"invalid positive float: {value}")
    return parsed


def canonical_month(value: str) -> str:
    try:
        return validate_month(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid month: {value}") from exc


def canonical_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid date: {value}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date: {value}") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError(f"invalid date: {value}")
    return value


def canonical_market(value: str) -> str:
    try:
        return parse_market_scope(value).value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_symbol_subset(value: str | None) -> set[str] | None:
    if value is None or not value.strip():
        return None
    symbols = {symbol.strip().upper() for symbol in value.split(",") if symbol.strip()}
    return symbols or None


def _parse_symbol_set(value: str | None) -> set[str]:
    return _parse_symbol_subset(value) or set()


def _load_optional_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return _read_env_file(path)


def _optional_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def _load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else None


def _parse_key_value_options(values: list[str], *, option_name: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_value in values:
        if "=" not in raw_value:
            raise ValueError(f"{option_name} must use MARKET.SYMBOL=value: {raw_value}")
        raw_key, raw_item_value = raw_value.split("=", 1)
        key = raw_key.strip().upper()
        item_value = raw_item_value.strip()
        if not key or not item_value:
            raise ValueError(f"{option_name} must use MARKET.SYMBOL=value: {raw_value}")
        if key in parsed:
            raise ValueError(f"{option_name} contains duplicate key: {key}")
        parsed[key] = item_value
    return parsed


def _kelly_sync_trd_markets(
    trd_market: str,
    symbol_index_details: object,
) -> list[str]:
    requested = str(trd_market).strip()
    if requested != "auto":
        return [requested]

    markets: set[str] = set()
    for attr in ("unique", "ambiguous"):
        index = getattr(symbol_index_details, attr, {})
        if not isinstance(index, dict):
            continue
        for key in index:
            if not isinstance(key, tuple) or not key:
                continue
            market = str(key[0]).strip().upper()
            if market in {"HK", "US", "CN"}:
                markets.add(market)
    if not markets:
        raise ValueError("no Kelly experiment markets found for auto Futu sync")
    return sorted(markets)


def _print_tiger_sync_result(result: TigerPortfolioSyncResult) -> None:
    print(f"run_date: {result.run_date}")
    print(f"accounts: {result.account_count}")
    print(f"positions: {result.position_count}")
    print(f"cash: {result.cash_count}")
    print(f"merged_rows: {result.merged_row_count}")
    print(f"snapshot: {result.snapshot_path}")
    print(f"portfolio: {result.portfolio_path}")
    print(f"report: {result.report_path}")
    print(f"latest: {result.latest_path}")
    print(f"updated_latest: {'true' if result.updated_latest else 'false'}")


def _active_trade_action_plans_for_quotes(
    plans: list[TradingPlanRow],
    run_date: str | None,
) -> list[TradingPlanRow]:
    active_plans = [plan for plan in plans if plan.status == "active"]
    if run_date is not None:
        matching_plans = [
            plan
            for plan in active_plans
            if not plan.run_date.strip() or plan.run_date == run_date
        ]
        if not matching_plans:
            raise ValueError(f"no active trading plans match run_date {run_date}")
        return matching_plans

    dates = sorted({
        plan.run_date.strip() for plan in active_plans if plan.run_date.strip()
    })
    if not dates:
        raise ValueError("--date is required when trading plan has no active run_date rows")
    effective_run_date = dates[-1]
    return [
        plan
        for plan in active_plans
        if not plan.run_date.strip() or plan.run_date == effective_run_date
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-statements",
        help="Import monthly broker statements and generate portfolio.csv",
    )
    import_parser.add_argument(
        "--month",
        type=canonical_month,
        required=True,
        help="Statement month, YYYY-MM",
    )
    import_parser.add_argument("--phillips", type=Path)
    import_parser.add_argument("--eastmoney", type=Path)
    import_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
    )
    import_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    import_parser.add_argument(
        "--usd-hkd",
        type=positive_decimal,
        help="Month-end USD/HKD exchange rate",
    )
    import_parser.add_argument("--cny-hkd", type=positive_decimal)
    import_parser.add_argument("--fx-date", type=canonical_date)
    import_parser.add_argument("--update-latest", action="store_true")

    premarket_parser = subparsers.add_parser(
        "run-premarket",
        help="Run daily premarket TradingAgents advice and write action report",
    )
    premarket_parser.add_argument(
        "--date",
        type=canonical_date,
        required=True,
        help="Run date, YYYY-MM-DD",
    )
    premarket_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    premarket_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    premarket_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    premarket_parser.add_argument(
        "--tradingagents-path",
        type=Path,
        default=Path("/Users/ray/projects/TradingAgents"),
    )
    premarket_parser.add_argument(
        "--ta-provider",
        default="deepseek",
        help="TradingAgents LLM provider",
    )
    premarket_parser.add_argument(
        "--ta-deep-model",
        default="deepseek-v4-pro",
        help="TradingAgents deep-thinking model",
    )
    premarket_parser.add_argument(
        "--ta-quick-model",
        default="deepseek-v4-flash",
        help="TradingAgents quick-thinking model",
    )
    premarket_parser.add_argument(
        "--ta-timeout-seconds",
        type=positive_float,
        default=120.0,
        help="TradingAgents LLM request timeout in seconds",
    )
    premarket_parser.add_argument(
        "--ta-max-retries",
        type=positive_int,
        default=1,
        help="TradingAgents LLM request retry count",
    )
    premarket_parser.add_argument(
        "--symbol-timeout-seconds",
        type=positive_float,
        default=300.0,
        help="Hard timeout for one symbol's TradingAgents analysis",
    )
    premarket_parser.add_argument(
        "--no-symbol-timeout",
        action="store_true",
        help="Disable the per-symbol TradingAgents subprocess timeout",
    )
    premarket_parser.add_argument(
        "--symbols",
        help="Comma-separated subset of symbols to analyze",
    )
    premarket_parser.add_argument(
        "--exclude-symbols",
        default="",
        help="Comma-separated symbols to skip in addition to the default blacklist",
    )
    premarket_parser.add_argument(
        "--classifier-model",
        default="deepseek-v4-flash",
        help="DeepSeek model for change classification",
    )
    premarket_parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=3,
        help="Maximum symbols to analyze in parallel",
    )
    premarket_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write run outputs but do not update latest advice or actions",
    )

    daily_parser = subparsers.add_parser(
        "run-daily-premarket",
        help="Run the scheduled daily premarket automation workflow",
    )
    daily_parser.add_argument(
        "--date",
        required=True,
        help="Run date, YYYY-MM-DD, or today",
    )
    daily_parser.add_argument(
        "--market",
        type=canonical_market,
        required=True,
        choices=["HK", "US"],
        help="Market workflow to run: HK or US",
    )
    daily_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
    )
    daily_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write dated outputs but do not update latest artifacts",
    )
    daily_parser.add_argument(
        "--max-workers",
        type=positive_int,
        help="Override OPEN_TRADER_MAX_WORKERS for this daily run",
    )

    trend_report = subparsers.add_parser(
        "trend-a-share-report", help="Generate the Eastmoney A-share trend plan"
    )
    trend_report.add_argument("--date", default="today")
    trend_report.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    trend_report.add_argument("--revision", action="store_true")

    trend_watch = subparsers.add_parser(
        "watch-trend-a-share", help="Watch Eastmoney A-share protection lines"
    )
    trend_watch.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    trend_watch.add_argument("--poll-seconds", type=positive_float, default=5.0)
    trend_watch.add_argument(
        "--reconnect-seconds", type=positive_float, default=60.0
    )
    trend_watch.add_argument("--once", action="store_true")

    market_trend_report = subparsers.add_parser(
        "trend-market-report",
        help="Generate a Tiger US or Phillips HK trend plan",
        description="Generate a Tiger US or Phillips HK trend plan",
    )
    market_trend_report.add_argument("--market", choices=("US", "HK"), required=True)
    market_trend_report.add_argument("--date", default="today")
    market_trend_report.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    market_trend_report.add_argument("--revision", action="store_true")

    market_trend_watch = subparsers.add_parser(
        "watch-trend-market",
        help="Watch Tiger US or Phillips HK trend protection lines",
        description="Watch Tiger US or Phillips HK trend protection lines",
    )
    market_trend_watch.add_argument("--market", choices=("US", "HK"), required=True)
    market_trend_watch.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    market_trend_watch.add_argument("--poll-seconds", type=positive_float, default=5.0)
    market_trend_watch.add_argument(
        "--reconnect-seconds", type=positive_float, default=60.0
    )
    market_trend_watch.add_argument("--once", action="store_true")

    test_notification_parser = subparsers.add_parser(
        "test-notification",
        help="Send a test notification using configured notifiers",
    )
    test_notification_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
    )

    watchlist_parser = subparsers.add_parser(
        "build-watchlist",
        help="Convert premarket action rows into watchlist.csv",
    )
    watchlist_parser.add_argument(
        "--actions",
        type=Path,
        default=Path("data/latest/premarket_actions.csv"),
    )
    watchlist_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    watchlist_parser.add_argument(
        "--date",
        type=canonical_date,
        help="Run date, YYYY-MM-DD. Required only when actions rows do not contain run_date.",
    )
    watchlist_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write run output but do not update latest watchlist",
    )

    technical_facts_parser = subparsers.add_parser(
        "extract-technical-facts",
        help="Extract structured technical facts from TradingAgents advice CSV",
    )
    technical_facts_parser.add_argument(
        "--advice",
        type=Path,
        required=True,
        help="TradingAgents trading advice CSV path",
    )
    technical_facts_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    technical_facts_parser.add_argument(
        "--date",
        type=canonical_date,
        help="Run date, YYYY-MM-DD. Defaults to latest run_date in advice rows.",
    )
    technical_facts_parser.add_argument(
        "--market",
        type=canonical_market,
        choices=["HK", "US"],
        help="Optional market scope: HK or US",
    )
    technical_facts_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest technical_facts.json after writing dated artifact",
    )

    decision_facts_parser = subparsers.add_parser(
        "extract-decision-facts",
        help="Extract structured decision facts from TradingAgents advice CSV",
    )
    decision_facts_parser.add_argument(
        "--advice",
        type=Path,
        required=True,
        help="TradingAgents trading advice CSV path",
    )
    decision_facts_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    decision_facts_parser.add_argument(
        "--date",
        type=canonical_date,
        help="Run date, YYYY-MM-DD. Defaults to latest run_date in advice rows.",
    )
    decision_facts_parser.add_argument(
        "--market",
        type=canonical_market,
        choices=["HK", "US"],
        help="Optional market scope: HK or US",
    )
    decision_facts_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest decision_facts.json after writing dated artifact",
    )

    futu_skill_facts_parser = subparsers.add_parser(
        "extract-futu-skill-facts",
        help="Extract Futu Skills-backed facts for dashboard plugin cards",
    )
    futu_skill_facts_parser.add_argument(
        "--portfolio",
        type=Path,
        required=True,
        help="Portfolio CSV path",
    )
    futu_skill_facts_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    futu_skill_facts_parser.add_argument(
        "--date",
        type=canonical_date,
        required=True,
        help="Run date, YYYY-MM-DD",
    )
    futu_skill_facts_parser.add_argument(
        "--market",
        type=canonical_market,
        choices=["HK", "US"],
        help="Optional market scope: HK or US",
    )
    futu_skill_facts_parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Natural-day anomaly window, 1-30 days. Defaults to 7.",
    )
    futu_skill_facts_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest futu_skill_facts.json after writing dated artifact",
    )

    tradingagents_summary_parser = subparsers.add_parser(
        "extract-tradingagents-summary",
        help="Extract fixed TradingAgents card summary fields from run artifacts",
    )
    tradingagents_summary_parser.add_argument(
        "--advice",
        type=Path,
        required=True,
        help="TradingAgents trading advice CSV path",
    )
    tradingagents_summary_parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="Trading plan CSV path",
    )
    tradingagents_summary_parser.add_argument(
        "--actions",
        type=Path,
        required=True,
        help="Trade actions CSV path",
    )
    tradingagents_summary_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    tradingagents_summary_parser.add_argument(
        "--date",
        type=canonical_date,
        help="Run date, YYYY-MM-DD. Defaults to latest run_date in advice rows.",
    )
    tradingagents_summary_parser.add_argument(
        "--market",
        type=canonical_market,
        choices=["HK", "US"],
        help="Optional market scope: HK or US",
    )
    tradingagents_summary_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest tradingagents_summary.json after writing dated artifact",
    )

    watch_futu_parser = subparsers.add_parser(
        "watch-futu",
        help="Watch active US/HK price triggers with Futu OpenD quotes",
    )
    watch_futu_parser.add_argument(
        "--watchlist",
        type=Path,
        default=Path("data/latest/watchlist.csv"),
    )
    watch_futu_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    watch_futu_parser.add_argument("--date", type=canonical_date)
    watch_futu_parser.add_argument("--host", default="127.0.0.1")
    watch_futu_parser.add_argument("--port", type=positive_int, default=11111)
    watch_futu_parser.add_argument(
        "--poll-seconds",
        type=positive_float,
        default=5.0,
    )
    watch_futu_parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch one quote snapshot and exit",
    )

    watch_decision_parser = subparsers.add_parser(
        "watch-decision-plans",
        help="Watch validated daily decision-plan conditions with Futu quotes",
    )
    watch_decision_parser.add_argument(
        "--plans", type=Path, required=True,
    )
    watch_decision_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    watch_decision_parser.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env"),
    )
    watch_decision_parser.add_argument("--host", default="127.0.0.1")
    watch_decision_parser.add_argument("--port", type=positive_int, default=11111)
    watch_decision_parser.add_argument("--poll-seconds", type=positive_float, default=5.0)
    watch_decision_parser.add_argument("--once", action="store_true")

    watch_t_parser = subparsers.add_parser(
        "watch-t",
        help="Generate 做T signals for current HK/US holdings",
    )
    watch_t_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    watch_t_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    watch_t_parser.add_argument("--date", type=canonical_date, required=True)
    watch_t_parser.add_argument(
        "--market",
        type=canonical_market,
        choices=["HK", "US"],
        required=True,
    )
    watch_t_parser.add_argument(
        "--session-phase",
        choices=["pre_market", "regular", "post_market", "closed", "unknown"],
        default="regular",
    )
    watch_t_parser.add_argument("--host", default="127.0.0.1")
    watch_t_parser.add_argument("--port", type=positive_int, default=11111)
    watch_t_parser.add_argument(
        "--poll-seconds",
        type=positive_float,
        default=5.0,
    )
    watch_t_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
        help="Notification config env file",
    )
    watch_t_parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch one market-data snapshot and exit",
    )

    check_futu_quotes_parser = subparsers.add_parser(
        "check-futu-quotes",
        help="Fetch Futu quote snapshots for quoteable portfolio positions",
    )
    check_futu_quotes_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    check_futu_quotes_parser.add_argument("--host", default="127.0.0.1")
    check_futu_quotes_parser.add_argument("--port", type=positive_int, default=11111)

    check_futu_account_parser = subparsers.add_parser(
        "check-futu-account",
        help="Diagnose read-only Futu real-account access",
    )
    check_futu_account_parser.add_argument("--host", default="127.0.0.1")
    check_futu_account_parser.add_argument("--port", type=positive_int, default=11111)

    sync_futu_portfolio_parser = subparsers.add_parser(
        "sync-futu-portfolio",
        help="Merge live Futu real-account data into portfolio.csv",
    )
    sync_futu_portfolio_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    sync_futu_portfolio_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sync_futu_portfolio_parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
    )
    sync_futu_portfolio_parser.add_argument("--date", type=canonical_date, required=True)
    sync_futu_portfolio_parser.add_argument("--host", default="127.0.0.1")
    sync_futu_portfolio_parser.add_argument("--port", type=positive_int, default=11111)
    sync_futu_portfolio_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest/portfolio.csv after writing dated artifacts",
    )

    check_tiger_account_parser = subparsers.add_parser(
        "check-tiger-account",
        help="Diagnose read-only Tiger OpenAPI account access",
    )
    check_tiger_account_parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("~/.tigeropen/"),
    )
    check_tiger_account_parser.add_argument("--account")
    check_tiger_account_parser.add_argument("--sandbox", action="store_true")

    sync_tiger_portfolio_parser = subparsers.add_parser(
        "sync-tiger-portfolio",
        help="Merge live Tiger OpenAPI account data into portfolio.csv",
    )
    sync_tiger_portfolio_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    sync_tiger_portfolio_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sync_tiger_portfolio_parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
    )
    sync_tiger_portfolio_parser.add_argument("--date", type=canonical_date, required=True)
    sync_tiger_portfolio_parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("~/.tigeropen/"),
    )
    sync_tiger_portfolio_parser.add_argument("--account")
    sync_tiger_portfolio_parser.add_argument("--sandbox", action="store_true")
    sync_tiger_portfolio_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest/portfolio.csv after writing dated artifacts",
    )

    kelly_parser = subparsers.add_parser(
        "kelly",
        help="Run Kelly Lab workflows",
    )
    kelly_subparsers = kelly_parser.add_subparsers(
        dest="kelly_command",
        required=True,
    )
    kelly_sync_paper_orders_parser = kelly_subparsers.add_parser(
        "sync-paper-orders",
        help="Refresh Kelly Lab paper-order artifact",
    )
    kelly_order_source_group = kelly_sync_paper_orders_parser.add_mutually_exclusive_group(
        required=True
    )
    kelly_order_source_group.add_argument(
        "--fake",
        action="store_true",
        help="Use built-in fake simulate orders.",
    )
    kelly_order_source_group.add_argument(
        "--futu-simulate",
        action="store_true",
        help="Read orders from Futu simulate account through OpenD.",
    )
    kelly_sync_paper_orders_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_sync_paper_orders_parser.add_argument(
        "--synced-at",
        help="Override sync timestamp for deterministic local demos",
    )
    kelly_sync_paper_orders_parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Write a paper-order sync diagnostic report.",
    )
    kelly_sync_paper_orders_parser.add_argument("--host", default="127.0.0.1")
    kelly_sync_paper_orders_parser.add_argument(
        "--port",
        type=positive_int,
        default=11111,
    )
    kelly_sync_paper_orders_parser.add_argument(
        "--trd-market",
        choices=("auto", "HK", "US", "CN"),
        default="auto",
        help="Futu trading market used to select the simulate account. Use auto to follow Kelly experiment markets.",
    )

    kelly_build_order_intents_parser = kelly_subparsers.add_parser(
        "build-order-intents",
        help="Build Kelly order intents from pending lifecycle states",
    )
    kelly_build_order_intents_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_build_order_intents_parser.add_argument(
        "--created-at",
        help="Override intent creation timestamp for deterministic local demos",
    )

    kelly_build_strategy_capital_parser = kelly_subparsers.add_parser(
        "build-strategy-capital",
        help="Build Kelly strategy capital from lab state and latest order artifacts",
    )
    kelly_build_strategy_capital_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_build_strategy_capital_parser.add_argument(
        "--calculated-at",
        help="Override capital calculation timestamp for deterministic local demos",
    )

    kelly_build_trade_samples_parser = kelly_subparsers.add_parser(
        "build-trade-samples",
        help="Build Kelly trade samples and stats from synced paper orders",
    )
    kelly_build_trade_samples_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_build_trade_samples_parser.add_argument(
        "--generated-at",
        help="Override sample generation timestamp for deterministic local demos",
    )

    kelly_build_strategy_stats_parser = kelly_subparsers.add_parser(
        "build-strategy-stats",
        help="Build Kelly strategy stats from the latest trade samples",
    )
    kelly_build_strategy_stats_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_build_strategy_stats_parser.add_argument(
        "--generated-at",
        help="Override stats generation timestamp for deterministic local demos",
    )

    kelly_check_order_risk_parser = kelly_subparsers.add_parser(
        "check-order-risk",
        help="Check Kelly order intents against first-pass risk limits",
    )
    kelly_check_order_risk_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_check_order_risk_parser.add_argument(
        "--checked-at",
        help="Override risk-check timestamp for deterministic local demos",
    )
    kelly_check_order_risk_parser.add_argument(
        "--max-entry-position-pct",
        default="4",
        help="Maximum allowed Kelly entry position percentage per symbol",
    )

    kelly_execute_orders_parser = kelly_subparsers.add_parser(
        "execute-orders",
        help="Execute approved Kelly order risk checks",
    )
    execution_mode_group = kelly_execute_orders_parser.add_mutually_exclusive_group()
    execution_mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Build execution records without submitting to Futu. This is the default.",
    )
    execution_mode_group.add_argument(
        "--futu-simulate",
        action="store_true",
        help="Submit approved orders to the Futu SIMULATE trading environment.",
    )
    kelly_execute_orders_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_execute_orders_parser.add_argument(
        "--executed-at",
        help="Override execution timestamp for deterministic local demos",
    )
    kelly_execute_orders_parser.add_argument(
        "--limit-price",
        action="append",
        default=[],
        help="Limit price as MARKET.SYMBOL=PRICE. Repeat for multiple symbols.",
    )
    kelly_execute_orders_parser.add_argument(
        "--order-qty",
        action="append",
        default=[],
        help="Explicit order quantity as MARKET.SYMBOL=QTY. Required for sell orders.",
    )
    kelly_execute_orders_parser.add_argument("--host", default="127.0.0.1")
    kelly_execute_orders_parser.add_argument(
        "--port",
        type=positive_int,
        default=11111,
    )
    kelly_execute_orders_parser.add_argument(
        "--simulate-acc-id",
        type=int,
        help="Futu SIMULATE securities account id to use when multiple exist.",
    )
    kelly_execute_orders_parser.add_argument(
        "--trd-market",
        choices=("auto", "HK", "US", "CN"),
        default="auto",
        help="Futu trading market used to select the simulate account. Use auto to follow Kelly order markets.",
    )

    trading_plan_parser = subparsers.add_parser(
        "build-trading-plan",
        help="Convert trading_advice.csv into structured trading_plan.csv",
    )
    trading_plan_parser.add_argument(
        "--advice",
        type=Path,
        default=Path("data/latest/trading_advice.csv"),
    )
    trading_plan_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    trading_plan_parser.add_argument("--date", type=canonical_date)
    trading_plan_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write run output but do not update latest trading plan",
    )

    translate_reports_parser = subparsers.add_parser(
        "translate-agent-reports",
        help="Translate TradingAgents report fields into Chinese columns",
    )
    translate_reports_parser.add_argument(
        "--advice",
        type=Path,
        default=Path("data/latest/trading_advice.csv"),
    )
    translate_reports_parser.add_argument(
        "--plan",
        type=Path,
        default=Path("data/latest/trading_plan.csv"),
    )
    translate_reports_parser.add_argument(
        "--model",
        default="deepseek-v4-flash",
        help="DeepSeek model for report translation",
    )
    translate_reports_parser.add_argument(
        "--force",
        action="store_true",
        help="Retranslate fields even when Chinese columns already exist",
    )

    check_futu_plan_parser = subparsers.add_parser(
        "check-futu-plan",
        help="Evaluate live Futu quotes against trading_plan.csv",
    )
    check_futu_plan_parser.add_argument(
        "--plan",
        type=Path,
        default=Path("data/latest/trading_plan.csv"),
    )
    check_futu_plan_parser.add_argument("--host", default="127.0.0.1")
    check_futu_plan_parser.add_argument("--port", type=positive_int, default=11111)

    trade_actions_parser = subparsers.add_parser(
        "generate-trade-actions",
        help="Generate trade action CSV and report from trading_plan.csv",
    )
    trade_actions_parser.add_argument(
        "--plan",
        type=Path,
        default=Path("data/latest/trading_plan.csv"),
    )
    trade_actions_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    trade_actions_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    trade_actions_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    trade_actions_parser.add_argument(
        "--date",
        type=canonical_date,
        help="Run date, YYYY-MM-DD. Required only when active plan rows do not contain run_date.",
    )
    trade_actions_parser.add_argument("--host", default="127.0.0.1")
    trade_actions_parser.add_argument("--port", type=positive_int, default=11111)
    trade_actions_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write dated output and report but do not update latest trade actions",
    )

    backtest_parser = subparsers.add_parser(
        "run-backtest",
        help="Backtest one active trading-plan rule against historical daily prices",
    )
    backtest_parser.add_argument(
        "--plan",
        type=Path,
        default=Path("data/latest/trading_plan.csv"),
    )
    backtest_parser.add_argument(
        "--prices",
        type=Path,
        required=True,
        help="Historical OHLC CSV with date, open, high, low, close columns",
    )
    backtest_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    backtest_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    backtest_parser.add_argument("--symbol", required=True)
    backtest_parser.add_argument(
        "--market",
        type=canonical_market,
        required=True,
        choices=["HK", "US"],
    )
    backtest_parser.add_argument(
        "--date",
        type=canonical_date,
        required=True,
        help="Trading plan run date, YYYY-MM-DD",
    )
    backtest_parser.add_argument(
        "--initial-cash",
        type=positive_decimal,
        default=Decimal("100000"),
    )
    backtest_parser.add_argument(
        "--initial-position-quantity",
        type=non_negative_decimal,
        default=Decimal("0"),
        help="Existing position quantity to seed sell-side backtests",
    )
    backtest_parser.add_argument(
        "--commission-bps",
        type=non_negative_decimal,
        default=Decimal("10"),
    )
    backtest_parser.add_argument(
        "--slippage-bps",
        type=non_negative_decimal,
        default=Decimal("5"),
    )
    backtest_parser.add_argument(
        "--adapter",
        choices=["backtrader", "simple"],
        default="backtrader",
        help="Backtest execution adapter",
    )

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Serve the realtime portfolio dashboard",
    )
    dashboard_parser.add_argument("--host", default="127.0.0.1")
    dashboard_parser.add_argument("--port", type=positive_int, default=8765)
    dashboard_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    dashboard_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    dashboard_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    dashboard_parser.add_argument(
        "--poll-seconds",
        type=positive_float,
        default=5.0,
    )
    dashboard_parser.add_argument("--futu-host", default="127.0.0.1")
    dashboard_parser.add_argument("--futu-port", type=positive_int, default=11111)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-statements":
        if args.phillips is not None and args.usd_hkd is None:
            parser.error("--phillips requires --usd-hkd")
        config_values = _load_optional_env_values(args.config)
        eastmoney_path = args.eastmoney or (
            None
            if args.phillips is not None
            else _optional_path(config_values.get("OPEN_TRADER_EASTMONEY_STATEMENT"))
        )
        if eastmoney_path is not None and args.cny_hkd is None:
            parser.error("--eastmoney requires --cny-hkd")
        if eastmoney_path is not None and not eastmoney_path.is_file():
            parser.error(f"Eastmoney statement file does not exist: {eastmoney_path}")
        if args.phillips is None and eastmoney_path is None:
            parser.error(
                "provide --phillips, --eastmoney, or "
                "OPEN_TRADER_EASTMONEY_STATEMENT in --config"
            )

        statement_paths: dict[str, Path] = {}
        parsers = []
        rates: dict[str, Decimal] = {}
        if args.phillips is not None:
            statement_paths["phillips"] = args.phillips
            parsers.append(PhillipsStatementParser())
            rates["USD"] = args.usd_hkd
        if eastmoney_path is not None:
            eastmoney_password = (
                config_values.get("OPEN_TRADER_EASTMONEY_PDF_PASSWORD", "").strip()
                or getpass("东方财富对账单密码: ")
            )
            statement_paths["eastmoney"] = eastmoney_path
            parsers.append(EastmoneyStatementParser(eastmoney_password))
            rates["CNY"] = args.cny_hkd
        result = run_import(
            month=args.month,
            statement_paths=statement_paths,
            parsers=parsers,
            data_dir=args.data_dir,
            fx_provider=StaticMonthEndFxProvider(
                args.month, rates, fx_date=args.fx_date
            ),
            update_latest=args.update_latest,
        )
        print(f"portfolio: {result.portfolio_path}")
        print(f"latest: {result.latest_path}")
        print(f"positions: {result.positions_count}")
        print(f"cash: {result.cash_count}")
        print(f"warnings: {result.warnings_count}")
        return 0

    if args.command == "run-premarket":
        symbols = _parse_symbol_subset(args.symbols)
        tradingagents_config_overrides = {
            "llm_provider": args.ta_provider,
            "deep_think_llm": args.ta_deep_model,
            "quick_think_llm": args.ta_quick_model,
            "llm_timeout": args.ta_timeout_seconds,
            "llm_max_retries": args.ta_max_retries,
        }

        def advice_runner_factory() -> TradingAgentsSubprocessRunner:
            return TradingAgentsSubprocessRunner(
                project_path=args.tradingagents_path,
                config_overrides=tradingagents_config_overrides,
                timeout_seconds=(
                    None if args.no_symbol_timeout else args.symbol_timeout_seconds
                ),
            )

        result = run_premarket(
            run_date=args.date,
            portfolio_path=args.portfolio,
            data_dir=args.data_dir,
            reports_dir=args.reports_dir,
            advice_runner=None,
            advice_runner_factory=advice_runner_factory,
            classifier=ChangeClassifier(
                client=OpenAIClassifierClient(model=args.classifier_model)
            ),
            symbols=symbols,
            excluded_symbols=_parse_symbol_set(args.exclude_symbols),
            update_latest=not args.dry_run,
            max_workers=args.max_workers,
        )
        print(f"eligible: {result.eligible_count}")
        print(f"advice: {result.advice_count}")
        print(f"actions: {result.action_count}")
        print(f"advice_csv: {result.advice_path}")
        print(f"actions_csv: {result.actions_path}")
        print(f"report: {result.report_path}")
        return 0

    if args.command == "trend-market-report":
        try:
            config = load_env_config(args.config, dry_run=False)
            pool_ids = (
                config.trend_animals_us_tm_ids
                if args.market == "US"
                else config.trend_animals_hk_tm_ids
            )
            missing = []
            if not config.trend_animals_api_key.strip():
                missing.append("TREND_ANIMALS_API_KEY")
            if not pool_ids:
                missing.append(f"TREND_ANIMALS_WARM_TO_HOT_{args.market}_TM_IDS")
            if missing:
                raise ValueError(f"missing config value(s): {', '.join(missing)}")
            run_date = (
                datetime.now(ZoneInfo(config.timezone)).date().isoformat()
                if args.date == "today"
                else canonical_date(args.date)
            )
            notifier = build_notifier(config)
        except (
            FileNotFoundError,
            ValueError,
            argparse.ArgumentTypeError,
            ZoneInfoNotFoundError,
        ) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            result = run_market_trend_report(
                config=config,
                market=args.market,
                run_date=run_date,
                revision=args.revision,
                notifier=notifier,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps({
            "status": result.status,
            "report_path": str(result.report_path) if result.report_path else None,
            "json_path": str(result.json_path) if result.json_path else None,
        }, ensure_ascii=False))
        return 0 if result.status in {"generated", "existing", "holiday"} else 1

    if args.command == "watch-trend-market":
        try:
            config = load_env_config(args.config, dry_run=False)
            notifier = build_notifier(config)
            paths = market_paths(config.data_dir, config.reports_dir, args.market)
        except (FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

        def market_quote_factory() -> FutuQuoteClient:
            return FutuQuoteClient(host=config.futu_host, port=config.futu_port)

        try:
            with RunLock(paths.watch_lock):
                result = watch_market_protection(
                    market=args.market,
                    data_dir=config.data_dir,
                    portfolio_path=config.portfolio,
                    state_path=paths.state,
                    events_path=paths.events,
                    report_lock_path=paths.report_lock,
                    quote_client=None,
                    quote_client_factory=market_quote_factory,
                    notifier=notifier,
                    poll_seconds=args.poll_seconds,
                    reconnect_seconds=args.reconnect_seconds,
                    once=args.once,
                )
        except (FileNotFoundError, FutuQuoteError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps({
            "status": result.status,
            "watched_symbol_count": result.watched_symbol_count,
            "trigger_count": result.trigger_count,
            "exception_count": result.exception_count,
            "unknown_quote_count": result.unknown_quote_count,
            "events_path": str(result.events_path),
        }, ensure_ascii=False))
        return 0

    if args.command == "trend-a-share-report":
        try:
            config = load_env_config(args.config, dry_run=False)
            missing = []
            if not config.trend_animals_api_key.strip():
                missing.append("TREND_ANIMALS_API_KEY")
            if config.trend_animals_a_share_tm_id != 622466:
                missing.append("TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID")
            if config.trend_animals_etf_tm_id != 697199:
                missing.append("TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID")
            if missing:
                raise ValueError(f"missing config value(s): {', '.join(missing)}")
            run_date = (
                datetime.now(ZoneInfo(config.timezone)).date().isoformat()
                if args.date == "today"
                else canonical_date(args.date)
            )
            notifier = build_notifier(config)
        except (
            FileNotFoundError,
            ValueError,
            argparse.ArgumentTypeError,
            ZoneInfoNotFoundError,
        ) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            result = run_a_share_trend_report(
                config=config,
                run_date=run_date,
                revision=args.revision,
                notifier=notifier,
            )
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
        ) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "status": result.status,
                    "report_path": str(result.report_path) if result.report_path else None,
                    "json_path": str(result.json_path) if result.json_path else None,
                },
                ensure_ascii=False,
            )
        )
        return 0 if result.status in {"generated", "existing", "holiday"} else 1

    if args.command == "watch-trend-a-share":
        try:
            config = load_env_config(args.config, dry_run=False)
            notifier = build_notifier(config)
        except (FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

        def quote_factory() -> FutuQuoteClient:
            return FutuQuoteClient(host=config.futu_host, port=config.futu_port)

        try:
            with RunLock(config.data_dir / "runs/.trend_a_share_watch.lock"):
                result = watch_a_share_protection(
                    portfolio_path=config.portfolio,
                    state_path=config.data_dir
                    / "trend_a_share/protection_state.json",
                    events_path=config.data_dir / "trend_a_share/watch_events.jsonl",
                    report_lock_path=config.data_dir
                    / "runs/.trend_a_share_report.lock",
                    quote_client=None,
                    quote_client_factory=quote_factory,
                    notifier=notifier,
                    poll_seconds=args.poll_seconds,
                    reconnect_seconds=args.reconnect_seconds,
                    once=args.once,
                )
        except (FileNotFoundError, FutuQuoteError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "status": result.status,
                    "watched_symbol_count": result.watched_symbol_count,
                    "trigger_count": result.trigger_count,
                    "exception_count": result.exception_count,
                    "unknown_quote_count": result.unknown_quote_count,
                    "events_path": str(result.events_path),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "test-notification":
        try:
            config = load_env_config(args.config, dry_run=False)
            notifier = build_notifier(config)
            attempts = send_notification_with_results(
                notifier,
                "Open Trader 测试通知",
                "这是一条 Open Trader 测试通知。",
            )
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
            argparse.ArgumentTypeError,
            ZoneInfoNotFoundError,
        ) as exc:
            print(f"通知测试失败：{exc}", file=sys.stderr)
            return 1
        voice_suppressed = any(attempt.suppressed for attempt in attempts)
        failed_attempts = [
            attempt
            for attempt in attempts
            if not attempt.success and not attempt.suppressed
        ]
        if failed_attempts:
            for attempt in failed_attempts:
                print(
                    (
                        "通知测试失败："
                        f"{attempt.channel} {attempt.error_type}: {attempt.error}"
                    ),
                    file=sys.stderr,
                )
            return 1
        if voice_suppressed:
            print("通知测试已发送；语音已跳过：静默时段。")
        else:
            print("通知测试已发送。")
        return 0

    if args.command == "run-daily-premarket":
        try:
            config = load_env_config(args.config, dry_run=args.dry_run)
            if args.max_workers is not None:
                config = replace(config, max_workers=args.max_workers)
            run_date = (
                datetime.now(ZoneInfo(config.timezone)).date().isoformat()
                if args.date == "today"
                else canonical_date(args.date)
            )
            result = DailyPremarketRunner(
                config=config,
                notifier=build_notifier(config),
                portfolio_refresher=(
                    None if args.dry_run else refresh_live_portfolio
                ),
            ).run(
                run_date=run_date,
                market=args.market,
                dry_run=args.dry_run,
            )
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
            argparse.ArgumentTypeError,
            ZoneInfoNotFoundError,
        ) as exc:
            parser.error(str(exc))
        print(f"status: {result.status}")
        print(f"status_json: {result.status_path}")
        print(f"report: {result.report_path}")
        print(f"log: {result.log_path}")
        return 1 if result.status in {"failed", "already_running"} else 0

    if args.command == "build-watchlist":
        try:
            result = build_watchlist(
                actions_path=args.actions,
                data_dir=args.data_dir,
                run_date=args.date,
                update_latest=not args.dry_run,
            )
        except (FileNotFoundError, ValueError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"watchlist: {result.watchlist_count}")
        print(f"watchlist_csv: {result.watchlist_path}")
        print(f"latest: {result.latest_path}")
        return 0

    if args.command == "extract-technical-facts":
        if not args.advice.exists():
            parser.error(f"advice CSV not found: {args.advice}")
        try:
            extractor = LLMTechnicalFactsExtractor()
        except Exception as exc:
            parser.error(str(exc))
        try:
            result = generate_technical_facts(
                advice_path=args.advice,
                data_dir=args.data_dir,
                run_date=args.date,
                extractor=extractor,
                update_latest=args.update_latest,
                market=args.market,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"technical_facts: {result.records}")
        print(f"extracted: {result.extracted}")
        print(f"failed: {result.failed}")
        print(f"reused: {result.reused}")
        print(f"technical_facts_json: {result.run_path}")
        print(f"latest: {result.latest_path}")
        return 0

    if args.command == "extract-decision-facts":
        if not args.advice.exists():
            parser.error(f"advice CSV not found: {args.advice}")
        try:
            extractor = LLMDecisionFactsExtractor()
        except Exception as exc:
            parser.error(str(exc))
        try:
            result = generate_decision_facts(
                advice_path=args.advice,
                data_dir=args.data_dir,
                run_date=args.date,
                extractor=extractor,
                update_latest=args.update_latest,
                market=args.market,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"decision_facts: {result.records}")
        print(f"extracted: {result.extracted}")
        print(f"failed: {result.failed}")
        print(f"decision_facts_json: {result.run_path}")
        print(f"latest: {result.latest_path}")
        return 0

    if args.command == "extract-futu-skill-facts":
        if not args.portfolio.exists():
            parser.error(f"portfolio CSV not found: {args.portfolio}")
        if args.window_days < 1 or args.window_days > 30:
            parser.error("window-days must be between 1 and 30")
        try:
            extractor = FutuSkillFactsExtractor()
        except Exception as exc:
            parser.error(str(exc))
        try:
            result = generate_futu_skill_facts(
                portfolio_path=args.portfolio,
                data_dir=args.data_dir,
                run_date=args.date,
                market=args.market,
                extractor=extractor,
                update_latest=args.update_latest,
                window_days=args.window_days,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"futu_skill_facts: {result.records}")
        print(f"generated: {result.generated}")
        print(f"failed: {result.failed}")
        print(f"window_days: {args.window_days}")
        print(f"futu_skill_facts_json: {result.run_path}")
        print(f"latest: {result.latest_path}")
        return 0

    if args.command == "extract-tradingagents-summary":
        for label, path in (
            ("advice", args.advice),
            ("plan", args.plan),
            ("actions", args.actions),
        ):
            if not path.exists():
                parser.error(f"{label} CSV not found: {path}")
        try:
            extractor = LLMTradingAgentsSummaryExtractor()
        except Exception as exc:
            parser.error(str(exc))
        try:
            result = generate_tradingagents_summary(
                advice_path=args.advice,
                plan_path=args.plan,
                actions_path=args.actions,
                data_dir=args.data_dir,
                run_date=args.date,
                market=args.market,
                extractor=extractor,
                update_latest=args.update_latest,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"summaries: {result.records}")
        print(f"extracted: {result.extracted}")
        print(f"failed: {result.failed}")
        print(f"summary_json: {result.run_path}")
        print(f"latest: {result.latest_path}")
        return 0

    if args.command == "watch-futu":
        try:
            quote_client = FutuQuoteClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            result = run_futu_watch(
                watchlist_path=args.watchlist,
                data_dir=args.data_dir,
                run_date=args.date,
                quote_client=quote_client,
                poll_seconds=args.poll_seconds,
                once=args.once,
            )
        except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"triggers: {result.trigger_count}")
        print(f"skipped: {result.skipped_count}")
        print(f"alerts: {result.alert_count}")
        print(f"alerts_csv: {result.alerts_path}")
        return 0

    if args.command == "watch-decision-plans":
        try:
            plans = load_decision_plans(args.plans)
            if not plans:
                raise ValueError("decision plans 文件没有记录")
            run_date = str(plans[0]["run_date"])
            market = str(plans[0]["market"])
            if market not in {"US", "HK"}:
                raise ValueError("v1 计划 watcher 仅支持美股和港股")
            if any(
                plan.get("run_date") != run_date or plan.get("market") != market
                for plan in plans
            ):
                raise ValueError("decision plans 包含跨日期或跨市场记录")
            result = run_decision_plan_watch(
                plans=plans,
                events_path=args.data_dir / "runs" / run_date / market / "plan_events.jsonl",
                quote_client=FutuQuoteClient(host=args.host, port=args.port),
                notifier=build_notifier(load_env_config(args.config)),
                poll_seconds=args.poll_seconds,
                once=args.once,
            )
        except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
            parser.error(str(exc))
        print(f"plans: {result.watched_plan_count}")
        print(f"triggers: {result.trigger_count}")
        print(f"resets: {result.reset_count}")
        print(f"notifications_sent: {result.notification_sent_count}")
        print(f"notifications_failed: {result.notification_failed_count}")
        print(f"events_jsonl: {result.events_path}")
        return 0

    if args.command == "watch-t":
        try:
            while True:
                result = run_t_signal_watch_once(
                    portfolio_path=args.portfolio,
                    data_dir=args.data_dir,
                    run_date=args.date,
                    market=args.market,
                    session_phase=args.session_phase,
                    market_data_client=FutuTSignalMarketDataClient(
                        host=args.host,
                        port=args.port,
                    ),
                    interpreter=TSignalInterpreter(),
                    notifier=NullNotifier(),
                )
                print(f"run_date: {result.run_date}")
                print(f"market: {result.market}")
                print(f"signals: {result.signal_count}")
                print(f"notified: {result.notified_count}")
                print(f"signals_json: {result.run_path}")
                print(f"latest: {result.latest_path}")
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            return 130
        except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
            parser.error(str(exc))

    if args.command == "check-futu-quotes":
        quote_client = None
        try:
            universe = load_futu_quote_universe(args.portfolio)
            quote_client = FutuQuoteClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            print(f"loaded {len(universe.items)} quoteable position(s)")
            symbols = sorted({item.futu_symbol for item in universe.items})
            snapshots = quote_client.get_snapshots(symbols) if symbols else {}
            quote_count = 0
            missing_count = 0
            for futu_symbol in symbols:
                quote = snapshots.get(futu_symbol)
                if quote is None:
                    missing_count += 1
                    print(f"warning: missing quote for {futu_symbol}")
                    continue
                quote_count += 1
                print(f"quote {futu_symbol} last_price={quote.last_price}")
            for skipped in universe.skipped:
                skipped_symbol = (
                    f"{skipped.market}.{skipped.symbol}"
                    if skipped.market and skipped.symbol
                    else skipped.symbol
                )
                print(
                    f"skipped {skipped_symbol} "
                    f"asset_class={skipped.asset_class} "
                    f"reason={skipped.reason}"
                )
        except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
            parser.error(str(exc))
        finally:
            if quote_client is not None:
                quote_client.close()
        print(f"quotes: {quote_count}")
        print(f"missing: {missing_count}")
        print(f"skipped: {len(universe.skipped)}")
        return 0

    if args.command == "check-futu-account":
        account_client = None
        try:
            account_client = FutuAccountClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            snapshot = account_client.fetch_snapshot()
        except (RuntimeError, FutuAccountError) as exc:
            parser.error(str(exc))
        finally:
            if account_client is not None:
                account_client.close()
        print(f"real_accounts: {len(snapshot.accounts)}")
        print(f"positions: {len(snapshot.position_records)}")
        print(f"cash_records: {len(snapshot.cash_records)}")
        return 0

    if args.command == "sync-futu-portfolio":
        account_client = None
        try:
            account_client = FutuAccountClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            snapshot = account_client.fetch_snapshot()
            result = sync_futu_portfolio(
                snapshot=snapshot,
                portfolio_path=args.portfolio,
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                run_date=args.date,
                update_latest=args.update_latest,
            )
        except (FileNotFoundError, ValueError, RuntimeError, FutuAccountError) as exc:
            parser.error(str(exc))
        finally:
            if account_client is not None:
                account_client.close()
        print(f"run_date: {result.run_date}")
        print(f"real_accounts: {result.account_count}")
        print(f"positions: {result.position_count}")
        print(f"cash: {result.cash_count}")
        print(f"merged_rows: {result.merged_row_count}")
        print(f"snapshot: {result.snapshot_path}")
        print(f"portfolio: {result.portfolio_path}")
        print(f"report: {result.report_path}")
        print(f"latest: {result.latest_path}")
        print(f"updated_latest: {'true' if result.updated_latest else 'false'}")
        return 0

    if args.command == "check-tiger-account":
        account_client = None
        try:
            config = load_tiger_account_config(
                config_dir=args.config_dir,
                account=args.account,
                sandbox=args.sandbox,
            )
            account_client = TigerAccountClient(config=config)
            print(
                "connected to Tiger OpenAPI account "
                f"{mask_account_id(config.account)}"
            )
            snapshot = account_client.fetch_snapshot()
        except (FileNotFoundError, ValueError, RuntimeError, TigerAccountError) as exc:
            parser.error(str(exc))
        finally:
            if account_client is not None:
                account_client.close()
        print(f"accounts: {len(snapshot.accounts)}")
        for account in snapshot.accounts:
            print(
                "account: "
                f"alias={account.account_alias} "
                f"account_type={account.account_type} "
                f"status={account.status} "
                f"asset_method={account.asset_method}"
            )
        print(f"positions: {len(snapshot.position_records)}")
        print(f"cash_records: {len(snapshot.cash_records)}")
        cash_currencies = sorted(
            {
                str(record.get("currency", "")).strip().upper()
                for record in snapshot.cash_records
                if str(record.get("currency", "")).strip()
            }
        )
        if cash_currencies:
            print(f"cash_currencies: {','.join(cash_currencies)}")
        return 0

    if args.command == "sync-tiger-portfolio":
        account_client = None
        try:
            config = load_tiger_account_config(
                config_dir=args.config_dir,
                account=args.account,
                sandbox=args.sandbox,
            )
            account_client = TigerAccountClient(config=config)
            print(
                "connected to Tiger OpenAPI account "
                f"{mask_account_id(config.account)}"
            )
            snapshot = account_client.fetch_snapshot()
            result = sync_tiger_portfolio(
                snapshot=snapshot,
                portfolio_path=args.portfolio,
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                run_date=args.date,
                update_latest=args.update_latest,
            )
        except TigerAccountError as exc:
            if exc.error_type == "blocking_data_error" and exc.sync_result is not None:
                _print_tiger_sync_result(exc.sync_result)
            parser.error(str(exc))
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        finally:
            if account_client is not None:
                account_client.close()
        _print_tiger_sync_result(result)
        return 0

    if args.command == "kelly" and args.kelly_command == "sync-paper-orders":
        client = None
        try:
            if args.fake:
                client = FakeFutuPaperOrderClient(
                    orders=default_fake_kelly_paper_orders(),
                )
            else:
                symbol_index_details = load_kelly_experiment_symbol_index_details(
                    args.data_dir
                )
                order_link_index = load_kelly_order_links(args.data_dir)
                sync_markets = _kelly_sync_trd_markets(
                    args.trd_market,
                    symbol_index_details,
                )
                clients = [
                    FutuSimulatePaperOrderClient(
                        host=args.host,
                        port=args.port,
                        experiment_symbol_index=symbol_index_details.unique,
                        ambiguous_symbol_index=symbol_index_details.ambiguous,
                        order_link_index=order_link_index,
                        trd_market=trd_market,
                    )
                    for trd_market in sync_markets
                ]
                client = (
                    clients[0]
                    if len(clients) == 1
                    else MultiMarketPaperOrderClient(clients)
                )
            payload = sync_kelly_paper_orders(
                data_dir=args.data_dir,
                client=client,
                synced_at=args.synced_at,
            )
            if args.diagnose:
                sync_report = build_kelly_paper_order_sync_report(payload, client)
                sync_report_path = write_kelly_paper_order_sync_report(
                    args.data_dir,
                    sync_report,
                )
        except (FileNotFoundError, ValueError, RuntimeError, FutuPaperOrderSyncError) as exc:
            parser.error(str(exc))
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
        print(f"environment: {payload['environment']}")
        print(f"orders: {len(payload['orders'])}")
        print(f"synced_at: {payload['synced_at']}")
        print(f"latest: {args.data_dir / 'latest' / 'kelly_paper_orders.json'}")
        if args.diagnose:
            counts = sync_report["counts"]
            print(f"matched: {counts['matched']}")
            print(f"skipped_untracked_symbol: {counts['skipped_untracked_symbol']}")
            print(f"skipped_ambiguous_symbol: {counts['skipped_ambiguous_symbol']}")
            print(f"skipped_invalid_code: {counts['skipped_invalid_code']}")
            print(f"sync_report: {sync_report_path}")
        return 0

    if args.command == "kelly" and args.kelly_command == "build-order-intents":
        try:
            payload = build_kelly_order_intents(
                data_dir=args.data_dir,
                created_at=args.created_at,
            )
            latest_path = write_kelly_order_intents(args.data_dir, payload)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"intents: {payload['intent_count']}")
        print(f"latest: {latest_path}")
        return 0

    if args.command == "kelly" and args.kelly_command == "build-strategy-capital":
        try:
            lab_state = load_kelly_lab_state(
                args.data_dir,
                include_strategy_capital=False,
                include_strategy_stats=False,
            )
            if not lab_state.available:
                raise ValueError(lab_state.error)
            latest_dir = args.data_dir / "latest"
            paper_orders_payload = _load_optional_json(
                latest_dir / "kelly_paper_orders.json",
            )
            order_executions_payload = _load_optional_json(
                latest_dir / "kelly_order_executions.json",
            )
            payload = build_kelly_strategy_capital_payload(
                lab_state.experiments,
                paper_orders_payload=paper_orders_payload,
                order_executions_payload=order_executions_payload,
                calculated_at=args.calculated_at,
            )
            latest_path = write_kelly_strategy_capital(args.data_dir, payload)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"strategies: {payload['strategy_count']}")
        print(f"latest: {latest_path}")
        return 0

    if args.command == "kelly" and args.kelly_command == "build-trade-samples":
        try:
            lab_state = load_kelly_lab_state(
                args.data_dir,
                include_strategy_capital=False,
                include_strategy_stats=False,
            )
            if not lab_state.available:
                raise ValueError(lab_state.error)
            latest_dir = args.data_dir / "latest"
            paper_orders_payload = _load_optional_json(
                latest_dir / "kelly_paper_orders.json",
            )
            if paper_orders_payload is None:
                raise FileNotFoundError(latest_dir / "kelly_paper_orders.json")
            payload = build_kelly_trade_samples_payload(
                lab_state.experiments,
                paper_orders_payload,
                generated_at=args.generated_at,
            )
            latest_path = write_kelly_trade_samples(args.data_dir, payload)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"samples: {payload['sample_count']}")
        print(f"open_positions: {payload['open_position_count']}")
        print(f"skipped_orders: {payload['skipped_order_count']}")
        print(f"latest: {latest_path}")
        return 0

    if args.command == "kelly" and args.kelly_command == "build-strategy-stats":
        try:
            lab_state = load_kelly_lab_state(
                args.data_dir,
                include_strategy_capital=False,
                include_strategy_stats=False,
            )
            if not lab_state.available:
                raise ValueError(lab_state.error)
            trade_samples_payload = load_kelly_trade_samples(args.data_dir)
            payload = build_kelly_strategy_stats_payload(
                lab_state.experiments,
                trade_samples_payload,
                generated_at=args.generated_at,
            )
            latest_path = write_kelly_strategy_stats(args.data_dir, payload)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"experiments: {payload['experiment_count']}")
        print(f"latest: {latest_path}")
        return 0

    if args.command == "kelly" and args.kelly_command == "check-order-risk":
        try:
            try:
                strategy_capital_payload = load_kelly_strategy_capital(args.data_dir)
            except FileNotFoundError:
                strategy_capital_payload = None
            risk_kwargs = {
                "data_dir": args.data_dir,
                "checked_at": args.checked_at,
                "max_entry_position_pct": args.max_entry_position_pct,
            }
            if strategy_capital_payload is not None:
                risk_kwargs["strategy_capital_payload"] = strategy_capital_payload
            payload = build_kelly_order_risk_checks(
                **risk_kwargs,
            )
            latest_path = write_kelly_order_risk_checks(args.data_dir, payload)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"intents: {payload['intent_count']}")
        print(f"approved: {payload['approved_count']}")
        print(f"blocked: {payload['blocked_count']}")
        print(f"latest: {latest_path}")
        return 0

    if args.command == "kelly" and args.kelly_command == "execute-orders":
        client = None
        try:
            limit_prices = _parse_key_value_options(
                args.limit_price,
                option_name="--limit-price",
            )
            order_quantities = _parse_key_value_options(
                args.order_qty,
                option_name="--order-qty",
            )
            dry_run = not args.futu_simulate
            if not dry_run:
                if args.trd_market == "auto":
                    client = MarketRoutingOrderExecutionClient(
                        host=args.host,
                        port=args.port,
                        simulate_acc_id=args.simulate_acc_id,
                    )
                else:
                    client = FutuSimulateOrderExecutionClient(
                        host=args.host,
                        port=args.port,
                        simulate_acc_id=args.simulate_acc_id,
                        trd_market=args.trd_market,
                    )
            payload = execute_kelly_orders(
                data_dir=args.data_dir,
                dry_run=dry_run,
                executed_at=args.executed_at,
                limit_prices=limit_prices,
                order_quantities=order_quantities,
                client=client,
            )
            latest_path = write_kelly_order_executions(args.data_dir, payload)
            if not dry_run:
                write_kelly_order_links_from_executions(args.data_dir, payload)
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
            FutuOrderExecutionError,
        ) as exc:
            parser.error(str(exc))
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
        print(f"environment: {payload['environment']}")
        print(f"executions: {payload['execution_count']}")
        print(f"dry_run: {payload['dry_run_count']}")
        print(f"submitted: {payload['submitted_count']}")
        print(f"skipped: {payload['skipped_count']}")
        print(f"failed: {payload['failed_count']}")
        print(f"latest: {latest_path}")
        return 0

    if args.command == "build-trading-plan":
        try:
            result = build_trading_plan(
                advice_path=args.advice,
                data_dir=args.data_dir,
                run_date=args.date,
                update_latest=not args.dry_run,
            )
        except (FileNotFoundError, ValueError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"plans: {result.plan_count}")
        print(f"plan_csv: {result.plan_path}")
        print(f"latest: {result.latest_path}")
        return 0

    if args.command == "translate-agent-reports":
        try:
            result = translate_agent_report_files(
                advice_path=args.advice,
                plan_path=args.plan,
                translator=DeepSeekReportTranslator(model=args.model),
                force=args.force,
            )
        except (FileNotFoundError, ValueError) as exc:
            parser.error(str(exc))
        print(f"advice: {result.advice_path}")
        print(f"plan: {result.plan_path}")
        print(f"translated_fields: {result.translated_fields}")
        return 0

    if args.command == "check-futu-plan":
        quote_client = None
        try:
            plans = [
                plan
                for plan in load_trading_plan_rows(args.plan)
                if plan.status == "active"
            ]
            quote_client = FutuQuoteClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            print(f"loaded {len(plans)} active trading plan(s)")
            symbols = sorted({plan.futu_symbol for plan in plans})
            snapshots = quote_client.get_snapshots(symbols) if symbols else {}
            plans_by_symbol = {plan.futu_symbol: plan for plan in plans}
            for futu_symbol in symbols:
                quote = snapshots.get(futu_symbol)
                if quote is None:
                    print(f"plan {futu_symbol} status=missing_quote message=Futu did not return a quote.")
                    continue
                status = evaluate_plan_quote(plans_by_symbol[futu_symbol], quote.last_price)
                print(
                    f"plan {status.futu_symbol} last_price={status.last_price} "
                    f"status={status.status} message={status.message}"
                )
        except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
            parser.error(str(exc))
        finally:
            if quote_client is not None:
                quote_client.close()
        return 0

    if args.command == "generate-trade-actions":
        quote_client = None
        try:
            plans = _active_trade_action_plans_for_quotes(
                load_trading_plan_rows(args.plan),
                args.date,
            )
            quote_client = FutuQuoteClient(host=args.host, port=args.port)
            print(f"connected to Futu OpenD at {args.host}:{args.port}")
            print(f"loaded {len(plans)} active trading plan(s)")
            symbols = sorted({plan.futu_symbol for plan in plans})
            try:
                snapshots = quote_client.get_snapshots(symbols) if symbols else {}
            except FutuQuoteError as exc:
                print(f"warning: Futu quote snapshot failed: {exc}")
                print(
                    "continuing with missing quotes for "
                    f"{len(plans)} active plan(s)"
                )
                snapshots = {}
            result = generate_trade_actions(
                plan_path=args.plan,
                portfolio_path=args.portfolio,
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                snapshots=snapshots,
                run_date=args.date,
                update_latest=not args.dry_run,
            )
        except (FileNotFoundError, ValueError, FutuQuoteError) as exc:
            parser.error(str(exc))
        finally:
            if quote_client is not None:
                quote_client.close()
        print(f"run_date: {result.run_date}")
        print(f"actions: {result.action_count}")
        print(f"ready: {result.ready_count}")
        print(f"review: {result.review_count}")
        print(f"watch: {result.watch_count}")
        print(f"trade_actions_csv: {result.actions_path}")
        print(f"report: {result.report_path}")
        print(f"latest: {result.latest_path}")
        return 0

    if args.command == "run-backtest":
        try:
            result = run_backtest(
                plan_path=args.plan,
                prices_path=args.prices,
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                run_date=args.date,
                symbol=args.symbol,
                market=args.market,
                initial_cash=args.initial_cash,
                initial_position_quantity=args.initial_position_quantity,
                commission_bps=args.commission_bps,
                slippage_bps=args.slippage_bps,
                adapter=args.adapter,
            )
        except (FileNotFoundError, ValueError) as exc:
            parser.error(str(exc))
        print(f"run_id: {result.run_id}")
        print(f"run_date: {result.run_date}")
        print(f"market: {result.market}")
        print(f"symbol: {result.symbol}")
        print(f"adapter: {result.adapter}")
        print(f"trades: {result.trade_count}")
        print(f"final_equity: {result.final_equity}")
        print(f"total_return_pct: {result.total_return_pct}")
        print(f"max_drawdown_pct: {result.max_drawdown_pct}")
        print(f"metrics: {result.metrics_path}")
        print(f"trades_csv: {result.trades_path}")
        print(f"equity_curve_csv: {result.equity_curve_path}")
        print(f"report: {result.report_path}")
        return 0

    if args.command == "dashboard":
        config = DashboardConfig(
            portfolio_path=args.portfolio,
            data_dir=args.data_dir,
            reports_dir=args.reports_dir,
            poll_seconds=args.poll_seconds,
            futu_host=args.futu_host,
            futu_port=args.futu_port,
        )
        serve_dashboard(config, host=args.host, port=args.port)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
