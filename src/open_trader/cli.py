from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .advice.change_classifier import ChangeClassifier, OpenAIClassifierClient
from .advice.premarket import run_premarket
from .advice.tradingagents_adapter import TradingAgentsSubprocessRunner
from .daily_premarket import (
    DailyPremarketRunner,
    build_notifier,
    load_env_config,
    send_notification_with_results,
)
from .dashboard import DashboardConfig
from .dashboard_web import serve_dashboard
from .decision_facts import LLMDecisionFactsExtractor, generate_decision_facts
from .futu_account import FutuAccountClient, FutuAccountError, sync_futu_portfolio
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_universe import load_futu_quote_universe
from .futu_watch import run_futu_watch
from .fx import StaticMonthEndFxProvider
from .market_scope import parse_market_scope
from .parsers.phillips import PhillipsStatementParser
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
    import_parser.add_argument("--phillips", type=Path, required=True)
    import_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    import_parser.add_argument(
        "--usd-hkd",
        type=positive_decimal,
        required=True,
        help="Month-end USD/HKD exchange rate",
    )

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
        result = run_import(
            month=args.month,
            statement_paths={
                "phillips": args.phillips,
            },
            parsers=[
                PhillipsStatementParser(),
            ],
            data_dir=args.data_dir,
            fx_provider=StaticMonthEndFxProvider(args.month, {"USD": args.usd_hkd}),
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
        failed_attempts = [attempt for attempt in attempts if not attempt.success]
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
        print("通知测试已发送。")
        return 0

    if args.command == "run-daily-premarket":
        try:
            config = load_env_config(args.config, dry_run=args.dry_run)
            run_date = (
                datetime.now(ZoneInfo(config.timezone)).date().isoformat()
                if args.date == "today"
                else canonical_date(args.date)
            )
            result = DailyPremarketRunner(
                config=config,
                notifier=build_notifier(config),
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
