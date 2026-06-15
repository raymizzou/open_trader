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
