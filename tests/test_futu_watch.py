from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.futu_watch import (
    ALERT_FIELDNAMES,
    WATCHLIST_REQUIRED_FIELDNAMES,
    AlertRecord,
    FutuWatchResult,
    MonitorTrigger,
    QuoteSnapshot,
    WatchState,
    append_alert,
    evaluate_quote,
    load_monitor_triggers,
    run_futu_watch,
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


def test_load_monitor_triggers_keeps_hk_active_price_rows(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.csv"
    write_watchlist(
        path,
        [
            base_row(
                symbol="00700",
                market="HK",
                operator=">=",
                trigger_price="390",
                trigger_text="升穿 390",
            ),
            base_row(symbol="BADHK", market="HK"),
            base_row(symbol="MSFT", market="US"),
        ],
    )

    loaded = load_monitor_triggers(path, run_date=None)

    assert loaded.triggers == [
        MonitorTrigger(
            run_date="2026-06-15",
            symbol="00700",
            market="HK",
            futu_symbol="HK.00700",
            trigger_type="price",
            operator=">=",
            trigger_price=Decimal("390"),
            suggested_action="reduce",
            severity="high",
            trigger_text="升穿 390",
        ),
        MonitorTrigger(
            run_date="2026-06-15",
            symbol="MSFT",
            market="US",
            futu_symbol="US.MSFT",
            trigger_type="price",
            operator="<=",
            trigger_price=Decimal("95"),
            suggested_action="reduce",
            severity="high",
            trigger_text="below 95",
        ),
    ]
    assert loaded.skipped_count == 1


def test_load_monitor_triggers_maps_cn_exchange_prefixes(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.csv"
    write_watchlist(
        path,
        [
            base_row(symbol="600025", market="CN"),
            base_row(symbol="000001", market="CN"),
            base_row(symbol="800001", market="CN"),
        ],
    )

    loaded = load_monitor_triggers(path, run_date=None)

    assert [trigger.futu_symbol for trigger in loaded.triggers] == [
        "SH.600025",
        "SZ.000001",
    ]
    assert loaded.skipped_count == 1


def test_load_monitor_triggers_skips_malformed_hk_symbols(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.csv"
    write_watchlist(
        path,
        [
            base_row(symbol="700.HK", market="HK"),
            base_row(symbol="ABC", market="HK"),
            base_row(symbol="", market="HK"),
            base_row(symbol="123456", market="HK"),
            base_row(symbol="700", market="HK"),
        ],
    )

    loaded = load_monitor_triggers(path, run_date=None)

    assert [trigger.futu_symbol for trigger in loaded.triggers] == ["HK.00700"]
    assert loaded.skipped_count == 4


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


def test_run_futu_watch_once_fetches_quotes_and_writes_alert(
    tmp_path: Path,
) -> None:
    watchlist_path = tmp_path / "watchlist.csv"
    write_watchlist(
        watchlist_path,
        [
            base_row(symbol="VIXY", operator="<=", trigger_price="95"),
            base_row(symbol="QQQ", operator=">=", trigger_price="510"),
        ],
    )
    client = FakeQuoteClient([{"US.VIXY": Decimal("94.5"), "US.QQQ": Decimal("500")}])

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


def test_run_futu_watch_output_does_not_claim_only_us_triggers(
    tmp_path: Path,
) -> None:
    watchlist_path = tmp_path / "watchlist.csv"
    write_watchlist(watchlist_path, [base_row(symbol="00700", market="HK")])
    client = FakeQuoteClient([{"HK.00700": Decimal("94.5")}])
    messages: list[str] = []

    run_futu_watch(
        watchlist_path=watchlist_path,
        data_dir=tmp_path / "data",
        run_date=None,
        quote_client=client,
        poll_seconds=5.0,
        once=True,
        sleep_fn=lambda seconds: None,
        now_fn=lambda: datetime(2026, 6, 15, 13, 30, 0),
        output_fn=messages.append,
    )

    assert messages[0] == "loaded 1 active trigger(s)"


def test_run_futu_watch_returns_zero_alerts_when_no_triggers(
    tmp_path: Path,
) -> None:
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
