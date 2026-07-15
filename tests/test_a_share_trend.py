from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import pytest
import open_trader.a_share_trend as trend_module
import open_trader.trend_delivery as trend_delivery_module

from open_trader.a_share_trend import (
    AShareTrendRunResult,
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
    render_trend_failure_text,
    render_trend_feishu_text,
    render_markdown,
    run_a_share_trend_report,
    update_protection_line,
    write_protection_state,
    write_frozen_report,
)
from open_trader.daily_premarket import DailyPremarketConfig, RunLock
from open_trader.futu_quote import FutuQuoteError
from open_trader.kline_technical_facts import DailyKlineBar
from open_trader.notifications import CompositeNotifier, FeishuWebhookNotifier, MacOSNotifier
from open_trader.trend_animals import TrendAnimalsError, TrendAnimalsLookupError


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
        name=f"股票{symbol}" if name is None else name,
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


@pytest.mark.parametrize("name", ["ST示例", "*ST示例", "示例ST", "退市示例"])
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


def test_candidate_infers_bj_exchange_without_suffix_for_exclusion() -> None:
    item = evaluate_candidate(
        {
            "tmId": 920000,
            "tickerSymbol": "920000",
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

    assert item.exchange == "BJ"
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
    ("ticker_symbol", "exchange"), [("159835", "SZ"), ("515120", "SH")]
)
def test_candidate_infers_exchange_when_api_omits_suffix(
    ticker_symbol: str, exchange: str
) -> None:
    item = evaluate_candidate(
        {
            "tmId": 1,
            "tickerSymbol": ticker_symbol,
            "tickerName": "示例ETF",
            "asset": "ETF基金",
            "industryName": "医药",
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

    assert (item.symbol, item.exchange) == (ticker_symbol, exchange)


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


@pytest.mark.parametrize(
    ("changes", "reason"),
    [({"name": ""}, "name_missing"), ({"asset": ""}, "asset_missing")],
)
def test_candidate_missing_identity_field_is_excluded(
    changes: dict[str, object], reason: str
) -> None:
    item = replace(candidate("600001"), **changes)

    assert reason in build_candidate_list([item], held_symbols=set()).excluded["600001"]


@pytest.mark.parametrize(
    ("asset", "exchange", "reason"),
    [
        ("港股", "SH", "unsupported_asset"),
        ("期货", "SH", "unsupported_asset"),
        ("stock", "SH", "unsupported_asset"),
        ("A股", "BJ", "excluded_security"),
        ("A股", "HK", "unsupported_exchange"),
    ],
)
def test_candidate_asset_and_exchange_fail_closed(
    asset: str, exchange: str, reason: str
) -> None:
    item = replace(candidate("600001", exchange=exchange), asset=asset)

    decision = build_candidate_list([item], held_symbols=set())

    assert decision.eligible == ()
    assert reason in decision.excluded["600001"]


@pytest.mark.parametrize("asset", ["A股", "ETF基金"])
def test_candidate_accepts_only_official_supported_assets(asset: str) -> None:
    item = replace(candidate("600001"), asset=asset)

    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


def test_candidate_accepts_days_amount_and_strength_boundaries() -> None:
    item = candidate("600001", days=9, amount="1", strength="90.001")
    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


@pytest.mark.parametrize(
    ("market", "ticker_symbol", "asset", "symbol", "exchange"),
    [
        ("US", "VIXY.US", "美股", "VIXY", "US"),
        ("HK", "700.HK", "港股", "00700", "HK"),
    ],
)
def test_candidate_supports_hk_us_symbols_and_market_assets(
    market: str,
    ticker_symbol: str,
    asset: str,
    symbol: str,
    exchange: str,
) -> None:
    item = evaluate_candidate(
        {
            "tmId": 1,
            "tickerSymbol": ticker_symbol,
            "tickerName": "示例",
            "asset": asset,
            "industryName": "ETF",
            "asOfDate": "2026-07-14",
            "tradableFlag": True,
            "amount1d": "1",
            "isTrendRightSide": True,
            "daysSinceTrendEntry": 9,
            "trendStrengthLocalCurr": "90.001",
            "stopwinFlagByDangerSignal": False,
        },
        bars(),
        market=market,
    )

    assert (item.symbol, item.exchange) == (symbol, exchange)
    assert build_candidate_list(
        [item], held_symbols=set(), market=market
    ).eligible == (item,)


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


def test_buy_action_targets_never_reserve_more_than_available_cash() -> None:
    actions = estimate_buy_actions(
        ranked=[candidate("600001"), candidate("600002")],
        account_fresh=True,
        net_value=Decimal("676549.55"),
        available_cash=Decimal("7000"),
        current_position_count=8,
    )

    assert [(item.symbol, item.target_amount) for item in actions] == [
        ("600001", Decimal("6765.50"))
    ]
    assert sum((item.target_amount for item in actions), Decimal("0")) <= Decimal(
        "7000"
    )


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


def test_market_buy_actions_use_whole_us_shares_and_hk_lot_sizes() -> None:
    us = replace(candidate("600001", close="123"), symbol="VIXY", exchange="US")
    hk = replace(candidate("600002", close="51"), symbol="00700", exchange="HK")

    us_actions = estimate_buy_actions(
        ranked=[us],
        account_fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("1000"),
        current_position_count=0,
        market="US",
    )
    hk_actions = estimate_buy_actions(
        ranked=[hk],
        account_fresh=False,
        require_fresh_account=False,
        net_value=Decimal("1000000"),
        available_cash=Decimal("6000"),
        current_position_count=0,
        market="HK",
        lot_sizes={"00700": 100},
    )

    assert us_actions[0].estimated_shares == 8
    assert hk_actions[0].estimated_shares == 100


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


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (holding("600001", right_side=None, danger=True), "danger_signal"),
        (holding("600001", right_side=False, danger=None), "left_trend_right_side"),
    ],
)
def test_strong_holding_sell_signal_wins_over_other_unknowns(
    snapshot: HoldingSnapshot, reason: str
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": snapshot},
        bars_by_symbol={"600001": bars()},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == ("SELL_ALL", reason)


@pytest.mark.parametrize("field", ["boiling", "champagne"])
def test_unknown_overheat_signal_requires_review_and_preserves_line(field: str) -> None:
    snapshot = replace(holding("600001"), **{field: None})
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": snapshot},
        bars_by_symbol={"600001": bars()},
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
        "MANUAL_REVIEW",
        Decimal("8.5"),
    )


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


def test_markdown_prioritizes_actions_before_source_summary() -> None:
    markdown = render_markdown(report(candidates=(candidate("600001"),)))
    assert markdown.index("## 09:30–10:00：按顺序考虑买入") < markdown.index(
        "## 中文附录"
    )
    assert "其他接口事实：详见 JSON 审计文件" in markdown
    assert "## API 原始事实" not in markdown


def test_trend_feishu_text_lists_actions_but_only_counts_holds() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": {"fresh": True},
        "metadata": {"market": "US", "broker": "futu"},
        "strategy_judgments": {
            "holding_decisions": [
                {
                    "action": "SELL_ALL",
                    "symbol": "AAPL",
                    "name": "苹果",
                    "reason": "left_trend_right_side",
                    "active_line": "190",
                },
                {
                    "action": "HOLD",
                    "symbol": "MSFT",
                    "name": "微软",
                    "reason": "trend_intact",
                },
                {
                    "action": "MANUAL_REVIEW",
                    "symbol": "TSLA",
                    "name": "特斯拉",
                    "reason": "missing_snapshot",
                },
                {
                    "action": "NEW_CODE",
                    "symbol": "NVDA",
                    "name": "英伟达",
                    "reason": "new_reason",
                },
            ],
            "formal_actions": [
                {
                    "action": "SELL_ALL",
                    "symbol": "AAPL",
                    "name": "苹果",
                    "reason": "left_trend_right_side",
                    "active_line": "190",
                },
                {
                    "action": "BUY",
                    "symbol": "CRWD",
                    "name": "CrowdStrike",
                    "estimated_shares": 2,
                    "target_amount": "500",
                    "estimated_initial_line": "198",
                },
            ],
        },
    }

    title, message = render_trend_feishu_text(
        payload, broker_label="富途", market_label="美股"
    )

    assert title == "【富途｜美股趋势报告｜2026-07-15】"
    assert message == "\n".join(
        [
            "数据截至：2026-07-14",
            "账户状态：已更新",
            "今日动作：卖出 1｜买入 1｜持有 1｜复核 2",
            "",
            "卖出",
            "1. AAPL 苹果｜右侧趋势已结束｜保护线 190",
            "",
            "买入",
            "1. CRWD CrowdStrike｜美股常规交易时段｜约 2 股｜金额上限 500｜保护线 198",
            "",
            "人工复核",
            "1. TSLA 特斯拉｜未知动作或原因，需人工确认",
            "2. NVDA 英伟达｜未知动作或原因，需人工确认",
            "",
            "请人工确认，不自动下单。",
        ]
    )
    assert "MSFT" not in message
    assert "http" not in message.lower()


def test_trend_feishu_text_uses_short_no_trade_template() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": {"fresh": False},
        "metadata": {"market": "HK", "broker": "phillips"},
        "strategy_judgments": {
            "holding_decisions": [
                {"action": "HOLD", "symbol": "02800", "reason": "trend_intact"}
            ],
            "formal_actions": [],
        },
    }
    title, message = render_trend_feishu_text(
        payload, broker_label="辉立", market_label="港股"
    )
    assert title == "【辉立｜港股趋势报告｜2026-07-15】"
    assert message == (
        "数据截至：2026-07-14\n"
        "账户状态：已过期\n"
        "今日无买卖动作｜持有 1｜复核 0\n\n"
        "请人工确认，不自动下单。"
    )


def test_trend_feishu_text_moves_unknown_hold_reason_to_review() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": {"fresh": True},
        "metadata": {"market": "HK"},
        "strategy_judgments": {
            "holding_decisions": [
                {
                    "action": "HOLD",
                    "symbol": "02800",
                    "name": "盈富基金",
                    "reason": "future_hold_reason",
                }
            ],
            "formal_actions": [],
        },
    }

    _, message = render_trend_feishu_text(
        payload, broker_label="辉立", market_label="港股"
    )

    assert message == (
        "数据截至：2026-07-14\n"
        "账户状态：已更新\n"
        "今日无买卖动作｜持有 0｜复核 1\n\n"
        "人工复核\n"
        "1. 02800 盈富基金｜未知动作或原因，需人工确认\n\n"
        "请人工确认，不自动下单。"
    )
    assert "future_hold_reason" not in message


def test_trend_feishu_text_moves_unknown_formal_buy_reason_to_review() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": {"fresh": True},
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "holding_decisions": [],
            "formal_actions": [
                {
                    "action": "BUY",
                    "symbol": "600001",
                    "name": "未知原因买入",
                    "reason": "future_reason",
                    "estimated_shares": 100,
                    "target_amount": "1000",
                    "estimated_initial_line": "9",
                }
            ],
        },
    }

    _, message = render_trend_feishu_text(
        payload, broker_label="东方财富", market_label="A股"
    )

    assert "今日无买卖动作｜持有 0｜复核 1" in message
    assert "\n买入\n" not in message
    assert "600001 未知原因买入｜未知动作或原因，需人工确认" in message


def test_trend_feishu_text_lists_reviews_on_no_trade_days() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": {"fresh": True},
        "metadata": {"market": "CN"},
        "strategy_judgments": {
            "holding_decisions": [
                {
                    "action": "MANUAL_REVIEW",
                    "symbol": "600001",
                    "name": "测试股票",
                    "reason": "future_reason_code",
                }
            ],
            "formal_actions": [],
        },
    }

    _, message = render_trend_feishu_text(
        payload, broker_label="东方财富", market_label="A股"
    )

    assert message == (
        "数据截至：2026-07-14\n"
        "账户状态：已更新\n"
        "今日无买卖动作｜持有 0｜复核 1\n\n"
        "人工复核\n"
        "1. 600001 测试股票｜未知动作或原因，需人工确认\n\n"
        "请人工确认，不自动下单。"
    )
    assert "future_reason_code" not in message


def test_trend_failure_text_is_plain_and_actionable() -> None:
    title, message = render_trend_failure_text(
        broker_label="东方财富",
        market_label="A股",
        report_date="2026-07-15",
        reason="趋势数据在截止时间前仍未更新",
        recovery_action="确认 Trend Animals 数据状态后手动重跑东方财富报告",
    )
    assert title == "【东方财富｜A股趋势报告生成失败｜2026-07-15】"
    assert message == (
        "原因：趋势数据在截止时间前仍未更新\n"
        "现在做：确认 Trend Animals 数据状态后手动重跑东方财富报告\n\n"
        "报告未生成，请勿依据旧报告交易。"
    )


def test_markdown_is_operation_first_and_translates_internal_codes() -> None:
    built = replace(
        report(candidates=(candidate("600001"),)),
        holdings=(
            trend_module.HoldingDecision(
                symbol="600025",
                name="华能水电",
                industry="电力",
                action="SELL_ALL",
                reason="left_trend_right_side",
                initial_line=Decimal("9.32"),
                active_line=Decimal("9.32"),
                atr=Decimal("0.10"),
                historical=True,
            ),
        ),
    )
    markdown = render_markdown(built)

    assert markdown.index("## 操作摘要") < markdown.index("## 开盘前：确认卖出")
    assert markdown.index("## 开盘前：确认卖出") < markdown.index(
        "## 09:30–10:00：按顺序考虑买入"
    )
    assert "全部卖出" in markdown
    assert "SELL_ALL" not in markdown
    assert "HOLD" not in markdown
    assert "left_trend_right_side" not in markdown


def test_markdown_translates_exclusion_and_api_facts_without_paths() -> None:
    built = replace(
        report(),
        excluded={
            "002303": ["right_side_days_not_below_10"],
            "159835": ["amount_below_1"],
            "551520": ["atr_unavailable"],
        },
        api_facts=(
            "getUpdateStatus rows=6",
            "getComponentTicker rows=39 cache=client-managed",
            "getTickerSnapshot fields=tmId,tickerName rows=44 cache=client-managed",
        ),
        data_sources=(
            "Trend Animals",
            "Futu CN calendar/QFQ daily K-line",
            "/Users/ray/projects/open_trader/data/latest/portfolio.csv",
        ),
    )
    markdown = render_markdown(built)

    assert "进入右侧趋势已满 10 天" in markdown
    assert "日成交额不足 1 亿元" in markdown
    assert "缺少 ATR 数据" in markdown
    assert "数据更新状态：已检查 6 条" in markdown
    assert "候选池成分：39 条" in markdown
    assert "趋势快照：44 条" in markdown
    assert "getUpdateStatus" not in markdown
    assert "cache=client-managed" not in markdown
    assert "/Users/ray" not in markdown
    assert "东方财富账户快照" in markdown


def test_markdown_unknown_reason_is_visible_but_json_keeps_raw_codes() -> None:
    built = replace(report(), excluded={"600001": ["future_reason_code"]})

    markdown = render_markdown(built)
    payload = trend_module._report_payload(built)

    assert "未知原因（future_reason_code）" in markdown
    assert payload["excluded"]["600001"] == ["future_reason_code"]


def test_markdown_unknown_action_is_visible_and_json_keeps_raw_code() -> None:
    built = replace(
        report(),
        holdings=(
            trend_module.HoldingDecision(
                symbol="600025",
                name="华能水电",
                industry="电力",
                action="FUTURE_ACTION",
                reason="trend_intact",
                initial_line=Decimal("9.32"),
                active_line=Decimal("9.32"),
                atr=Decimal("0.10"),
                historical=True,
            ),
        ),
    )

    markdown = render_markdown(built)
    payload = trend_module._report_payload(built)

    assert "其他动作 1" in markdown
    assert "未知动作（FUTURE_ACTION）" in markdown
    assert payload["strategy_judgments"]["holding_decisions"][0]["action"] == (
        "FUTURE_ACTION"
    )


def test_markdown_translates_holding_kline_unavailable() -> None:
    built = replace(
        report(),
        holdings=(
            trend_module.HoldingDecision(
                symbol="600025",
                name="华能水电",
                industry="电力",
                action="MANUAL_REVIEW",
                reason="holding_kline_unavailable",
                initial_line=Decimal("9.32"),
                active_line=Decimal("9.32"),
                atr=None,
                historical=True,
            ),
        ),
    )

    markdown = render_markdown(built)

    assert "持仓日线数据不可用" in markdown
    assert "holding_kline_unavailable" not in markdown


def test_markdown_translates_account_exceptions_without_raw_details() -> None:
    built = report()
    built = replace(
        built,
        account=replace(
            built.account,
            exceptions=(
                "unsupported Eastmoney asset: 110001 可转债 (CN/bond)",
                "future account exception payload",
            ),
        ),
    )

    markdown = render_markdown(built)

    assert "东方财富账户不支持的资产：110001 可转债" in markdown
    assert "其他账户例外：详见 JSON 审计文件" in markdown
    assert "unsupported Eastmoney asset" not in markdown
    assert "CN/bond" not in markdown
    assert "future account exception payload" not in markdown


def test_markdown_translates_missing_account_exception_fields(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(
        path,
        [portfolio_row(symbol="", name="", asset_class="bond")],
    )
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(path, (timestamp, timestamp))
    built = replace(
        report(),
        account=load_eastmoney_account(path, expected_date="2026-07-14"),
    )

    markdown = render_markdown(built)

    assert "东方财富账户不支持的资产：代码缺失 名称缺失" in markdown
    assert "<missing-symbol>" not in markdown
    assert "<missing-name>" not in markdown


def test_markdown_hides_any_absolute_data_source_path() -> None:
    built = replace(
        report(), data_sources=("/private/tmp/eastmoney-account-export.csv",)
    )

    markdown = render_markdown(built)

    assert "东方财富账户快照" in markdown
    assert "/private/tmp" not in markdown


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
    assert "约 600 股" in markdown
    assert "金额上限 6765.50 元" in markdown
    assert "预计保护线 9.00" in markdown
    assert "按东方财富实时价格向下取整为 100 股整数倍" in markdown


def test_candidate_row_shows_industry_slots_and_weight() -> None:
    markdown = render_markdown(report(candidates=(candidate("600001"),)))
    assert "行业 电力（已占 0 个席位，当前仓位 0.00%）" in markdown


def test_flat_bars_keep_zero_atr_in_state_and_render() -> None:
    flat = [
        DailyKlineBar(
            date=f"2026-06-{index + 1:02d}",
            open=10,
            high=10,
            low=10,
            close=10,
            volume=100,
        )
        for index in range(15)
    ]
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": flat},
    )

    assert built.holdings[0].atr == Decimal("0")
    assert built.protection_state["positions"]["600001"]["atr14"] == "0"
    assert "活动保护线 10.00" in render_markdown(built)


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


@pytest.mark.parametrize("failed_suffix", [".md", ".json"])
def test_new_frozen_pair_rolls_back_any_failed_final_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_suffix: str
) -> None:
    original_replace = Path.replace
    failed = False

    def fail_once(path: Path, target: Path) -> Path:
        nonlocal failed
        if not failed and Path(target).suffix == failed_suffix:
            failed = True
            raise OSError("injected final replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_once)

    with pytest.raises(OSError, match="injected final replace failure"):
        write_frozen_report(report(), tmp_path)

    assert not (tmp_path / "2026-07-14.md").exists()
    assert not (tmp_path / "2026-07-14.json").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing_suffix", [".md", ".json"])
@pytest.mark.parametrize("failed_suffix", [".md", ".json"])
def test_partial_frozen_pair_restores_preexisting_final_on_any_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_suffix: str,
    failed_suffix: str,
) -> None:
    existing = tmp_path / f"2026-07-14{existing_suffix}"
    existing.write_text("old generation", encoding="utf-8")
    original_replace = Path.replace
    failed = False

    def fail_once(path: Path, target: Path) -> Path:
        nonlocal failed
        if not failed and Path(target).suffix == failed_suffix:
            failed = True
            raise OSError("injected final replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_once)

    with pytest.raises(OSError, match="injected final replace failure"):
        write_frozen_report(report(), tmp_path)

    assert existing.read_text(encoding="utf-8") == "old generation"
    other_suffix = ".json" if existing_suffix == ".md" else ".md"
    assert not (tmp_path / f"2026-07-14{other_suffix}").exists()
    assert set(tmp_path.iterdir()) == {existing}


def test_frozen_json_has_explicit_no_action_strategy_contract(tmp_path: Path) -> None:
    _, json_path = write_frozen_report(report(), tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert payload["disclaimer"] == (
        "本报告是确定性纪律清单，不是订单或成交事实；所有交易由用户人工确认与执行。"
    )
    assert payload["no_action"] == "现金也是有效仓位，本日无需交易。"
    assert payload["api_facts"] == ["A股数据日期：2026-07-14"]
    assert payload["strategy_judgments"] == {
        "holding_decisions": [],
        "top10_candidates": [],
        "formal_actions": [],
    }


def test_frozen_json_formal_actions_include_sells_and_buys(tmp_path: Path) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600009"),
        candidates=(candidate("600001"),),
        holding_snapshots={"600009": holding("600009", danger=True)},
        bars_by_symbol={"600009": None},
        api_facts=("A股数据日期：2026-07-14",),
    )
    _, json_path = write_frozen_report(built, tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    judgments = payload["strategy_judgments"]

    assert [item["symbol"] for item in judgments["holding_decisions"]] == ["600009"]
    assert [item["symbol"] for item in judgments["top10_candidates"]] == ["600001"]
    assert [(item["action"], item["symbol"]) for item in judgments["formal_actions"]] == [
        ("SELL_ALL", "600009"),
        ("BUY", "600001"),
    ]
    assert "no_action" not in payload


def test_report_records_generation_time_and_whitelisted_signal_audit(
    tmp_path: Path,
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        generated_at="2026-07-14T17:00:01+08:00",
        account=account("600009"),
        candidates=(candidate("600001", danger=True),),
        holding_snapshots={"600009": replace(holding("600009"), boiling=None)},
        bars_by_symbol={"600009": bars()},
        metadata={
            "paid_response_cache": {
                "hits": 1,
                "misses": 2,
                "events": [
                    {"endpoint": "getComponentTicker", "cache": "hit"},
                    {"endpoint": "getTickerSnapshot", "cache": "miss"},
                ],
            }
        },
    )

    markdown_path, json_path = write_frozen_report(built, tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert payload["generated_at"] == "2026-07-14T17:00:01+08:00"
    assert payload["metadata"]["paid_response_cache"]["hits"] == 1
    assert payload["signal_snapshots"]["holdings"]["600009"] == {
        "tm_id": 600009,
        "symbol": "600009",
        "as_of_date": "2026-07-14",
        "right_side": True,
        "danger": False,
        "boiling": None,
        "champagne": False,
    }
    excluded = payload["signal_snapshots"]["excluded"]["600001"][0]
    assert excluded["danger"] is True
    assert set(excluded) == {
        "tm_id",
        "symbol",
        "exchange",
        "name",
        "asset",
        "industry",
        "as_of_date",
        "tradable",
        "amount",
        "right_side",
        "days",
        "strength",
        "danger",
    }
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "2026-07-14T17:00:01+08:00" in markdown
    assert "危险信号触发" in markdown
    assert "danger=True" not in markdown


def test_candidate_audit_includes_all_ranked_and_excluded_pool_facts() -> None:
    ranked = [
        replace(
            candidate(f"6000{index:02d}", strength=str(100 - index / 10)),
            pools=("622466",),
        )
        for index in range(1, 13)
    ]
    excluded = replace(
        candidate("600099", name="", danger=True),
        pools=("697199",),
    )

    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(*ranked, excluded),
        holding_snapshots={},
        bars_by_symbol={},
    )

    audit = built.signal_snapshots["candidates"]
    assert len(audit) == 13
    number_eleven = next(item for item in audit if item["symbol"] == "600011")
    assert (number_eleven["eligible"], number_eleven["rank"]) == (True, 11)
    rejected = next(item for item in audit if item["symbol"] == "600099")
    assert rejected["excluded_reasons"] == ["danger_signal", "name_missing"]
    assert rejected["pools"] == ["697199"]
    assert rejected["source"] == "Trend Animals"
    assert set(rejected) == {
        "tm_id",
        "symbol",
        "exchange",
        "name",
        "asset",
        "industry",
        "as_of_date",
        "tradable",
        "amount",
        "right_side",
        "days",
        "strength",
        "danger",
        "eligible",
        "excluded_reasons",
        "rank",
        "pools",
        "source",
    }


def trend_config(tmp_path: Path) -> DailyPremarketConfig:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    portfolio.parent.mkdir(parents=True, exist_ok=True)
    write_portfolio(
        portfolio,
        [
            portfolio_row(
                market="CASH",
                asset_class="cash",
                symbol="CNY_CASH",
                name="人民币现金",
                currency="CNY",
                total_quantity="100000",
                avg_cost_price="1",
                market_value="100000",
            )
        ],
    )
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(portfolio, (timestamp, timestamp))
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
        portfolio=portfolio,
        trend_animals_api_key="secret-value",
        trend_animals_a_share_tm_id=622466,
        trend_animals_etf_tm_id=697199,
    )


def write_9885_receipt(
    config: DailyPremarketConfig,
    *,
    status: str,
    protection_state: object | None = None,
    write_final_pair: bool = False,
) -> tuple[Path, str, str]:
    markdown = "# 9885 frozen report\n"
    report_payload = {
        "delivery_status": status,
        "metadata": {"delivery_status": status},
        "protection_state": (
            {"schema_version": 1, "positions": {}}
            if protection_state is None
            else protection_state
        ),
    }
    report_json = json.dumps(
        report_payload, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    markdown_bytes = markdown.encode("utf-8")
    json_bytes = report_json.encode("utf-8")
    receipt = {
        "status": status,
        "generated_at": "2026-07-14T17:00:00+08:00",
        "artifact_stem": "2026-07-14",
        "markdown": markdown,
        "report_json": report_json,
        "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "json_sha256": hashlib.sha256(json_bytes).hexdigest(),
        "content_hash": hashlib.sha256(
            markdown_bytes + b"\0" + json_bytes
        ).hexdigest(),
    }
    receipt_path = config.data_dir / "trend_a_share/delivery/2026-07-14.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    if write_final_pair:
        report_dir = config.reports_dir / "trend_a_share"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "2026-07-14.md").write_text(markdown, encoding="utf-8")
        (report_dir / "2026-07-14.json").write_text(report_json, encoding="utf-8")
    return receipt_path, markdown, report_json


class RecordingMacOS(MacOSNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class RecordingFeishu(FeishuWebhookNotifier):
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail = fail

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
        if self.fail:
            raise RuntimeError("delivery failed")


class ReadyApi:
    def __init__(
        self,
        calls: list[str],
        *,
        ready: bool = True,
        snapshot_date: str = "2026-07-14",
        holding_error: Exception | None = None,
        invalid_billing: bool = False,
        snapshot_ids: list[object] | None = None,
    ) -> None:
        self.calls = calls
        self.ready = ready
        self.snapshot_date = snapshot_date
        self.holding_error = holding_error
        self.invalid_billing = invalid_billing
        self.snapshot_ids = snapshot_ids
        self.balance_calls = 0

    def get_update_status(self) -> list[dict[str, object]]:
        self.calls.append("api.update_status")
        date = "2026-07-14" if self.ready else "2026-07-13"
        return [{"asset": "A股", "asOfDate": date}, {"asset": "ETF基金", "asOfDate": date}]

    def get_account_balance(self) -> dict[str, object]:
        self.balance_calls += 1
        self.calls.append("api.balance_before" if self.balance_calls == 1 else "api.balance_after")
        return {"balance": "100" if self.balance_calls == 1 else "99"}

    def get_components(self, *, tm_id: int, expected_date: str) -> list[dict[str, object]]:
        self.calls.append(f"api.components.{tm_id}")
        component_id = 1 if tm_id == 622466 else 2
        return [{"tmId": component_id, "tickerSymbol": f"60000{component_id}.SH", "asOfDate": expected_date}]

    def search_exact_symbol(self, symbol: str) -> int:
        self.calls.append(f"api.search.{symbol}")
        if self.holding_error:
            raise self.holding_error
        return int(symbol)

    def get_snapshot_billing(self) -> list[dict[str, object]]:
        self.calls.append("api.billing")
        return [{"columnName": field, "priceCost": "bad" if self.invalid_billing else "0.01"} for field in {
            "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
            "tradableFlag", "industryName", "amount1d", "isTrendRightSide",
            "daysSinceTrendEntry", "trendStrengthLocalCurr",
            "stopwinFlagByDangerSignal", "stopwinFlagByBoilingTemperature",
            "stopwinFlagByPopChampagne",
        }]

    def get_snapshots(self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str) -> list[dict[str, object]]:
        self.calls.append("api.snapshots")
        rows = []
        for tm_id in self.snapshot_ids if self.snapshot_ids is not None else tm_ids:
            symbol = f"{tm_id:06d}" if isinstance(tm_id, int) else "600099"
            rows.append({
                "tmId": tm_id,
                "tickerName": f"股票{symbol}",
                "tickerSymbol": f"{symbol}.SH",
                "asset": "A股",
                "asOfDate": self.snapshot_date,
                "tradableFlag": True,
                "industryName": "电力",
                "amount1d": "2",
                "isTrendRightSide": True,
                "daysSinceTrendEntry": 3,
                "trendStrengthLocalCurr": "96",
                "stopwinFlagByDangerSignal": False,
                "stopwinFlagByBoilingTemperature": False,
                "stopwinFlagByPopChampagne": False,
            })
        return rows


class ReadyQuote:
    def __init__(
        self,
        calls: list[str],
        *,
        trading_days: list[str] | None = None,
        fail_calendar: bool = False,
        failed_klines: set[str] | None = None,
        kline_error: FutuQuoteError | None = None,
    ) -> None:
        self.calls = calls
        self.trading_days = trading_days or ["2026-07-14", "2026-07-15"]
        self.fail_calendar = fail_calendar
        self.failed_klines = failed_klines or set()
        self.kline_error = kline_error

    def get_cn_trading_days(self, *, start: str, end: str) -> list[str]:
        self.calls.append("futu.calendar")
        if self.fail_calendar:
            raise FutuQuoteError("calendar unavailable")
        return self.trading_days

    def get_daily_kline(self, symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        self.calls.append(f"futu.kline.{symbol}")
        if symbol in self.failed_klines:
            raise self.kline_error or FutuQuoteError("kline unavailable")
        return bars()

    def close(self) -> None:
        pass


def test_report_runner_sends_exact_broker_v1_text(tmp_path: Path) -> None:
    calls: list[str] = []
    api_kwargs: dict[str, object] = {}
    config = trend_config(tmp_path)
    notifier = RecordingFeishu()

    def api_factory(**kwargs: object) -> ReadyApi:
        api_kwargs.update(kwargs)
        return ReadyApi(calls)

    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
        api_factory=api_factory,
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        notifier=notifier,
    )

    assert result.status == "generated"
    assert calls[:5] == [
        "futu.calendar", "api.update_status", "api.balance_before",
        "api.components.622466", "api.components.697199",
    ]
    assert calls.index("api.billing") < calls.index("api.snapshots")
    assert api_kwargs["cache_dir"] == config.data_dir / "trend_animals/cache"
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert notifier.messages == [
        (
            "【东方财富｜A股趋势报告｜2026-07-15】",
            "数据截至：2026-07-14\n"
            "账户状态：已更新\n"
            "今日动作：卖出 0｜买入 2｜持有 0｜复核 0\n\n"
            "买入\n"
            "1. 000001 股票000001｜09:30–10:00｜约 100 股｜金额上限 1000｜保护线 6\n"
            "2. 000002 股票000002｜09:30–10:00｜约 100 股｜金额上限 1000｜保护线 6\n\n"
            "请人工确认，不自动下单。",
        )
    ]
    assert payload["execution_date"] == "2026-07-15"
    assert payload["delivery_status"] == "sent"
    assert payload["process_version"]
    assert payload["metadata"]["market"] == "CN"
    assert payload["metadata"]["broker"] == "eastmoney"


def test_report_runner_holiday_is_silent_and_free(tmp_path: Path) -> None:
    calls: list[str] = []
    notifier = RecordingMacOS()
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("paid API must not be built"),
        quote_factory=lambda **kwargs: ReadyQuote(calls, trading_days=["2026-07-15"]),
        notifier=notifier,
    )
    assert result.status == "holiday"
    assert calls == ["futu.calendar"]
    assert notifier.messages == []


def test_report_execution_rejects_wrong_pool_ids_before_external_calls(
    tmp_path: Path,
) -> None:
    config = replace(trend_config(tmp_path), trend_animals_a_share_tm_id=1)

    with pytest.raises(
        ValueError, match="TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID"
    ):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: pytest.fail("invalid config must not call API"),
            quote_factory=lambda **kwargs: pytest.fail("invalid config must not call Futu"),
        )


def test_report_runner_waits_once_then_retries_until_ready(tmp_path: Path) -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    notifier = RecordingMacOS()
    attempts = iter([False, True])
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 17, 0, tzinfo=SHANGHAI),
        sleep_fn=sleeps.append,
        api_factory=lambda **kwargs: ReadyApi(calls, ready=next(attempts)),
        quote_factory=lambda **kwargs: ReadyQuote(calls), notifier=notifier,
    )
    assert result.status == "generated"
    assert sleeps == [600.0]
    assert [title for title, _ in notifier.messages] == ["A股趋势数据等待中", "A股趋势计划发送失败"]
    assert calls[:4] == ["futu.calendar", "api.update_status", "futu.calendar", "api.update_status"]


def test_report_runner_failure_owns_day_at_inclusive_1800_deadline(tmp_path: Path) -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    feishu = RecordingFeishu()
    macos = RecordingMacOS()
    notifier = CompositeNotifier([feishu, macos])
    config = trend_config(tmp_path)
    times = iter([
        datetime(2026, 7, 14, 17, 50, tzinfo=SHANGHAI),
        datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
    ])
    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14", now_fn=lambda: next(times),
        sleep_fn=sleeps.append, api_factory=lambda **kwargs: ReadyApi(calls, ready=False),
        quote_factory=lambda **kwargs: ReadyQuote(calls), notifier=notifier,
    )
    assert result == AShareTrendRunResult("failed", None, None)
    assert sleeps == [600.0]
    assert [title for title, _ in macos.messages] == ["A股趋势数据等待中", "A股趋势计划失败"]
    assert feishu.messages == [
        (
            "【东方财富｜A股趋势报告生成失败｜2026-07-14】",
            "原因：趋势数据在截止时间前仍未更新\n"
            "现在做：确认 Trend Animals 数据状态后手动重跑东方财富报告\n\n"
            "报告未生成，请勿依据旧报告交易。",
        )
    ]
    ledger = config.data_dir / "trend_a_share/daily_delivery/2026-07-14.json"
    assert json.loads(ledger.read_text(encoding="utf-8"))["status"] == "sent"
    assert not list((tmp_path / "reports").rglob("*.md"))
    assert not list((tmp_path / "reports").rglob("*.json"))


def test_report_runner_retries_systemic_futu_failure_through_deadline(tmp_path: Path) -> None:
    calls: list[str] = []
    times = iter([
        datetime(2026, 7, 14, 17, 50, tzinfo=SHANGHAI),
        datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
    ])
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14", now_fn=lambda: next(times),
        sleep_fn=lambda seconds: None,
        api_factory=lambda **kwargs: pytest.fail("paid API must not be built"),
        quote_factory=lambda **kwargs: ReadyQuote(calls, fail_calendar=True),
        notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert calls == ["futu.calendar", "futu.calendar"]
    assert not list((tmp_path / "reports").rglob("*.md"))


def test_report_runner_existing_base_makes_no_external_or_notification_call(tmp_path: Path) -> None:
    config = trend_config(tmp_path)
    report_dir = config.reports_dir / "trend_a_share"
    report_dir.mkdir(parents=True)
    (report_dir / "2026-07-14.md").write_text("frozen", encoding="utf-8")
    (report_dir / "2026-07-14.json").write_text("{}", encoding="utf-8")
    notifier = RecordingMacOS()
    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("no API"),
        quote_factory=lambda **kwargs: pytest.fail("no Futu"), notifier=notifier,
    )
    assert result.status == "existing"
    assert notifier.messages == []


def test_report_runner_accepts_complete_pair_bound_to_legacy_sent_receipt(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    report_dir = config.reports_dir / "trend_a_share"
    report_dir.mkdir(parents=True)
    markdown = "frozen"
    report_json = "{}"
    (report_dir / "2026-07-14.md").write_text(markdown, encoding="utf-8")
    (report_dir / "2026-07-14.json").write_text(report_json, encoding="utf-8")
    receipt_path = config.data_dir / "trend_a_share/delivery/2026-07-14.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            {
                "status": "sent",
                "artifact_stem": "2026-07-14",
                "generated_at": "2026-07-14T17:00:00+08:00",
                "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                "json_sha256": hashlib.sha256(report_json.encode()).hexdigest(),
                "content_hash": hashlib.sha256(
                    markdown.encode() + b"\0" + report_json.encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("no API"),
        quote_factory=lambda **kwargs: pytest.fail("no Futu"),
        notifier=RecordingMacOS(),
    )

    assert result.status == "existing"


def test_9885_sent_receipt_migrates_and_keeps_complete_pair_existing(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    receipt_path, _, _ = write_9885_receipt(
        config, status="sent", write_final_pair=True
    )

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("9885 migration must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("9885 migration must not refetch"),
        notifier=RecordingMacOS(),
    )

    migrated = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result.status == "existing"
    assert migrated["status"] == "sent"
    assert migrated["protection_state"] == {"schema_version": 1, "positions": {}}
    assert len(migrated["protection_state_sha256"]) == 64


@pytest.mark.parametrize(
    ("status", "expected_delivery_status", "expected_send_count"),
    [("pending", "sent", 1), ("delivery_failed", "sent", 1)],
)
def test_9885_pending_receipt_uses_daily_ledger_without_refetch(
    tmp_path: Path,
    status: str,
    expected_delivery_status: str,
    expected_send_count: int,
) -> None:
    config = trend_config(tmp_path)
    receipt_path, _, _ = write_9885_receipt(config, status=status)
    notifier = RecordingFeishu()

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("9885 recovery must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("9885 recovery must not refetch"),
        notifier=notifier,
    )

    assert json.loads(result.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == expected_delivery_status
    assert len(notifier.messages) == expected_send_count
    daily_ledger = config.data_dir / "trend_a_share/daily_delivery/2026-07-14.json"
    assert json.loads(daily_ledger.read_text(encoding="utf-8"))["status"] == "sent"
    assert "protection_state_sha256" in json.loads(
        receipt_path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("failure", ["tampered_hash", "invalid_state"])
def test_9885_migration_fails_closed_for_tampering_or_invalid_state(
    tmp_path: Path, failure: str
) -> None:
    config = trend_config(tmp_path)
    receipt_path, _, _ = write_9885_receipt(
        config,
        status="pending",
        protection_state=(
            {"schema_version": 2, "positions": {}}
            if failure == "invalid_state"
            else None
        ),
    )
    if failure == "tampered_hash":
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["content_hash"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: pytest.fail("invalid receipt must not refetch"),
            quote_factory=lambda **kwargs: pytest.fail("invalid receipt must not refetch"),
            notifier=RecordingFeishu(),
        )

    assert "protection_state_sha256" not in json.loads(
        receipt_path.read_text(encoding="utf-8")
    )


def test_9885_migration_replace_failure_preserves_old_receipt_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    receipt_path, _, _ = write_9885_receipt(config, status="pending")
    old_receipt = receipt_path.read_bytes()
    original_replace = Path.replace

    def fail_migration_replace(path: Path, target: Path) -> Path:
        if Path(target) == receipt_path:
            raise OSError("migration replace failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_migration_replace)
    with pytest.raises(OSError, match="migration replace failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: pytest.fail("migration must not refetch"),
            quote_factory=lambda **kwargs: pytest.fail("migration must not refetch"),
            notifier=RecordingFeishu(),
        )

    assert receipt_path.read_bytes() == old_receipt
    monkeypatch.setattr(Path, "replace", original_replace)
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("migration retry must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("migration retry must not refetch"),
        notifier=RecordingFeishu(),
    )

    assert json.loads(recovered.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == "sent"


def test_report_runner_takes_lock_before_accepting_existing_pair(tmp_path: Path) -> None:
    config = trend_config(tmp_path)
    report_dir = config.reports_dir / "trend_a_share"
    report_dir.mkdir(parents=True)
    (report_dir / "2026-07-14.md").write_text("frozen", encoding="utf-8")
    (report_dir / "2026-07-14.json").write_text("{}", encoding="utf-8")

    with RunLock(config.data_dir / "runs/.trend_a_share_report.lock"):
        with pytest.raises(RuntimeError, match="already active"):
            run_a_share_trend_report(config=config, run_date="2026-07-14")


def test_report_runner_persists_state_before_delivery_and_freezes_pair_last(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    report_dir = config.reports_dir / "trend_a_share"

    class OrderingFeishu(RecordingFeishu):
        def notify(self, title: str, message: str) -> None:
            assert (config.data_dir / "trend_a_share/protection_state.json").exists()
            assert not (report_dir / "2026-07-14.md").exists()
            assert not (report_dir / "2026-07-14.json").exists()
            super().notify(title, message)

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=OrderingFeishu(),
    )

    assert result.report_path.exists() and result.json_path.exists()


def test_report_runner_state_failure_leaves_no_formal_pair_or_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingFeishu()
    monkeypatch.setattr(
        trend_module,
        "write_protection_state",
        lambda path, state: (_ for _ in ()).throw(OSError("state write failed")),
    )

    with pytest.raises(OSError, match="state write failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    assert notifier.messages == []
    assert not list((config.reports_dir / "trend_a_share").glob("2026-07-14.*"))


def test_initial_receipt_failure_leaves_no_stage_or_reusable_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    state_path = config.data_dir / "trend_a_share/protection_state.json"
    write_protection_state(state_path, {"schema_version": 1, "positions": {}})
    original_state = state_path.read_bytes()
    state_writes: list[Mapping[str, object]] = []
    monkeypatch.setattr(
        trend_module,
        "write_protection_state",
        lambda path, state: state_writes.append(state),
    )
    monkeypatch.setattr(
        trend_module,
        "_write_delivery_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("receipt write failed")),
    )

    with pytest.raises(OSError, match="receipt write failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=RecordingFeishu(),
        )

    assert not list((config.data_dir / "trend_a_share/staged").rglob("*"))
    assert not (config.data_dir / "trend_a_share/delivery/2026-07-14.json").exists()
    assert state_writes == []
    assert state_path.read_bytes() == original_state


def test_prepared_receipt_recovers_state_write_failure_without_refetch_or_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    receipt_path = config.data_dir / "trend_a_share/delivery/2026-07-14.json"
    state_path = config.data_dir / "trend_a_share/protection_state.json"
    notifier = RecordingFeishu()
    original_write_state = trend_module.write_protection_state
    prepared_receipt: dict[str, object] = {}

    def fail_state_once(path: Path, state: Mapping[str, object]) -> None:
        prepared_receipt.update(json.loads(receipt_path.read_text(encoding="utf-8")))
        assert prepared_receipt["status"] == "prepared"
        assert prepared_receipt["protection_state"] == state
        assert len(str(prepared_receipt["protection_state_sha256"])) == 64
        raise OSError("state write failed")

    monkeypatch.setattr(trend_module, "write_protection_state", fail_state_once)
    with pytest.raises(OSError, match="state write failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    assert notifier.messages == []
    monkeypatch.setattr(trend_module, "write_protection_state", original_write_state)
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("prepared recovery must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("prepared recovery must not refetch"),
        notifier=notifier,
    )

    assert len(notifier.messages) == 1
    assert recovered.report_path.read_text(encoding="utf-8") == prepared_receipt["markdown"]
    assert load_protection_state(state_path) == prepared_receipt["protection_state"]
    prepared_payload = json.loads(str(prepared_receipt["report_json"]))
    recovered_payload = json.loads(recovered.json_path.read_text(encoding="utf-8"))
    prepared_payload.pop("delivery_status")
    recovered_payload.pop("delivery_status")
    prepared_payload["metadata"].pop("delivery_status")
    recovered_payload["metadata"].pop("delivery_status")
    assert recovered_payload == prepared_payload


def test_pending_transition_failure_recovers_prepared_receipt_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingFeishu()
    original_transition = trend_module._transition_delivery_receipt

    def fail_pending_transition(
        *args: object, status: str, **kwargs: object
    ) -> object:
        if status == "pending":
            raise OSError("pending transition failed")
        return original_transition(*args, status=status, **kwargs)

    monkeypatch.setattr(
        trend_module, "_transition_delivery_receipt", fail_pending_transition
    )
    with pytest.raises(OSError, match="pending transition failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    receipt_path = config.data_dir / "trend_a_share/delivery/2026-07-14.json"
    prepared = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert load_protection_state(
        config.data_dir / "trend_a_share/protection_state.json"
    ) == prepared["protection_state"]
    assert notifier.messages == []

    monkeypatch.setattr(
        trend_module, "_transition_delivery_receipt", original_transition
    )
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("prepared retry must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("prepared retry must not refetch"),
        notifier=notifier,
    )

    assert recovered.report_path.read_text(encoding="utf-8") == prepared["markdown"]
    assert len(notifier.messages) == 1
    prepared_payload = json.loads(str(prepared["report_json"]))
    recovered_payload = json.loads(recovered.json_path.read_text(encoding="utf-8"))
    prepared_payload.pop("delivery_status")
    recovered_payload.pop("delivery_status")
    prepared_payload["metadata"].pop("delivery_status")
    recovered_payload["metadata"].pop("delivery_status")
    assert recovered_payload == prepared_payload


def test_atomic_receipt_preserves_old_embedded_payload_if_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_path = tmp_path / "data/trend_a_share/delivery/2026-07-14.json"
    trend_module._write_delivery_receipt(
        receipt_path,
        status="delivery_failed",
        generated_at="2026-07-14T17:00:00+08:00",
        artifact_stem="2026-07-14",
        markdown="old report",
        report_json='{\n  "delivery_status": "delivery_failed"\n}\n',
        protection_state={"schema_version": 1, "positions": {}},
    )
    old_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    original_replace = Path.replace

    def fail_receipt_replace(path: Path, target: Path) -> Path:
        if Path(target) == receipt_path:
            raise OSError("receipt replace failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_receipt_replace)
    with pytest.raises(OSError, match="receipt replace failed"):
        trend_module._write_delivery_receipt(
            receipt_path,
            status="sent",
            generated_at="2026-07-14T17:00:00+08:00",
            artifact_stem="2026-07-14",
            markdown="new report",
            report_json='{\n  "delivery_status": "sent"\n}\n',
            protection_state={"schema_version": 1, "positions": {}},
        )

    assert json.loads(receipt_path.read_text(encoding="utf-8")) == old_receipt
    recovered = trend_module._read_delivery_receipt(
        receipt_path, artifact_stem="2026-07-14"
    )
    assert recovered is not None
    assert recovered["markdown"] == "old report"
    assert recovered["report_json"] == '{\n  "delivery_status": "delivery_failed"\n}\n'


def test_sent_receipt_prevents_duplicate_delivery_after_final_freeze_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingFeishu()
    failed = False
    original_replace = Path.replace

    def fail_once(path: Path, target: Path) -> Path:
        nonlocal failed
        if not failed and Path(target).parent == config.reports_dir / "trend_a_share":
            failed = True
            raise OSError("final freeze failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_once)
    with pytest.raises(OSError, match="final freeze failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    assert len(notifier.messages) == 1
    assert not list((config.reports_dir / "trend_a_share").glob("2026-07-14.*"))

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("recovery must not refetch API"),
        quote_factory=lambda **kwargs: pytest.fail("recovery must not refetch Futu"),
        notifier=notifier,
    )

    assert len(notifier.messages) == 1
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["delivery_status"] == "sent_prior_attempt"
    assert result.report_path.read_text(encoding="utf-8").startswith(
        "# A股趋势操作计划"
    )
    assert not notifier.messages[0][1].startswith("# A股趋势操作计划")
    receipt = json.loads(
        (config.data_dir / "trend_a_share/delivery/2026-07-14.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["artifact_stem"] == "2026-07-14"
    assert len(receipt["content_hash"]) == 64


def test_sent_recovery_receipt_write_failure_can_retry_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingFeishu()
    original_replace = Path.replace
    failed_freeze = False

    def fail_final_once(path: Path, target: Path) -> Path:
        nonlocal failed_freeze
        if not failed_freeze and Path(target).parent == config.reports_dir / "trend_a_share":
            failed_freeze = True
            raise OSError("final freeze failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_final_once)
    with pytest.raises(OSError, match="final freeze failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    monkeypatch.setattr(Path, "replace", original_replace)
    original_transition = trend_module._transition_delivery_receipt
    monkeypatch.setattr(
        trend_module,
        "_transition_delivery_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("sent recovery receipt write failed")
        ),
    )
    with pytest.raises(OSError, match="receipt write failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: pytest.fail("sent recovery must not refetch"),
            quote_factory=lambda **kwargs: pytest.fail("sent recovery must not refetch"),
            notifier=notifier,
        )

    monkeypatch.setattr(
        trend_module, "_transition_delivery_receipt", original_transition
    )
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("sent retry must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("sent retry must not refetch"),
        notifier=notifier,
    )

    assert len(notifier.messages) == 1
    assert json.loads(recovered.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == "sent_prior_attempt"


def test_pending_delivery_crash_retries_frozen_text_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    original_send = trend_delivery_module.send_notification_with_results

    def crash_on_delivery(*args: object, **kwargs: object) -> object:
        if kwargs.get("channels") == {"feishu", "feishu_app"}:
            raise RuntimeError("crash before delivery result")
        return original_send(*args, **kwargs)

    monkeypatch.setattr(
        trend_delivery_module, "send_notification_with_results", crash_on_delivery
    )
    with pytest.raises(RuntimeError, match="crash before delivery result"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=RecordingFeishu(),
        )

    ledger_path = config.data_dir / "trend_a_share/daily_delivery/2026-07-14.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["status"] == "pending"
    monkeypatch.setattr(
        trend_delivery_module, "send_notification_with_results", original_send
    )
    notifier = RecordingFeishu()
    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("pending recovery must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("pending recovery must not refetch"),
        notifier=notifier,
    )

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["delivery_status"] == "sent"
    assert notifier.messages == [(ledger["title"], ledger["message"])]


@pytest.mark.parametrize("delivery_succeeds", [True, False])
def test_delivery_result_receipt_write_failure_uses_daily_ledger_without_refetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery_succeeds: bool,
) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingFeishu(fail=not delivery_succeeds)
    original_transition = trend_module._transition_delivery_receipt

    def fail_result_transition(
        *args: object, status: str, **kwargs: object
    ) -> object:
        if status in {"sent", "delivery_failed"}:
            raise OSError("delivery result receipt write failed")
        return original_transition(*args, status=status, **kwargs)

    monkeypatch.setattr(
        trend_module,
        "_transition_delivery_receipt",
        fail_result_transition,
    )
    with pytest.raises(OSError, match="receipt write failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    receipt_path = config.data_dir / "trend_a_share/delivery/2026-07-14.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "pending"
    assert len(notifier.messages) == 1

    monkeypatch.setattr(
        trend_module, "_transition_delivery_receipt", original_transition
    )
    recovered_notifier = RecordingFeishu()
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("unknown recovery must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("unknown recovery must not refetch"),
        notifier=recovered_notifier,
    )

    assert json.loads(recovered.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == ("sent_prior_message" if delivery_succeeds else "sent")
    assert len(notifier.messages) == 1
    assert recovered_notifier.messages == (
        [] if delivery_succeeds else notifier.messages
    )


def test_sent_prior_attempt_status_is_not_reported_as_delivery_failure() -> None:
    notifier = RecordingMacOS()

    trend_module._notify_delivery_status(
        notifier,
        run_date="2026-07-14",
        delivery_status="sent_prior_attempt",
    )

    assert notifier.messages == [
        (
            "A股趋势计划已生成",
            "2026-07-14 本地报告已冻结；飞书状态：sent_prior_attempt",
        )
    ]


def test_delivery_failed_stage_retries_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    original_replace = Path.replace
    failed_freeze = False

    def fail_final_once(path: Path, target: Path) -> Path:
        nonlocal failed_freeze
        if (
            not failed_freeze
            and Path(target).parent == config.reports_dir / "trend_a_share"
        ):
            failed_freeze = True
            raise OSError("final freeze failed")
        return original_replace(path, target)

    class FailDeliveryOnce(RecordingFeishu):
        def notify(self, title: str, message: str) -> None:
            self.messages.append((title, message))
            if len(self.messages) == 1:
                raise RuntimeError("delivery failed")

    notifier = FailDeliveryOnce()
    monkeypatch.setattr(Path, "replace", fail_final_once)
    with pytest.raises(OSError, match="final freeze failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("failed delivery retry must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("failed delivery retry must not refetch"),
        notifier=notifier,
    )

    assert len(notifier.messages) == 2
    assert json.loads(result.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == "sent"


def test_existing_delivery_failed_report_retries_stage_without_refetch(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)

    class FailDeliveryOnce(RecordingFeishu):
        def notify(self, title: str, message: str) -> None:
            self.messages.append((title, message))
            if len(self.messages) == 1:
                raise RuntimeError("delivery failed")

    notifier = FailDeliveryOnce()
    first = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=notifier,
    )
    assert json.loads(first.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == "delivery_failed"

    retried = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("delivery retry must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("delivery retry must not refetch"),
        notifier=notifier,
    )

    assert len(notifier.messages) == 2
    assert retried.status == "generated"
    assert json.loads(retried.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == "sent"


def test_failed_retry_pending_receipt_write_failure_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)

    class FailDeliveryOnce(RecordingFeishu):
        def notify(self, title: str, message: str) -> None:
            self.messages.append((title, message))
            if len(self.messages) == 1:
                raise RuntimeError("delivery failed")

    notifier = FailDeliveryOnce()
    run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=notifier,
    )
    original_transition = trend_module._transition_delivery_receipt
    monkeypatch.setattr(
        trend_module,
        "_transition_delivery_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("pending receipt write failed")
        ),
    )

    with pytest.raises(OSError, match="pending receipt write failed"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: pytest.fail("failed retry must not refetch"),
            quote_factory=lambda **kwargs: pytest.fail("failed retry must not refetch"),
            notifier=notifier,
        )

    assert len(notifier.messages) == 1
    monkeypatch.setattr(
        trend_module, "_transition_delivery_receipt", original_transition
    )
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("failed retry must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("failed retry must not refetch"),
        notifier=notifier,
    )

    assert len(notifier.messages) == 2
    assert json.loads(recovered.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == "sent"


def test_failed_retry_crash_resends_frozen_text_from_daily_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingFeishu(fail=True),
    )
    receipt_path = config.data_dir / "trend_a_share/delivery/2026-07-14.json"
    ledger_path = config.data_dir / "trend_a_share/daily_delivery/2026-07-14.json"
    frozen = json.loads(ledger_path.read_text(encoding="utf-8"))
    original_send = trend_delivery_module.send_notification_with_results
    send_calls = 0

    def accepted_then_crashed(*args: object, **kwargs: object) -> object:
        nonlocal send_calls
        if kwargs.get("channels") == {"feishu", "feishu_app"}:
            send_calls += 1
            assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "pending"
            raise RuntimeError("crash after Feishu accepted message")
        return original_send(*args, **kwargs)

    monkeypatch.setattr(
        trend_delivery_module, "send_notification_with_results", accepted_then_crashed
    )
    with pytest.raises(RuntimeError, match="crash after Feishu"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            api_factory=lambda **kwargs: pytest.fail("retry must not refetch"),
            quote_factory=lambda **kwargs: pytest.fail("retry must not refetch"),
            notifier=RecordingFeishu(),
        )

    monkeypatch.setattr(
        trend_delivery_module, "send_notification_with_results", original_send
    )
    notifier = RecordingFeishu()
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: pytest.fail("unknown must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("unknown must not refetch"),
        notifier=notifier,
    )

    assert send_calls == 1
    assert json.loads(recovered.json_path.read_text(encoding="utf-8"))[
        "delivery_status"
    ] == "sent"
    assert notifier.messages == [(frozen["title"], frozen["message"])]


def test_revision_does_not_resend_semantic_message(tmp_path: Path) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingFeishu()
    first = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=notifier,
    )
    revision = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        revision=True,
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=notifier,
    )

    assert (first.report_path.name, revision.report_path.name) == (
        "2026-07-14.md",
        "2026-07-14-r1.md",
    )
    assert len(notifier.messages) == 1
    assert {
        path.stem
        for path in (config.data_dir / "trend_a_share/delivery").glob("*.json")
    } == {"2026-07-14", "2026-07-14-r1"}


def test_revision_recovers_same_stem_after_kill_between_final_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingFeishu()
    run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=notifier,
    )
    report_dir = config.reports_dir / "trend_a_share"
    original_replace = Path.replace

    def kill_before_revision_json(path: Path, target: Path) -> Path:
        if Path(target) == report_dir / "2026-07-14-r1.json":
            raise KeyboardInterrupt("killed between final replaces")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", kill_before_revision_json)
    with pytest.raises(KeyboardInterrupt, match="between final replaces"):
        run_a_share_trend_report(
            config=config,
            run_date="2026-07-14",
            revision=True,
            api_factory=lambda **kwargs: ReadyApi([]),
            quote_factory=lambda **kwargs: ReadyQuote([]),
            notifier=notifier,
        )

    assert (report_dir / "2026-07-14-r1.md").exists()
    assert not (report_dir / "2026-07-14-r1.json").exists()

    monkeypatch.setattr(Path, "replace", original_replace)
    recovered = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        revision=True,
        api_factory=lambda **kwargs: pytest.fail("half-pair recovery must not refetch"),
        quote_factory=lambda **kwargs: pytest.fail("half-pair recovery must not refetch"),
        notifier=notifier,
    )

    assert recovered.report_path.name == "2026-07-14-r1.md"
    assert recovered.json_path.name == "2026-07-14-r1.json"
    assert len(notifier.messages) == 1
    assert not (report_dir / "2026-07-14-r2.md").exists()
    assert not (report_dir / "2026-07-14-r2.json").exists()


def test_report_runner_keeps_files_when_feishu_delivery_fails_without_refetch(tmp_path: Path) -> None:
    calls: list[str] = []
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi(calls),
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        notifier=RecordingFeishu(fail=True),
    )
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert result.status == "generated"
    assert result.report_path.exists() and result.json_path.exists()
    assert payload["delivery_status"] == "delivery_failed"
    assert calls.count("api.snapshots") == 1


def test_report_runner_sends_v1_text_only_to_feishu_and_short_status_to_macos(tmp_path: Path) -> None:
    calls: list[str] = []
    feishu = RecordingFeishu()
    macos = RecordingMacOS()
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi(calls),
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        notifier=CompositeNotifier([feishu, macos]),
    )
    assert result.status == "generated"
    assert len(feishu.messages) == len(macos.messages) == 1
    assert feishu.messages[0][0] == "【东方财富｜A股趋势报告｜2026-07-15】"
    assert "# A股趋势操作计划" not in feishu.messages[0][1]
    assert "# A股趋势操作计划" not in macos.messages[0][1]


def test_report_runner_excludes_only_candidate_with_failed_kline(tmp_path: Path) -> None:
    calls: list[str] = []
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi(calls),
        quote_factory=lambda **kwargs: ReadyQuote(calls, failed_klines={"SH.000001"}),
        notifier=RecordingFeishu(),
    )
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["excluded"]["000001"] == ["atr_unavailable"]
    assert [item["symbol"] for item in payload["strategy_judgments"]["top10_candidates"]] == ["000002"]


@pytest.mark.parametrize("with_prior", [False, True])
def test_report_runner_degrades_holding_kline_without_blocking_report(
    tmp_path: Path, with_prior: bool
) -> None:
    config = trend_config(tmp_path)
    write_portfolio(config.portfolio, [portfolio_row(symbol="600009")])
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(config.portfolio, (timestamp, timestamp))
    if with_prior:
        write_protection_state(
            config.data_dir / "trend_a_share/protection_state.json",
            {"schema_version": 1, "positions": {"600009": {
                "initial_line": "8", "active_line": "8.5", "atr14": "1",
                "updated_for": "2026-07-13",
            }}},
        )
    calls: list[str] = []
    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi(calls),
        quote_factory=lambda **kwargs: ReadyQuote(calls, failed_klines={"SH.600009"}),
        notifier=RecordingFeishu(),
    )
    decision = json.loads(result.json_path.read_text(encoding="utf-8"))[
        "strategy_judgments"
    ]["holding_decisions"][0]
    if with_prior:
        assert (decision["action"], decision["active_line"]) == ("HOLD", "8.5")
    else:
        assert (decision["action"], decision["reason"]) == (
            "MANUAL_REVIEW", "holding_kline_unavailable"
        )


def test_report_runner_degrades_beijing_holding_kline_value_error(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    write_portfolio(config.portfolio, [portfolio_row(symbol="920000", name="北交所持仓")])
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(config.portfolio, (timestamp, timestamp))

    class BeijingApi(ReadyApi):
        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            rows = super().get_snapshots(**kwargs)
            for row in rows:
                if row["tmId"] == 920000:
                    row["tickerSymbol"] = "920000.BJ"
            return rows

    class RejectingBeijingQuote(ReadyQuote):
        def get_daily_kline(self, symbol: str, **kwargs: object) -> list[DailyKlineBar]:
            if symbol == "BJ.920000":
                raise ValueError("unsupported BJ symbol")
            return super().get_daily_kline(symbol, **kwargs)

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: BeijingApi([]),
        quote_factory=lambda **kwargs: RejectingBeijingQuote([]),
        notifier=RecordingFeishu(),
    )

    decision = json.loads(result.json_path.read_text(encoding="utf-8"))[
        "strategy_judgments"
    ]["holding_decisions"][0]
    assert (decision["symbol"], decision["action"]) == ("920000", "MANUAL_REVIEW")


def test_report_runner_snapshot_date_mismatch_uses_deadline_contract(tmp_path: Path) -> None:
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([], snapshot_date="2026-07-13"),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.json"))


@pytest.mark.parametrize(
    "snapshot_ids",
    [[1], [1, 2, 3], [1, 1, 2], [1, "bad"]],
    ids=["missing", "unexpected", "duplicate", "malformed"],
)
def test_report_runner_rejects_snapshot_tm_id_integrity_failures(
    tmp_path: Path, snapshot_ids: list[object]
) -> None:
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([], snapshot_ids=snapshot_ids),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.json"))


def test_report_runner_retries_systemic_kline_outage_without_formal_report(tmp_path: Path) -> None:
    outage = FutuQuoteError("network down", error_type="quote_server_interrupted")
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote(
            [], failed_klines={"SH.000001"}, kline_error=outage
        ),
        notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.md"))


def test_report_runner_rejects_invalid_live_billing_price(tmp_path: Path) -> None:
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([], invalid_billing=True),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.json"))


def test_report_runner_does_not_invent_zero_cost_when_balance_increases(
    tmp_path: Path,
) -> None:
    class IncreasedBalanceApi(ReadyApi):
        def get_account_balance(self) -> dict[str, object]:
            self.balance_calls += 1
            return {"balance": "99" if self.balance_calls == 1 else "100"}

    result = run_a_share_trend_report(
        config=trend_config(tmp_path),
        run_date="2026-07-14",
        api_factory=lambda **kwargs: IncreasedBalanceApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingFeishu(),
    )

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["actual_api_cost"] is None


def test_report_runner_losing_lock_does_not_overwrite_active_log(tmp_path: Path) -> None:
    config = trend_config(tmp_path)
    log_path = config.logs_dir / "trend_a_share/2026-07-14.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text('{"process_version":"active"}\n', encoding="utf-8")
    with RunLock(config.data_dir / "runs/.trend_a_share_report.lock"):
        with pytest.raises(RuntimeError, match="already active"):
            run_a_share_trend_report(config=config, run_date="2026-07-14")
    assert log_path.read_text(encoding="utf-8") == '{"process_version":"active"}\n'


def test_report_runner_uses_first_later_cn_session_across_closed_days(tmp_path: Path) -> None:
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote(
            [], trading_days=["2026-07-14", "2026-07-20", "2026-07-21"]
        ),
        notifier=RecordingFeishu(),
    )
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["execution_date"] == "2026-07-20"


def test_report_runner_lookup_miss_is_manual_but_transport_failure_blocks(tmp_path: Path) -> None:
    config = trend_config(tmp_path)
    write_portfolio(config.portfolio, [portfolio_row(symbol="600009")])
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(config.portfolio, (timestamp, timestamp))
    calls: list[str] = []
    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi(calls, holding_error=TrendAnimalsLookupError("missing")),
        quote_factory=lambda **kwargs: ReadyQuote(calls), notifier=RecordingFeishu(),
    )
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    decision = payload["strategy_judgments"]["holding_decisions"][0]
    assert (decision["symbol"], decision["action"]) == ("600009", "MANUAL_REVIEW")

    blocked = trend_config(tmp_path / "blocked")
    write_portfolio(blocked.portfolio, [portfolio_row(symbol="600009")])
    os.utime(blocked.portfolio, (timestamp, timestamp))
    blocked_result = run_a_share_trend_report(
        config=blocked, run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([], holding_error=TrendAnimalsError("transport")),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=RecordingMacOS(),
    )
    assert blocked_result.status == "failed"


def test_report_runner_redacts_api_key_from_all_outputs(tmp_path: Path) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingMacOS()

    class SecretApi(ReadyApi):
        def get_update_status(self) -> list[dict[str, object]]:
            raise TrendAnimalsError(f"failed {config.trend_animals_api_key}")

    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: SecretApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=notifier,
    )
    captured = repr(result) + repr(notifier.messages)
    for path in [*config.logs_dir.rglob("*"), *config.reports_dir.rglob("*")]:
        if path.is_file():
            captured += path.read_text(encoding="utf-8")
    assert config.trend_animals_api_key not in captured
