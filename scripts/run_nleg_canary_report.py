#!/usr/bin/env python3
"""Issue #65: export the read-only manual-canary fact report.

Opens the runtime prediction SQLite strictly read-only (``mode=ro`` URI),
builds the deterministic canary report with
``open_trader.prediction_n_leg_canary_report.build_canary_report_from_path``
and writes two artifacts into the report archive directory (default
``<repo>/reports/n_leg_canary/``, following the per-subsystem ``reports/``
convention):

- ``<UTC timestamp>.json``  — the builder's JSON, byte-exact
- ``<UTC timestamp>.md``    — the Chinese fact-sheet Markdown rendered by
  ``render_canary_report_markdown``

The tool never writes to the source database, never creates tables and
never submits orders. Exit codes: 0 = both artifacts written; 2 = the
store path does not exist (fail-closed, nothing written).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from open_trader.prediction_n_leg_canary_report import (  # noqa: E402
    build_canary_report_from_path,
    render_canary_report_markdown,
)

DEFAULT_STORE = _REPO / "data"
DEFAULT_OUT = _REPO / "reports" / "n_leg_canary"
_DB_REL = Path("prediction_arbitrage") / "prediction_arbitrage.sqlite3"


def _store_db_path(store: Path) -> Path:
    """``--store`` accepts the runtime data dir (the store convention) or a
    direct SQLite file path."""
    return store / _DB_REL if store.is_dir() else store


def _parse_now(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the read-only N-leg canary fact report (.json + .md).",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="Runtime data dir (or direct prediction_arbitrage.sqlite3 path).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Report archive directory (default: reports/n_leg_canary).",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="Optional ISO instant for a deterministic report (default: now).",
    )
    args = parser.parse_args(argv)

    db = _store_db_path(args.store)
    if not db.is_file():
        print(f"refusing: prediction store not found: {db}", file=sys.stderr)
        return 2
    moment = _parse_now(args.now) or datetime.now(UTC)

    report = build_canary_report_from_path(db, now=moment)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / f"{stamp}.json"
    markdown_path = args.out / f"{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_canary_report_markdown(report), encoding="utf-8"
    )
    print(json_path)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
