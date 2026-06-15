from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import PortfolioInputRow
from .tradingagents_adapter import TradingAgentsAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingagents-worker")
    parser.add_argument("--project-path", type=Path, required=True)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--row-json", required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    row = PortfolioInputRow(**json.loads(args.row_json))
    config_overrides = json.loads(args.config_json)
    adapter = TradingAgentsAdapter.from_project_path(
        args.project_path,
        config_overrides=config_overrides,
    )
    advice = adapter.analyze(row, args.run_date)
    args.output.write_text(
        json.dumps(advice.to_row(), ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
