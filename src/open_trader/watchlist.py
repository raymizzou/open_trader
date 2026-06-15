from __future__ import annotations

import csv
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import date
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
ACTION_REQUIRED_NONBLANK_FIELDS = ACTION_REQUIRED_FIELDS - {"run_date", "watch_trigger"}


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


@dataclass(frozen=True)
class ActionRow:
    values: dict[str, str]
    row_number: int
    error: str = ""


PRICE_RE = r"(?P<price>\d+(?:\.\d+)?)"
DOWNSIDE_RE = re.compile(
    rf"^(?P<open>(?:if\s+)?open\s+)?(?:(?:breaks\s+)?(?:below|under)|<=|<)\s*\$?{PRICE_RE}$",
    re.IGNORECASE,
)
UPSIDE_RE = re.compile(
    rf"^(?P<open>(?:if\s+)?open\s+)?(?:(?:breaks\s+)?(?:above|over)|>=|>)\s*\$?{PRICE_RE}$",
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
    if run_date is not None:
        run_date = _validated_run_date(run_date)
    rows = _read_action_rows(actions_path)
    effective_run_date = run_date or _latest_run_date(rows)
    filtered_rows = _filter_action_rows(
        rows,
        effective_run_date,
        allow_blank_run_date=run_date is not None,
    )
    if rows and not filtered_rows:
        raise ValueError(f"no action rows match run_date {effective_run_date}")
    watchlist_rows = [_row_from_action(row, effective_run_date) for row in filtered_rows]
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


def _read_action_rows(actions_path: Path) -> list[ActionRow]:
    with actions_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        raw_fieldnames = reader.fieldnames or []
        if any(not (column or "").strip() for column in raw_fieldnames):
            raise ValueError("unnamed action column(s)")
        duplicate_columns = sorted(
            column for column, count in Counter(raw_fieldnames).items() if count > 1
        )
        if duplicate_columns:
            raise ValueError(
                f"duplicate action column(s): {', '.join(duplicate_columns)}"
            )
        fieldnames = set(raw_fieldnames)
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
) -> ActionRow:
    normalized = {
        column: "" if value is None else str(value)
        for column, value in row.items()
        if column is not None
    }
    symbol = normalized.get("symbol", "").strip()
    if not symbol:
        raise ValueError(f"malformed action row {row_number}: blank symbol")

    csv_run_date = normalized.get("run_date", "").strip()
    if csv_run_date:
        try:
            normalized["run_date"] = _validated_run_date(csv_run_date)
        except ValueError as exc:
            raise ValueError(
                f"malformed action row {row_number} symbol {symbol}: "
                f"invalid run_date {csv_run_date}"
            ) from exc

    if None in row:
        extra_values = row[None]
        extra_text = ""
        if isinstance(extra_values, list):
            extra_text = ", ".join(str(value) for value in extra_values)
        elif extra_values is not None:
            extra_text = str(extra_values)
        details = f": {extra_text}" if extra_text else ""
        return ActionRow(
            values=normalized,
            row_number=row_number,
            error=(
                f"malformed action row {row_number} symbol {symbol}: "
                f"extra column(s){details}"
            ),
        )

    missing_values = [column for column, value in row.items() if value is None]
    if missing_values:
        columns = ", ".join(str(column) for column in missing_values)
        return ActionRow(
            values=normalized,
            row_number=row_number,
            error=(
                f"malformed action row {row_number} symbol {symbol}: "
                f"missing value for column(s): {columns}"
            ),
        )

    blank_values = [
        column
        for column in sorted(ACTION_REQUIRED_NONBLANK_FIELDS - {"symbol"})
        if not normalized[column].strip()
    ]
    if blank_values:
        columns = ", ".join(blank_values)
        return ActionRow(
            values=normalized,
            row_number=row_number,
            error=(
                f"malformed action row {row_number} symbol {symbol}: "
                f"blank value for column(s): {columns}"
            ),
        )

    return ActionRow(values=normalized, row_number=row_number)


def _error_row_from_action(
    row: dict[str, str],
    fallback_run_date: str,
    error: str,
) -> WatchlistRow:
    return WatchlistRow(
        run_date=row.get("run_date", "").strip() or fallback_run_date,
        symbol=row.get("symbol", "").strip(),
        market=row.get("market", "").strip(),
        suggested_action=row.get("suggested_action", "").strip(),
        severity=row.get("severity", "low").strip() or "low",
        portfolio_weight_hkd=row.get("portfolio_weight_hkd", "").strip(),
        trigger_type="none",
        operator="",
        trigger_price="",
        trigger_text=row.get("watch_trigger", ""),
        status="error",
        error=error,
    )


def _validated_run_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid run_date {value}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid run_date {value}")
    return value


def _latest_run_date(rows: list[ActionRow]) -> str:
    if not rows:
        return date.today().isoformat()
    dates = sorted(
        {
            row.values.get("run_date", "").strip()
            for row in rows
            if row.values.get("run_date", "").strip()
        }
    )
    if not dates:
        raise ValueError("--date is required when actions file has no run_date rows")
    return dates[-1]


def _filter_action_rows(
    rows: list[ActionRow],
    run_date: str,
    *,
    allow_blank_run_date: bool,
) -> list[ActionRow]:
    return [
        row
        for row in rows
        if row.values.get("run_date", "").strip() == run_date
        or (allow_blank_run_date and not row.values.get("run_date", "").strip())
    ]


def _row_from_action(action_row: ActionRow, fallback_run_date: str) -> WatchlistRow:
    row = action_row.values
    if action_row.error:
        return _error_row_from_action(row, fallback_run_date, action_row.error)
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
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=WATCHLIST_FIELDNAMES)
            writer.writeheader()
            writer.writerows(row.to_row() for row in rows)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return path


def _promote_latest(*, source_path: Path, latest_path: Path) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
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
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
