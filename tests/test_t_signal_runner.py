from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from open_trader.portfolio import PORTFOLIO_FIELDNAMES
from open_trader.t_signal import TMarketFacts, apply_ai_interpretation
from open_trader.t_signal_runner import run_t_signal_watch_once
from open_trader.t_signal_store import load_t_signals_cache
from open_trader.notifications import NullNotifier


def write_portfolio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "sort_group": "4",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "Volatility ETF",
                "currency": "USD",
                "total_quantity": "100",
                "avg_cost_price": "45.00",
                "last_price": "48.50",
                "market_value": "4850.00",
                "cost_value": "4500.00",
                "unrealized_pnl": "350.00",
                "unrealized_pnl_pct": "7.78%",
                "fx_source": "fixture",
                "fx_date": "2026-05-31",
                "fx_to_hkd": "7.8",
                "market_value_hkd": "37830.00",
                "cost_value_hkd": "35100.00",
                "portfolio_weight_hkd": "97.80%",
                "brokers": "futu",
                "accounts": "main",
                "ai_eligible": "true",
                "analysis_symbol": "VIXY",
                "risk_flag": "normal",
                "confidence": "high",
                "notes": "",
            }
        )


class FakeMarketDataClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.closed = False

    def get_market_facts(
        self,
        *,
        run_date: str,
        market: str,
        symbol: str,
        futu_symbol: str,
        name: str,
        session_phase: str,
        updated_at: str,
    ) -> TMarketFacts:
        self.calls.append(
            {
                "run_date": run_date,
                "market": market,
                "symbol": symbol,
                "futu_symbol": futu_symbol,
                "session_phase": session_phase,
            }
        )
        return TMarketFacts(
            run_date=run_date,
            market=market,
            symbol=symbol,
            futu_symbol=futu_symbol,
            name=name,
            session_phase=session_phase,
            updated_at=updated_at,
            last_price=Decimal("48.50"),
            day_change_pct=Decimal("-1.20"),
            vwap=Decimal("49.10"),
            ma_1m=Decimal("48.55"),
            ma_5m=Decimal("48.85"),
            day_low=Decimal("48.00"),
            day_high=Decimal("50.20"),
            bid=Decimal("48.49"),
            ask=Decimal("48.50"),
            bid_depth=Decimal("5000"),
            ask_depth=Decimal("4700"),
            rsi_5m=Decimal("34"),
            volume_ratio_5m=Decimal("1.30"),
        )

    def close(self) -> None:
        self.closed = True


class HoldMarketDataClient(FakeMarketDataClient):
    def get_market_facts(self, **kwargs) -> TMarketFacts:
        facts = super().get_market_facts(**kwargs)
        return facts.with_field("last_price", Decimal("49.10"))


class SellMarketDataClient(FakeMarketDataClient):
    def get_market_facts(self, **kwargs) -> TMarketFacts:
        facts = super().get_market_facts(**kwargs)
        return (
            facts.with_field("last_price", Decimal("49.00"))
            .with_field("vwap", Decimal("48.50"))
            .with_field("ma_1m", Decimal("49.20"))
            .with_field("rsi_5m", Decimal("68"))
        )


class FailingMarketDataClient(FakeMarketDataClient):
    def get_market_facts(self, **kwargs) -> TMarketFacts:
        raise RuntimeError("OpenD connection failed")


class PassthroughInterpreter:
    def interpret(self, signal):
        return signal


class RejectingInterpreter:
    def interpret(self, signal):
        return apply_ai_interpretation(signal, "{}")


class CapturingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class FailingNotifier:
    def notify(self, title: str, message: str) -> None:
        raise RuntimeError("Feishu webhook failed")


def fixed_now() -> datetime:
    return datetime.fromisoformat("2026-07-02T22:32:00+08:00")


def test_t_signal_runner_writes_artifact_and_sends_once(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)
    client = FakeMarketDataClient()
    notifier = CapturingNotifier()

    result = run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=client,
        interpreter=PassthroughInterpreter(),
        notifier=notifier,
        now_fn=fixed_now,
    )

    assert result.signal_count == 1
    assert result.notified_count == 1
    assert client.closed is True
    assert notifier.messages[0][0] == "Open Trader｜做T提醒｜US.VIXY｜买入做T"
    assert "动作：买入做T" in notifier.messages[0][1]
    assert "比例：15%" in notifier.messages[0][1]
    assert "状态：盘中有效，等待执行确认" in notifier.messages[0][1]
    assert "结论：" in notifier.messages[0][1]
    assert "依据：\n1. 价格低于 VWAP 后回收，出现低吸做T信号。" in notifier.messages[0][1]
    assert "时间：2026-07-02 22:32:00" in notifier.messages[0][1]
    assert "BUY_T" not in notifier.messages[0][0]
    assert "BUY_T" not in notifier.messages[0][1]
    cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    record = cache["records"][0]
    assert record["action"] == "BUY_T"
    assert record["suggested_ratio"] == "15"
    assert record["notification"]["notified"] is True
    assert record["notification"]["should_notify"] is False
    assert record["timeline"][-1]["event_type"] == "notification_sent"


def test_t_signal_notification_uses_structured_chinese_template(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)
    notifier = CapturingNotifier()

    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=SellMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=notifier,
        now_fn=fixed_now,
    )

    title, message = notifier.messages[0]
    assert title == "Open Trader｜做T提醒｜US.VIXY｜卖出做T"
    assert message == (
        "动作：卖出做T\n"
        "比例：15%\n"
        "状态：盘中有效，等待执行确认\n"
        "\n"
        "结论：\n"
        "触发卖出做T，建议比例 15%。\n"
        "\n"
        "依据：\n"
        "1. 价格高于 VWAP 后受压，出现高抛做T信号。\n"
        "2. 5分钟 RSI 处于偏高区间，回落信号更明确。\n"
        "3. 5分钟量比放大，价格受压具备成交配合。\n"
        "\n"
        "时间：2026-07-02 22:32:00"
    )
    assert "SELL_T" not in title
    assert "SELL_T" not in message


def test_t_signal_runner_does_not_mark_null_notifier_as_sent(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)

    result = run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=NullNotifier(),
        now_fn=fixed_now,
    )

    assert result.signal_count == 1
    assert result.notified_count == 0
    cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    record = cache["records"][0]
    assert record["notification"]["notified"] is False
    assert record["notification"]["should_notify"] is True
    assert record["notification"]["last_notified_at"] == ""
    assert record["notification"]["last_notified_dedupe_key"] == ""
    assert record["notification"]["last_attempted_dedupe_key"] == ""
    assert record["timeline"][-1]["event_type"] == "signal_created"


def test_t_signal_runner_suppresses_duplicate_notification(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)
    first_notifier = CapturingNotifier()

    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=first_notifier,
        now_fn=fixed_now,
    )
    second_notifier = CapturingNotifier()
    second = run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=second_notifier,
        now_fn=fixed_now,
    )

    assert second.notified_count == 0
    assert second_notifier.messages == []
    cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    assert cache["records"][0]["timeline"][-1]["event_type"] == "notification_suppressed"


def test_t_signal_runner_writes_error_artifact_when_market_data_fails(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)
    client = FailingMarketDataClient()

    result = run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=client,
        interpreter=PassthroughInterpreter(),
        notifier=CapturingNotifier(),
        now_fn=fixed_now,
    )

    assert result.signal_count == 1
    assert result.notified_count == 0
    assert client.closed is True
    cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    record = cache["records"][0]
    assert record["action"] == "REVIEW"
    assert record["status"] == "error"
    assert record["notification"]["should_notify"] is False
    assert "OpenD connection failed" in record["error"]


def test_t_signal_runner_persists_notification_failure_without_traceback(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)

    result = run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=FailingNotifier(),
        now_fn=fixed_now,
    )

    assert result.signal_count == 1
    assert result.notified_count == 0
    cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    record = cache["records"][0]
    assert record["action"] == "BUY_T"
    assert record["status"] == "review"
    assert record["notification"]["notified"] is False
    assert record["notification"]["should_notify"] is False
    assert record["notification"]["last_attempted_dedupe_key"] == record["notification"]["dedupe_key"]
    assert record["timeline"][-1]["event_type"] == "notification_failed"
    assert "Feishu webhook failed" in record["error"]


def test_t_signal_runner_keeps_dedupe_across_hold_between_same_buy_signal(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)
    first_notifier = CapturingNotifier()
    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=first_notifier,
        now_fn=fixed_now,
    )

    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=HoldMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=CapturingNotifier(),
        now_fn=fixed_now,
    )
    hold_cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    hold_notification = hold_cache["records"][0]["notification"]
    assert hold_cache["records"][0]["action"] == "HOLD"
    assert hold_notification["last_notified_dedupe_key"].endswith("|BUY_T|15")

    third_notifier = CapturingNotifier()
    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=third_notifier,
        now_fn=fixed_now,
    )

    assert third_notifier.messages == []
    cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    assert cache["records"][0]["timeline"][-1]["event_type"] == "notification_suppressed"


def test_t_signal_runner_keeps_dedupe_across_ai_review_between_same_buy_signal(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path)
    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=CapturingNotifier(),
        now_fn=fixed_now,
    )

    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=RejectingInterpreter(),
        notifier=CapturingNotifier(),
        now_fn=fixed_now,
    )
    review_cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    review_notification = review_cache["records"][0]["notification"]
    assert review_cache["records"][0]["action"] == "REVIEW"
    assert review_notification["last_notified_dedupe_key"].endswith("|BUY_T|15")

    notifier = CapturingNotifier()
    run_t_signal_watch_once(
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        session_phase="regular",
        market_data_client=FakeMarketDataClient(),
        interpreter=PassthroughInterpreter(),
        notifier=notifier,
        now_fn=fixed_now,
    )

    assert notifier.messages == []
    cache = load_t_signals_cache(tmp_path / "data/latest/US/t_signals.json")
    assert cache["records"][0]["timeline"][-1]["event_type"] == "notification_suppressed"
