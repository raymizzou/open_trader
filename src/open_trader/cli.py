from __future__ import annotations

import argparse
import ipaddress
import json
from getpass import getpass
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlparse
from urllib.request import urlopen

from .a_share_trend import (
    _process_version,
    live_trend_strategy_snapshot,
    load_futu_simulate_trend_account,
)
from .backtest import run_backtest
from .account_sync_worker import (
    AccountSyncWorkerConfig,
    default_portfolio_path,
    run_account_sync_worker,
)
from .account_http import (
    AccountHttpError,
    DEFAULT_ACCOUNT_API_URL,
    DEFAULT_ACCOUNT_TIMEOUT_SECONDS,
    fetch_account_snapshot,
)
from .daily_premarket import (
    _optional_positive_tm_id,
    _positive_tm_ids,
    _read_env_file,
    build_notifier,
    load_env_config,
    require_trend_executor,
    require_trend_review_config,
    send_notification_with_results,
)
from .dashboard import DashboardConfig
from .dashboard_web import serve_dashboard
from .decision_facts import LLMDecisionFactsExtractor, generate_decision_facts
from .decision_plan import load_decision_plans
from .decision_plan_watch import run_decision_plan_watch
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
from .futu_watch import run_futu_watch
from .fx import StaticMonthEndFxProvider
from .market_scope import parse_market_scope
from .notifications import NullNotifier
from .parsers.phillips import PhillipsStatementParser
from .parsers.eastmoney import EastmoneyStatementParser
from .pipeline import run_import, validate_month
from .polymarket_trading import (
    KEYCHAIN_ACCOUNTS,
    KeychainError,
    PolymarketTradingClient,
    PolymarketTradingError,
    load_trading_config,
    store_keychain_secret,
    store_predict_api_key,
)
from .polymarket_monitor import monitor_once_diagnostic
from .prediction_arbitrage_store import PredictionArbitrageStore
from .report_translation import DeepSeekReportTranslator, translate_agent_report_files
from .tiger_account import load_tiger_account_config
from .technical_facts import LLMTechnicalFactsExtractor, generate_technical_facts
from .trend_api_stats import (
    FutuSimulateFillClient,
    TigerActualFillClient,
    run_trend_statistics_cycle,
)
from .tradingagents_summary import (
    LLMTradingAgentsSummaryExtractor,
    generate_tradingagents_summary,
)
from .trading_plan import (
    build_trading_plan,
    evaluate_plan_quote,
    load_trading_plan_rows,
)
from .watchlist import build_watchlist
from .trend_review import (
    refresh_long_term_benchmark,
    rebuild_trend_report_from_evidence,
    replay_trend_evidence,
    resolve_trend_action,
)
from .trend_market_controller import (
    load_trend_market_status,
    run_trend_market_controller,
)
from .trend_allocation import (
    allocation_reference_for_report,
    load_allocation_reference,
    load_trend_allocation_status,
    run_trend_allocation_controller,
)
from .strategy_drawdown import manual_unlock_strategy_drawdown
from .drawdown_preflight import (
    DrawdownMarketInput,
    market_preflight_dates,
    run_drawdown_preflight,
)


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _drawdown_unlock_now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def _drawdown_preflight_now() -> datetime:
    return datetime.now().astimezone()


class _LazyFutuQuote:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.client: FutuQuoteClient | None = None

    def get_daily_kline(self, *args: object, **kwargs: object) -> object:
        if self.client is None:
            self.client = FutuQuoteClient(host=self.host, port=self.port)
        return self.client.get_daily_kline(*args, **kwargs)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def run_trend_review_replay(
    config: object, evidence_path: Path
) -> dict[str, object]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise ValueError("trend review evidence must be an object")
    path = replay_trend_evidence(
        evidence_path,
        config.data_dir,
        fixed_process_version=_process_version(config.repo),
        rebuild=rebuild_trend_report_from_evidence,
    )
    return {
        "status": "corrected",
        "market": str(evidence.get("market") or ""),
        "date": str(evidence.get("report_id") or ""),
        "artifact_path": str(path),
    }


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

    trend_market = subparsers.add_parser(
        "trend-market", help="Run and inspect one trend market controller"
    )
    trend_market_commands = trend_market.add_subparsers(
        dest="trend_market_command", required=True
    )
    trend_market_run = trend_market_commands.add_parser("run")
    trend_market_run.add_argument(
        "--market", choices=("CN", "HK", "US"), required=True
    )
    trend_market_run.add_argument("--revision", action="store_true")
    trend_market_run.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    trend_market_status = trend_market_commands.add_parser("status")
    trend_market_status.add_argument(
        "--market", choices=("CN", "HK", "US"), required=True
    )
    trend_market_status.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    trend_market_resolve = trend_market_commands.add_parser("resolve")
    trend_market_resolve.add_argument(
        "--market", choices=("CN", "HK", "US"), required=True
    )
    trend_market_resolve.add_argument(
        "--execution-date", type=canonical_date, required=True
    )
    trend_market_resolve.add_argument("--symbol", required=True)
    trend_market_resolve.add_argument("--side", choices=("buy", "sell"), required=True)
    trend_market_resolve.add_argument(
        "--resolution",
        choices=("confirm-submitted", "authorize-retry", "abandon"),
        required=True,
    )
    trend_market_resolve.add_argument("--actor", required=True)
    trend_market_resolve.add_argument("--reason", required=True)
    trend_market_resolve.add_argument("--futu-order-id")
    trend_market_resolve.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )

    trend_allocation = subparsers.add_parser(
        "trend-allocation", help="Run and inspect the shared Trend Animals allocation"
    )
    trend_allocation_commands = trend_allocation.add_subparsers(
        dest="trend_allocation_command", required=True
    )
    trend_allocation_run = trend_allocation_commands.add_parser("run")
    trend_allocation_run.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    trend_allocation_once = trend_allocation_commands.add_parser("once")
    trend_allocation_once.add_argument("--date", dest="allocation_date", type=canonical_date, required=True)
    trend_allocation_once.add_argument("--revision", action="store_true")
    trend_allocation_once.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    trend_allocation_status = trend_allocation_commands.add_parser("status")
    trend_allocation_status.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )

    drawdown_unlock = subparsers.add_parser(
        "trend-drawdown-unlock",
        help="Manually unlock and rebase one simulated trend strategy",
    )
    drawdown_unlock.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    drawdown_unlock.add_argument(
        "--market", choices=("CN", "US", "HK"), required=True
    )
    drawdown_unlock.add_argument("--event-id", required=True)
    drawdown_unlock.add_argument("--actor", required=True)

    drawdown_preflight = subparsers.add_parser(
        "trend-drawdown-preflight",
        help="Initialize or recover audited trend drawdown baselines",
    )
    drawdown_preflight.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    drawdown_preflight.add_argument("--repo", type=Path, default=Path.cwd())
    drawdown_preflight.add_argument("--actor", default="deployment")

    trend_review_parser = subparsers.add_parser(
        "trend-review", help="Run or replay one market trend review workflow"
    )
    trend_review_commands = trend_review_parser.add_subparsers(
        dest="trend_review_command", required=True
    )
    replay_parser = trend_review_commands.add_parser("replay")
    replay_parser.add_argument("--evidence", type=Path, required=True)
    replay_parser.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    sync_stats_parser = trend_review_commands.add_parser("sync-stats")
    sync_stats_parser.add_argument("--market", choices=("CN", "HK", "US"), required=True)
    sync_stats_parser.add_argument("--as-of-date", type=canonical_date, required=True)
    sync_stats_parser.add_argument("--force", action="store_true")
    sync_stats_parser.add_argument("--actor", default="")
    sync_stats_parser.add_argument("--reason", default="")
    sync_stats_parser.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    sync_stats_parser.add_argument(
        "--tiger-config-dir", type=Path, default=Path("~/.tigeropen/")
    )
    sync_stats_parser.add_argument("--tiger-account")
    refresh_benchmark_parser = trend_review_commands.add_parser("refresh-benchmark")
    refresh_benchmark_parser.add_argument(
        "--market", choices=("CN", "HK", "US"), required=True
    )
    refresh_benchmark_parser.add_argument("--force", action="store_true")
    refresh_benchmark_parser.add_argument("--actor", default="")
    refresh_benchmark_parser.add_argument("--reason", default="")
    refresh_benchmark_parser.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )

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

    account_sync_worker = subparsers.add_parser(
        "account-sync-worker", help="Run the sole account and quote publisher"
    )
    account_sync_worker.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    account_sync_worker.add_argument("--data-dir", type=Path, default=Path("data"))
    account_sync_worker.add_argument("--reports-dir", type=Path, default=Path("reports"))
    account_sync_worker.add_argument("--portfolio", type=Path)
    account_sync_worker.add_argument(
        "--tiger-config-dir", type=Path, default=Path("~/.tigeropen/")
    )
    account_sync_worker.add_argument(
        "--account-interval-seconds", type=positive_float, default=60.0
    )
    account_sync_worker.add_argument(
        "--quote-interval-seconds", type=positive_float, default=5.0
    )
    account_sync_worker.add_argument("--once", action="store_true")

    account_sync_status = subparsers.add_parser(
        "account-sync-status", help="Show Account snapshot health"
    )
    account_sync_status.add_argument("--account-url", default=DEFAULT_ACCOUNT_API_URL)
    account_sync_status.add_argument("--json", action="store_true")

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
    )
    dashboard_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    dashboard_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    dashboard_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
    )
    dashboard_parser.add_argument(
        "--poll-seconds",
        type=positive_float,
        default=5.0,
    )
    dashboard_parser.add_argument("--futu-host", default="127.0.0.1")
    dashboard_parser.add_argument("--futu-port", type=positive_int, default=11111)
    dashboard_parser.add_argument(
        "--public-url",
        default="",
        help="Public dashboard URL used in generated links",
    )
    prediction_parser = subparsers.add_parser(
        "prediction-arb",
        help="Run the guarded prediction-market wallet diagnostics",
    )
    prediction_commands = prediction_parser.add_subparsers(
        dest="prediction_command", required=True
    )
    wallet_parser = prediction_commands.add_parser("wallet", help="Manage wallet setup")
    wallet_commands = wallet_parser.add_subparsers(
        dest="wallet_command", required=True
    )
    wallet_setup = wallet_commands.add_parser(
        "setup", help="Store wallet addresses and hidden credentials"
    )
    wallet_setup.add_argument(
        "--config", type=Path, default=Path("config/prediction_arbitrage.json")
    )
    wallet_setup.add_argument("--signer-address", required=True)
    wallet_setup.add_argument("--wallet-address", required=True)
    wallet_status = wallet_commands.add_parser(
        "status", help="Show safe wallet readiness facts"
    )
    wallet_status.add_argument(
        "--config", type=Path, default=Path("config/prediction_arbitrage.json")
    )
    predict_parser = prediction_commands.add_parser(
        "predict", help="Manage the read-only Predict source"
    )
    predict_commands = predict_parser.add_subparsers(
        dest="predict_command", required=True
    )
    predict_setup = predict_commands.add_parser(
        "setup", help="Store the public Predict wallet and hidden API key"
    )
    predict_setup.add_argument(
        "--config", type=Path, default=Path("config/prediction_arbitrage.json")
    )
    predict_setup.add_argument("--wallet-address", required=True)

    prediction_preflight = prediction_commands.add_parser(
        "preflight", help="Run the no-submit compatibility diagnostic"
    )
    prediction_preflight.add_argument(
        "--config", type=Path, default=Path("config/prediction_arbitrage.json")
    )
    prediction_preflight.add_argument(
        "--no-submit",
        action="store_true",
        help="Required: sign the in-memory probe without posting it",
    )

    prediction_monitor_once = prediction_commands.add_parser(
        "monitor-once", help="Run one non-mutating public monitor diagnostic"
    )
    prediction_monitor_once.add_argument(
        "--config", type=Path, default=Path("config/prediction_arbitrage.json")
    )
    prediction_monitor_once.add_argument("--data-dir", type=Path, default=Path("data"))
    prediction_monitor_once.add_argument("--timeout", type=positive_float, default=30.0)
    prediction_status = prediction_commands.add_parser(
        "status", help="Show safe local Dashboard prediction runtime facts"
    )
    prediction_status.add_argument("--url", default="http://127.0.0.1:8766")
    prediction_status.add_argument("--timeout", type=positive_float, default=5.0)

    cross_auto_parser = prediction_commands.add_parser(
        "cross-auto", help="Inspect or locally arm cross-venue automatic execution"
    )
    cross_auto_commands = cross_auto_parser.add_subparsers(
        dest="cross_auto_command", required=True
    )
    cross_auto_status = cross_auto_commands.add_parser("status", help="Show local arm state")
    cross_auto_status.add_argument("--data-dir", type=Path, default=Path("data"))
    cross_auto_mode = cross_auto_commands.add_parser(
        "mode", help="Set the durable cross-venue execution mode locally"
    )
    cross_auto_mode.add_argument(
        "mode", choices=("observe_only", "manual_confirm", "auto_submit")
    )
    cross_auto_mode.add_argument("--data-dir", type=Path, default=Path("data"))
    cross_auto_arm = cross_auto_commands.add_parser(
        "arm", help="Arm only after local Dashboard readiness checks"
    )
    cross_auto_arm.add_argument("--data-dir", type=Path, default=Path("data"))
    cross_auto_arm.add_argument("--url", required=True)
    cross_auto_arm.add_argument("--expected-sha", required=True)

    health_parser = prediction_commands.add_parser(
        "health-check", help="Run or serve the prediction-arbitrage health check"
    )
    health_parser.add_argument("--url", default="http://127.0.0.1:8766")
    health_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    health_parser.add_argument(
        "--config", type=Path, default=Path("config/daily_premarket.env")
    )
    health_parser.add_argument("--repo", type=Path, default=Path.cwd())
    health_parser.add_argument(
        "--interval", type=positive_float, default=7200.0
    )
    health_parser.add_argument("--once", action="store_true")
    health_parser.add_argument("--no-notify", action="store_true")
    health_parser.add_argument("--json", action="store_true")

    return parser


def _account_status_projection(snapshot: dict[str, object]) -> dict[str, object]:
    sources = snapshot["sources"]
    assert isinstance(sources, dict)
    account = sources["account"]
    quotes = sources["quotes"]
    assert isinstance(account, dict) and isinstance(quotes, dict)
    brokers = account["brokers"]
    assert isinstance(brokers, dict)
    return {
        "status": snapshot["status"],
        "reason": account["reason"],
        "snapshot_generation": snapshot["snapshot_generation"],
        "account_generation": snapshot["account_generation"],
        "quotes": {"status": quotes["status"]},
        "brokers": {
            broker: {"status": source["status"]}
            for broker, source in brokers.items()
            if isinstance(source, dict)
        },
    }


def main(argv: list[str] | None = None) -> int:
    selected_argv = sys.argv[1:] if argv is None else argv
    if selected_argv[:1] == ["frontend-gateway"]:
        from .frontend_gateway import main as frontend_gateway_main

        return frontend_gateway_main(selected_argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "account-sync-worker":
        try:
            values = _read_env_file(args.config)
            config = AccountSyncWorkerConfig(
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                portfolio_path=args.portfolio or default_portfolio_path(args.data_dir),
                futu_host=values.get("OPEN_TRADER_FUTU_HOST", "127.0.0.1"),
                futu_port=positive_int(values.get("OPEN_TRADER_FUTU_PORT", "11111")),
                tiger_config_dir=args.tiger_config_dir.expanduser(),
                tiger_account=None,
                account_interval_seconds=args.account_interval_seconds,
                quote_interval_seconds=args.quote_interval_seconds,
            )
        except (OSError, ValueError, argparse.ArgumentTypeError) as exc:
            parser.error(str(exc))
        return run_account_sync_worker(config, once=args.once)

    if args.command == "account-sync-status":
        try:
            health = _account_status_projection(
                fetch_account_snapshot(args.account_url, DEFAULT_ACCOUNT_TIMEOUT_SECONDS)
            )
        except AccountHttpError as error:
            print(error.code, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(health, ensure_ascii=False))
        else:
            print(f"status: {health['status']}")
            print(f"reason: {health['reason'] or ''}")
            print(f"snapshot_generation: {health['snapshot_generation']}")
            print(f"account_generation: {health['account_generation']}")
            print(f"quotes: {health['quotes']['status']}")
            for broker, source in health["brokers"].items():
                print(f"{broker}: {source['status']}")
        return 0

    if args.command == "prediction-arb":
        if args.prediction_command == "cross-auto":
            store = PredictionArbitrageStore(args.data_dir.expanduser())
            if args.cross_auto_command == "status":
                state = store.cross_auto_state()
                latest = store.cross_auto_attempts(limit=1)
                configured_mode = str(state.get("configured_mode", "observe_only"))
                armed = state.get("armed") is True
                effective_mode = (
                    "observe_only"
                    if configured_mode == "auto_submit" and not armed
                    else configured_mode
                )
                print(f"configured_mode: {configured_mode}")
                print(f"effective_mode: {effective_mode}")
                print(f"armed: {armed}")
                print(f"pause_reason: {state.get('reason', 'not_armed')}")
                print(f"daily_principal: {format(store.cross_auto_daily_principal(), 'f')}/100")
                if latest:
                    print(f"latest_attempt: {latest[0].get('decision', 'unknown')}")
                    print(f"latest_reason: {latest[0].get('reason_code', '')}")
                print("result: PASS")
                return 0

            if args.cross_auto_command == "mode":
                state = store.set_cross_auto_mode(args.mode, "operator_configured")
                print(f"configured_mode: {state['configured_mode']}")
                print(f"armed: {state['armed'] is True}")
                print("result: PASS")
                return 0

            parsed_url = urlparse(args.url)
            host = parsed_url.hostname or ""
            try:
                loopback_url = parsed_url.scheme == "http" and ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback_url = host == "localhost"
            if not loopback_url:
                print(f"armed: {store.cross_auto_state().get('armed') is True}")
                print("reason: url_not_loopback")
                print("result: BLOCKED")
                return 2

            def fetch_json(path: str) -> dict[str, object] | None:
                try:
                    with urlopen(args.url.rstrip("/") + path, timeout=10) as response:
                        if getattr(response, "status", 200) != 200:
                            return None
                        payload = json.loads(response.read().decode("utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    return None
                return payload if isinstance(payload, dict) else None

            health = fetch_json("/healthz")
            reason = ""
            if health is None:
                reason = "healthz_unavailable"
            elif health.get("git_sha") != args.expected_sha:
                reason = "git_sha_mismatch"
            elif health.get("source_state") != "clean":
                reason = "source_dirty"
            state = fetch_json("/api/prediction-arbitrage/state") if not reason else None
            if not reason and state is None:
                reason = "prediction_state_unavailable"
            if not reason:
                assert state is not None
                cross = state.get("cross_venue")
                cross = cross if isinstance(cross, dict) else {}
                if cross.get("status") != "ready":
                    reason = "cross_venue_not_ready"
                elif isinstance(cross.get("breaker"), dict) and cross["breaker"].get("open"):
                    reason = "cross_breaker_open"
                venues = state.get("venues")
                venue_rows = venues if isinstance(venues, list) else []
                for venue_name, reason_prefix in (
                    ("polymarket", "polymarket"),
                    ("predict.fun", "predict_fun"),
                ):
                    venue = next(
                        (
                            row
                            for row in venue_rows
                            if isinstance(row, dict) and row.get("venue") == venue_name
                        ),
                        {},
                    )
                    if not reason and venue.get("rest") != "ready":
                        reason = f"{reason_prefix}_rest_not_ready"
                    if not reason and venue.get("ws") != "ready":
                        reason = f"{reason_prefix}_ws_not_ready"
                breaker = state.get("breaker")
                if not reason and isinstance(breaker, dict) and breaker.get("open"):
                    reason = "breaker_open"
                active = state.get("current_execution")
                if not reason and isinstance(active, dict) and active:
                    reason = "active_execution"
                cross_auto = state.get("cross_auto")
                cross_auto = cross_auto if isinstance(cross_auto, dict) else {}
                if not reason and cross_auto.get("configured_mode") != "auto_submit":
                    reason = "configured_mode_not_auto_submit"
                if not reason and cross_auto.get("notification_ready") is not True:
                    reason = "notification_config_unavailable"
            if reason:
                print(f"armed: {store.cross_auto_state().get('armed') is True}")
                print(f"reason: {reason}")
                print("result: BLOCKED")
                return 2
            armed = store.arm_cross_auto()
            print(f"armed: {armed.get('armed') is True}")
            print(f"git_sha: {args.expected_sha}")
            print("result: PASS")
            return 0

        if args.prediction_command == "predict" and args.predict_command == "setup":
            config_path = args.config.expanduser()
            try:
                config = load_trading_config(config_path)
                config_path.write_text(
                    json.dumps(
                        {
                            "signer_address": config.signer_address,
                            "wallet_address": config.wallet_address,
                            "predict": {
                                "wallet_address": args.wallet_address,
                                "environment": "mainnet",
                            },
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(config_path, 0o600)
                load_trading_config(config_path)
                store_predict_api_key(getpass("Predict API key: "))
            except (KeyboardInterrupt, EOFError):
                print("predict setup cancelled", file=sys.stderr)
                return 2
            except (OSError, ValueError, KeychainError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"config: {config_path}")
            print("predict_keychain: configured")
            return 0

        if args.prediction_command == "wallet" and args.wallet_command == "setup":
            config_path = args.config.expanduser()
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    json.dumps(
                        {
                            "signer_address": args.signer_address,
                            "wallet_address": args.wallet_address,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(config_path, 0o600)
                config = load_trading_config(config_path)
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            for account in KEYCHAIN_ACCOUNTS:
                try:
                    secret = getpass(f"{account}: ")
                    store_keychain_secret(account, secret)
                except (KeyboardInterrupt, EOFError):
                    print("wallet setup cancelled", file=sys.stderr)
                    return 2
                except (ValueError, KeychainError) as exc:
                    print(str(exc), file=sys.stderr)
                    return 2
            del config
            print(f"config: {config_path}")
            print("keychain: configured")
            return 0

        if args.prediction_command == "wallet" and args.wallet_command == "status":
            try:
                config = load_trading_config(args.config.expanduser())
                client = PolymarketTradingClient.from_keychain(config)
                snapshot = client.account_snapshot()
                geoblock = client.geoblock_allowed()
                print(f"wallet: {config.wallet_address[:6]}...{config.wallet_address[-4:]}")
                print(f"geoblock: {'allowed' if geoblock else 'blocked'}")
                print(f"account_reads: pass ({len(snapshot.positions)} positions)")
                print(f"result: {'PASS' if geoblock else 'BLOCKED'}")
                return 0 if geoblock else 2
            except (FileNotFoundError, ValueError, KeychainError, PolymarketTradingError) as exc:
                code = getattr(exc, "error_code", "unavailable")
                print(f"result: BLOCKED\nerror_code: {code}", file=sys.stderr)
                return 2

        if args.prediction_command == "preflight":
            if not args.no_submit:
                print("preflight requires --no-submit", file=sys.stderr)
                return 2
            try:
                config = load_trading_config(args.config.expanduser())
                client = PolymarketTradingClient.from_keychain(config)
            except (FileNotFoundError, ValueError, KeychainError, PolymarketTradingError) as exc:
                code = getattr(exc, "error_code", "unavailable")
                print(f"result: BLOCKED\nerror_code: {code}")
                return 2
            report = client.preflight_report()
            for key in (
                "sdk_version",
                "signer_match",
                "wallet_match",
                "geoblock",
                "account_reads",
                "fok_pair_signed_not_submitted",
                "equal_requested_shares",
                "merge_capability",
                "relayer_readiness",
                "secret_scan",
                "result",
            ):
                print(f"{key}: {report.get(key, 'BLOCKED')}")
            return 0 if report.get("result") == "PASS" else 2

        if args.prediction_command == "monitor-once":
            try:
                config = load_trading_config(args.config.expanduser())
                # Constructing the authenticated adapter verifies the configured
                # Keychain boundary; the diagnostic itself only uses its public
                # client and never calls a mutating adapter method.
                PolymarketTradingClient.from_keychain(config)
                report = monitor_once_diagnostic(timeout=args.timeout)
            except (FileNotFoundError, ValueError, KeychainError, PolymarketTradingError) as exc:
                code = getattr(exc, "error_code", "unavailable")
                print(f"event_count: BLOCKED\nvolumes: BLOCKED\nwebsocket_heartbeat: BLOCKED\npaired_book_read: BLOCKED\nmutations: 0\nresult: BLOCKED\nerror_code: {code}")
                return 2
            for key in (
                "event_count",
                "volumes",
                "websocket_heartbeat",
                "paired_book_read",
                "mutations",
                "result",
            ):
                print(f"{key}: {report.get(key, 'BLOCKED')}")
            return 0 if report.get("result") == "PASS" else 2

        if args.prediction_command == "status":
            endpoint = args.url.rstrip("/") + "/api/prediction-arbitrage/state"
            try:
                with urlopen(endpoint, timeout=args.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Dashboard state must be an object")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print("health: BLOCKED")
                print("pid: unknown")
                print(f"result: BLOCKED\nerror: {exc}")
                return 2
            status = str(payload.get("status") or "unavailable")
            readiness = payload.get("readiness")
            readiness = readiness if isinstance(readiness, dict) else {}
            breaker = payload.get("breaker")
            breaker = breaker if isinstance(breaker, dict) else {}
            current_execution = payload.get("current_execution")
            current_execution = current_execution if isinstance(current_execution, dict) else {}
            active_states = {"running", "executing", "pending", "submitted", "reconciling", "validating", "final_validating", "submitting", "merging"}
            execution_state = str(current_execution.get("status") or current_execution.get("state") or "").lower()
            active = any(value in execution_state for value in active_states)
            actionable = 0
            opportunities = payload.get("opportunities")
            if isinstance(opportunities, list) and not payload.get("stale") and not breaker.get("open") and not active:
                actionable = sum(1 for item in opportunities if isinstance(item, dict) and item.get("actionable") is True)
            port = urlparse(args.url).port or 8766
            pid = "unknown"
            try:
                process_rows = subprocess.run(
                    ["ps", "-axo", "pid=,command="],
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout.splitlines()
                for row in process_rows:
                    candidate, _, command = row.strip().partition(" ")
                    if candidate.isdigit() and "open_trader" in command and " dashboard" in command and f"--port {port}" in command:
                        pid = candidate
                        break
            except OSError:
                pass
            health = "degraded" if status in {"degraded", "unavailable", "error"} else status
            print(f"health: {health}")
            print(f"pid: {pid}")
            print(f"heartbeat_at: {payload.get('heartbeat_at') or payload.get('heartbeat') or 'unknown'}")
            print(f"universe_refreshed_at: {payload.get('universe_refreshed_at') or 'unknown'}")
            print(f"websocket: {'degraded' if payload.get('stale') else 'normal'}")
            print(f"event_count: {payload.get('event_count', len(payload.get('events', [])) if isinstance(payload.get('events'), list) else 0)}")
            print(f"market_count: {payload.get('market_count', 'unknown')}")
            print(f"actionable_count: {actionable}")
            print(f"breaker: {'open' if breaker.get('open') else 'ready'}")
            print(f"masked_wallet: {payload.get('masked_wallet') or readiness.get('masked_address') or 'unknown'}")
            print(f"result: {'PASS' if status not in {'unavailable', 'error'} else 'BLOCKED'}")
            return 0 if status not in {"unavailable", "error"} else 2

        if args.prediction_command == "health-check":
            from .prediction_arbitrage_health import main as health_main

            health_argv = [
                "--url",
                args.url,
                "--data-dir",
                str(args.data_dir),
                "--config",
                str(args.config),
                "--repo",
                str(args.repo),
                "--interval",
                str(args.interval),
            ]
            if args.once:
                health_argv.append("--once")
            if args.no_notify:
                health_argv.append("--no-notify")
            if args.json:
                health_argv.append("--json")
            return health_main(health_argv)

    if args.command == "trend-allocation":
        try:
            config = load_env_config(args.config, dry_run=False)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.trend_allocation_command in {"run", "once"}:
            try:
                require_trend_executor(config, hostname_fn=socket.gethostname)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        try:
            if args.trend_allocation_command == "status":
                result = load_trend_allocation_status(config)
            else:
                runtime = replace(
                    config,
                    repo=Path.cwd().resolve(),
                    python=Path(sys.executable).resolve(),
                )
                result = run_trend_allocation_controller(
                    runtime,
                    once=args.trend_allocation_command == "once",
                    allocation_date=(
                        args.allocation_date
                        if args.trend_allocation_command == "once"
                        else None
                    ),
                    revision=(args.revision if args.trend_allocation_command == "once" else False),
                )
        except (FileNotFoundError, ValueError, RuntimeError, ZoneInfoNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "trend-market":
        if args.trend_market_command == "resolve":
            order_id = str(args.futu_order_id or "").strip()
            if args.resolution == "confirm-submitted" and not order_id:
                print("confirm-submitted requires --futu-order-id", file=sys.stderr)
                return 2
            if args.resolution != "confirm-submitted" and order_id:
                print(
                    f"{args.resolution} does not accept --futu-order-id",
                    file=sys.stderr,
                )
                return 2
        try:
            config = load_env_config(args.config, dry_run=False)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.trend_market_command in {"run", "resolve"}:
            try:
                require_trend_executor(config, hostname_fn=socket.gethostname)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        try:
            if args.trend_market_command == "run":
                config = replace(
                    config,
                    repo=Path.cwd().resolve(),
                    python=Path(sys.executable).resolve(),
                )
                result = run_trend_market_controller(
                    config, args.market, revision=args.revision
                )
            elif args.trend_market_command == "status":
                result = load_trend_market_status(config, args.market)
            else:
                artifact = resolve_trend_action(
                    config.data_dir,
                    market=args.market,
                    execution_date=args.execution_date,
                    symbol=args.symbol,
                    side=args.side,
                    resolution=args.resolution,
                    actor=args.actor,
                    reason=args.reason,
                    resolved_at=datetime.now(ZoneInfo(config.timezone)).isoformat(
                        timespec="seconds"
                    ),
                    futu_order_id=order_id or None,
                )
                result = {
                    "status": "resolved",
                    "market": args.market,
                    "execution_date": args.execution_date,
                    "symbol": args.symbol,
                    "side": args.side,
                    "resolution": args.resolution,
                    "artifact_path": str(artifact),
                }
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
            ZoneInfoNotFoundError,
        ) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "trend-drawdown-preflight":
        quote = None
        try:
            config = load_env_config(args.config, dry_run=False)
            accepted_git_sha = _process_version(args.repo)
            now = _drawdown_preflight_now()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("drawdown preflight clock must be timezone-aware")
            occurred_at = now.isoformat(timespec="seconds")
            quote = FutuQuoteClient(host=config.futu_host, port=config.futu_port)
            allocation = None
            allocation_calendar_error: str | None = None
            allocation_date = now.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).date().isoformat()
            allocation_status_path = (
                config.data_dir / "trend_allocation/controller_status.json"
            )
            if allocation_status_path.is_file():
                allocation_status = load_trend_allocation_status(config, now=now)
                allocation_day = date.fromisoformat(allocation_date)
                try:
                    allocation_days = quote.get_trading_days(
                        market="CN",
                        start=(allocation_day - timedelta(days=35)).isoformat(),
                        end=(allocation_day + timedelta(days=1)).isoformat(),
                    )
                except (FutuQuoteError, OSError) as exc:
                    allocation_calendar_error = str(exc) or exc.__class__.__name__
                else:
                    if (
                        allocation_status.get("attempted_for") == allocation_date
                        and allocation_status.get("phase")
                        in {"ready", "fallback", "holiday"}
                    ):
                        allocation = allocation_reference_for_report(
                            config,
                            allocation_date=allocation_date,
                            a_trading_days=allocation_days,
                        )
                    else:
                        allocation = load_allocation_reference(
                            config.data_dir,
                            allocation_date=allocation_date,
                            a_trading_days=allocation_days,
                            status_failure_reason=None,
                        )
                        if allocation is not None and allocation.get(
                            "stale_a_trading_days"
                        ) != 0:
                            allocation = None
            allocation_kwargs = (
                {"allocation": allocation} if allocation is not None else {}
            )
            inputs: dict[str, DrawdownMarketInput] = {}
            for market in ("CN", "HK", "US"):
                pool_ids = {
                    "CN": (
                        config.trend_animals_a_share_tm_id,
                        config.trend_animals_etf_tm_id,
                    ),
                    "HK": config.trend_animals_hk_tm_ids,
                    "US": config.trend_animals_us_tm_ids,
                }[market]
                calendar_error = allocation_calendar_error
                source_date = None
                entry_eligible_from = None
                if calendar_error is None:
                    today = now.date()
                    try:
                        trading_days = quote.get_trading_days(
                            market=market,
                            start=(today - timedelta(days=14)).isoformat(),
                            end=(today + timedelta(days=21)).isoformat(),
                        )
                    except (FutuQuoteError, OSError) as exc:
                        calendar_error = str(exc) or exc.__class__.__name__
                    else:
                        try:
                            source_date, entry_eligible_from = market_preflight_dates(
                                market, now=now, trading_days=trading_days
                            )
                        except ValueError as exc:
                            calendar_error = str(exc) or exc.__class__.__name__
                strategy = live_trend_strategy_snapshot(
                    market,
                    accepted_git_sha,
                    pool_ids,
                    execution_date=(
                        entry_eligible_from or now.date().isoformat()
                    ),
                    **allocation_kwargs,
                )
                if calendar_error is None:
                    inputs[market] = DrawdownMarketInput(
                        market=market,
                        strategy_snapshot=strategy,
                        baseline_equity=None,
                        source_date=source_date,
                        entry_eligible_from=entry_eligible_from,
                    )
                else:
                    inputs[market] = DrawdownMarketInput(
                        market=market,
                        strategy_snapshot=strategy,
                        baseline_equity=None,
                        source_date=None,
                        entry_eligible_from=None,
                        error=calendar_error,
                    )
            result = run_drawdown_preflight(
                data_dir=config.data_dir,
                reports_dir=config.reports_dir,
                market_inputs=inputs,
                accepted_git_sha=accepted_git_sha,
                actor=args.actor,
                occurred_at=occurred_at,
                notifier=(
                    NullNotifier()
                    if args.actor == "acceptance"
                    else build_notifier(config)
                ),
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            if quote is not None:
                quote.close()
        print(json.dumps(result, ensure_ascii=False))
        return {"ready": 0, "failed": 1, "unavailable": 2}[str(result["status"])]

    if args.command == "trend-drawdown-unlock":
        try:
            config = load_env_config(args.config, dry_run=False)
            configured_timezone = ZoneInfo(config.timezone)
            now = _drawdown_unlock_now(config.timezone)
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("drawdown unlock clock must be timezone-aware")
            now = now.astimezone(configured_timezone)
            occurred_at = now.isoformat(timespec="seconds")
            expected_date = now.date().isoformat()
            account = load_futu_simulate_trend_account(
                host=config.futu_host,
                port=config.futu_port,
                simulate_acc_id=require_trend_review_config(config, args.market),
                market=args.market,
                expected_date=expected_date,
            )
            pool_ids = {
                "CN": (
                    config.trend_animals_a_share_tm_id,
                    config.trend_animals_etf_tm_id,
                ),
                "US": config.trend_animals_us_tm_ids,
                "HK": config.trend_animals_hk_tm_ids,
            }[args.market]
            strategy = live_trend_strategy_snapshot(
                args.market,
                _process_version(config.repo),
                pool_ids,
                execution_date=expected_date,
            )
            result = manual_unlock_strategy_drawdown(
                config.data_dir,
                market=args.market,
                strategy_id=strategy["strategy_id"],
                strategy_version=strategy["strategy_version"],
                current_equity=account.net_value,
                occurred_at=occurred_at,
                event_id=args.event_id,
                actor=args.actor,
            )
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
            ZoneInfoNotFoundError,
        ) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "trend-review":
        if args.trend_review_command == "refresh-benchmark":
            if args.force and (not args.actor.strip() or not args.reason.strip()):
                print("--force requires --actor and --reason", file=sys.stderr)
                return 1
            quote_client = None
            try:
                config = load_env_config(args.config, dry_run=False)
                quote_client = _LazyFutuQuote(config.futu_host, config.futu_port)
                result = refresh_long_term_benchmark(
                    data_dir=config.data_dir,
                    market=args.market,
                    quote=quote_client,
                    now=datetime.now().astimezone(),
                    process_git_sha=_process_version(config.repo),
                    force=args.force,
                    actor=args.actor,
                    reason=args.reason,
                )
            except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            finally:
                if quote_client is not None:
                    quote_client.close()
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["status"] == "failed" else 0
        stats_clients: list[object] = []
        try:
            config = load_env_config(args.config, dry_run=False)
            if args.trend_review_command == "replay":
                result = run_trend_review_replay(config, args.evidence)
            else:
                futu_client = FutuSimulateFillClient(
                    host=config.futu_host,
                    port=config.futu_port,
                    simulate_acc_id=require_trend_review_config(config, args.market),
                    trd_market=args.market,
                )
                stats_clients.append(futu_client)
                tiger_client = None
                if args.market == "US":
                    tiger_config = load_tiger_account_config(
                        config_dir=args.tiger_config_dir,
                        account=args.tiger_account,
                        sandbox=False,
                    )
                    tiger_client = TigerActualFillClient(config=tiger_config)
                    stats_clients.append(tiger_client)
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                result = run_trend_statistics_cycle(
                    data_dir=config.data_dir,
                    reports_dir=config.reports_dir,
                    market=args.market,
                    as_of_date=args.as_of_date,
                    generated_at=timestamp,
                    process_git_sha=_process_version(config.repo),
                    futu_client=futu_client,
                    tiger_client=tiger_client,
                    force=args.force,
                    actor=args.actor,
                    reason=args.reason,
                )
        except (FileNotFoundError, ValueError, RuntimeError, FutuQuoteError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            for client in stats_clients:
                client.close()
        if args.trend_review_command == "sync-stats":
            print(f"status: {result['status']}")
            print(f"statistics_cutoff_at: {result.get('statistics_cutoff_at', '')}")
            print(f"latest: {config.data_dir / 'latest' / 'trend_api_stats.json'}")
            return 1 if result["status"] == "failed" else 0
        print(json.dumps(result, ensure_ascii=False))
        return 0

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
        )
        print(f"portfolio: {result.portfolio_path}")
        print(f"positions: {result.positions_count}")
        print(f"cash: {result.cash_count}")
        print(f"warnings: {result.warnings_count}")
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
        config_values = _load_optional_env_values(args.config)
        try:
            trend_a_share_tm_id = _optional_positive_tm_id(
                config_values, "TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID"
            )
            trend_etf_tm_id = _optional_positive_tm_id(
                config_values, "TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID"
            )
            trend_cn_candidate_pool_ids = (
                (trend_a_share_tm_id, trend_etf_tm_id)
                if trend_a_share_tm_id and trend_etf_tm_id
                else ()
            )
            trend_us_candidate_pool_ids = _positive_tm_ids(
                config_values.get("TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS", "")
            )
            trend_hk_candidate_pool_ids = _positive_tm_ids(
                config_values.get("TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS", "")
            )
            simulate_account_ids = {
                market: _optional_positive_tm_id(
                    config_values,
                    f"OPEN_TRADER_TREND_REVIEW_{market}_SIMULATE_ACC_ID",
                )
                for market in ("CN", "US", "HK")
            }
            populated_account_ids = [
                value for value in simulate_account_ids.values() if value > 0
            ]
            if len(populated_account_ids) != len(set(populated_account_ids)):
                raise ValueError(
                    "trend review simulate account IDs must be distinct"
                )
        except ValueError as exc:
            parser.error(str(exc))
        config = DashboardConfig(
            portfolio_path=args.portfolio,
            data_dir=args.data_dir,
            reports_dir=args.reports_dir,
            poll_seconds=args.poll_seconds,
            futu_host=args.futu_host,
            futu_port=args.futu_port,
            trend_review_cn_simulate_acc_id=simulate_account_ids["CN"],
            trend_review_us_simulate_acc_id=simulate_account_ids["US"],
            trend_review_hk_simulate_acc_id=simulate_account_ids["HK"],
            trend_executor_host=config_values.get(
                "OPEN_TRADER_TREND_EXECUTOR_HOST", ""
            ).strip(),
            trend_cn_candidate_pool_ids=trend_cn_candidate_pool_ids,
            trend_us_candidate_pool_ids=trend_us_candidate_pool_ids,
            trend_hk_candidate_pool_ids=trend_hk_candidate_pool_ids,
        )
        serve_dashboard(
            config,
            host=args.host,
            port=args.port,
            public_url=args.public_url,
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
