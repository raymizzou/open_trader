from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.futu_watch import (
    ALERT_FIELDNAMES,
    WATCHLIST_REQUIRED_FIELDNAMES,
    AlertRecord,
    MonitorTrigger,
    QuoteSnapshot,
    WatchState,
    append_alert,
    evaluate_quote,
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
