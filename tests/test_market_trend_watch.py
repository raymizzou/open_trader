from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from open_trader.a_share_trend import write_protection_state
from open_trader.futu_watch import QuoteSnapshot
from open_trader.market_trend_watch import (
    market_session,
    next_market_open,
    watch_market_protection,
)
from open_trader.notifications import NullNotifier


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_hk_regular_sessions_exclude_lunch_and_auction() -> None:
    hk = ZoneInfo("Asia/Hong_Kong")
    assert market_session(datetime(2026, 7, 16, 9, 29, tzinfo=hk), "HK") == "before"
    assert market_session(datetime(2026, 7, 16, 9, 30, tzinfo=hk), "HK") == "morning"
    assert market_session(datetime(2026, 7, 16, 12, 0, tzinfo=hk), "HK") == "morning"
    assert market_session(datetime(2026, 7, 16, 12, 1, tzinfo=hk), "HK") == "lunch"
    assert market_session(datetime(2026, 7, 16, 16, 0, tzinfo=hk), "HK") == "afternoon"
    assert market_session(datetime(2026, 7, 16, 16, 1, tzinfo=hk), "HK") == "closed"


def test_us_regular_session_is_new_york_dst_aware() -> None:
    summer = datetime(2026, 7, 16, 21, 30, tzinfo=SHANGHAI)
    winter = datetime(2026, 12, 16, 22, 30, tzinfo=SHANGHAI)
    assert market_session(summer, "US") == "open"
    assert market_session(winter, "US") == "open"
    assert market_session(datetime(2026, 7, 17, 4, 1, tzinfo=SHANGHAI), "US") == "closed"


def test_next_market_open_waits_from_early_report_until_next_session() -> None:
    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-15", "2026-07-16", "2026-07-17"]

    hk_open = next_market_open(
        Quote(),
        market="HK",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SHANGHAI),
    )
    us_open = next_market_open(
        Quote(),
        market="US",
        now=datetime(2026, 7, 15, 9, 0, tzinfo=SHANGHAI),
    )

    assert hk_open == datetime(2026, 7, 16, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    assert us_open == datetime(2026, 7, 15, 9, 30, tzinfo=ZoneInfo("America/New_York"))


def _write_hk_details(data_dir: Path) -> None:
    run_dir = data_dir / "runs/2026-06"
    run_dir.mkdir(parents=True)
    with (run_dir / "extracted_positions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "statement_id", "broker", "market", "asset_class", "symbol", "name",
            "currency", "quantity", "cost_price", "market_value",
        ])
        writer.writeheader()
        writer.writerow({
            "statement_id": "2026-06-phillips", "broker": "phillips", "market": "HK",
            "asset_class": "stock", "symbol": "700", "name": "腾讯", "currency": "HKD",
            "quantity": "100", "cost_price": "400", "market_value": "50000",
        })
    with (run_dir / "extracted_cash.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "statement_id", "broker", "currency", "cash_balance", "available_balance",
        ])
        writer.writeheader()
        writer.writerow({
            "statement_id": "2026-06-phillips", "broker": "phillips", "currency": "HKD",
            "cash_balance": "10000", "available_balance": "10000",
        })


def test_market_watcher_uses_hk_account_and_triggers_once(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_hk_details(data_dir)
    state_path = data_dir / "trend_hk_phillips/protection_state.json"
    write_protection_state(state_path, {
        "schema_version": 1,
        "managed_symbols": ["00700"],
        "positions": {
            "00700": {
                "initial_line": "11", "active_line": "11", "atr14": "1",
                "position_started_for": "2026-07-15", "tracking_active": False,
                "updated_for": "2026-07-15",
            }
        },
    })

    class Quote:
        host = "127.0.0.1"
        port = 11111

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-16"]

        def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
            assert symbols == ["HK.00700"]
            return {"HK.00700": QuoteSnapshot("HK.00700", Decimal("10"))}

        def close(self) -> None:
            pass

    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    result = watch_market_protection(
        market="HK",
        data_dir=data_dir,
        portfolio_path=tmp_path / "unused.csv",
        state_path=state_path,
        events_path=data_dir / "trend_hk_phillips/watch_events.jsonl",
        report_lock_path=data_dir / "runs/.trend_hk_phillips_report.lock",
        quote_client=Quote(),
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=60,
        once=True,
        now_fn=lambda: now,
        sleep_fn=lambda seconds: None,
    )

    assert result.status == "completed"
    assert result.watched_symbol_count == 1
    assert result.trigger_count == 1
