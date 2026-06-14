from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-statements",
        help="Import monthly broker statements and generate portfolio.csv",
    )
    import_parser.add_argument("--month", required=True, help="Statement month, YYYY-MM")
    import_parser.add_argument("--futu", type=Path, required=True)
    import_parser.add_argument("--tiger", type=Path, required=True)
    import_parser.add_argument("--phillips", type=Path, required=True)
    import_parser.add_argument("--data-dir", type=Path, default=Path("data"))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-statements":
        parser.error("import-statements is not implemented yet")

    parser.error(f"unknown command: {args.command}")
    return 2
