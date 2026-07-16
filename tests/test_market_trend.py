from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from open_trader import market_trend
from open_trader.a_share_trend import AShareTrendRunResult
from open_trader.daily_premarket import DailyPremarketConfig
from open_trader.market_trend import (
    MARKET_NOTIFICATION_LABELS,
    MARKET_SETTINGS,
    MarketHoliday,
    load_market_account,
    market_paths,
    resolve_market_dates,
    run_market_trend_report,
    updates_ready,
)
from open_trader.notifications import (
    FeishuWebhookNotifier,
    NotificationError,
    NullNotifier,
)
from open_trader.tiger_account import TigerAccountError
from open_trader.kline_technical_facts import DailyKlineBar
from open_trader.a_share_trend import UNIFIED_TREND_FIELDS


SHANGHAI = ZoneInfo("Asia/Shanghai")


class RecordingFeishu(FeishuWebhookNotifier):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(webhook_url="https://example.invalid")
        self.fail = fail
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
        if self.fail:
            raise NotificationError("network down")


def config(tmp_path: Path) -> DailyPremarketConfig:
    return DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        trend_animals_api_key="secret",
        trend_animals_us_tm_ids=(622460,),
        trend_animals_hk_tm_ids=(622494,),
        trend_us_symbols=("VIXY",),
        trend_hk_symbols=("00700",),
    )


def write_details(
    root: Path,
    run: str,
    *,
    positions: list[dict[str, str]],
    cash: list[dict[str, str]],
) -> None:
    run_dir = root / "runs" / run
    run_dir.mkdir(parents=True)
    for name, rows in (("extracted_positions.csv", positions), ("extracted_cash.csv", cash)):
        path = run_dir / name
        fieldnames = list(rows[0])
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def write_tiger_snapshot(
    data_dir: Path,
    run_date: str,
    *,
    cash_records: list[dict[str, object]],
    position_records: list[dict[str, object]],
) -> None:
    path = data_dir / "runs" / run_date / "tiger_account_snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "accounts": [],
            "cash_records": cash_records,
            "position_records": position_records,
        }),
        encoding="utf-8",
    )


def test_load_tiger_account_uses_hkd_nav_cash_and_managed_positions(
    tmp_path: Path,
) -> None:
    write_tiger_snapshot(
        tmp_path / "data",
        "2026-07-15",
        cash_records=[
            {
                "record_type": "account_total", "currency": "USD",
                "account_total": "100000",
            },
            {
                "currency": "USD", "cash_balance": "12000",
                "available_balance": "10000",
            },
            {
                "currency": "HKD", "cash_balance": "20000",
                "available_balance": "25000",
            },
        ],
        position_records=[{
            "market": "US", "sec_type": "STK", "symbol": "VIXY",
            "name": "VIX Short ETF", "currency": "USD", "position_qty": "10",
            "average_cost": "40", "market_value": "1000",
        }, {
            "market": "US", "sec_type": "STK", "symbol": "AAPL",
            "name": "Apple", "currency": "USD", "position_qty": "2",
            "average_cost": "200", "market_value": "420",
        }],
    )

    account = market_trend.load_trend_account(
        data_dir=tmp_path / "data",
        market="US",
        expected_date="2026-07-15",
        managed_symbols={"VIXY"},
    )

    assert account.source_date == "2026-07-15"
    assert account.fresh is True
    assert account.net_value == Decimal("785000")
    assert account.available_cash == Decimal("98500")
    assert [item.symbol for item in account.positions] == ["VIXY"]
    assert account.positions[0].market_value == Decimal("7850")
    assert market_trend.load_trend_account(
        data_dir=tmp_path / "data",
        market="US",
        expected_date="2026-07-16",
        managed_symbols={"VIXY"},
    ).fresh is False


def test_load_tiger_account_uses_latest_valid_snapshot_and_clamps_cash(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_tiger_snapshot(
        data_dir,
        "2026-07-14",
        cash_records=[
            {"record_type": "account_total", "currency": "USD", "account_total": "100"},
            {"currency": "USD", "cash_balance": "-1000", "available_balance": "-900"},
            {"currency": "SGD", "cash_balance": "100", "available_balance": "80", "fx_to_hkd": "5.8"},
        ],
        position_records=[],
    )
    write_tiger_snapshot(
        data_dir,
        "2026-07-15",
        cash_records=[],
        position_records=[],
    )

    account = market_trend.load_trend_account(
        data_dir=data_dir,
        market="US",
        expected_date="2026-07-15",
        managed_symbols=set(),
    )

    assert account.source_date == "2026-07-14"
    assert account.fresh is False
    assert account.available_cash == Decimal("0")


def test_us_account_refreshes_directly_from_tiger_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    calls: list[object] = []

    class Client:
        def __init__(self, *, config: object) -> None:
            calls.append(("connect", config))

        def fetch_snapshot(self) -> object:
            calls.append("fetch")
            return "snapshot"

        def close(self) -> None:
            calls.append("close")

    def sync(**kwargs: object) -> None:
        calls.append(("sync", kwargs))

    monkeypatch.setattr(
        "open_trader.market_trend.load_tiger_account_config",
        lambda **kwargs: calls.append(("config", kwargs)) or "tiger-config",
    )
    monkeypatch.setattr("open_trader.market_trend.TigerAccountClient", Client)
    monkeypatch.setattr("open_trader.market_trend.sync_tiger_portfolio", sync)

    market_trend._refresh_tiger_account(cfg, "2026-07-15")

    assert calls[:3] == [
        ("config", {"config_dir": Path("~/.tigeropen/"), "account": None, "sandbox": False}),
        ("connect", "tiger-config"),
        "fetch",
    ]
    assert calls[3][0] == "sync"
    assert calls[3][1]["snapshot"] == "snapshot"
    assert calls[3][1]["update_latest"] is True
    assert calls[4] == "close"


def test_load_phillips_statement_can_be_stale_and_caps_cash_to_known_balance(
    tmp_path: Path,
) -> None:
    write_details(
        tmp_path / "data",
        "2026-06",
        positions=[{
            "statement_id": "2026-06-phillips", "broker": "phillips",
            "market": "HK", "asset_class": "stock", "symbol": "700",
            "name": "腾讯", "currency": "HKD", "quantity": "100",
            "cost_price": "400", "market_value": "50000",
        }, {
            "statement_id": "2026-06-phillips", "broker": "phillips",
            "market": "HK", "asset_class": "stock", "symbol": "UT.SI",
            "name": "Unmanaged foreign holding", "currency": "HKD", "quantity": "10",
            "cost_price": "1", "market_value": "100",
        }],
        cash=[{
            "statement_id": "2026-06-phillips", "broker": "phillips",
            "currency": "HKD", "cash_balance": "20000", "available_balance": "15000",
        }],
    )

    account = load_market_account(
        data_dir=tmp_path / "data",
        broker="phillips",
        market="HK",
        expected_date="2026-07-15",
        managed_symbols={"00700"},
    )

    assert account.source_date == "2026-06"
    assert account.fresh is False
    assert account.available_cash == Decimal("15000")
    assert account.positions[0].symbol == "00700"


def test_market_paths_are_completely_separate() -> None:
    assert market_paths(Path("data"), Path("reports"), "US").root == Path("data/trend_us_tiger")
    assert market_paths(Path("data"), Path("reports"), "HK").root.name == "trend_hk_phillips"
    assert MARKET_SETTINGS["US"]["broker"] == "tiger"
    assert MARKET_NOTIFICATION_LABELS["US"][0] == "老虎"


def test_resolve_market_dates_uses_same_day_hk_and_prior_day_us() -> None:
    class Quote:
        def get_trading_days(self, *, market: str, start: str, end: str) -> list[str]:
            return ["2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16"]

    assert resolve_market_dates(Quote(), market="HK", run_date="2026-07-15") == (
        "2026-07-15", "2026-07-16"
    )
    assert resolve_market_dates(Quote(), market="US", run_date="2026-07-15") == (
        "2026-07-14", "2026-07-15"
    )


def test_resolve_market_dates_marks_missing_target_session_as_holiday() -> None:
    class Quote:
        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-10", "2026-07-13", "2026-07-16"]

    with pytest.raises(MarketHoliday):
        resolve_market_dates(Quote(), market="HK", run_date="2026-07-15")
    with pytest.raises(MarketHoliday):
        resolve_market_dates(Quote(), market="US", run_date="2026-07-15")


def test_updates_ready_requires_the_market_base_asset_date() -> None:
    rows = [
        {"asset": "港股", "asOfDate": "2026-07-15"},
        {"asset": "美股", "asOfDate": "2026-07-14"},
    ]
    assert updates_ready(rows, market="HK", as_of_date="2026-07-15") is True
    assert updates_ready(rows, market="US", as_of_date="2026-07-14") is True
    assert updates_ready(rows, market="US", as_of_date="2026-07-15") is False


def test_market_report_retries_every_ten_minutes_and_stops_after_success(
    tmp_path: Path,
) -> None:
    attempts = iter([
        AShareTrendRunResult("waiting", None, None),
        AShareTrendRunResult("generated", Path("report.md"), Path("report.json")),
    ])
    times = iter([
        datetime(2026, 7, 15, 9, 0, tzinfo=SHANGHAI),
        datetime(2026, 7, 15, 9, 10, tzinfo=SHANGHAI),
    ])
    sleeps: list[float] = []

    result = run_market_trend_report(
        config=config(tmp_path),
        market="US",
        run_date="2026-07-15",
        notifier=NullNotifier(),
        attempt_fn=lambda **kwargs: next(attempts),
        now_fn=lambda: next(times),
        sleep_fn=sleeps.append,
    )

    assert result.status == "generated"
    assert sleeps == [600.0]


def test_market_report_keeps_retrying_after_old_ten_deadline(
    tmp_path: Path,
) -> None:
    attempts = iter([
        AShareTrendRunResult("waiting", None, None),
        AShareTrendRunResult("waiting", None, None),
        AShareTrendRunResult("generated", Path("report.md"), Path("report.json")),
    ])
    times = iter([
        datetime(2026, 7, 15, 10, 0, tzinfo=SHANGHAI),
        datetime(2026, 7, 15, 11, 40, tzinfo=SHANGHAI),
    ])
    sleeps: list[float] = []

    result = run_market_trend_report(
        config=config(tmp_path),
        market="US",
        run_date="2026-07-15",
        notifier=NullNotifier(),
        attempt_fn=lambda **kwargs: next(attempts),
        now_fn=lambda: next(times),
        sleep_fn=sleeps.append,
    )

    assert result.status == "generated"
    assert sleeps == [600.0, 600.0]


def test_market_report_failure_owns_day_at_noon_deadline(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=SHANGHAI)
    cfg = config(tmp_path)
    notifier = RecordingFeishu()
    result = run_market_trend_report(
        config=cfg,
        market="US",
        run_date="2026-07-15",
        notifier=notifier,
        attempt_fn=lambda **kwargs: AShareTrendRunResult("waiting", None, None),
        now_fn=lambda: now,
        sleep_fn=lambda seconds: None,
    )

    assert result.status == "failed"
    assert notifier.messages == [
        (
            "【老虎｜美股趋势报告生成失败｜2026-07-15】",
            "原因：趋势数据在截止时间前仍未更新\n"
            "现在做：确认 Trend Animals 与老虎账户状态后手动重跑老虎报告\n\n"
            "报告未生成，请勿依据旧报告交易。",
        )
    ]
    ledger = cfg.data_dir / "trend_us_tiger/daily_delivery/2026-07-15.json"
    assert __import__("json").loads(ledger.read_text(encoding="utf-8"))["status"] == "sent"


def test_hk_report_keeps_buys_when_statement_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    write_details(
        cfg.data_dir,
        "2026-06",
        positions=[{
            "statement_id": "2026-06-phillips", "broker": "phillips",
            "market": "HK", "asset_class": "stock", "symbol": "700",
            "name": "腾讯", "currency": "HKD", "quantity": "100",
            "cost_price": "400", "market_value": "50000",
        }],
        cash=[{
            "statement_id": "2026-06-phillips", "broker": "phillips",
            "currency": "HKD", "cash_balance": "50000", "available_balance": "50000",
        }],
    )

    def snapshot(tm_id: int, symbol: str, name: str) -> dict[str, object]:
        return {
            "tmId": tm_id, "tickerName": name, "tickerSymbol": symbol,
            "asset": "港股", "asOfDate": "2026-07-15", "tradableFlag": True,
            "industryName": "科技", "amount1d": "2", "isTrendRightSide": True,
            "daysSinceTrendEntry": 3, "trendStrengthLocalCurr": "96",
            "gainSinceTrendEntry": "0.048", "trendPhasePrev": "谷雨",
            "trendPhaseCurr": "立夏", "trendStrengthLocalChange": "↑↑",
            "trendStrengthGlobalCurr": "91.8",
            "trendStrengthLocalPrevWeek": "86.0",
            "trendStrengthLocalPrevMonth": "77.4",
            "tickerLabels": "成交主力;市值龙头",
            "stopwinFlagByDangerSignal": False,
            "stopwinFlagByBoilingTemperature": False,
            "stopwinFlagByPopChampagne": False,
        }

    api_instances = 0

    class Api:
        ignored_stale_components = (
            {"tickerSymbol": "NUVL", "asOfDate": "2026-07-14"},
        )

        def __init__(self, **kwargs: object) -> None:
            nonlocal api_instances
            api_instances += 1

        def get_update_status(self) -> list[dict[str, object]]:
            return [{"asset": "港股", "asOfDate": "2026-07-15"}]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(self, *, tm_id: int, expected_date: str) -> list[dict[str, object]]:
            assert tm_id == 622494
            return [{"tmId": 1, "tickerSymbol": "02800.HK", "asOfDate": expected_date}]

        def search_exact_symbol(self, symbol: str) -> int:
            assert symbol == "00700"
            return 2

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            return [
                {
                    "field": field,
                    "priceCost": "0.071" if field == "tickerName" else "0",
                }
                for field in UNIFIED_TREND_FIELDS
            ]

        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            assert kwargs["tm_ids"] == [1, 2]
            assert kwargs["fields"] == UNIFIED_TREND_FIELDS
            return [
                snapshot(1, "02800.HK", "盈富基金"),
                snapshot(2, "00700.HK", "腾讯"),
            ]

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            self.closed = False

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-15", "2026-07-16"]

        def get_daily_kline(self, *args: object, **kwargs: object) -> list[DailyKlineBar]:
            return [
                DailyKlineBar(
                    date=f"2026-07-{index + 1:02d}", open=10, high=11,
                    low=9, close=10, volume=100,
                )
                for index in range(15)
            ]

        def get_lot_sizes(self, symbols: list[str]) -> dict[str, int]:
            return {symbol: 100 for symbol in symbols}

        def close(self) -> None:
            self.closed = True

    notifier = RecordingFeishu()
    from open_trader import market_trend

    original_freeze = market_trend._freeze_receipt_report
    freeze_attempts = 0

    def fail_first_freeze(**kwargs: object) -> tuple[Path, Path]:
        nonlocal freeze_attempts
        freeze_attempts += 1
        if freeze_attempts == 1:
            raise OSError("simulated report persistence failure after delivery")
        return original_freeze(**kwargs)

    monkeypatch.setattr(market_trend, "_freeze_receipt_report", fail_first_freeze)
    result = run_market_trend_report(
        config=cfg,
        market="HK",
        run_date="2026-07-15",
        notifier=notifier,
        api_factory=Api,
        quote_factory=Quote,
        now_fn=lambda: datetime(2026, 7, 15, 16, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
    )
    assert result.report_path is not None and result.json_path is not None
    frozen_json = result.json_path.read_text(encoding="utf-8")
    result.report_path.unlink()
    result.json_path.unlink()
    recovered = run_market_trend_report(
        config=cfg,
        market="HK",
        run_date="2026-07-15",
        notifier=notifier,
        api_factory=lambda **kwargs: pytest.fail("receipt recovery must not refetch"),
        quote_factory=Quote,
    )
    revised = run_market_trend_report(
        config=cfg,
        market="HK",
        run_date="2026-07-15",
        revision=True,
        notifier=notifier,
        api_factory=Api,
        quote_factory=Quote,
    )

    assert result.status == recovered.status == revised.status == "generated"
    assert recovered.json_path is not None
    assert recovered.json_path.read_text(encoding="utf-8") == frozen_json
    assert len(notifier.messages) == 1
    assert api_instances == 2  # initial report plus explicit revision; recovery did not refetch
    title, message = notifier.messages[0]
    assert title == "【辉立｜港股趋势报告｜2026-07-16】"
    assert "账户状态：账户数据非实时，执行前核对现金与持仓" in message
    assert "今日动作：卖出 0｜买入 1｜持有 1｜复核 0" in message
    assert "\n买入\n" in message
    assert "02800 盈富基金" in message
    assert "禁止买入" not in message
    assert "http" not in message.lower()
    payload = __import__("json").loads(result.json_path.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in payload["option_attention"]] == [
        "00700",
        "02800",
    ]
    assert "\n期权关注\n" in message
    assert payload["option_attention"][0]["source_broker"] == "辉立"
    candidate_snapshot = payload["signal_snapshots"]["candidates"][0]
    assert candidate_snapshot["boiling"] is False
    assert candidate_snapshot["champagne"] is False
    assert "忽略旧成分 1 条：NUVL（2026-07-14）" in payload["api_facts"]
    assert (
        f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows=2 "
        "cache=client-managed"
    ) in payload["api_facts"]
    actions = payload["strategy_judgments"]["formal_actions"]
    assert actions[0]["action"] == "BUY"
    assert actions[0]["symbol"] == "02800"
    assert actions[0]["target_amount"] == "4000.00"
    assert actions[0]["estimated_shares"] == 400
    assert payload["account"]["fresh"] is False
    assert payload["metadata"]["position_weight"] == "0.04"
    assert payload["metadata"]["position_weight_source"] == "fallback_4pct"
    assert payload["estimated_api_cost"] == "0.142"
    assert payload["signal_snapshots"]["holdings"]["00700"] | {
        "gain_since_entry": "0.048",
        "phase_prev": "谷雨",
        "phase_curr": "立夏",
        "strength_change": "↑↑",
        "global_strength": "91.8",
        "strength_prev_week": "86.0",
        "strength_prev_month": "77.4",
        "labels": ["成交主力", "市值龙头"],
        "kline_supplement": None,
    } == payload["signal_snapshots"]["holdings"]["00700"]
    assert payload["protection_state"]["managed_symbols"] == ["00700", "02800"]


@pytest.mark.parametrize(
    ("refresh_error", "expected_refresh_error", "forbidden_values"),
    [
        (
            TigerAccountError(
                "Tiger refresh failed token=TIGER-TOKEN account=123456789",
                error_type="account_query_failed",
            ),
            "account_query_failed",
            ("TIGER-TOKEN", "123456789"),
        ),
        (
            TigerAccountError(
                "Tiger refresh failed token=UNKNOWN-MESSAGE-SECRET",
                error_type="unknown_type_UNKNOWN-TYPE-SECRET",
            ),
            "tiger_account_error",
            ("UNKNOWN-MESSAGE-SECRET", "unknown_type_UNKNOWN-TYPE-SECRET"),
        ),
        (
            RuntimeError(
                "Tiger refresh failed token=TIGER-TOKEN account=123456789"
            ),
            "Tiger account refresh failed",
            ("TIGER-TOKEN", "123456789"),
        ),
    ],
)
def test_stale_us_tiger_account_blocks_buys_and_marks_holdings_for_review(
    tmp_path: Path,
    refresh_error: Exception,
    expected_refresh_error: str,
    forbidden_values: tuple[str, ...],
) -> None:
    cfg = config(tmp_path)
    write_tiger_snapshot(
        cfg.data_dir,
        "2026-07-14",
        cash_records=[
            {"record_type": "account_total", "currency": "USD", "account_total": "100000"},
            {"currency": "USD", "cash_balance": "10000", "available_balance": "10000"},
        ],
        position_records=[{
            "market": "US", "sec_type": "STK", "symbol": "VIXY",
            "name": "VIX Short ETF", "currency": "USD", "position_qty": "10",
            "average_cost": "40", "market_value": "500",
        }],
    )
    write_tiger_snapshot(
        cfg.data_dir,
        "2026-07-15",
        cash_records=[
            {"record_type": "account_total", "currency": "USD", "account_total": "200000"},
            {"currency": "USD", "cash_balance": "20000", "available_balance": "20000"},
        ],
        position_records=[{
            "market": "US", "sec_type": "STK", "symbol": "VIXY",
            "name": "VIX Short ETF", "currency": "USD", "position_qty": "10",
            "average_cost": "40", "market_value": "500",
        }],
    )

    class Api:
        ignored_stale_components: tuple[object, ...] = ()

        def __init__(self, **kwargs: object) -> None:
            pass

        def get_update_status(self) -> list[dict[str, object]]:
            return [{"asset": "美股", "asOfDate": "2026-07-14"}]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            assert (tm_id, expected_date) == (622460, "2026-07-14")
            return [{"tmId": 1, "tickerSymbol": "QQQ.US", "asOfDate": expected_date}]

        def search_exact_symbol(self, symbol: str) -> int:
            assert symbol == "VIXY"
            return 2

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            return [
                {
                    "field": field,
                    "priceCost": "0.071" if field == "tickerName" else "0",
                }
                for field in UNIFIED_TREND_FIELDS
            ]

        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            assert kwargs["fields"] == UNIFIED_TREND_FIELDS
            return [
                {
                    "tmId": tm_id, "tickerName": name, "tickerSymbol": f"{symbol}.US",
                    "asset": "美股", "asOfDate": "2026-07-14", "tradableFlag": True,
                    "industryName": "ETF", "amount1d": "2", "isTrendRightSide": True,
                    "daysSinceTrendEntry": 3, "trendStrengthLocalCurr": "96",
                    "stopwinFlagByDangerSignal": False,
                    "stopwinFlagByBoilingTemperature": False,
                    "stopwinFlagByPopChampagne": False,
                }
                for tm_id, symbol, name in ((1, "QQQ", "Invesco QQQ"), (2, "VIXY", "VIX Short"))
            ]

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-14", "2026-07-15"]

        def get_daily_kline(
            self, *args: object, **kwargs: object
        ) -> list[DailyKlineBar]:
            end = datetime(2026, 7, 14)
            return [
                DailyKlineBar(
                    date=(end - timedelta(days=14 - index))
                    .date()
                    .isoformat(),
                    open=10, high=11, low=9, close=10, volume=100,
                )
                for index in range(15)
            ]

        def close(self) -> None:
            pass

    notifier = RecordingFeishu()
    result = run_market_trend_report(
        config=cfg,
        market="US",
        run_date="2026-07-15",
        notifier=notifier,
        api_factory=Api,
        quote_factory=Quote,
        account_refresher=lambda *args: (_ for _ in ()).throw(
            refresh_error
        ),
    )

    assert result.json_path is not None
    assert result.report_path is not None
    payload = __import__("json").loads(result.json_path.read_text(encoding="utf-8"))
    assert (
        f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows=2 "
        "cache=client-managed"
    ) in payload["api_facts"]
    assert payload["estimated_api_cost"] == "0.142"
    assert payload["account"]["fresh"] is False
    assert payload["account"]["source_date"] == "2026-07-14"
    assert payload["strategy_judgments"]["formal_actions"] == []
    assert payload["strategy_judgments"]["holding_decisions"][0]["action"] == "MANUAL_REVIEW"
    assert payload["strategy_judgments"]["holding_decisions"][0]["reason"] == "stale_tiger_account"
    assert payload["metadata"]["account_currency"] == "HKD"
    assert payload["metadata"]["price_fx_to_hkd"] == "7.85"
    assert payload["metadata"]["account_refresh_error"] == expected_refresh_error
    assert "账户状态：账户数据非实时，禁止新增买入；持仓需复核" in notifier.messages[0][1]
    output = "\n".join(
        (
            result.json_path.read_text(encoding="utf-8"),
            result.report_path.read_text(encoding="utf-8"),
            market_paths(cfg.data_dir, cfg.reports_dir, "US").log.read_text(
                encoding="utf-8"
            ),
            *(f"{title}\n{message}" for title, message in notifier.messages),
        )
    )
    for value in forbidden_values:
        assert value not in output


def test_market_report_rejects_catalog_cost_drift_before_paid_snapshots(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    write_tiger_snapshot(
        cfg.data_dir,
        "2026-07-15",
        cash_records=[
            {"record_type": "account_total", "currency": "USD", "account_total": "1210"},
            {"currency": "USD", "cash_balance": "1000", "available_balance": "1000"},
        ],
        position_records=[{
            "market": "US", "sec_type": "STK", "symbol": "AAPL",
            "name": "Apple", "currency": "USD", "position_qty": "1",
            "average_cost": "200", "market_value": "210",
        }],
    )
    snapshot_calls: list[object] = []

    class Api:
        ignored_stale_components: tuple[object, ...] = ()

        def __init__(self, **kwargs: object) -> None:
            pass

        def get_update_status(self) -> list[dict[str, object]]:
            return [{"asset": "美股", "asOfDate": "2026-07-14"}]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return [{"tmId": 1, "tickerSymbol": "VIXY.US", "asOfDate": expected_date}]

        def search_exact_symbol(self, symbol: str) -> int:
            return 2

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            return [
                {
                    "field": field,
                    "priceCost": "0.072" if field == "tickerName" else "0",
                }
                for field in UNIFIED_TREND_FIELDS
            ]

        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            snapshot_calls.append(kwargs)
            return []

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-14", "2026-07-15"]

        def close(self) -> None:
            pass

    result = run_market_trend_report(
        config=cfg,
        market="US",
        run_date="2026-07-15",
        notifier=NullNotifier(),
        now_fn=lambda: datetime(2026, 7, 15, 12, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
        api_factory=Api,
        quote_factory=Quote,
        account_refresher=lambda *args: None,
    )

    assert result.status == "failed"
    assert snapshot_calls == []


def test_existing_report_retries_frozen_failure_without_refetch(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    reports = cfg.reports_dir / "trend_hk_phillips"
    reports.mkdir(parents=True)
    (reports / "2026-07-15.md").write_text("frozen", encoding="utf-8")
    (reports / "2026-07-15.json").write_text("{}", encoding="utf-8")
    ledger = cfg.data_dir / "trend_hk_phillips/daily_delivery/2026-07-15.json"
    failed = RecordingFeishu(fail=True)
    from open_trader.trend_delivery import deliver_daily_trend_text

    assert deliver_daily_trend_text(
        failed, ledger_path=ledger, title="frozen title", message="frozen body"
    ) == "delivery_failed"

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-15", "2026-07-16"]

        def close(self) -> None:
            pass

    recovered = RecordingFeishu()
    result = run_market_trend_report(
        config=cfg,
        market="HK",
        run_date="2026-07-15",
        notifier=recovered,
        api_factory=lambda **kwargs: pytest.fail("existing report must not refetch"),
        quote_factory=Quote,
    )

    assert result.status == "existing"
    assert recovered.messages == [("frozen title", "frozen body")]


def _attention_row(symbol: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": symbol,
        "name": symbol,
        "right_side": False,
        "temperature_curr": "温",
        "phase_curr": "谷雨",
        "strength": "90",
        "global_strength": "85",
        "strength_prev_week": "88",
        "strength_prev_month": "80",
        "strength_change": "→",
        "days": 0,
        "gain_since_entry": "0",
        "danger": False,
        "boiling": False,
        "champagne": False,
    }
    row.update(overrides)
    return row


def test_build_option_attention_emits_only_raw_trend_transitions() -> None:
    previous = [
        _attention_row("QQQ"),
        _attention_row("DRAM"),
        _attention_row("MSFT"),
    ]
    current = [
        _attention_row(
            "QQQ",
            right_side=True,
            temperature_curr="热",
            phase_curr="立夏",
            strength_change="↑↑",
            days=1,
            gain_since_entry="0.048",
        ),
        _attention_row("DRAM", danger=True),
        _attention_row("MSFT"),
    ]

    attention = market_trend.build_option_attention(
        current, previous, {"QQQ": "BUY"}, "US", "tiger"
    )

    assert [item["symbol"] for item in attention] == ["DRAM", "QQQ"]
    assert list(attention[0]) == [
        "market",
        "symbol",
        "name",
        "category",
        "right_side",
        "temperature",
        "phase",
        "local_strength",
        "global_strength",
        "strength_prev_week",
        "strength_prev_month",
        "strength_change",
        "days",
        "gain_since_entry",
        "danger",
        "boiling",
        "champagne",
        "source_broker",
        "source_action",
    ]
    assert attention[0]["category"] == "risk"
    assert attention[0]["danger"] == {
        "previous": False,
        "current": True,
        "changed": True,
    }
    assert attention[1]["category"] == "strengthened"
    assert attention[1]["right_side"] == {
        "previous": False,
        "current": True,
        "changed": True,
    }
    assert attention[1]["days"] == 1
    assert attention[1]["gain_since_entry"] == "0.048"
    assert attention[1]["source_action"] == "BUY"
    assert "headline" not in attention[1]
    assert "summary" not in attention[1]
    protection_only = [{**row, "active_line": "200"} for row in current]
    assert market_trend.build_option_attention(
        protection_only, current, {"MSFT": "SELL_ALL"}, "US", "tiger"
    ) == []


def test_build_option_attention_preserves_missing_values_and_holding_precedence() -> None:
    candidate = _attention_row("700.HK", name="候选腾讯", danger=False)
    holding = _attention_row(
        "00700",
        name=None,
        right_side=True,
        temperature_curr=None,
        phase_curr=None,
        strength=None,
        global_strength=None,
        strength_prev_week=None,
        strength_prev_month=None,
        strength_change=None,
        days=None,
        gain_since_entry=None,
        danger=True,
        boiling=None,
        champagne=None,
    )
    previous = [_attention_row("00700", right_side=True)]

    attention = market_trend.build_option_attention(
        [candidate, holding], previous, {"00700": "HOLD"}, "HK", "phillips"
    )

    assert len(attention) == 1
    assert attention[0]["symbol"] == "00700"
    assert attention[0]["name"] is None
    assert attention[0]["danger"]["current"] is True
    assert attention[0]["temperature"]["current"] is None
    assert attention[0]["strength_change"]["current"] is None
    assert attention[0]["global_strength"] is None

    first_entries = market_trend.build_option_attention(
        [
            _attention_row("RIGHT", right_side=True, danger=False, boiling=None),
            _attention_row("LEFT", right_side=False, danger=False),
            _attention_row("RISK", right_side=True, danger=True),
        ],
        [],
        {},
        "US",
        "tiger",
    )
    assert [item["symbol"] for item in first_entries] == ["RIGHT"]
    assert first_entries[0]["boiling"] == {
        "previous": None,
        "current": None,
        "changed": False,
    }


def test_previous_attention_rows_use_strict_dates_and_one_time_tiger_baseline(
    tmp_path: Path,
) -> None:
    paths = market_paths(tmp_path / "data", tmp_path / "reports", "US")
    paths.root.mkdir(parents=True)
    baseline = {
        "as_of_date": "2026-07-15",
        "signal_snapshots": {"candidates": [_attention_row("BASE")]},
    }
    (paths.root / "attention_baseline.json").write_text(
        json.dumps(baseline), encoding="utf-8"
    )

    assert [
        row["symbol"]
        for row in market_trend._previous_attention_rows(
            paths, current_as_of_date="2026-07-16", market="US"
        )
    ] == ["BASE"]

    paths.reports.mkdir(parents=True)
    for filename, as_of_date, symbol in (
        ("2026-07-14.json", "2026-07-14", "OLDER"),
        ("2026-07-15.json", "2026-07-15", "PRIOR"),
        ("2026-07-15-r2.json", "2026-07-15", "REVISION2"),
        ("2026-07-15-r10.json", "2026-07-15", "REVISION10"),
        ("2026-07-16.json", "2026-07-16", "SAME"),
        ("2026-07-16-r1.json", "2026-07-16", "REVISION"),
    ):
        (paths.reports / filename).write_text(
            json.dumps(
                {
                    "as_of_date": as_of_date,
                    "signal_snapshots": {
                        "candidates": [_attention_row(symbol)],
                        "holdings": {},
                    },
                }
            ),
            encoding="utf-8",
        )

    rows = market_trend._previous_attention_rows(
        paths, current_as_of_date="2026-07-16", market="US"
    )
    assert [row["symbol"] for row in rows] == ["REVISION10"]

    for path in paths.reports.glob("*.json"):
        path.unlink()
    (paths.reports / "malformed.json").write_text("{", encoding="utf-8")
    assert market_trend._previous_attention_rows(
        paths, current_as_of_date="2026-07-16", market="US"
    ) == []


@pytest.mark.parametrize(
    ("market", "section", "malformed_row", "older_symbol", "newer_symbol"),
    [
        ("US", "candidates", {}, "OLDER", "NEWER"),
        ("US", "candidates", {"symbol": "  "}, "OLDER", "NEWER"),
        ("US", "holdings", {"symbol": "600001"}, "OLDER", "NEWER"),
        ("HK", "holdings", {"symbol": "AAPL"}, "00001", "00002"),
    ],
)
def test_previous_attention_rows_skip_newest_report_with_invalid_symbol_row(
    tmp_path: Path,
    market: str,
    section: str,
    malformed_row: dict[str, object],
    older_symbol: str,
    newer_symbol: str,
) -> None:
    paths = market_paths(tmp_path / "data", tmp_path / "reports", market)
    paths.reports.mkdir(parents=True)
    for filename, as_of_date, symbol in (
        ("2026-07-14.json", "2026-07-14", older_symbol),
        ("2026-07-15.json", "2026-07-15", newer_symbol),
    ):
        snapshots: dict[str, object] = {
            "candidates": [_attention_row(symbol)],
            "holdings": {},
        }
        if filename == "2026-07-15.json":
            if section == "candidates":
                snapshots[section] = [*snapshots[section], malformed_row]
            else:
                snapshots[section] = {"malformed": malformed_row}
        (paths.reports / filename).write_text(
            json.dumps(
                {"as_of_date": as_of_date, "signal_snapshots": snapshots}
            ),
            encoding="utf-8",
        )

    rows = market_trend._previous_attention_rows(
        paths, current_as_of_date="2026-07-16", market=market
    )

    assert [row["symbol"] for row in rows] == [older_symbol]


def test_current_attention_rows_keep_valid_rows_when_one_symbol_is_invalid() -> None:
    rows = market_trend._attention_rows(
        {
            "candidates": [
                _attention_row("QQQ", right_side=True, danger=False),
                _attention_row("600001", right_side=True, danger=False),
            ]
        }
    )

    assert rows is not None
    assert [
        item["symbol"]
        for item in market_trend.build_option_attention(
            rows, [], {}, "US", "tiger"
        )
    ] == ["QQQ"]


def test_previous_attention_rows_reject_malformed_tiger_baseline(
    tmp_path: Path,
) -> None:
    paths = market_paths(tmp_path / "data", tmp_path / "reports", "US")
    paths.root.mkdir(parents=True)
    (paths.root / "attention_baseline.json").write_text(
        json.dumps(
            {
                "as_of_date": "2026-07-15",
                "signal_snapshots": {
                    "candidates": [_attention_row("VALID")],
                    "holdings": {"malformed": {"symbol": "600001"}},
                },
            }
        ),
        encoding="utf-8",
    )

    assert market_trend._previous_attention_rows(
        paths, current_as_of_date="2026-07-16", market="US"
    ) == []
