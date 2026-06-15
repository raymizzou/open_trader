# Futu Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only Futu quote watcher that can connect to OpenD, fetch US quotes from `watchlist.csv`, and prove live quotes are retrievable.

**Architecture:** Keep Futu SDK access isolated in a quote client module, keep watchlist parsing and trigger evaluation in a pure testable watcher module, and wire both through a long-running `watch-futu` CLI with a `--once` diagnostic mode. Tests use fake quote clients by default; only the final manual verification touches real OpenD.

**Tech Stack:** Python 3.12, `argparse`, CSV files, `Decimal`, optional `futu-api`, pytest.

---

### Task 1: Watchlist Trigger Loader

**Files:**
- Create: `src/open_trader/futu_watch.py`
- Test: `tests/test_futu_watch.py`

- [ ] **Step 1: Write failing loader tests**

Create `tests/test_futu_watch.py` with:

```python
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.futu_watch import (
    WATCHLIST_REQUIRED_FIELDNAMES,
    MonitorTrigger,
    load_monitor_triggers,
)


WATCHLIST_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "suggested_action",
    "severity",
    "portfolio_weight_hkd",
    "trigger_type",
    "operator",
    "trigger_price",
    "trigger_text",
    "status",
    "error",
]


def write_watchlist(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WATCHLIST_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def base_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-15",
        "symbol": "VIXY",
        "market": "US",
        "suggested_action": "reduce",
        "severity": "high",
        "portfolio_weight_hkd": "3.05%",
        "trigger_type": "price",
        "operator": "<=",
        "trigger_price": "95",
        "trigger_text": "below 95",
        "status": "active",
        "error": "",
    }
    row.update(overrides)
    return row


def test_load_monitor_triggers_keeps_supported_us_active_price_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlist.csv"
    write_watchlist(
        path,
        [
            base_row(symbol="VIXY", operator="<=", trigger_price="95"),
            base_row(symbol="QQQ", operator=">=", trigger_price="510"),
            base_row(symbol="HKROW", market="HK"),
            base_row(symbol="MANUAL", status="manual_review"),
            base_row(symbol="TEXT", trigger_type="manual_review"),
            base_row(symbol="BADOP", operator="="),
            base_row(symbol="BADPRICE", trigger_price="not-a-number"),
        ],
    )

    loaded = load_monitor_triggers(path, run_date=None)

    assert loaded.run_date == "2026-06-15"
    assert loaded.skipped_count == 5
    assert loaded.triggers == [
        MonitorTrigger(
            run_date="2026-06-15",
            symbol="VIXY",
            market="US",
            futu_symbol="US.VIXY",
            trigger_type="price",
            operator="<=",
            trigger_price=Decimal("95"),
            suggested_action="reduce",
            severity="high",
            trigger_text="below 95",
        ),
        MonitorTrigger(
            run_date="2026-06-15",
            symbol="QQQ",
            market="US",
            futu_symbol="US.QQQ",
            trigger_type="price",
            operator=">=",
            trigger_price=Decimal("510"),
            suggested_action="reduce",
            severity="high",
            trigger_text="below 95",
        ),
    ]


def test_load_monitor_triggers_uses_explicit_run_date_and_blank_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlist.csv"
    write_watchlist(
        path,
        [
            base_row(run_date="2026-06-14", symbol="OLD"),
            base_row(run_date="2026-06-15", symbol="NEW"),
            base_row(run_date="", symbol="BLANK"),
        ],
    )

    loaded = load_monitor_triggers(path, run_date="2026-06-15")

    assert loaded.run_date == "2026-06-15"
    assert [trigger.symbol for trigger in loaded.triggers] == ["NEW", "BLANK"]


def test_load_monitor_triggers_uses_latest_run_date_when_date_omitted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlist.csv"
    write_watchlist(
        path,
        [
            base_row(run_date="2026-06-14", symbol="OLD"),
            base_row(run_date="2026-06-15", symbol="NEW"),
        ],
    )

    loaded = load_monitor_triggers(path, run_date=None)

    assert loaded.run_date == "2026-06-15"
    assert [trigger.symbol for trigger in loaded.triggers] == ["NEW"]


def test_load_monitor_triggers_rejects_missing_required_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlist.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["run_date", "symbol"])
        writer.writeheader()
        writer.writerow({"run_date": "2026-06-15", "symbol": "VIXY"})

    with pytest.raises(ValueError) as exc_info:
        load_monitor_triggers(path, run_date=None)

    assert "missing watchlist column(s)" in str(exc_info.value)
    for column in set(WATCHLIST_REQUIRED_FIELDNAMES) - {"run_date", "symbol"}:
        assert column in str(exc_info.value)
```

- [ ] **Step 2: Run loader tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py -q
```

Expected: FAIL because `open_trader.futu_watch` does not exist.

- [ ] **Step 3: Implement loader**

Create `src/open_trader/futu_watch.py` with:

```python
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
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


def load_monitor_triggers(watchlist_path: Path, run_date: str | None) -> LoadedTriggers:
    rows = _read_watchlist_rows(watchlist_path)
    effective_run_date = _validated_run_date(run_date) if run_date else _latest_run_date(rows)
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


def _read_watchlist_rows(watchlist_path: Path) -> list[dict[str, str]]:
    with watchlist_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = sorted(set(WATCHLIST_REQUIRED_FIELDNAMES) - set(fieldnames))
        if missing:
            raise ValueError(f"missing watchlist column(s): {', '.join(missing)}")
        return [
            {column: "" if value is None else str(value) for column, value in row.items() if column}
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


def _trigger_from_row(row: dict[str, str], fallback_run_date: str) -> MonitorTrigger | None:
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
```

- [ ] **Step 4: Run loader tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit loader**

Run:

```bash
git add src/open_trader/futu_watch.py tests/test_futu_watch.py
git commit -m "feat: load futu watch triggers"
```

---

### Task 2: Alert Writer And Trigger Evaluation

**Files:**
- Modify: `src/open_trader/futu_watch.py`
- Modify: `tests/test_futu_watch.py`

- [ ] **Step 1: Add failing alert and evaluation tests**

Append to `tests/test_futu_watch.py`:

```python
from datetime import datetime

from open_trader.futu_watch import (
    ALERT_FIELDNAMES,
    AlertRecord,
    QuoteSnapshot,
    WatchState,
    append_alert,
    evaluate_quote,
)


def test_evaluate_quote_returns_alert_when_downside_trigger_hits() -> None:
    trigger = MonitorTrigger(
        run_date="2026-06-15",
        symbol="VIXY",
        market="US",
        futu_symbol="US.VIXY",
        trigger_type="price",
        operator="<=",
        trigger_price=Decimal("95"),
        suggested_action="reduce",
        severity="high",
        trigger_text="below 95",
    )
    state = WatchState()

    alert = evaluate_quote(
        trigger,
        QuoteSnapshot(futu_symbol="US.VIXY", last_price=Decimal("94.5")),
        alerted_at=datetime(2026, 6, 15, 13, 30, 0),
        state=state,
    )

    assert alert == AlertRecord(
        alerted_at="2026-06-15T13:30:00",
        run_date="2026-06-15",
        symbol="VIXY",
        market="US",
        futu_symbol="US.VIXY",
        trigger_type="price",
        operator="<=",
        trigger_price="95",
        last_price="94.5",
        suggested_action="reduce",
        severity="high",
        trigger_text="below 95",
    )


def test_evaluate_quote_returns_alert_when_upside_trigger_hits_once() -> None:
    trigger = MonitorTrigger(
        run_date="2026-06-15",
        symbol="QQQ",
        market="US",
        futu_symbol="US.QQQ",
        trigger_type="price",
        operator=">=",
        trigger_price=Decimal("510"),
        suggested_action="watch",
        severity="medium",
        trigger_text="above 510",
    )
    state = WatchState()

    first = evaluate_quote(
        trigger,
        QuoteSnapshot(futu_symbol="US.QQQ", last_price=Decimal("511")),
        alerted_at=datetime(2026, 6, 15, 13, 31, 0),
        state=state,
    )
    second = evaluate_quote(
        trigger,
        QuoteSnapshot(futu_symbol="US.QQQ", last_price=Decimal("512")),
        alerted_at=datetime(2026, 6, 15, 13, 32, 0),
        state=state,
    )

    assert first is not None
    assert second is None


def test_evaluate_quote_returns_none_when_price_does_not_hit() -> None:
    trigger = MonitorTrigger(
        run_date="2026-06-15",
        symbol="VIXY",
        market="US",
        futu_symbol="US.VIXY",
        trigger_type="price",
        operator="<=",
        trigger_price=Decimal("95"),
        suggested_action="reduce",
        severity="high",
        trigger_text="below 95",
    )

    alert = evaluate_quote(
        trigger,
        QuoteSnapshot(futu_symbol="US.VIXY", last_price=Decimal("96")),
        alerted_at=datetime(2026, 6, 15, 13, 30, 0),
        state=WatchState(),
    )

    assert alert is None


def test_append_alert_creates_csv_header_and_appends_rows(tmp_path: Path) -> None:
    path = tmp_path / "data/runs/2026-06-15/alerts.csv"
    alert = AlertRecord(
        alerted_at="2026-06-15T13:30:00",
        run_date="2026-06-15",
        symbol="VIXY",
        market="US",
        futu_symbol="US.VIXY",
        trigger_type="price",
        operator="<=",
        trigger_price="95",
        last_price="94.5",
        suggested_action="reduce",
        severity="high",
        trigger_text="below 95",
    )

    append_alert(path, alert)
    append_alert(path, alert)

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert list(rows[0]) == ALERT_FIELDNAMES
    assert len(rows) == 2
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["last_price"] == "94.5"
```

- [ ] **Step 2: Run alert tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py -q
```

Expected: FAIL because alert classes and functions are missing.

- [ ] **Step 3: Implement alert writer and evaluation**

Append to `src/open_trader/futu_watch.py`:

```python
from datetime import datetime


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
```

- [ ] **Step 4: Run alert tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit alert logic**

Run:

```bash
git add src/open_trader/futu_watch.py tests/test_futu_watch.py
git commit -m "feat: evaluate futu watch alerts"
```

---

### Task 3: Polling Watch Runner

**Files:**
- Modify: `src/open_trader/futu_watch.py`
- Modify: `tests/test_futu_watch.py`

- [ ] **Step 1: Add failing runner tests**

Append to `tests/test_futu_watch.py`:

```python
from collections.abc import Sequence

from open_trader.futu_watch import FutuWatchResult, run_futu_watch


class FakeQuoteClient:
    def __init__(self, responses: list[dict[str, Decimal] | Exception]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []
        self.closed = False

    def get_snapshots(self, futu_symbols: Sequence[str]) -> dict[str, QuoteSnapshot]:
        self.calls.append(list(futu_symbols))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return {
            symbol: QuoteSnapshot(futu_symbol=symbol, last_price=price)
            for symbol, price in response.items()
        }

    def close(self) -> None:
        self.closed = True


def test_run_futu_watch_once_fetches_quotes_and_writes_alert(tmp_path: Path) -> None:
    watchlist_path = tmp_path / "watchlist.csv"
    write_watchlist(
        watchlist_path,
        [
            base_row(symbol="VIXY", operator="<=", trigger_price="95"),
            base_row(symbol="QQQ", operator=">=", trigger_price="510"),
        ],
    )
    client = FakeQuoteClient([
        {"US.VIXY": Decimal("94.5"), "US.QQQ": Decimal("500")}
    ])

    result = run_futu_watch(
        watchlist_path=watchlist_path,
        data_dir=tmp_path / "data",
        run_date=None,
        quote_client=client,
        poll_seconds=5.0,
        once=True,
        sleep_fn=lambda seconds: None,
        now_fn=lambda: datetime(2026, 6, 15, 13, 30, 0),
        output_fn=lambda message: None,
    )

    assert result == FutuWatchResult(
        run_date="2026-06-15",
        trigger_count=2,
        skipped_count=0,
        alert_count=1,
        alerts_path=tmp_path / "data/runs/2026-06-15/alerts.csv",
    )
    assert client.calls == [["US.QQQ", "US.VIXY"]]
    assert client.closed is True
    rows = list(csv.DictReader(result.alerts_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in rows] == ["VIXY"]


def test_run_futu_watch_returns_zero_alerts_when_no_triggers(tmp_path: Path) -> None:
    watchlist_path = tmp_path / "watchlist.csv"
    write_watchlist(watchlist_path, [base_row(market="HK")])
    client = FakeQuoteClient([])

    result = run_futu_watch(
        watchlist_path=watchlist_path,
        data_dir=tmp_path / "data",
        run_date=None,
        quote_client=client,
        poll_seconds=5.0,
        once=True,
        sleep_fn=lambda seconds: None,
        now_fn=lambda: datetime(2026, 6, 15, 13, 30, 0),
        output_fn=lambda message: None,
    )

    assert result.trigger_count == 0
    assert result.alert_count == 0
    assert client.calls == []
    assert client.closed is True


def test_run_futu_watch_startup_quote_failure_is_clear(tmp_path: Path) -> None:
    watchlist_path = tmp_path / "watchlist.csv"
    write_watchlist(watchlist_path, [base_row()])
    client = FakeQuoteClient([RuntimeError("quote failed")])

    with pytest.raises(RuntimeError, match="quote failed"):
        run_futu_watch(
            watchlist_path=watchlist_path,
            data_dir=tmp_path / "data",
            run_date=None,
            quote_client=client,
            poll_seconds=5.0,
            once=True,
            sleep_fn=lambda seconds: None,
            now_fn=lambda: datetime(2026, 6, 15, 13, 30, 0),
            output_fn=lambda message: None,
        )

    assert client.closed is True
```

- [ ] **Step 2: Run runner tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py -q
```

Expected: FAIL because `run_futu_watch` and `FutuWatchResult` are missing.

- [ ] **Step 3: Implement runner**

Append to `src/open_trader/futu_watch.py`:

```python
from collections.abc import Callable, Sequence
import time


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
```

- [ ] **Step 4: Run runner tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit runner**

Run:

```bash
git add src/open_trader/futu_watch.py tests/test_futu_watch.py
git commit -m "feat: run futu watch loop"
```

---

### Task 4: Futu SDK Quote Client

**Files:**
- Create: `src/open_trader/futu_quote.py`
- Create: `tests/test_futu_quote.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `futu-api` project dependency**

Modify `pyproject.toml` dependencies to include `futu-api`:

```toml
dependencies = [
    "futu-api",
    "openai>=2.0.0",
    "pdfplumber>=0.11.9",
]
```

- [ ] **Step 2: Write failing quote client tests**

Create `tests/test_futu_quote.py` with:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.futu_quote import FutuQuoteClient, FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


class FakeOpenQuoteContext:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.requested_symbols: list[str] = []

    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        self.requested_symbols = symbols
        return (
            0,
            FakeDataFrame(
                [
                    {"code": "US.VIXY", "last_price": 94.5},
                    {"code": "US.QQQ", "last_price": "510.25"},
                ]
            ),
        )

    def close(self) -> None:
        self.closed = True


class FakeFailingContext(FakeOpenQuoteContext):
    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        return -1, "OpenD connection failed"


def test_futu_quote_client_returns_normalized_snapshots() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
    )

    snapshots = client.get_snapshots(["US.VIXY", "US.QQQ"])

    assert snapshots == {
        "US.VIXY": QuoteSnapshot("US.VIXY", Decimal("94.5")),
        "US.QQQ": QuoteSnapshot("US.QQQ", Decimal("510.25")),
    }
    assert client.context.requested_symbols == ["US.VIXY", "US.QQQ"]


def test_futu_quote_client_raises_clear_error_on_sdk_failure() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeFailingContext,
    )

    with pytest.raises(FutuQuoteError, match="OpenD connection failed"):
        client.get_snapshots(["US.VIXY"])


def test_futu_quote_client_close_closes_context() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
    )

    client.close()

    assert client.context.closed is True
```

- [ ] **Step 3: Run quote tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py -q
```

Expected: FAIL because `open_trader.futu_quote` does not exist.

- [ ] **Step 4: Implement quote client**

Create `src/open_trader/futu_quote.py` with:

```python
from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from .futu_watch import QuoteSnapshot


class FutuQuoteError(RuntimeError):
    pass


def _default_context_factory(*, host: str, port: int) -> Any:
    try:
        from futu import OpenQuoteContext
    except ImportError as exc:
        raise FutuQuoteError(
            "futu-api is not installed. Install it with: "
            ".venv/bin/python -m pip install futu-api"
        ) from exc
    return OpenQuoteContext(host=host, port=port)


class FutuQuoteClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        context_factory: Callable[..., Any] = _default_context_factory,
    ) -> None:
        try:
            self.context = context_factory(host=host, port=port)
        except FutuQuoteError:
            raise
        except Exception as exc:
            raise FutuQuoteError(
                f"failed to connect to Futu OpenD at {host}:{port}: {exc}"
            ) from exc
        self.host = host
        self.port = port

    def get_snapshots(self, futu_symbols: Sequence[str]) -> dict[str, QuoteSnapshot]:
        ret_code, data = self.context.get_market_snapshot(list(futu_symbols))
        if ret_code != 0:
            raise FutuQuoteError(str(data))
        snapshots: dict[str, QuoteSnapshot] = {}
        for record in data.to_dict("records"):
            code = str(record.get("code", "")).strip()
            raw_price = record.get("last_price")
            if not code or raw_price in {None, ""}:
                continue
            try:
                price = Decimal(str(raw_price))
            except (InvalidOperation, ValueError):
                continue
            if price.is_finite():
                snapshots[code] = QuoteSnapshot(futu_symbol=code, last_price=price)
        return snapshots

    def close(self) -> None:
        self.context.close()
```

- [ ] **Step 5: Run quote tests and full focused watch tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py tests/test_futu_watch.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit quote client**

Run:

```bash
git add pyproject.toml src/open_trader/futu_quote.py tests/test_futu_quote.py
git commit -m "feat: add futu quote client"
```

---

### Task 5: CLI Wiring

**Files:**
- Modify: `src/open_trader/cli.py`
- Create: `tests/test_futu_watch_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_futu_watch_cli.py` with:

```python
from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_watch import FutuWatchResult


def test_watch_futu_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["watch-futu", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--watchlist" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--host" in output
    assert "--port" in output
    assert "--poll-seconds" in output
    assert "--once" in output


def test_watch_futu_main_wires_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

    def fake_run_futu_watch(**kwargs: object) -> FutuWatchResult:
        captured.update(kwargs)
        assert isinstance(kwargs["quote_client"], FakeFutuQuoteClient)
        data_dir = kwargs["data_dir"]
        assert isinstance(data_dir, Path)
        return FutuWatchResult(
            run_date="2026-06-15",
            trigger_count=2,
            skipped_count=1,
            alert_count=0,
            alerts_path=data_dir / "runs/2026-06-15/alerts.csv",
        )

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "run_futu_watch", fake_run_futu_watch)

    result = cli.main(
        [
            "watch-futu",
            "--watchlist",
            "watchlist.csv",
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-15",
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
            "--poll-seconds",
            "1.5",
            "--once",
        ]
    )

    assert result == 0
    assert captured["watchlist_path"] == Path("watchlist.csv")
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-15"
    assert captured["poll_seconds"] == 1.5
    assert captured["once"] is True
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 11111
    output = capsys.readouterr().out
    assert "run_date: 2026-06-15" in output
    assert "triggers: 2" in output
    assert "alerts: 0" in output
    assert "alerts_csv:" in output


def test_watch_futu_main_reports_runner_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            pass

    def fake_run_futu_watch(**kwargs: object) -> FutuWatchResult:
        raise RuntimeError("OpenD connection failed")

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "run_futu_watch", fake_run_futu_watch)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["watch-futu", "--once"])

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "OpenD connection failed" in stderr
    assert "Traceback" not in stderr
```

- [ ] **Step 2: Run CLI tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch_cli.py -q
```

Expected: FAIL because `watch-futu` command is missing.

- [ ] **Step 3: Wire CLI**

Modify imports near the top of `src/open_trader/cli.py`:

```python
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_watch import run_futu_watch
```

Add parser arguments in `build_parser()` after `build-watchlist`:

```python
    watch_futu_parser = subparsers.add_parser(
        "watch-futu",
        help="Watch active US price triggers with Futu OpenD quotes",
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
```

Add command handling in `main()` before the unknown-command fallback:

```python
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
```

- [ ] **Step 4: Run CLI tests and focused suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_watch_cli.py tests/test_futu_quote.py tests/test_futu_watch.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit CLI**

Run:

```bash
git add src/open_trader/cli.py tests/test_futu_watch_cli.py
git commit -m "feat: add futu watch cli"
```

---

### Task 6: Documentation And Verification

**Files:**
- Modify: `docs/monthly_portfolio_import.md`

- [ ] **Step 1: Document Futu watch usage**

Append this section to `docs/monthly_portfolio_import.md`:

```markdown
## Futu Quote Watch

Start Futu OpenD and log in before running the watcher. The first verification
mode fetches one quote snapshot and exits:

```bash
.venv/bin/python -m open_trader watch-futu \
  --watchlist data/runs/2026-06-15/watchlist.csv \
  --data-dir data \
  --date 2026-06-15 \
  --poll-seconds 5 \
  --once
```

Expected successful output includes:

```text
connected to Futu OpenD at 127.0.0.1:11111
loaded N active US trigger(s)
quote US.<SYMBOL> last_price=...
```

To keep watching until interrupted, omit `--once`.
```

- [ ] **Step 2: Run full automated tests**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Commit docs**

Run:

```bash
git add docs/monthly_portfolio_import.md
git commit -m "docs: add futu watch usage"
```

- [ ] **Step 4: Ensure a usable watchlist exists**

If `data/latest/watchlist.csv` is missing, use the existing dated file:

```bash
ls data/runs/2026-06-15/watchlist.csv
```

Expected: file exists. If it does not exist, run:

```bash
.venv/bin/python -m open_trader build-watchlist \
  --actions data/runs/2026-06-15/premarket_actions.csv \
  --data-dir data \
  --date 2026-06-15
```

Expected: command writes `data/runs/2026-06-15/watchlist.csv`.

- [ ] **Step 5: Run real Futu `--once` verification**

Run:

```bash
.venv/bin/python -m open_trader watch-futu \
  --watchlist data/runs/2026-06-15/watchlist.csv \
  --data-dir data \
  --date 2026-06-15 \
  --poll-seconds 5 \
  --once
```

Expected when OpenD is running and logged in:

```text
connected to Futu OpenD at 127.0.0.1:11111
loaded 1 active US trigger(s)
quote US.VIXY last_price=<number>
run_date: 2026-06-15
triggers: 1
alerts: 0
alerts_csv: data/runs/2026-06-15/alerts.csv
```

If the command cannot connect to OpenD, do not mark the goal complete. Report
the exact error and the fact that implementation is present but real quote
retrieval still needs OpenD running and logged in.

- [ ] **Step 6: Final status check**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: only intentional generated data files may be uncommitted. Source,
tests, docs, and plan changes are committed.
