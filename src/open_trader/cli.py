from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .advice.change_classifier import ChangeClassifier, OpenAIClassifierClient
from .advice.premarket import run_premarket
from .advice.tradingagents_adapter import TradingAgentsAdapter
from .fx import StaticMonthEndFxProvider
from .parsers.futu import FutuStatementParser
from .parsers.phillips import PhillipsStatementParser
from .parsers.tiger import TigerStatementParser
from .pipeline import run_import, validate_month


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


def canonical_month(value: str) -> str:
    try:
        return validate_month(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid month: {value}") from exc


def _parse_symbol_subset(value: str | None) -> set[str] | None:
    if value is None or not value.strip():
        return None
    return {symbol.strip().upper() for symbol in value.split(",") if symbol.strip()}


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
    import_parser.add_argument("--futu", type=Path, required=True)
    import_parser.add_argument("--tiger", type=Path, required=True)
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
    premarket_parser.add_argument("--date", required=True, help="Run date, YYYY-MM-DD")
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
        "--symbols",
        help="Comma-separated subset of symbols to analyze",
    )
    premarket_parser.add_argument(
        "--classifier-model",
        default="gpt-5.4-mini",
        help="OpenAI model for change classification",
    )
    premarket_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write run outputs but do not update latest trading advice",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-statements":
        result = run_import(
            month=args.month,
            statement_paths={
                "futu": args.futu,
                "tiger": args.tiger,
                "phillips": args.phillips,
            },
            parsers=[
                FutuStatementParser(),
                TigerStatementParser(),
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
        result = run_premarket(
            run_date=args.date,
            portfolio_path=args.portfolio,
            data_dir=args.data_dir,
            reports_dir=args.reports_dir,
            advice_runner=TradingAgentsAdapter.from_project_path(
                args.tradingagents_path
            ),
            classifier=ChangeClassifier(
                client=OpenAIClassifierClient(model=args.classifier_model)
            ),
            symbols=symbols,
            update_latest=not args.dry_run,
        )
        print(f"eligible: {result.eligible_count}")
        print(f"advice: {result.advice_count}")
        print(f"actions: {result.action_count}")
        print(f"advice_csv: {result.advice_path}")
        print(f"actions_csv: {result.actions_path}")
        print(f"report: {result.report_path}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
