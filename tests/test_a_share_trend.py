from __future__ import annotations

import csv
import json
import os
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from open_trader.a_share_trend import (
    AccountPosition,
    AccountSnapshot,
    CandidateInput,
    HoldingSnapshot,
    TrendReport,
    atr14,
    build_candidate_list,
    build_report,
    estimate_buy_actions,
    evaluate_candidate,
    load_eastmoney_account,
    load_protection_state,
    load_watch_events,
    render_markdown,
    update_protection_line,
    write_protection_state,
    write_frozen_report,
)
from open_trader.kline_technical_facts import DailyKlineBar


SHANGHAI = ZoneInfo("Asia/Shanghai")


def candidate(
    symbol: str,
    *,
    strength: str | None = "96",
    days: int | None = 3,
    amount: str | None = "2",
    right_side: object = True,
    tradable: object = True,
    danger: object = False,
    exchange: str = "SH",
    name: str | None = None,
    close: str = "10",
    atr: str | None = "0.5",
    industry: str = "电力",
) -> CandidateInput:
    return CandidateInput(
        tm_id=int(symbol),
        symbol=symbol,
        exchange=exchange,
        name=name or f"股票{symbol}",
        asset="A股",
        industry=industry,
        as_of_date="2026-07-14",
        tradable=tradable,
        amount=None if amount is None else Decimal(amount),
        right_side=right_side,
        days=days,
        strength=None if strength is None else Decimal(strength),
        danger=danger,
        close=Decimal(close),
        atr=None if atr is None else Decimal(atr),
    )


def bars(count: int = 15, *, close: float = 10, low: float = 9) -> list[DailyKlineBar]:
    return [
        DailyKlineBar(
            date=f"2026-06-{index + 1:02d}",
            open=close,
            high=close + 1,
            low=low,
            close=close,
            volume=100,
        )
        for index in range(count)
    ]


def account(*symbols: str, fresh: bool = True) -> AccountSnapshot:
    return AccountSnapshot(
        source_date="2026-07-14" if fresh else "2026-07-13",
        fresh=fresh,
        net_value=Decimal("676549.55"),
        available_cash=Decimal("405219.55"),
        positions=tuple(
            AccountPosition(symbol, f"股票{symbol}", "stock", Decimal("100"), None)
            for symbol in symbols
        ),
        exceptions=(),
    )


def holding(
    symbol: str,
    *,
    right_side: bool | None = True,
    danger: bool | None = False,
    boiling: bool = False,
    champagne: bool = False,
    industry: str = "电力",
) -> HoldingSnapshot:
    return HoldingSnapshot(
        tm_id=int(symbol),
        symbol=symbol,
        exchange="SH",
        name=f"股票{symbol}",
        as_of_date="2026-07-14",
        right_side=right_side,
        danger=danger,
        boiling=boiling,
        champagne=champagne,
        industry=industry,
    )


def report(*, candidates: tuple[CandidateInput, ...] = ()) -> TrendReport:
    return build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=candidates,
        holding_snapshots={},
        bars_by_symbol={},
        api_facts=("A股数据日期：2026-07-14",),
        data_sources=("Trend Animals", "Futu 日 K", "portfolio.csv"),
        estimated_api_cost=Decimal("1.20"),
        actual_api_cost=Decimal("1.00"),
    )


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def portfolio_row(**overrides: str) -> dict[str, str]:
    row = {
        "market": "CN",
        "asset_class": "stock",
        "symbol": "600001",
        "name": "股票600001",
        "currency": "CNY",
        "total_quantity": "100",
        "avg_cost_price": "9.5",
        "market_value": "1000",
        "brokers": "eastmoney",
    }
    row.update(overrides)
    return row


def test_candidates_filter_then_sort_deterministically() -> None:
    rows = [
        candidate("600004", strength="95", days=2, amount="3"),
        candidate("600003", strength="96", days=4, amount="2"),
        candidate("600002", strength="96", days=3, amount="1"),
        candidate("600001", strength="96", days=3, amount="2"),
        candidate("600005", strength="90"),
        candidate("600006", danger=True),
    ]

    decisions = build_candidate_list(rows, held_symbols={"600003"})

    assert [item.symbol for item in decisions.eligible[:10]] == [
        "600001",
        "600002",
        "600004",
    ]
    assert decisions.excluded["600003"] == ["already_held"]
    assert decisions.excluded["600005"] == ["strength_not_above_90"]
    assert decisions.excluded["600006"] == ["danger_signal"]


@pytest.mark.parametrize("name", ["ST示例", "*ST示例", "退市示例"])
def test_candidate_excludes_special_treatment_and_delisting_names(name: str) -> None:
    decisions = build_candidate_list([candidate("600001", name=name)], held_symbols=set())
    assert decisions.excluded["600001"] == ["excluded_security"]


def test_candidate_preserves_bj_suffix_for_exclusion() -> None:
    row = {
        "tmId": 920000,
        "tickerSymbol": "920000.BJ",
        "tickerName": "示例",
        "asset": "A股",
        "industryName": "工业",
        "asOfDate": "2026-07-14",
        "tradableFlag": True,
        "amount1d": "2",
        "isTrendRightSide": True,
        "daysSinceTrendEntry": 3,
        "trendStrengthLocalCurr": "96",
        "stopwinFlagByDangerSignal": False,
    }

    item = evaluate_candidate(row, bars())

    assert (item.symbol, item.exchange) == ("920000", "BJ")
    assert build_candidate_list([item], held_symbols=set()).excluded["920000"] == [
        "excluded_security"
    ]


def test_candidate_normalizes_returned_exchange_without_inference() -> None:
    item = evaluate_candidate(
        {
            "tmId": 1,
            "tickerSymbol": "600000.SZ",
            "tickerName": "示例",
            "asset": "A股",
            "industryName": "工业",
            "asOfDate": "2026-07-14",
            "tradableFlag": True,
            "amount1d": "2",
            "isTrendRightSide": True,
            "daysSinceTrendEntry": 3,
            "trendStrengthLocalCurr": "96",
            "stopwinFlagByDangerSignal": False,
        },
        bars(),
    )
    assert (item.symbol, item.exchange) == ("600000", "SZ")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"right_side": None}, "right_side_not_true"),
        ({"tradable": None}, "not_tradable"),
        ({"danger": None}, "danger_unknown"),
        ({"days": 10}, "right_side_days_not_below_10"),
        ({"amount": "0.999"}, "amount_below_1"),
        ({"strength": "90"}, "strength_not_above_90"),
    ],
)
def test_candidate_rejects_exact_hard_gate_boundaries(
    overrides: dict[str, object], reason: str
) -> None:
    item = candidate("600001", **overrides)  # type: ignore[arg-type]
    assert reason in build_candidate_list([item], held_symbols=set()).excluded["600001"]


def test_candidate_accepts_days_amount_and_strength_boundaries() -> None:
    item = candidate("600001", days=9, amount="1", strength="90.001")
    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


def test_candidate_kline_failure_is_an_atr_exclusion() -> None:
    item = evaluate_candidate(
        {
            "tmId": 1,
            "tickerSymbol": "600001.SH",
            "tickerName": "示例",
            "asset": "A股",
            "industryName": "工业",
            "asOfDate": "2026-07-14",
            "tradableFlag": True,
            "amount1d": "2",
            "isTrendRightSide": True,
            "daysSinceTrendEntry": 3,
            "trendStrengthLocalCurr": "96",
            "stopwinFlagByDangerSignal": False,
        },
        None,
    )
    assert item.atr is None
    assert build_candidate_list([item], held_symbols=set()).excluded["600001"] == [
        "atr_unavailable"
    ]


def test_invalid_candidate_kline_is_an_atr_exclusion() -> None:
    invalid = bars()
    invalid[-1] = replace(invalid[-1], close=float("nan"))
    item = evaluate_candidate(
        {
            "tmId": 1,
            "tickerSymbol": "600001.SH",
            "tickerName": "示例",
            "asset": "A股",
            "industryName": "工业",
            "asOfDate": "2026-07-14",
            "tradableFlag": True,
            "amount1d": "2",
            "isTrendRightSide": True,
            "daysSinceTrendEntry": 3,
            "trendStrengthLocalCurr": "96",
            "stopwinFlagByDangerSignal": False,
        },
        invalid,
    )
    assert item.atr is None
    assert build_candidate_list([item], held_symbols=set()).excluded["600001"] == [
        "atr_unavailable"
    ]


def test_atr14_requires_fifteen_valid_bars() -> None:
    assert atr14(bars(14)) is None
    assert atr14(bars(15)) == Decimal("2")


def test_buy_actions_use_one_percent_cash_slots_and_round_lots() -> None:
    ranked = [candidate("600001"), candidate("600002")]

    actions = estimate_buy_actions(
        ranked=ranked,
        account_fresh=True,
        net_value=Decimal("676549.55"),
        available_cash=Decimal("7000"),
        current_position_count=9,
    )

    assert [
        (item.symbol, item.target_amount, item.estimated_shares) for item in actions
    ] == [("600001", Decimal("6765.50"), 600)]


def test_stale_account_has_no_formal_buys() -> None:
    assert (
        estimate_buy_actions(
            ranked=[candidate("600001")],
            account_fresh=False,
            net_value=Decimal("676549.55"),
            available_cash=Decimal("405219.55"),
            current_position_count=5,
        )
        == []
    )


def test_more_than_ten_positions_has_no_formal_buys() -> None:
    assert (
        estimate_buy_actions(
            ranked=[candidate("600001")],
            account_fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            current_position_count=11,
        )
        == []
    )


def test_unaffordable_candidate_does_not_consume_cash_or_slot() -> None:
    actions = estimate_buy_actions(
        ranked=[
            candidate("600001", close="20"),
            candidate("600002", close="1"),
        ],
        account_fresh=True,
        net_value=Decimal("10000"),
        available_cash=Decimal("600"),
        current_position_count=9,
    )
    assert [(item.symbol, item.target_amount, item.estimated_shares) for item in actions] == [
        ("600002", Decimal("100.00"), 100)
    ]


def test_formal_buys_do_not_promote_beyond_displayed_top_ten() -> None:
    ranked = [candidate(f"6000{index:02d}", close="100") for index in range(1, 11)]
    ranked.append(candidate("600011", close="1"))
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=replace(account(), net_value=Decimal("10000")),
        candidates=ranked,
        holding_snapshots={},
        bars_by_symbol={},
    )
    assert len(built.candidates) == 10
    assert built.buy_actions == ()


def test_duplicate_pool_members_produce_one_candidate_and_one_buy() -> None:
    item = candidate("600001")
    decisions = build_candidate_list([item, item], held_symbols=set())
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(item, item),
        holding_snapshots={},
        bars_by_symbol={},
    )
    assert decisions.eligible == (item,)
    assert [action.symbol for action in built.buy_actions] == ["600001"]


def test_stale_candidate_is_excluded_from_formal_buys() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(replace(candidate("600001"), as_of_date="2026-07-13"),),
        holding_snapshots={},
        bars_by_symbol={},
    )
    assert built.buy_actions == ()
    assert built.excluded["600001"] == ["data_date_mismatch"]


def test_overheat_line_uses_prior_five_lows_and_never_decreases() -> None:
    assert update_protection_line(
        old_line=Decimal("27.31"),
        boiling=True,
        champagne=False,
        prior_five_lows=[
            Decimal(value) for value in ["28", "29", "27.8", "28.5", "29"]
        ],
    ) == Decimal("27.80")
    assert update_protection_line(
        old_line=Decimal("28.20"),
        boiling=True,
        champagne=False,
        prior_five_lows=[Decimal("27.80")] * 5,
    ) == Decimal("28.20")


def test_holding_kline_failure_preserves_old_line() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": None},
        prior_state={
            "schema_version": 1,
            "positions": {
                "600001": {
                    "initial_line": "8",
                    "active_line": "8.5",
                    "atr14": "1",
                    "updated_for": "2026-07-13",
                }
            },
        },
    )
    assert built.holdings[0].action == "HOLD"
    assert built.holdings[0].active_line == Decimal("8.5")


def test_invalid_holding_kline_preserves_old_line() -> None:
    invalid = bars()
    invalid[-1] = replace(invalid[-1], low=float("nan"))
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": invalid},
        prior_state={
            "schema_version": 1,
            "positions": {
                "600001": {
                    "initial_line": "8",
                    "active_line": "8.5",
                    "atr14": "1",
                    "updated_for": "2026-07-13",
                }
            },
        },
    )
    assert (built.holdings[0].action, built.holdings[0].active_line) == (
        "HOLD",
        Decimal("8.5"),
    )


def test_holding_kline_failure_without_old_line_requires_review() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": None},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "MANUAL_REVIEW",
        "holding_kline_unavailable",
    )


def test_unknown_holding_signal_keeps_exact_precedence_without_kline() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001", right_side=None)},
        bars_by_symbol={"600001": None},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "MANUAL_REVIEW",
        "holding_signal_unknown",
    )


def test_stale_holding_snapshot_is_an_unknown_signal() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": replace(holding("600001"), as_of_date="2026-07-13")
        },
        bars_by_symbol={"600001": bars()},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "MANUAL_REVIEW",
        "holding_signal_unknown",
    )


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (holding("600001", danger=True), "danger_signal"),
        (holding("600001", right_side=False), "left_trend_right_side"),
    ],
)
def test_holding_danger_and_left_trend_force_full_sell(
    snapshot: HoldingSnapshot, reason: str
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": snapshot},
        bars_by_symbol={"600001": None},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == ("SELL_ALL", reason)


def test_all_current_holdings_are_checked_outside_candidate_pools() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001", "600002"),
        candidates=(),
        holding_snapshots={
            "600001": holding("600001"),
            "600002": holding("600002", danger=True),
        },
        bars_by_symbol={"600001": bars(), "600002": bars()},
    )
    assert [(item.symbol, item.action) for item in built.holdings] == [
        ("600001", "HOLD"),
        ("600002", "SELL_ALL"),
    ]


def test_current_holding_without_state_becomes_historical_with_close_based_line() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": bars()},
    )
    decision = built.holdings[0]
    assert decision.historical is True
    assert (decision.initial_line, decision.active_line) == (Decimal("6"), Decimal("6"))
    assert built.protection_state["positions"]["600001"]["active_line"] == "6"


def test_tracking_activation_persists_after_overheat_signal_clears() -> None:
    prior = {
        "schema_version": 1,
        "positions": {
            "600001": {
                "initial_line": "10",
                "active_line": "10",
                "atr14": "1",
                "updated_for": "2026-07-13",
            }
        },
    }
    activated = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": bars(low=9)},
        prior_state=prior,
    )
    advanced = build_report(
        as_of_date="2026-07-15",
        execution_date="2026-07-16",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": replace(
                holding("600001", boiling=False), as_of_date="2026-07-15"
            )
        },
        bars_by_symbol={"600001": bars(close=12, low=11)},
        prior_state=activated.protection_state,
    )
    assert activated.protection_state["positions"]["600001"]["tracking_active"] is True
    assert advanced.holdings[0].active_line == Decimal("11")


def test_unknown_signal_keeps_line_after_tracking_activation() -> None:
    built = build_report(
        as_of_date="2026-07-15",
        execution_date="2026-07-16",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": None},
        bars_by_symbol={"600001": bars(close=12, low=11)},
        prior_state={
            "schema_version": 1,
            "positions": {
                "600001": {
                    "initial_line": "10",
                    "active_line": "10",
                    "atr14": "1",
                    "tracking_active": True,
                    "position_started_for": "2026-07-14",
                    "updated_for": "2026-07-14",
                }
            },
        },
    )
    assert (built.holdings[0].reason, built.holdings[0].active_line) == (
        "holding_signal_unknown",
        Decimal("10"),
    )


def test_triggered_protection_replays_until_position_disappears() -> None:
    event = {"symbol": "600001", "event_type": "protection_triggered"}
    current = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": None},
        bars_by_symbol={},
        prior_state={
            "schema_version": 1,
            "positions": {
                "600001": {
                    "initial_line": "8",
                    "active_line": "8",
                    "atr14": "1",
                    "updated_for": "2026-07-13",
                }
            },
        },
        watch_events=(event,),
    )
    gone = build_report(
        as_of_date="2026-07-15",
        execution_date="2026-07-16",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        prior_state=current.protection_state,
        watch_events=(event,),
    )
    assert (current.holdings[0].action, current.holdings[0].reason) == (
        "SELL_ALL",
        "protection_line_already_triggered",
    )
    assert gone.protection_state == {"schema_version": 1, "positions": {}}


def test_old_trigger_does_not_poison_a_later_reentry() -> None:
    event = {
        "symbol": "600001",
        "event_type": "protection_triggered",
        "trading_date": "2026-07-15",
    }
    repurchased = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": replace(holding("600001"), as_of_date="2026-07-16")
        },
        bars_by_symbol={"600001": bars()},
        prior_state={"schema_version": 1, "positions": {}},
        watch_events=(event,),
    )
    following_day = build_report(
        as_of_date="2026-07-17",
        execution_date="2026-07-20",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": replace(holding("600001"), as_of_date="2026-07-17")
        },
        bars_by_symbol={"600001": bars()},
        prior_state=repurchased.protection_state,
        watch_events=(event,),
    )
    assert (following_day.holdings[0].action, following_day.holdings[0].reason) == (
        "HOLD",
        "trend_intact",
    )


def test_protection_state_round_trips_and_jsonl_trigger_replays(tmp_path: Path) -> None:
    state_path = tmp_path / "data" / "trend_a_share" / "protection_state.json"
    events_path = state_path.with_name("watch_events.jsonl")
    state = {
        "schema_version": 1,
        "positions": {
            "600001": {
                "initial_line": "8",
                "active_line": "8.5",
                "atr14": "1",
                "updated_for": "2026-07-13",
            }
        },
    }
    write_protection_state(state_path, state)
    events_path.write_text(
        json.dumps({"symbol": "600001", "event_type": "protection_triggered"})
        + "\n",
        encoding="utf-8",
    )

    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": None},
        bars_by_symbol={},
        prior_state=load_protection_state(state_path),
        watch_events=load_watch_events(events_path),
    )

    assert built.holdings[0].reason == "protection_line_already_triggered"
    assert load_protection_state(state_path) == state


def test_load_account_uses_only_exact_eastmoney_rows_and_keeps_exceptions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [
            portfolio_row(symbol="600001", market_value="1000"),
            portfolio_row(
                market="CASH",
                asset_class="cash",
                symbol="CNY_CASH",
                name="人民币现金",
                total_quantity="500",
                market_value="500",
            ),
            portfolio_row(symbol="600002", brokers="futu", market_value="9999"),
            portfolio_row(
                symbol="110001",
                name="可转债",
                asset_class="bond",
                market_value="200",
            ),
        ],
    )
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(path, (timestamp, timestamp))

    snapshot = load_eastmoney_account(path, expected_date="2026-07-14")

    assert snapshot.fresh is True
    assert (snapshot.net_value, snapshot.available_cash) == (
        Decimal("1700"),
        Decimal("500"),
    )
    assert [item.symbol for item in snapshot.positions] == ["600001"]
    assert any("110001" in exception for exception in snapshot.exceptions)


def test_load_account_marks_stale_mtime(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [portfolio_row()])
    timestamp = datetime(2026, 7, 13, 23, 59, tzinfo=SHANGHAI).timestamp()
    os.utime(path, (timestamp, timestamp))
    snapshot = load_eastmoney_account(path, expected_date="2026-07-14")
    assert (snapshot.source_date, snapshot.fresh) == ("2026-07-13", False)


def test_load_account_rejects_mixed_eastmoney_broker_row(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [portfolio_row(brokers="futu;eastmoney")])
    with pytest.raises(ValueError, match="mixes Eastmoney"):
        load_eastmoney_account(path, expected_date="2026-07-14")


def test_markdown_separates_source_facts_from_strategy_judgments() -> None:
    markdown = render_markdown(report(candidates=(candidate("600001"),)))
    assert markdown.index("## API 原始事实") < markdown.index("## 策略纪律判断")
    assert "A股数据日期：2026-07-14" in markdown.split("## 策略纪律判断", 1)[0]
    assert "600001" in markdown.split("## 策略纪律判断", 1)[1]


def test_industry_concentration_includes_slots_and_account_weight() -> None:
    snapshot = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("1000"),
        available_cash=Decimal("800"),
        positions=(
            AccountPosition(
                "600001",
                "股票600001",
                "stock",
                Decimal("100"),
                None,
                market_value=Decimal("200"),
            ),
        ),
        exceptions=(),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=snapshot,
        candidates=(),
        holding_snapshots={"600001": holding("600001", industry="电力")},
        bars_by_symbol={"600001": bars()},
    )
    assert "电力：当前持仓 1 个席位，当前仓位 20.00%" in render_markdown(built)


def test_no_action_report_uses_exact_cash_sentence() -> None:
    assert "现金也是有效仓位，本日无需交易。" in render_markdown(report())


def test_formal_buy_text_includes_window_estimates_target_and_line() -> None:
    markdown = render_markdown(report(candidates=(candidate("600001"),)))
    assert "09:30–10:00" in markdown
    assert "收盘价估算 600 股" in markdown
    assert "1% 目标金额 6765.50 元" in markdown
    assert "预计初始保护线 9.00" in markdown
    assert "按东方财富实时价格向下重算为 100 股整数倍且不得超过建议金额" in markdown


def test_candidate_row_shows_industry_slots_and_weight() -> None:
    markdown = render_markdown(report(candidates=(candidate("600001"),)))
    assert "行业 电力（已占 0 个席位，当前仓位 0.00%）" in markdown


def test_frozen_base_artifact_is_idempotent(tmp_path: Path) -> None:
    first = report()
    markdown_path, json_path = write_frozen_report(first, tmp_path)
    original_markdown = markdown_path.read_text(encoding="utf-8")
    original_json = json_path.read_text(encoding="utf-8")

    same_paths = write_frozen_report(
        replace(first, execution_date="2099-01-01"), tmp_path
    )

    assert same_paths == (markdown_path, json_path)
    assert markdown_path.read_text(encoding="utf-8") == original_markdown
    assert json_path.read_text(encoding="utf-8") == original_json
    assert json.loads(original_json)["execution_date"] == "2026-07-15"


def test_frozen_revisions_choose_first_free_pair(tmp_path: Path) -> None:
    base = report()
    assert write_frozen_report(base, tmp_path, revision=True)[0].name == "2026-07-14-r1.md"
    assert write_frozen_report(base, tmp_path, revision=True)[0].name == "2026-07-14-r2.md"
