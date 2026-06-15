from __future__ import annotations

import csv
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


WATCHLIST_REQUIRED_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "suggested_action",
    "severity",
    "trigger_type",
    "operator",
    "trigger_price",
    "trigger_text",
    "status",
]


@dataclass(frozen=True)
class MonitorTrigger:
    run_date: str
    symbol: str
    market: str
    futu_symbol: str
    trigger_type: str
    operator: str
    trigger_price: Decimal
    suggested_action: str
    severity: str
    trigger_text: str


@dataclass(frozen=True)
class LoadedTriggers:
    run_date: str
    triggers: list[MonitorTrigger]
    skipped_count: int


ALERT_FIELDNAMES = [
    "alerted_at",
    "run_date",
    "symbol",
    "market",
    "futu_symbol",
    "trigger_type",
    "operator",
    "trigger_price",
    "last_price",
    "suggested_action",
    "severity",
    "trigger_text",
]


@dataclass(frozen=True)
class QuoteSnapshot:
    futu_symbol: str
    last_price: Decimal


@dataclass(frozen=True)
class AlertRecord:
    alerted_at: str
    run_date: str
    symbol: str
    market: str
    futu_symbol: str
    trigger_type: str
    operator: str
    trigger_price: str
    last_price: str
    suggested_action: str
    severity: str
    trigger_text: str

    def to_row(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in ALERT_FIELDNAMES}


@dataclass
class WatchState:
    alerted_keys: set[tuple[str, str, str, str]]

    def __init__(self) -> None:
        self.alerted_keys = set()


@dataclass(frozen=True)
class FutuWatchResult:
    run_date: str
    trigger_count: int
    skipped_count: int
    alert_count: int
    alerts_path: Path


class QuoteClientProtocol:
    def get_snapshots(self, futu_symbols: Sequence[str]) -> dict[str, QuoteSnapshot]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def load_monitor_triggers(watchlist_path: Path, run_date: str | None) -> LoadedTriggers:
    rows = _read_watchlist_rows(watchlist_path)
    effective_run_date = (
        _validated_run_date(run_date) if run_date else _latest_run_date(rows)
    )
    triggers: list[MonitorTrigger] = []
    skipped_count = 0
    for row in rows:
        row_run_date = row.get("run_date", "").strip()
        if row_run_date and row_run_date != effective_run_date:
            skipped_count += 1
            continue
        if not row_run_date and run_date is None:
            skipped_count += 1
            continue
        trigger = _trigger_from_row(row, effective_run_date)
        if trigger is None:
            skipped_count += 1
            continue
        triggers.append(trigger)
    return LoadedTriggers(
        run_date=effective_run_date,
        triggers=triggers,
        skipped_count=skipped_count,
    )


def evaluate_quote(
    trigger: MonitorTrigger,
    quote: QuoteSnapshot,
    *,
    alerted_at: datetime,
    state: WatchState,
) -> AlertRecord | None:
    key = (
        trigger.run_date,
        trigger.futu_symbol,
        trigger.operator,
        str(trigger.trigger_price),
    )
    if key in state.alerted_keys:
        return None
    hit = (
        quote.last_price <= trigger.trigger_price
        if trigger.operator == "<="
        else quote.last_price >= trigger.trigger_price
    )
    if not hit:
        return None
    state.alerted_keys.add(key)
    return AlertRecord(
        alerted_at=alerted_at.isoformat(timespec="seconds"),
        run_date=trigger.run_date,
        symbol=trigger.symbol,
        market=trigger.market,
        futu_symbol=trigger.futu_symbol,
        trigger_type=trigger.trigger_type,
        operator=trigger.operator,
        trigger_price=str(trigger.trigger_price),
        last_price=str(quote.last_price),
        suggested_action=trigger.suggested_action,
        severity=trigger.severity,
        trigger_text=trigger.trigger_text,
    )


def append_alert(path: Path, alert: AlertRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALERT_FIELDNAMES)
        if needs_header:
            writer.writeheader()
        writer.writerow(alert.to_row())


def run_futu_watch(
    *,
    watchlist_path: Path,
    data_dir: Path,
    run_date: str | None,
    quote_client: QuoteClientProtocol,
    poll_seconds: float,
    once: bool,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = datetime.now,
    output_fn: Callable[[str], None] = print,
) -> FutuWatchResult:
    loaded = load_monitor_triggers(watchlist_path, run_date)
    alerts_path = data_dir / "runs" / loaded.run_date / "alerts.csv"
    output_fn(f"loaded {len(loaded.triggers)} active US trigger(s)")
    if not loaded.triggers:
        quote_client.close()
        return FutuWatchResult(
            run_date=loaded.run_date,
            trigger_count=0,
            skipped_count=loaded.skipped_count,
            alert_count=0,
            alerts_path=alerts_path,
        )

    symbols = sorted({trigger.futu_symbol for trigger in loaded.triggers})
    triggers_by_symbol: dict[str, list[MonitorTrigger]] = {}
    for trigger in loaded.triggers:
        triggers_by_symbol.setdefault(trigger.futu_symbol, []).append(trigger)

    state = WatchState()
    alert_count = 0
    try:
        while True:
            snapshots = quote_client.get_snapshots(symbols)
            for futu_symbol in symbols:
                quote = snapshots.get(futu_symbol)
                if quote is None:
                    output_fn(f"warning: missing quote for {futu_symbol}")
                    continue
                output_fn(f"quote {futu_symbol} last_price={quote.last_price}")
                for trigger in triggers_by_symbol[futu_symbol]:
                    alert = evaluate_quote(
                        trigger,
                        quote,
                        alerted_at=now_fn(),
                        state=state,
                    )
                    if alert is None:
                        continue
                    append_alert(alerts_path, alert)
                    alert_count += 1
                    output_fn(
                        "ALERT "
                        f"{alert.futu_symbol} last_price={alert.last_price} "
                        f"{alert.operator} {alert.trigger_price} "
                        f"severity={alert.severity} action={alert.suggested_action}"
                    )
            if once:
                break
            sleep_fn(poll_seconds)
    finally:
        quote_client.close()

    return FutuWatchResult(
        run_date=loaded.run_date,
        trigger_count=len(loaded.triggers),
        skipped_count=loaded.skipped_count,
        alert_count=alert_count,
        alerts_path=alerts_path,
    )


def _read_watchlist_rows(watchlist_path: Path) -> list[dict[str, str]]:
    with watchlist_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = sorted(set(WATCHLIST_REQUIRED_FIELDNAMES) - set(fieldnames))
        if missing:
            raise ValueError(f"missing watchlist column(s): {', '.join(missing)}")
        return [
            {
                column: "" if value is None else str(value)
                for column, value in row.items()
                if column
            }
            for row in reader
        ]


def _latest_run_date(rows: list[dict[str, str]]) -> str:
    dates = sorted(
        {
            row.get("run_date", "").strip()
            for row in rows
            if row.get("run_date", "").strip()
        }
    )
    if not dates:
        return date.today().isoformat()
    return dates[-1]


def _validated_run_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid run_date {value}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid run_date {value}")
    return value


def _trigger_from_row(
    row: dict[str, str],
    fallback_run_date: str,
) -> MonitorTrigger | None:
    symbol = row.get("symbol", "").strip().upper()
    market = row.get("market", "").strip().upper()
    trigger_type = row.get("trigger_type", "").strip()
    operator = row.get("operator", "").strip()
    if (
        market != "US"
        or row.get("status", "").strip() != "active"
        or trigger_type not in {"price", "open_price"}
        or operator not in {"<=", ">="}
        or not symbol
    ):
        return None
    try:
        trigger_price = Decimal(row.get("trigger_price", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not trigger_price.is_finite():
        return None
    return MonitorTrigger(
        run_date=row.get("run_date", "").strip() or fallback_run_date,
        symbol=symbol,
        market=market,
        futu_symbol=f"US.{symbol}",
        trigger_type=trigger_type,
        operator=operator,
        trigger_price=trigger_price,
        suggested_action=row.get("suggested_action", "").strip(),
        severity=row.get("severity", "").strip(),
        trigger_text=row.get("trigger_text", "").strip(),
    )
