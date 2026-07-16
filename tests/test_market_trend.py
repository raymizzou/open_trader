from __future__ import annotations

import csv
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from open_trader.a_share_trend import AShareTrendRunResult
from open_trader.daily_premarket import DailyPremarketConfig
from open_trader.market_trend import (
    MarketHoliday,
    _refresh_futu_account,
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


def test_load_market_account_uses_full_native_account_but_only_managed_positions(
    tmp_path: Path,
) -> None:
    write_details(
        tmp_path / "data",
        "2026-07-15",
        positions=[
            {
                "statement_id": "2026-07-15-futu-live", "broker": "futu",
                "market": "US", "asset_class": "etf", "symbol": "VIXY",
                "name": "VIX Short", "currency": "USD", "quantity": "10",
                "cost_price": "40", "market_value": "500",
            },
            {
                "statement_id": "2026-07-15-futu-live", "broker": "futu",
                "market": "US", "asset_class": "stock", "symbol": "AAPL",
                "name": "Apple", "currency": "USD", "quantity": "2",
                "cost_price": "200", "market_value": "420",
            },
        ],
        cash=[{
            "statement_id": "2026-07-15-futu-live", "broker": "futu",
            "currency": "USD", "cash_balance": "1000", "available_balance": "800",
        }],
    )

    account = load_market_account(
        data_dir=tmp_path / "data",
        broker="futu",
        market="US",
        expected_date="2026-07-15",
        managed_symbols={"VIXY"},
    )

    assert account.source_date == "2026-07-15"
    assert account.fresh is True
    assert account.net_value == Decimal("1920")
    assert account.available_cash == Decimal("800")
    assert [item.symbol for item in account.positions] == ["VIXY"]


def test_us_account_refreshes_directly_from_futu_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    calls: list[object] = []

    class Client:
        def __init__(self, *, host: str, port: int) -> None:
            calls.append(("connect", host, port))

        def fetch_snapshot(self) -> object:
            calls.append("fetch")
            return "snapshot"

        def close(self) -> None:
            calls.append("close")

    def sync(**kwargs: object) -> None:
        calls.append(("sync", kwargs))

    monkeypatch.setattr("open_trader.market_trend.FutuAccountClient", Client)
    monkeypatch.setattr("open_trader.market_trend.sync_futu_portfolio", sync)

    _refresh_futu_account(cfg, "2026-07-15")

    assert calls[:3] == [("connect", "127.0.0.1", 11111), "fetch", "close"]
    assert calls[3][0] == "sync"
    assert calls[3][1]["snapshot"] == "snapshot"
    assert calls[3][1]["update_latest"] is True


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
    assert market_paths(Path("data"), Path("reports"), "US").root.name == "trend_us_futu"
    assert market_paths(Path("data"), Path("reports"), "HK").root.name == "trend_hk_phillips"


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
            "【富途｜美股趋势报告生成失败｜2026-07-15】",
            "原因：趋势数据在截止时间前仍未更新\n"
            "现在做：确认 Trend Animals 与富途账户状态后手动重跑富途报告\n\n"
            "报告未生成，请勿依据旧报告交易。",
        )
    ]
    ledger = cfg.data_dir / "trend_us_futu/daily_delivery/2026-07-15.json"
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
                {"field": field, "priceCost": "0"}
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


def test_us_report_api_fact_uses_unified_trend_fields(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write_details(
        cfg.data_dir,
        "2026-07-15",
        positions=[{
            "statement_id": "2026-07-15-futu-live", "broker": "futu",
            "market": "US", "asset_class": "stock", "symbol": "AAPL",
            "name": "Apple", "currency": "USD", "quantity": "1",
            "cost_price": "200", "market_value": "210",
        }],
        cash=[{
            "statement_id": "2026-07-15-futu-live", "broker": "futu",
            "currency": "USD", "cash_balance": "1000", "available_balance": "1000",
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
            return [{"tmId": 1, "tickerSymbol": "VIXY.US", "asOfDate": expected_date}]

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            return [
                {"field": field, "priceCost": "0"}
                for field in UNIFIED_TREND_FIELDS
            ]

        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            assert kwargs["fields"] == UNIFIED_TREND_FIELDS
            return [{
                "tmId": 1, "tickerName": "VIX Short", "tickerSymbol": "VIXY.US",
                "asset": "美股", "asOfDate": "2026-07-14", "tradableFlag": True,
                "industryName": "ETF", "amount1d": "2", "isTrendRightSide": True,
                "daysSinceTrendEntry": 3, "trendStrengthLocalCurr": "96",
                "stopwinFlagByDangerSignal": False,
            }]

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

    result = run_market_trend_report(
        config=cfg,
        market="US",
        run_date="2026-07-15",
        notifier=NullNotifier(),
        api_factory=Api,
        quote_factory=Quote,
        account_refresher=lambda *args: None,
    )

    assert result.json_path is not None
    payload = __import__("json").loads(result.json_path.read_text(encoding="utf-8"))
    assert (
        f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows=1 "
        "cache=client-managed"
    ) in payload["api_facts"]


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
