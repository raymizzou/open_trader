from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from .advice.models import WATCHLIST_FIELDNAMES, WatchlistRow


ACTION_REQUIRED_FIELDS = {
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "suggested_action",
    "watch_trigger",
}


@dataclass(frozen=True)
class ParsedTrigger:
    trigger_type: str
    operator: str
    trigger_price: str
    trigger_text: str
    status: str
    error: str


@dataclass(frozen=True)
class WatchlistResult:
    run_date: str
    watchlist_count: int
    watchlist_path: Path
    latest_path: Path


PRICE_RE = r"(?P<price>\d+(?:\.\d+)?)"
DOWNSIDE_RE = re.compile(
    rf"^(?P<open>open\s+)?(?:(?:breaks\s+)?(?:below|under)|<=|<)\s*\$?{PRICE_RE}$",
    re.IGNORECASE,
)
UPSIDE_RE = re.compile(
    rf"^(?P<open>open\s+)?(?:(?:breaks\s+)?(?:above|over)|>=|>)\s*\$?{PRICE_RE}$",
    re.IGNORECASE,
)


def parse_watch_trigger(text: str) -> ParsedTrigger:
    original = text.strip()
    if not original:
        return ParsedTrigger(
            trigger_type="none",
            operator="",
            trigger_price="",
            trigger_text="",
            status="no_trigger",
            error="",
        )

    downside = DOWNSIDE_RE.fullmatch(original)
    if downside:
        return ParsedTrigger(
            trigger_type="open_price" if downside.group("open") else "price",
            operator="<=",
            trigger_price=downside.group("price"),
            trigger_text=original,
            status="active",
            error="",
        )

    upside = UPSIDE_RE.fullmatch(original)
    if upside:
        return ParsedTrigger(
            trigger_type="open_price" if upside.group("open") else "price",
            operator=">=",
            trigger_price=upside.group("price"),
            trigger_text=original,
            status="active",
            error="",
        )

    return ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=original,
        status="manual_review",
        error="",
    )


def build_watchlist(
    actions_path: Path,
    data_dir: Path,
    run_date: str | None = None,
    update_latest: bool = True,
) -> WatchlistResult:
    rows = _read_action_rows(actions_path)
    effective_run_date = run_date or _latest_run_date(rows)
    watchlist_rows = [_row_from_action(row, effective_run_date) for row in rows]
    watchlist_path = _write_watchlist_rows(
        data_dir / "runs" / effective_run_date / "watchlist.csv",
        watchlist_rows,
    )
    latest_path = data_dir / "latest" / "watchlist.csv"
    if update_latest:
        _promote_latest(source_path=watchlist_path, latest_path=latest_path)
    return WatchlistResult(
        run_date=effective_run_date,
        watchlist_count=len(watchlist_rows),
        watchlist_path=watchlist_path,
        latest_path=latest_path,
    )


def _read_action_rows(actions_path: Path) -> list[dict[str, str]]:
    with actions_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing = sorted(ACTION_REQUIRED_FIELDS - fieldnames)
        if missing:
            raise ValueError(f"missing action column(s): {', '.join(missing)}")
        return [
            _validated_action_row(row, row_number)
            for row_number, row in enumerate(reader, 2)
        ]


def _validated_action_row(
    row: dict[str | None, str | list[str] | None],
    row_number: int,
) -> dict[str, str]:
    if None in row:
        symbol = row.get("symbol") or "<unknown>"
        raise ValueError(
            f"malformed action row {row_number} symbol {symbol}: extra column(s)"
        )

    missing_values = [column for column, value in row.items() if value is None]
    if missing_values:
        symbol = row.get("symbol") or "<unknown>"
        columns = ", ".join(str(column) for column in missing_values)
        raise ValueError(
            f"malformed action row {row_number} symbol {symbol}: "
            f"missing value for column(s): {columns}"
        )

    return {column: str(value) for column, value in row.items()}


def _latest_run_date(rows: list[dict[str, str]]) -> str:
    dates = sorted(
        {
            row.get("run_date", "").strip()
            for row in rows
            if row.get("run_date", "").strip()
        }
    )
    if not dates:
        raise ValueError("--date is required when actions file has no run_date rows")
    return dates[-1]


def _row_from_action(row: dict[str, str], fallback_run_date: str) -> WatchlistRow:
    parsed = parse_watch_trigger(row.get("watch_trigger", ""))
    return WatchlistRow(
        run_date=row.get("run_date", "").strip() or fallback_run_date,
        symbol=row.get("symbol", "").strip(),
        market=row.get("market", "").strip(),
        suggested_action=row.get("suggested_action", "").strip(),
        severity=row.get("severity", "low").strip() or "low",
        portfolio_weight_hkd=row.get("portfolio_weight_hkd", "").strip(),
        trigger_type=parsed.trigger_type,
        operator=parsed.operator,
        trigger_price=parsed.trigger_price,
        trigger_text=parsed.trigger_text,
        status=parsed.status,
        error=parsed.error,
    )


def _write_watchlist_rows(path: Path, rows: list[WatchlistRow]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WATCHLIST_FIELDNAMES)
        writer.writeheader()
        writer.writerows(row.to_row() for row in rows)
    return path


def _promote_latest(*, source_path: Path, latest_path: Path) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "wb",
        dir=latest_path.parent,
        prefix=f".{latest_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        with source_path.open("rb") as source:
            shutil.copyfileobj(source, handle)
    temp_path.replace(latest_path)
