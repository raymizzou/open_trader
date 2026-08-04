from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import pytest
import open_trader.a_share_trend as trend_module
import open_trader.trend_delivery as trend_delivery_module
from open_trader import trend_review

from open_trader.a_share_trend import (
    A_SHARE_INDUSTRY_FIELDS,
    A_SHARE_SNAPSHOT_FIELDS,
    UNIFIED_TREND_FIELDS,
    AShareTrendRunResult,
    AccountPosition,
    AccountSnapshot,
    CandidateInput,
    HoldingSnapshot,
    RealHoldingInput,
    TrendReport,
    atr14,
    build_candidate_list,
    build_report as _build_report,
    estimate_buy_actions,
    evaluate_candidate,
    load_futu_simulate_trend_account,
    load_protection_state,
    load_watch_events,
    plan_rotation_pairs,
    plan_rotation_pairs_with_comparisons,
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
from open_trader.trend_animals import (
    TrendAnimalsError,
    TrendAnimalsLookupError,
    TrendAnimalsNoCurrentRowsError,
)
from open_trader.trend_kelly import TrendKellyRound
from open_trader.strategy_drawdown import automatic_bootstrap_strategy_drawdown
from open_trader.trend_industry_context import IndustryContext
from open_trader.trend_industry_context import _context_to_mapping


SHANGHAI = ZoneInfo("Asia/Shanghai")
MISSING_FRESH = object()
ACCOUNT_SNAPSHOT = {
    "snapshot_generation": "sha256:" + "a" * 64,
    "account_generation": "sha256:" + "b" * 64,
    "status": "healthy",
    "sources": {
        "account": {
            "status": "healthy",
            "as_of": "2026-07-14T12:00:00+08:00",
            "reason": None,
            "brokers": {
                broker: {
                    "source_kind": "live" if broker == "tiger" else "statement",
                    "data_as_of": "2026-07-14T12:00:00+08:00",
                    "last_success_at": "2026-07-14T12:00:00+08:00",
                    "status": "healthy",
                    "reason": None,
                }
                for broker in ("eastmoney", "futu", "phillips", "tiger")
            },
        },
        "quotes": {
            "status": "healthy",
            "as_of": "2026-07-14T12:00:00+08:00",
            "reason": None,
        },
    },
    "positions": [],
    "cash_balances": [
        {
            "broker": broker,
            "account_alias": f"{broker}_main",
            "currency": currency,
            "cash_balance": "0",
            "available_balance": "0",
        }
        for broker, currency in (
            ("eastmoney", "CNY"), ("phillips", "HKD"), ("tiger", "USD")
        )
    ],
}
ACCOUNT_INPUT = {
    key: ACCOUNT_SNAPSHOT[key]
    for key in ("snapshot_generation", "account_generation", "status")
}


def build_report(*args: object, **kwargs: object) -> TrendReport:
    kwargs.setdefault("account_input", ACCOUNT_INPUT)
    return _build_report(*args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def account_http_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trend_module,
        "fetch_account_snapshot",
        lambda: copy.deepcopy(ACCOUNT_SNAPSHOT),
        raising=False,
    )


class DefaultSimAccountClient:
    def __init__(self, **kwargs: object) -> None:
        self.acc_id = int(kwargs["simulate_acc_id"])

    def account_snapshot(self) -> dict[str, object]:
        return {
            "acc_id": self.acc_id,
            "net_value": "100000",
            "cash": "100000",
            "positions": [],
        }

    def close(self) -> None:
        pass


def simulation_account_with_positions(
    *codes: str,
) -> type[DefaultSimAccountClient]:
    class SimAccountClient(DefaultSimAccountClient):
        def account_snapshot(self) -> dict[str, object]:
            return {
                **super().account_snapshot(),
                "positions": [
                    {
                        "code": code,
                        "stock_name": code.split(".", 1)[-1],
                        "qty": "100",
                        "cost_price": "9.5",
                        "market_val": "1000",
                    }
                    for code in codes
                ],
            }

    return SimAccountClient


@pytest.fixture(autouse=True)
def default_simulation_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trend_module,
        "FutuSimulateOrderExecutionClient",
        DefaultSimAccountClient,
    )


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        (
            {"acc_id": 101, "net_value": "0", "cash": "100", "positions": []},
            "net value must be positive",
        ),
        (
            {"acc_id": 101, "net_value": "100", "cash": "-1", "positions": []},
            "cash must be nonnegative",
        ),
        (
            {
                "acc_id": 101,
                "net_value": "100",
                "cash": "100",
                "positions": [
                    {"code": "US.AAPL", "qty": "1", "market_val": "10"}
                ],
            },
            "position market does not match CN",
        ),
        (
            {
                "acc_id": 101,
                "net_value": "100",
                "cash": "100",
                "positions": [{"code": "SH.600001", "qty": "-1", "market_val": "10"}],
            },
            "position quantity must be nonnegative",
        ),
        (
            {
                "acc_id": 101,
                "net_value": "100",
                "cash": "100",
                "positions": [{"code": "SH.600001", "qty": "1"}],
            },
            "position market value is invalid",
        ),
    ],
)
def test_futu_simulation_account_rejects_invalid_boundary_rows(
    snapshot: dict[str, object], message: str
) -> None:
    class Client:
        def account_snapshot(self) -> dict[str, object]:
            return snapshot

        def close(self) -> None:
            pass

    with pytest.raises(ValueError, match=message):
        load_futu_simulate_trend_account(
            host="127.0.0.1",
            port=11111,
            simulate_acc_id=101,
            market="CN",
            expected_date="2026-07-17",
            account_factory=lambda **kwargs: Client(),
        )


def test_futu_simulation_account_borrows_existing_client() -> None:
    class Client:
        closed = False

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100",
                "cash": "100",
                "positions": [],
            }

        def close(self) -> None:
            self.closed = True

    client = Client()
    account = load_futu_simulate_trend_account(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=101,
        market="CN",
        expected_date="2026-07-22",
        account_client=client,
        account_factory=lambda **_kwargs: pytest.fail(
            "borrowed account opened another context"
        ),
    )

    assert account.net_value == Decimal("100")
    assert client.closed is False


def test_futu_simulation_account_ignores_explicit_zero_quantity_rows() -> None:
    class Client:
        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100",
                "cash": "100",
                "positions": [{"code": "US.AAPL", "qty": "0"}],
            }

        def close(self) -> None:
            pass

    account = load_futu_simulate_trend_account(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=101,
        market="CN",
        expected_date="2026-07-17",
        account_factory=lambda **kwargs: Client(),
    )

    assert account.positions == ()


def test_futu_simulation_account_preserves_exact_position_code() -> None:
    class Client:
        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100",
                "cash": "50",
                "positions": [
                    {
                        "code": "SH.000001",
                        "stock_name": "上证测试",
                        "qty": "100",
                        "market_val": "50",
                    }
                ],
            }

        def close(self) -> None:
            pass

    account = load_futu_simulate_trend_account(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=101,
        market="CN",
        expected_date="2026-07-17",
        account_factory=lambda **_kwargs: Client(),
    )

    assert account.positions[0].symbol == "000001"
    assert account.positions[0].futu_symbol == "SH.000001"


def test_unified_trend_fields_match_the_paid_catalog_selection() -> None:
    from open_trader.a_share_trend import UNIFIED_TREND_FIELDS

    assert UNIFIED_TREND_FIELDS == (
        "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
        "tradableFlag", "industryTmId", "industryName", "priceIndex",
        "marketCap", "amount1d", "isTrendRightSide",
        "trendTemperatureCurr", "trendTemperaturePrev",
        "daysSinceTrendEntry", "gainSinceTrendEntry",
        "trendPhasePrev", "trendPhaseCurr", "trendStrengthLocalCurr",
        "trendStrengthLocalChange", "trendStrengthGlobalCurr",
        "trendStrengthLocalPrevWeek", "trendStrengthLocalPrevMonth",
        "stopwinFlagByDangerSignal",
        "stopwinFlagByBoilingTemperature",
        "stopwinFlagByPopChampagne", "tickerLabels",
    )


def candidate(
    symbol: str,
    *,
    tm_id: int | None = None,
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
    industry_tm_id: int | None = 700001,
    industry_temperature: str | None = "热",
    filter_price: str | None = "10",
    market_cap: str | None = "100",
    temperature_prev: str | None = "温",
    temperature_curr: str | None = "热",
    phase: str | None = "立夏",
    asset: str = "A股",
    global_strength: str | None = None,
) -> CandidateInput:
    return CandidateInput(
        tm_id=(int(symbol) if tm_id is None and symbol.isdigit() else tm_id or 900002),
        symbol=symbol,
        exchange=exchange,
        name=f"股票{symbol}" if name is None else name,
        asset=asset,
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
        industry_tm_id=industry_tm_id,
        industry_temperature=industry_temperature,
        filter_price=None if filter_price is None else Decimal(filter_price),
        market_cap=None if market_cap is None else Decimal(market_cap),
        temperature_prev=temperature_prev,
        temperature_curr=temperature_curr,
        phase=phase,
        global_strength=(
            None if global_strength is None else Decimal(global_strength)
        ),
    )


def bars(
    count: int = 15,
    *,
    close: float = 10,
    low: float = 9,
    end_date: str = "2026-07-14",
) -> list[DailyKlineBar]:
    end = datetime.fromisoformat(end_date)
    return [
        DailyKlineBar(
            date=(end - timedelta(days=count - index - 1)).date().isoformat(),
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


def serialized_account(*, fresh: object = MISSING_FRESH) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_date": "2026-07-14",
        "net_value": "100000",
        "available_cash": "50000",
        "positions": [],
        "exceptions": [],
    }
    if fresh is not MISSING_FRESH:
        payload["fresh"] = fresh
    return payload


def serialized_position() -> dict[str, object]:
    return {
        "symbol": "600001",
        "name": "测试股票",
        "asset_class": "stock",
        "quantity": "100",
        "avg_cost_price": "9.5",
        "market_value": "1000",
    }


def holding(
    symbol: str,
    *,
    tm_id: int | None = None,
    right_side: bool | None = True,
    danger: bool | None = False,
    boiling: bool | None = False,
    champagne: bool | None = False,
    industry: str = "电力",
    industry_tm_id: int | None = 700001,
    industry_temperature: str | None = "热",
    asset: str = "A股",
    filter_price: str | None = "10",
    market_cap: str | None = "100",
    strength: str | None = "96",
    temperature_prev: str | None = "温",
    temperature_curr: str | None = "热",
    phase: str | None = "立夏",
    days: int | None = None,
    global_strength: str | None = None,
) -> HoldingSnapshot:
    return HoldingSnapshot(
        tm_id=(int(symbol) if tm_id is None and symbol.isdigit() else tm_id or 900001),
        symbol=symbol,
        exchange="SH",
        name=f"股票{symbol}",
        as_of_date="2026-07-14",
        right_side=right_side,
        danger=danger,
        boiling=boiling,
        champagne=champagne,
        asset=asset,
        industry=industry,
        industry_tm_id=industry_tm_id,
        industry_temperature=industry_temperature,
        filter_price=None if filter_price is None else Decimal(filter_price),
        market_cap=None if market_cap is None else Decimal(market_cap),
        strength=None if strength is None else Decimal(strength),
        temperature_prev=temperature_prev,
        temperature_curr=temperature_curr,
        phase=phase,
        days=days,
        global_strength=(
            None if global_strength is None else Decimal(global_strength)
        ),
    )


def test_same_category_rotation_uses_local_strength() -> None:
    pairs, comparisons = plan_rotation_pairs_with_comparisons(
        holdings=(
            holding(
                "PM", asset="美股", strength="76", global_strength="86.18",
            ),
        ),
        candidates=(
            candidate(
                "SHEL", asset="美股", strength="98.6", global_strength="95.36",
            ),
        ),
        entry_weight=Decimal("0.04"),
        available_slots=0,
        pair_slots=(0, 1),
        market="US",
    )

    assert [(pair.sell_symbol, pair.buy_symbol) for pair in pairs] == [
        ("PM", "SHEL"),
    ]
    assert comparisons[0].strength_basis == "local"
    assert comparisons[0].strength_gap == Decimal("22.6")
    assert comparisons[0].outcome == "planned"


def test_cross_category_rotation_uses_global_strength() -> None:
    pairs, comparisons = plan_rotation_pairs_with_comparisons(
        holdings=(
            holding(
                "SPY", asset="美国ETF", strength="99", global_strength="70",
            ),
        ),
        candidates=(
            candidate(
                "SHEL", asset="美股", strength="75", global_strength="90",
            ),
        ),
        entry_weight=Decimal("0.04"),
        available_slots=0,
        pair_slots=(0, 1),
        market="US",
    )

    assert len(pairs) == 1
    assert comparisons[0].strength_basis == "global"
    assert comparisons[0].strength_gap == Decimal("20")


def test_rotation_comparison_does_not_fallback_between_strength_scopes() -> None:
    _, same_category = plan_rotation_pairs_with_comparisons(
        holdings=(holding("PM", asset="美股", strength=None, global_strength="10"),),
        candidates=(candidate("SHEL", asset="美股", strength="99", global_strength="90"),),
        entry_weight=Decimal("0.04"), available_slots=0, pair_slots=(0, 1), market="US",
    )
    _, cross_category = plan_rotation_pairs_with_comparisons(
        holdings=(holding("SPY", asset="美国ETF", strength="1", global_strength=None),),
        candidates=(candidate("SHEL", asset="美股", strength="99", global_strength="90"),),
        entry_weight=Decimal("0.04"), available_slots=0, pair_slots=(0, 1), market="US",
    )

    assert same_category[0].outcome == "data_unavailable"
    assert cross_category[0].outcome == "data_unavailable"


def test_rotation_pairs_weakest_with_strongest_at_inclusive_twenty_points() -> None:
    """A 20-point edge is actionable; 19.9 would not be."""
    pairs = plan_rotation_pairs(
        holdings=[
            holding("100001", strength="10", global_strength="10"),
            holding("100002", strength="20", global_strength="20"),
            holding("100003", strength="80", global_strength="80"),
        ],
        candidates=[
            candidate("200001", asset="ETF基金", strength="96", global_strength="90"),
            candidate("200002", asset="ETF基金", strength="96", global_strength="40"),
        ],
        entry_weight=Decimal("0.04"),
        available_slots=0,
        pair_slots=(0, 1),
    )

    assert [(pair.sell_symbol, pair.buy_symbol, pair.strength_gap) for pair in pairs] == [
        ("100001", "200001", Decimal("80")),
        ("100002", "200002", Decimal("20")),
    ]


def test_rotation_pairs_reject_19_9_and_use_stable_unique_symbols() -> None:
    pairs = plan_rotation_pairs(
        holdings=[
            holding("100002", strength="20", global_strength="20"),
            holding("100001", strength="20", global_strength="20"),
        ],
        candidates=[
            candidate("200002", strength="39.9", global_strength="39.9"),
            candidate("200001", strength="40", global_strength="40"),
            candidate("200001", strength="99", global_strength="99"),
            candidate("100001", strength="100", global_strength="100"),
        ],
        entry_weight=Decimal("0.04"),
        available_slots=0,
        pair_slots=(0, 1),
    )

    assert [(pair.sell_symbol, pair.buy_symbol) for pair in pairs] == [
        ("100001", "200001"),
    ]
    assert plan_rotation_pairs(
        holdings=[holding("100001", global_strength="10")],
        candidates=[candidate("200001", global_strength="90")],
        entry_weight=Decimal("0.04"),
        available_slots=1,
        pair_slots=(0, 1),
    ) == ()


def test_full_simulate_account_freezes_two_rotation_pairs_after_buy_planning() -> None:
    held_symbols = tuple(f"10{index:04d}" for index in range(10))
    simulated = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("0"),
        positions=tuple(
            AccountPosition(symbol, symbol, "stock", Decimal("500"), Decimal("10"), Decimal("5000"))
            for symbol in held_symbols
        ),
        exceptions=(),
    )
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199),
        allocation=allocation_for("CN", rank=2, entry_weight="0.04"),
    )
    allocation = allocation_for("CN", rank=2, entry_weight="0.04")
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=simulated,
        candidates=[
            candidate("200001", asset="ETF基金", strength="96", global_strength="90"),
            candidate("200002", asset="ETF基金", strength="96", global_strength="40"),
        ],
        holding_snapshots={
            symbol: holding(
                symbol,
                strength=("10" if index == 0 else "20"),
                global_strength=("10" if index == 0 else "20"),
            )
            for index, symbol in enumerate(held_symbols)
        },
        bars_by_symbol={symbol: bars() for symbol in held_symbols},
        prior_state={
            "positions": {
                symbol: {
                    "initial_line": "10", "active_line": "10", "atr14": "0.5",
                    "tracking_active": False,
                }
                for symbol in held_symbols
            }
        },
        strategy_snapshot=strategy,
        drawdown_summary=active_drawdown_for(strategy, equity="100000"),
        allocation_reference=allocation,
    )

    assert [(pair.sell_symbol, pair.buy_symbol) for pair in built.simulate_rotation_pairs] == [
        ("100000", "200001"),
        ("100001", "200002"),
    ]
    assert built.real_rotation_pairs == ()
    payload = trend_module._report_payload(built)
    assert payload["allocation"] == {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": "b" * 64,
        "allocation_date": "2026-08-03",
        "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
        "roots": allocation["snapshot"]["roots"],
        "markets": allocation["snapshot"]["markets"],
    }
    assert [
        (pair["sell_symbol"], pair["buy_symbol"])
        for pair in payload["strategy_judgments"]["simulate_rotation_pairs"]
    ] == [("100000", "200001"), ("100001", "200002")]
    comparisons = payload["strategy_judgments"][
        "simulate_rotation_comparisons"
    ]
    assert comparisons[0]["strength_basis"] == "global"
    assert comparisons[0]["strength_gap"] == "80"
    assert comparisons[0]["outcome"] == "planned"
    assert trend_module.valid_frozen_report_contract(payload)
    invalid_comparison = json.loads(json.dumps(payload))
    invalid_comparison["strategy_judgments"][
        "simulate_rotation_comparisons"
    ][0]["strength_gap"] = "79"
    assert not trend_module.valid_frozen_report_contract(invalid_comparison)
    valid_real_pair = json.loads(json.dumps(payload))
    real_pair = copy.deepcopy(
        valid_real_pair["strategy_judgments"]["simulate_rotation_pairs"][0]
    )
    real_pair.update(
        sell_symbol="REAL",
        sell_name="Real",
        sell_futu_symbol="SH.REAL",
        execution_mode="manual",
    )
    real_comparison = copy.deepcopy(
        valid_real_pair["strategy_judgments"]["simulate_rotation_comparisons"][0]
    )
    real_comparison.update(sell_symbol="REAL", sell_name="Real")
    valid_real_pair["strategy_judgments"].update(
        real_holding_decisions=[{"symbol": "REAL"}],
        real_holding_decisions_status="available",
        real_holding_decisions_source={},
        real_rotation_pairs=[real_pair],
        real_rotation_comparisons=[real_comparison],
    )
    assert trend_module.valid_frozen_report_contract(valid_real_pair)

    allocationless_pairs = json.loads(json.dumps(payload))
    del allocationless_pairs["allocation"]
    assert not trend_module.valid_frozen_report_contract(allocationless_pairs)
    allocation_only_without_snapshot = json.loads(json.dumps(allocationless_pairs))
    allocation_only_without_snapshot["strategy_judgments"].pop("simulate_rotation_pairs")
    allocation_only_without_snapshot["strategy_judgments"].pop("real_rotation_pairs")
    for name in (
        "allocation_snapshot_path", "allocation_snapshot_sha256",
        "allocation_rank", "allocation_score", "allocation_score_source",
        "target_weight", "nominal_weight",
    ):
        allocation_only_without_snapshot["strategy_snapshot"]["parameters"].pop(name)
    assert not trend_module.valid_frozen_report_contract(
        allocation_only_without_snapshot
    )
    for market_value in (None, ""):
        missing_market = json.loads(json.dumps(allocation_only_without_snapshot))
        if market_value is None:
            missing_market["strategy_snapshot"].pop("market")
        else:
            missing_market["strategy_snapshot"]["market"] = market_value
        assert not trend_module.valid_frozen_report_contract(missing_market)
    historical = json.loads(json.dumps(allocation_only_without_snapshot))
    historical.pop("account_input", None)
    historical["strategy_snapshot"] = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v10",
    )
    historical["metadata"]["market"] = "CN"
    assert trend_module.valid_frozen_report_contract(historical)
    with_account_input = copy.deepcopy(historical)
    with_account_input["account_input"] = {
        "snapshot_generation": "sha256:" + "a" * 64,
        "account_generation": "sha256:" + "b" * 64,
        "status": "healthy",
    }
    assert trend_module.valid_frozen_report_contract(with_account_input)
    invalid_account_input = copy.deepcopy(with_account_input)
    invalid_account_input["account_input"]["status"] = "unavailable"
    assert not trend_module.valid_frozen_report_contract(invalid_account_input)
    mismatched_historical_identity = json.loads(json.dumps(historical))
    mismatched_historical_identity["strategy_snapshot"]["strategy_id"] = (
        "trend_animals_warm_to_hot/CN/v11"
    )
    assert not trend_module.valid_frozen_report_contract(
        mismatched_historical_identity
    )
    cross_market_historical_identity = json.loads(json.dumps(historical))
    cross_market_historical_identity["strategy_snapshot"].update(
        market="HK", strategy_id="trend_animals_warm_to_hot/HK/v10",
    )
    assert not trend_module.valid_frozen_report_contract(
        cross_market_historical_identity
    )
    for market, version, pools in (
        ("CN", "v7", (622466, 697199)),
        ("US", "v5", (622460, 705013)),
    ):
        legacy = json.loads(json.dumps(historical))
        legacy["metadata"]["market"] = market
        legacy["strategy_snapshot"] = trend_module.live_trend_strategy_snapshot(
            market, "abc123", pools, strategy_version=version,
        )
        assert trend_module.valid_frozen_report_contract(legacy)
        wrong_id = json.loads(json.dumps(legacy))
        wrong_id["strategy_snapshot"]["strategy_id"] = (
            "trend_animals_warm_to_hot/ZZ/v999"
        )
        assert not trend_module.valid_frozen_report_contract(wrong_id)
        blank_market = json.loads(json.dumps(legacy))
        blank_market["strategy_snapshot"]["market"] = ""
        assert not trend_module.valid_frozen_report_contract(blank_market)
    allocation_parameters_without_snapshot = json.loads(json.dumps(historical))
    allocation_parameters_without_snapshot["strategy_snapshot"]["parameters"][
        "allocation_rank"
    ] = 1
    assert not trend_module.valid_frozen_report_contract(
        allocation_parameters_without_snapshot
    )
    assert "模拟盘自动轮换" not in render_markdown(
        replace(built, allocation=None)
    )
    markdown = render_markdown(built)
    comparison_only = replace(
        built,
        simulate_rotation_pairs=(),
        simulate_rotation_comparisons=(replace(
            built.simulate_rotation_comparisons[0],
            strength_gap=Decimal("19.9"),
            outcome="gap_below_threshold",
            reason="强度差 19.9 小于门槛 20",
        ),),
    )
    comparison_markdown = render_markdown(comparison_only)
    assert "未触发" in comparison_markdown
    assert "门槛 20" in comparison_markdown
    assert "还差 0.1" in comparison_markdown
    assert "无。" not in comparison_markdown.split("## 模拟盘自动轮换", 1)[1].split("##", 1)[0]
    ordered_payload = json.loads(json.dumps(payload))
    ordered_payload["strategy_judgments"]["formal_actions"] = [
        {"action": "SELL_ALL", "symbol": "EXIT", "name": "Exit", "reason": "danger_signal"},
        {"action": "BUY", "symbol": "ENTRY", "name": "Entry", "estimated_shares": 100, "target_amount": "1000"},
    ]
    _, feishu = render_trend_feishu_text(
        ordered_payload, broker_label="东方财富", market_label="A股"
    )
    for text in (markdown, feishu):
        assert "市场资源排名" in text
        assert "模拟盘自动轮换" in text
        assert "MARKET 卖出全成后才买入" in text
    assert (
        feishu.index("市场资源排名")
        < feishu.index("\n卖出\n")
        < feishu.index("模拟盘自动轮换")
        < feishu.index("\n买入\n")
    )

    invalid_payloads = []
    wrong_hash = json.loads(json.dumps(payload))
    wrong_hash["allocation"]["sha256"] = "A" * 64
    invalid_payloads.append(wrong_hash)
    moving_pointer = json.loads(json.dumps(payload))
    moving_pointer["allocation"]["daily_path"] = "data/trend_allocation/latest.json"
    invalid_payloads.append(moving_pointer)
    missing_root_date = json.loads(json.dumps(payload))
    del missing_root_date["allocation"]["roots"]["CN"]["stock"]["as_of_date"]
    invalid_payloads.append(missing_root_date)
    duplicate_rank = json.loads(json.dumps(payload))
    duplicate_rank["allocation"]["markets"]["HK"]["rank"] = 2
    invalid_payloads.append(duplicate_rank)
    below_gap = json.loads(json.dumps(payload))
    below_gap["strategy_judgments"]["simulate_rotation_pairs"][0]["strength_gap"] = "19.9"
    invalid_payloads.append(below_gap)
    too_many = json.loads(json.dumps(payload))
    too_many["strategy_judgments"]["simulate_rotation_pairs"].append(
        copy.deepcopy(too_many["strategy_judgments"]["simulate_rotation_pairs"][0])
    )
    invalid_payloads.append(too_many)
    wrong_mode = json.loads(json.dumps(payload))
    wrong_mode["strategy_judgments"]["simulate_rotation_pairs"][0]["execution_mode"] = "manual"
    invalid_payloads.append(wrong_mode)
    wrong_real_mode = json.loads(json.dumps(payload))
    real_pair = copy.deepcopy(
        wrong_real_mode["strategy_judgments"]["simulate_rotation_pairs"][0]
    )
    real_pair["execution_mode"] = "automatic"
    wrong_real_mode["strategy_judgments"]["real_rotation_pairs"] = [real_pair]
    invalid_payloads.append(wrong_real_mode)
    wrong_date = json.loads(json.dumps(payload))
    wrong_date["strategy_judgments"]["simulate_rotation_pairs"][0]["execution_date"] = "2026-07-16"
    invalid_payloads.append(wrong_date)
    absent_candidate = json.loads(json.dumps(payload))
    absent_candidate["strategy_judgments"]["simulate_rotation_pairs"][0]["buy_symbol"] = "MISSING"
    invalid_payloads.append(absent_candidate)
    mismatched_strategy_allocation = json.loads(json.dumps(payload))
    mismatched_strategy_allocation["strategy_snapshot"]["parameters"].update({
        "allocation_snapshot_path": "data/trend_allocation/daily/2026-08-04.json",
        "allocation_snapshot_sha256": "c" * 64,
        "allocation_rank": 1,
        "allocation_score": "99",
        "allocation_score_source": "A股",
        "target_weight": "0.06",
        "nominal_weight": "0.60",
    })
    invalid_payloads.append(mismatched_strategy_allocation)
    mismatched_market = json.loads(json.dumps(payload))
    mismatched_market["metadata"]["market"] = "HK"
    invalid_payloads.append(mismatched_market)
    predecessor_with_allocation = json.loads(json.dumps(payload))
    predecessor_with_allocation["strategy_snapshot"] = (
        trend_module.live_trend_strategy_snapshot(
            "CN", "abc123", (622466, 697199), strategy_version="v10",
        )
    )
    invalid_payloads.append(predecessor_with_allocation)
    boolean_rank = json.loads(json.dumps(payload))
    boolean_rank["strategy_snapshot"]["parameters"]["allocation_rank"] = True
    invalid_payloads.append(boolean_rank)

    assert not any(
        trend_module.valid_frozen_report_contract(invalid)
        for invalid in invalid_payloads
    )


def test_new_report_construction_and_serialization_require_account_input() -> None:
    with pytest.raises(
        ValueError,
        match="new Trend reports require Account snapshot identity",
    ):
        _build_report(
            as_of_date="2026-07-14",
            execution_date="2026-07-15",
            account=AccountSnapshot(
                source_date="2026-07-14",
                fresh=True,
                net_value=Decimal("1000"),
                available_cash=Decimal("1000"),
                positions=(),
                exceptions=(),
            ),
            candidates=(),
            holding_snapshots={},
            bars_by_symbol={},
        )

    with pytest.raises(
        ValueError,
        match="new Trend reports require Account snapshot identity",
    ):
        trend_module._report_payload(replace(report(), account_input={}))


def test_rotation_keeps_the_ordinary_risk_data_gate() -> None:
    held_symbols = tuple(f"10{index:04d}" for index in range(10))
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199),
        allocation=allocation_for("CN", rank=2, entry_weight="0.04"),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14", fresh=True,
            net_value=Decimal("100000"), available_cash=Decimal("0"),
            positions=tuple(
                AccountPosition(
                    symbol, symbol, "stock", Decimal("500"), Decimal("10"),
                    Decimal("5000"),
                )
                for symbol in held_symbols
            ),
            exceptions=(),
        ),
        candidates=[candidate("200001", global_strength="90")],
        holding_snapshots={
            symbol: holding(symbol, global_strength="10")
            for symbol in held_symbols
        },
        bars_by_symbol={symbol: bars() for symbol in held_symbols},
        prior_state={
            "positions": {
                symbol: {
                    "initial_line": "10", "active_line": "10", "atr14": "0.5",
                    "tracking_active": False,
                }
                for symbol in held_symbols
            }
        },
        kelly_data_reason="frozen Kelly inputs unavailable",
        strategy_snapshot=strategy,
        drawdown_summary=active_drawdown_for(strategy, equity="100000"),
    )

    assert built.risk_summary["status"] == "paused"
    assert built.simulate_rotation_pairs == ()


def test_cold_start_without_allocation_never_plans_rotation() -> None:
    held_symbols = tuple(f"10{index:04d}" for index in range(10))
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199)
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14", fresh=True,
            net_value=Decimal("100000"), available_cash=Decimal("0"),
            positions=tuple(
                AccountPosition(
                    symbol, symbol, "stock", Decimal("500"), Decimal("10"),
                    Decimal("5000"),
                )
                for symbol in held_symbols
            ),
            exceptions=(),
        ),
        candidates=[candidate("200001", global_strength="90")],
        holding_snapshots={
            symbol: holding(symbol, global_strength="10")
            for symbol in held_symbols
        },
        bars_by_symbol={symbol: bars() for symbol in held_symbols},
        prior_state={
            "positions": {
                symbol: {
                    "initial_line": "10", "active_line": "10", "atr14": "0.5",
                    "tracking_active": False,
                }
                for symbol in held_symbols
            }
        },
        strategy_snapshot=strategy,
        drawdown_summary=active_drawdown_for(strategy, equity="100000"),
    )

    assert strategy["strategy_version"] == "v10"
    assert built.simulate_rotation_pairs == ()


def test_real_rotation_plan_is_independent_of_simulate_account() -> None:
    simulated_symbols = tuple(f"10{index:04d}" for index in range(10))
    real_symbols = tuple(f"30{index:04d}" for index in range(10))
    simulated = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        positions=tuple(
            AccountPosition(symbol, symbol, "stock", Decimal("500"), Decimal("10"), Decimal("5000"))
            for symbol in simulated_symbols
        ),
        exceptions=(),
    )
    real = RealHoldingInput(
        status="available",
        reason="",
        source={"broker": "eastmoney"},
        positions=tuple(
            AccountPosition(symbol, symbol, "stock", Decimal("500"), Decimal("10"), Decimal("5000"))
            for symbol in real_symbols
        ),
        holding_snapshots={
            symbol: (
                None
                if symbol == real_symbols[-1]
                else holding(symbol, strength="60", global_strength="60")
            )
            for symbol in real_symbols
        },
        bars_by_symbol={symbol: bars() for symbol in real_symbols},
        prior_state={
            "positions": {
                symbol: {
                    "initial_line": "10", "active_line": "10", "atr14": "0.5",
                    "tracking_active": False,
                }
                for symbol in real_symbols
            }
        },
        net_value=Decimal("50000"),
        available_cash=Decimal("10000"),
        position_count=10,
        instrument_ids_by_symbol={
            symbol: f"ins_{symbol}" for symbol in real_symbols
        },
        blocked_instrument_ids={
            f"ins_{real_symbols[0]}": "account_broker_stale:eastmoney"
        },
    )
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199),
        allocation=allocation_for("CN", rank=2, entry_weight="0.04"),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=simulated,
        candidates=[
            candidate(real_symbols[-1], asset="ETF基金", strength="96", global_strength="100"),
            candidate("200001", asset="ETF基金", strength="96", global_strength="90"),
            candidate("200002", asset="ETF基金", strength="96", global_strength="40"),
        ],
        holding_snapshots={
            symbol: holding(symbol, strength="10", global_strength="10")
            for symbol in simulated_symbols
        },
        bars_by_symbol={symbol: bars() for symbol in simulated_symbols},
        prior_state={
            "positions": {
                symbol: {
                    "initial_line": "10", "active_line": "10", "atr14": "0.5",
                    "tracking_active": False,
                }
                for symbol in simulated_symbols
            }
        },
        real_holdings=real,
        strategy_snapshot=strategy,
        drawdown_summary=active_drawdown_for(strategy, equity="100000"),
    )

    assert [pair.sell_symbol for pair in built.simulate_rotation_pairs] == [
        "100000", "100001",
    ]
    assert [pair.buy_symbol for pair in built.simulate_rotation_pairs] == [
        real_symbols[-1], "200001",
    ]
    assert (built.real_holdings[0].action, built.real_holdings[0].reason) == (
        "MANUAL_REVIEW", "account_broker_stale:eastmoney"
    )
    assert [(pair.sell_symbol, pair.buy_symbol) for pair in built.real_rotation_pairs] == [
        ("300001", "200001"),
    ]


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


def allocation_for(
    market: str,
    *,
    rank: int,
    entry_weight: str,
) -> dict[str, object]:
    ranks = {market: rank}
    for other_market in ("CN", "HK", "US"):
        if other_market != market:
            ranks[other_market] = ({1, 2, 3} - set(ranks.values())).pop()
    strengths = {1: ("90", "80"), 2: ("70", "60"), 3: ("50", "40")}
    assets = {
        "CN": ("A股", "ETF基金"),
        "HK": ("港股", "香港ETF"),
        "US": ("美股", "美国ETF"),
    }
    roots = {
        item: {
            role: {
                "asset": asset,
                "tm_id": (market_index + 1) * 10 + role_index,
                "as_of_date": "2026-08-03",
                "global_strength": strengths[ranks[item]][role_index],
            }
            for role_index, (role, asset) in enumerate(
                zip(("stock", "etf"), assets[item])
            )
        }
        for market_index, item in enumerate(("CN", "HK", "US"))
    }
    return {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": "b" * 64,
        "snapshot": {
            "version": 1,
            "allocation_date": "2026-08-03",
            "generated_at": "2026-08-03T16:18:00+08:00",
            "generator_version": "trend-allocation-v1",
            "git_sha": "a" * 40,
            "roots": roots,
            "markets": {
                item: {
                    "rank": ranks[item],
                    "score": strengths[ranks[item]][0],
                    "score_source": assets[item][0],
                    "entry_weight": {1: "0.06", 2: "0.04", 3: "0.02"}[ranks[item]],
                    "nominal_weight": {1: "0.60", 2: "0.40", 3: "0.20"}[ranks[item]],
                }
                for item in ("CN", "HK", "US")
            },
        },
    }


def test_freeze_allocation_reference_normalizes_success_reason() -> None:
    allocation = allocation_for("US", rank=2, entry_weight="0.04")
    allocation["failure_reason"] = None

    frozen = trend_module.freeze_allocation_reference(allocation)

    assert frozen is not None
    assert frozen["failure_reason"] == ""


def test_valid_frozen_allocation_rejects_null_success_reason() -> None:
    frozen = trend_module.freeze_allocation_reference(
        allocation_for("US", rank=2, entry_weight="0.04")
    )
    assert frozen is not None
    frozen["failure_reason"] = None

    assert not trend_module.valid_frozen_allocation(frozen)


def active_drawdown_for(
    snapshot: Mapping[str, object], *, equity: str,
) -> dict[str, object]:
    market = str(snapshot["market"])
    version = str(snapshot["strategy_version"])
    return {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": market,
        "strategy_id": snapshot["strategy_id"],
        "strategy_version": version,
        "kelly_sample_key": (
            f"{market}|trend_animals_warm_to_hot/{market}/{version}|{version}"
        ),
        "state_status": "ok",
        "status": "active",
        "status_label": "纪律内",
        "entry_allowed": True,
        "current_equity": equity,
        "high_water_mark": equity,
        "drawdown_pct": "0",
        "drawdown_limit_pct": "0.05",
        "pause_reason": "",
        "paused_at": None,
        "observed_at": "2026-07-14T18:00:00+08:00",
        "bootstrap_event": None,
        "recovery_event": None,
    }


@pytest.mark.parametrize(
    ("market", "version", "rank", "weight"),
    [
        ("CN", "v12", 1, "0.06"),
        ("HK", "v10", 2, "0.04"),
        ("US", "v10", 3, "0.02"),
    ],
)
def test_current_allocation_versions_freeze_rank_weight(
    market: str, version: str, rank: int, weight: str,
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market,
        "abc123",
        (1, 2),
        allocation=allocation_for(market, rank=rank, entry_weight=weight),
    )

    assert snapshot["strategy_version"] == version
    assert snapshot["parameters"]["allocation_rank"] == rank
    assert snapshot["parameters"]["target_weight"] == weight
    assert snapshot["parameters"]["allocation_snapshot_sha256"] == "b" * 64
    assert trend_review.normalize_trend_strategy_snapshot(snapshot, market) == snapshot


@pytest.mark.parametrize(
    ("market", "version", "pools"),
    [
        ("CN", "v10", (622466, 697199)),
        ("HK", "v8", (622494,)),
        ("US", "v8", (622460,)),
    ],
)
def test_cold_start_keeps_current_four_percent_versions_without_allocation_fields(
    market: str, version: str, pools: tuple[int, ...],
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(market, "abc123", pools)

    assert snapshot["strategy_version"] == version
    assert "allocation_rank" not in snapshot["parameters"]
    assert "allocation_snapshot_sha256" not in snapshot["parameters"]


def test_allocation_cn_v11_applies_rank_weight_only_to_new_hot_and_boiling_buys(
    tmp_path: Path,
) -> None:
    allocation = allocation_for("CN", rank=1, entry_weight="0.06")
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), allocation=allocation,
    )
    current_account = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        positions=(),
        exceptions=(),
    )
    automatic_bootstrap_strategy_drawdown(
        tmp_path,
        market="CN",
        strategy_id=str(snapshot["strategy_id"]),
        strategy_version=str(snapshot["strategy_version"]),
        parameters=snapshot["parameters"],
        baseline_equity=current_account.net_value,
        source_date="2026-07-14",
        accepted_git_sha="a" * 40,
        actor="pytest",
        occurred_at="2026-07-14T08:00:00+08:00",
        reason="first_activation",
        entry_eligible_from="2026-07-15",
    )
    drawdown = trend_module.observe_strategy_equity(
        tmp_path,
        market="CN",
        strategy_id=str(snapshot["strategy_id"]),
        strategy_version=str(snapshot["strategy_version"]),
        current_equity=current_account.net_value,
        observed_at="2026-07-14T18:00:00+08:00",
        entry_date="2026-07-15",
    )

    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=current_account,
        candidates=(
            candidate("600001", temperature_curr="热"),
            candidate("600002", temperature_curr="沸"),
        ),
        holding_snapshots={},
        bars_by_symbol={},
        market="CN",
        process_version="abc123",
        candidate_pool_ids=(622466, 697199),
        strategy_snapshot=snapshot,
        position_weight=Decimal("0.06"),
        position_weight_source="trend_allocation_rank",
        drawdown_summary=drawdown,
    )

    assert [action.target_weight for action in built.buy_actions] == [
        Decimal("0.06"), Decimal("0.06"),
    ]
    assert built.metadata["position_weight_source"] == "trend_allocation_rank"
    trend_module.validate_report_strategy_snapshot(built)


def test_new_mapping_report_freezes_actions_and_skips_unmapped_candidate() -> None:
    schema = "open_trader.trend_symbol_mapping.v1"
    mapped = replace(candidate("000001"), futu_symbol="SH.000001")
    mapped_report = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(mapped,),
        holding_snapshots={},
        bars_by_symbol={},
        metadata={"symbol_mapping_schema": schema},
    )
    mapped_payload = trend_module._report_payload(mapped_report)

    assert mapped_payload["metadata"]["symbol_mapping_schema"] == schema
    assert mapped_payload["strategy_judgments"]["formal_actions"][0][
        "futu_symbol"
    ] == "SH.000001"

    unmapped_report = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(candidate("600001"),),
        holding_snapshots={},
        bars_by_symbol={},
        metadata={"symbol_mapping_schema": schema},
    )
    unmapped_payload = trend_module._report_payload(unmapped_report)

    assert [item["symbol"] for item in unmapped_payload["strategy_judgments"][
        "top10_candidates"
    ]] == ["600001"]
    assert unmapped_payload["strategy_judgments"]["formal_actions"] == []
    assert unmapped_payload["strategy_judgments"]["risk_skips"][0][
        "reason"
    ] == "symbol_mapping_unavailable"


def test_new_mapping_report_freezes_simulated_sell_but_not_real_advice() -> None:
    schema = "open_trader.trend_symbol_mapping.v1"
    simulated = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        positions=(
            AccountPosition(
                "000001",
                "模拟持仓",
                "stock",
                Decimal("100"),
                Decimal("10"),
                Decimal("1000"),
                "SH.000001",
            ),
        ),
        exceptions=(),
    )
    real = RealHoldingInput(
        status="available",
        reason="",
        source={"broker": "eastmoney"},
        positions=(
            AccountPosition(
                "515450",
                "红利50",
                "etf",
                Decimal("100"),
                Decimal("1.4"),
                Decimal("146"),
                "SH.515450",
            ),
        ),
        holding_snapshots={"515450": holding("515450", danger=True)},
        bars_by_symbol={"515450": bars()},
        prior_state=None,
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=simulated,
        candidates=(),
        holding_snapshots={"000001": holding("000001", danger=True)},
        bars_by_symbol={"000001": bars()},
        metadata={"symbol_mapping_schema": schema},
        real_holdings=real,
    )
    payload = trend_module._report_payload(built)
    actions = payload["strategy_judgments"]["formal_actions"]

    assert [(item["symbol"], item["futu_symbol"]) for item in actions] == [
        ("000001", "SH.000001")
    ]
    assert all(item["symbol"] != "515450" for item in actions)


def unlock_live_drawdown(
    data_dir: Path,
    *,
    market: str = "CN",
    equity: str = "100000",
    strategy_version: str = "v8",
) -> None:
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market=market,
        strategy_id=f"trend_animals_warm_to_hot/{market}/{strategy_version}",
        strategy_version=strategy_version,
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal(equity),
        source_date="2026-07-13",
        accepted_git_sha="a" * 40,
        occurred_at="2026-07-14T08:00:00+08:00",
        actor="pytest",
        reason="first_activation",
        entry_eligible_from="2026-07-14",
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


def test_cn_strategy_snapshot_matches_runtime_rules_and_report_actions() -> None:
    snapshot = trend_module.trend_strategy_snapshot(
        "CN",
        "abc123",
        (622466, 697199),
    )

    assert {
        key: snapshot[key]
        for key in (
            "strategy_id",
            "strategy_name",
            "strategy_version",
            "market",
            "effective_from",
            "process_version",
        )
    } == {
        "strategy_id": "trend_animals_warm_to_hot/CN/v3",
        "strategy_name": "A 股短线右侧趋势",
        "strategy_version": "v3",
        "market": "CN",
        "effective_from": "2026-07-20",
        "process_version": "abc123",
    }
    assert snapshot["parameters"] == {
        "candidate_pool_ids": [622466, 697199],
        "allowed_exchanges": ["SH", "SZ"],
        "excluded_name_markers": ["ST", "退"],
        "temperature_transition": {"from": ["温"], "to": ["热", "沸"]},
        "max_filter_price": "200",
        "min_strength": "95",
        "allowed_industry_temperatures": ["热", "沸"],
        "allowed_phases": ["谷雨", "立夏", "夏至"],
        "min_market_cap_100m": "100",
        "min_amount_100m": "2",
        "requires_right_side": True,
        "requires_tradable": True,
        "requires_no_danger": True,
        "requires_matching_data_date": True,
        "requires_not_held": True,
        "requires_right_side_days": True,
        "requires_atr14": True,
        "sort": ["strength_desc", "days_asc", "amount_desc", "symbol_asc"],
        "candidate_limit": 10,
        "position_limit": 10,
        "single_entry_risk_limit": "0.004",
        "portfolio_risk_limit": "0.04",
        "abnormal_loss_buffer": "0.01",
        "normal_cost_rate": "0.001",
        "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
        "overheat_trim_fraction": "0.30",
        "overheat_trim_once_per_position": True,
        "overheat_trim_signals": ["boiling", "champagne"],
        "overheat_trim_rounding": "floor_to_market_lot",
        "overheat_trim_below_lot": "no_order_terminal",
        "full_exit_precedes_partial_exit": True,
        "kelly_sample_minimum": 30,
        "kelly_rolling_window": 200,
        "kelly_fraction": "0.25",
        "kelly_optimizer": "mean_log_growth_derivative_bisection_96_floor_1e-6",
        "kelly_sample_scope": "market+strategy_id+opening_strategy_version",
        "kelly_source": "cost_complete_attributed_simulation_closed_rounds",
        "target_weight": {"热": "0.04", "沸": "0.02"},
        "lot_size": 100,
        "buy_window": "09:30-10:00",
        "initial_protection_atr_multiple": "2",
        "exit_reasons": [
            "danger",
            "left_right_side",
            "temperature_to_flat",
            "protection",
        ],
        "trailing_low_days": 5,
        "protection_line_non_decreasing": True,
    }
    assert snapshot["parameter_rows"][0] == {
        "group": "候选来源",
        "name": "趋势动物组合",
        "value": "温转热（A 股）、温转热（ETF 基金个股）",
    }
    assert all(
        set(row) == {"group", "name", "value"}
        for row in snapshot["parameter_rows"]
    )
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}
    assert {
        "买入数量": "使用已有现金，按 100 股整数倍向下取整",
        "过热跟踪": "沸腾或开香槟触发后，活动保护线取原值与此前 5 个完整交易日最低价的较高者，只升不降",
    }.items() <= rows.items()

    built = report(candidates=(candidate("600001"),))
    assert built.buy_actions[0].lot_size == 100
    assert trend_module._report_payload(built)["strategy_snapshot"] == (
        built.strategy_snapshot
    )


@pytest.mark.parametrize(
    "market", ["CN", "US", "HK"],
)
def test_trend_v3_effective_date_is_shared_across_markets(market: str) -> None:
    assert trend_module.trend_strategy_snapshot(
        market, "abc123", (622466,)
    )["effective_from"] == "2026-07-20"


def test_live_cn_strategy_snapshot_is_v7_with_v4_sample_inheritance() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v7"
    )

    assert snapshot["strategy_id"] == "trend_animals_warm_to_hot/CN/v7"
    assert snapshot["strategy_version"] == "v7"
    assert snapshot["effective_from"] == "2026-07-24"
    assert snapshot["parameters"]["kelly_sample_inherits"] == [{
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "opening_strategy_version": "v4",
    }]
    assert snapshot["parameters"]["allowed_industry_temperatures"] == [
        "温", "热", "沸",
    ]
    assert "max_filter_price" not in snapshot["parameters"]
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}
    assert "筛选价格" not in rows
    assert rows["行业温度"] == "温、热或沸"


def test_live_cn_v6_strategy_snapshot_remains_historical() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v6"
    )

    assert snapshot["strategy_id"] == "trend_animals_warm_to_hot/CN/v6"
    assert snapshot["strategy_version"] == "v6"
    assert "kelly_sample_inherits" not in snapshot["parameters"]
    assert snapshot["parameters"]["allowed_industry_temperatures"] == [
        "温", "热", "沸",
    ]


def test_live_cn_strategy_snapshot_defaults_to_v10_with_all_approved_inheritance() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199)
    )

    assert snapshot["strategy_id"] == "trend_animals_warm_to_hot/CN/v10"
    assert snapshot["strategy_version"] == "v10"
    assert snapshot["effective_from"] == "2026-07-27"
    assert snapshot["parameters"]["kelly_sample_inherits"] == [
        {
            "market": "CN",
            "strategy_id": f"trend_animals_warm_to_hot/CN/{version}",
            "opening_strategy_version": version,
        }
        for version in ("v4", "v7", "v8", "v9", "v10")
    ]
    assert snapshot["parameters"]["allowed_assets"] == ["A股", "ETF基金"]
    assert {
        row["value"]
        for row in snapshot["parameter_rows"]
        if row["name"] == "交易市场"
    } == {"沪深 A 股及境内 ETF；排除北交所、ST、*ST 和退市标记"}


@pytest.mark.parametrize(
    ("market", "version", "inherits"),
    [
        ("CN", "v10", ("v4", "v7", "v8", "v9", "v10")),
        ("US", "v8", ("v4", "v5", "v6", "v7", "v8")),
        ("HK", "v8", ("v4", "v5", "v6", "v7", "v8")),
    ],
)
def test_current_live_snapshots_publish_exit_discipline_without_partial_profit(
    market: str, version: str, inherits: tuple[str, ...],
) -> None:
    pools = (
        (622466, 697199)
        if market == "CN"
        else (622460,)
        if market == "US"
        else (622494,)
    )
    snapshot = trend_module.live_trend_strategy_snapshot(market, "abc123", pools)
    parameters = snapshot["parameters"]
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}

    assert snapshot["strategy_version"] == version
    assert snapshot["strategy_id"] == f"trend_animals_warm_to_hot/{market}/{version}"
    assert parameters["kelly_sample_inherits"] == [
        {
            "market": market,
            "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
            "opening_strategy_version": item,
        }
        for item in inherits
    ]
    assert parameters["exit_reasons"] == [
        "danger", "left_right_side", "temperature_to_flat", "protection",
    ]
    assert not any(key.startswith("overheat_trim_") for key in parameters)
    assert "full_exit_precedes_partial_exit" not in parameters
    assert "trailing_low_days" not in parameters
    assert not {
        "过热止盈比例", "过热止盈信号", "过热止盈次数", "过热止盈取整",
        "不足一手处理", "清仓优先级", "过热跟踪",
    } & rows.keys()
    assert rows["退出条件"] == "危险信号、离开趋势右侧、温度转平或触发保护线时全部卖出"
    if market == "CN":
        assert parameters["target_weight"] == {"热": "0.04", "沸": "0.04"}
        assert rows["目标仓位"] == "账户净值的 4%"
        assert "热状态仓位" not in rows
        assert "沸状态仓位" not in rows


@pytest.mark.parametrize("market", ["CN", "US", "HK"])
def test_current_live_snapshots_summarize_industry_first_candidate_order(
    market: str,
) -> None:
    pools = (
        (622466, 697199)
        if market == "CN"
        else (622460,)
        if market == "US"
        else (622494,)
    )
    snapshot = trend_module.live_trend_strategy_snapshot(
        market,
        "abc123",
        pools,
    )
    rows = {
        row["name"]: row["value"]
        for row in snapshot["parameter_rows"]
    }

    assert rows["排序顺序"] == (
        "行业优先（变化、温度、强度、温转热数量、右侧占比），"
        "再按个股趋势强度、右侧天数、成交额、代码；"
        "缺历史省略变化键，行业上下文无效时回退个股排序"
    )


def test_cn_v8_snapshot_and_sizing_keep_legacy_boiling_two_percent() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v8"
    )
    assert snapshot["parameters"]["target_weight"] == {"热": "0.04", "沸": "0.02"}
    assert snapshot["parameters"]["overheat_trim_fraction"] == "0.30"
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}
    assert rows["沸状态仓位"] == "账户净值的 2%"


def test_current_cn_boiling_entry_uses_four_percent() -> None:
    actions = estimate_buy_actions(
        ranked=(candidate("600001", temperature_curr="沸"),),
        net_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
    )
    assert actions[0].target_weight == Decimal("0.04")


@pytest.mark.parametrize(
    ("market", "expected_version"),
    [("US", "v8"), ("HK", "v8")],
)
def test_live_non_cn_strategy_snapshot_defaults_to_v8_with_exact_inheritance(
    market: str, expected_version: str,
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market, "abc123", (622460,) if market == "US" else (622494,)
    )

    assert snapshot["strategy_id"] == (
        f"trend_animals_warm_to_hot/{market}/{expected_version}"
    )
    assert snapshot["strategy_version"] == expected_version
    assert snapshot["parameters"]["kelly_sample_inherits"] == [
        {
            "market": market,
            "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
            "opening_strategy_version": version,
        }
        for version in ("v4", "v5", "v6", "v7", "v8")
    ]


@pytest.mark.parametrize("market", ["US", "HK"])
def test_historical_market_v7_keeps_legacy_entry_parameters(market: str) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market,
        "abc123",
        (622460,) if market == "US" else (622494,),
        strategy_version="v7",
    )

    assert snapshot["strategy_version"] == "v7"
    assert snapshot["parameters"]["min_strength_exclusive"] == "90"
    assert snapshot["parameters"]["max_right_side_days_exclusive"] == 10
    assert snapshot["parameters"]["min_amount_100m"] == "1"
    assert "allowed_industry_temperatures" not in snapshot["parameters"]


@pytest.mark.parametrize(
    ("market", "currency", "rate"),
    [
        ("US", "USD", "7.268518518518518518518518519"),
        ("HK", "HKD", "0.9259259259259259259259259259"),
    ],
)
def test_current_market_strategy_snapshot_shares_cn_entry_rules(
    market: str, currency: str, rate: str,
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market, "abc123", (622460,) if market == "US" else (622494,)
    )
    parameters = snapshot["parameters"]

    assert {
        key: parameters[key]
        for key in (
            "temperature_transition",
            "min_strength",
            "allowed_industry_temperatures",
            "allowed_phases",
            "min_market_cap_cny_100m",
            "min_amount_cny_100m",
            "market_value_currency",
            "cny_per_local_currency",
            "requires_right_side_days",
        )
    } == {
        "temperature_transition": {"from": ["温"], "to": ["热", "沸"]},
        "min_strength": "95",
        "allowed_industry_temperatures": ["温", "热", "沸"],
        "allowed_phases": ["谷雨", "立夏", "夏至"],
        "min_market_cap_cny_100m": "100",
        "min_amount_cny_100m": "2",
        "market_value_currency": currency,
        "cny_per_local_currency": rate,
        "requires_right_side_days": True,
    }
    assert "max_right_side_days_exclusive" not in parameters
    rows = {
        row["name"]: row["value"]
        for row in snapshot["parameter_rows"]
        if row["group"] == "入场过滤"
    }
    assert {
        "趋势温度": "前一状态为温；当前状态为热或沸",
        "趋势强度": "不低于 95",
        "行业温度": "温、热或沸",
        "趋势节气": "谷雨、立夏或夏至",
        "总市值": "不低于人民币 100 亿元（按冻结汇率换算）",
        "单日成交额": "不低于人民币 2 亿元（按冻结汇率换算）",
    }.items() <= rows.items()


@pytest.mark.parametrize("market", ["US", "HK"])
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"temperature_prev": "平"}, "temperature_transition_not_entry"),
        ({"strength": Decimal("94.9")}, "strength_below_95"),
        ({"industry_temperature": "凉"}, "industry_temperature_not_hot"),
        ({"phase": "小暑"}, "phase_after_summer_solstice"),
        ({"market_cap": Decimal("0")}, "market_cap_below_100_cny"),
        ({"amount": Decimal("0")}, "amount_below_2_cny"),
        ({"days": None}, "right_side_days_missing"),
    ],
)
def test_current_market_entry_rejects_cn_discipline_failure(
    market: str, changes: dict[str, object], reason: str,
) -> None:
    item = replace(
        candidate(
            "620001",
            exchange=market,
            asset="美股" if market == "US" else "港股",
            market_cap="200",
            amount="3",
        ),
        **changes,
    )

    decision = build_candidate_list(
        [item],
        held_symbols=set(),
        expected_date="2026-07-14",
        market=market,
        strategy_version="v8",
        cny_per_local_currency=trend_module.CNY_PER_LOCAL_CURRENCY[market],
    )

    assert reason in decision.excluded[item.symbol]


def test_industry_temperature_loader_preserves_all_known_cold_states() -> None:
    class Api:
        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            return [
                {
                    "tmId": tm_id,
                    "asOfDate": kwargs["expected_date"],
                    "trendTemperatureCurr": temperature,
                }
                for tm_id, temperature in ((700001, "冻"), (700002, "寒"))
            ]

    _, temperatures = trend_module.load_industry_temperatures(
        Api(),
        tm_ids=(700001, 700002),
        expected_date="2026-07-14",
    )

    assert temperatures == {700001: "冻", 700002: "寒"}


@pytest.mark.parametrize(
    ("market", "asset", "pool_id"),
    [("US", "美股", 622460), ("HK", "港股", 622494)],
)
def test_current_market_report_keeps_cool_industry_out_of_every_buy_view(
    market: str, asset: str, pool_id: int,
) -> None:
    item = candidate(
        "620001",
        exchange=market,
        asset=asset,
        name="GRMN" if market == "US" else "港股测试标的",
        industry_temperature="凉",
        market_cap="200",
        amount="3",
    )
    strategy_snapshot = trend_module.live_trend_strategy_snapshot(
        market,
        "abc123",
        (pool_id,),
        strategy_version="v8",
    )
    drawdown_summary = {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": market,
        "strategy_id": strategy_snapshot["strategy_id"],
        "strategy_version": "v8",
        "kelly_sample_key": (
            f"{market}|trend_animals_warm_to_hot/{market}/v8|v8"
        ),
        "state_status": "ok",
        "status": "active",
        "status_label": "纪律内",
        "entry_allowed": True,
        "current_equity": "676549.55",
        "high_water_mark": "676549.55",
        "drawdown_pct": "0",
        "drawdown_limit_pct": "0.05",
        "pause_reason": "",
        "paused_at": None,
        "observed_at": "2026-07-14T18:00:00+08:00",
        "bootstrap_event": None,
        "recovery_event": None,
    }

    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(item,),
        holding_snapshots={},
        bars_by_symbol={},
        market=market,
        process_version="abc123",
        candidate_pool_ids=(pool_id,),
        strategy_snapshot=strategy_snapshot,
        drawdown_summary=drawdown_summary,
        metadata={"market": market},
    )
    payload = trend_module._report_payload(built)

    assert built.candidates == ()
    assert built.buy_actions == ()
    assert built.risk_skips == ()
    assert built.excluded[item.symbol] == ["industry_temperature_not_hot"]
    assert payload["strategy_judgments"]["top10_candidates"] == []
    assert payload["strategy_judgments"]["formal_actions"] == []
    assert payload["signal_snapshots"]["candidates"][0] | {
        "eligible": False,
        "excluded_reasons": ["industry_temperature_not_hot"],
        "market_cap_cny_threshold_met": True,
        "amount_cny_threshold_met": True,
    } == payload["signal_snapshots"]["candidates"][0]


def test_current_market_industry_failure_keeps_holding_exit_decisions() -> None:
    strategy_snapshot = trend_module.live_trend_strategy_snapshot(
        "US",
        "abc123",
        (622460,),
        strategy_version="v8",
    )
    item = candidate(
        "620001",
        exchange="US",
        asset="美股",
        industry_temperature=None,
        market_cap="200",
        amount="3",
    )

    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600009"),
        candidates=(item,),
        holding_snapshots={
            "600009": replace(
                holding("600009"),
                exchange="US",
                danger=True,
            )
        },
        bars_by_symbol={"600009": bars()},
        market="US",
        process_version="abc123",
        candidate_pool_ids=(622460,),
        strategy_snapshot=strategy_snapshot,
        metadata={"market": "US"},
        kelly_data_reason=(
            "行业温度数据不可用，暂停新开仓：industry endpoint unavailable"
        ),
    )

    assert [(decision.symbol, decision.action) for decision in built.holdings] == [
        ("600009", "SELL_ALL")
    ]
    assert built.buy_actions == ()
    assert built.excluded[item.symbol] == ["industry_temperature_missing"]


@pytest.mark.parametrize("version", ["v4", "v6", "v7", "v8", "v9", "v10"])
def test_live_cn_supported_versions_remain_replay_valid(version: str) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466,), strategy_version=version
    )
    assert snapshot["strategy_version"] == version


@pytest.mark.parametrize("version", ["v4", "v5", "v6", "v7"])
def test_live_us_hk_supported_versions_remain_replay_valid(version: str) -> None:
    for market, pools in (("US", (622460,)), ("HK", (622494,))):
        snapshot = trend_module.live_trend_strategy_snapshot(
            market, "abc123", pools, strategy_version=version
        )
        assert snapshot["strategy_version"] == version


@pytest.mark.parametrize(
    ("market", "version", "pools"),
    [
        ("CN", "v9", (622466, 697199)),
        ("US", "v6", (622460,)),
        ("HK", "v6", (622494,)),
    ],
)
def test_replaced_strategy_versions_keep_published_behavior(
    market: str,
    version: str,
    pools: tuple[int, ...],
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market, "abc123", pools, strategy_version=version
    )

    assert snapshot["effective_from"] == "2026-07-27"
    assert snapshot["parameters"]["candidate_pool_ids"] == list(pools)
    assert snapshot["parameters"]["exit_reasons"] == [
        "danger", "left_right_side", "temperature_to_flat", "protection",
    ]
    if market == "CN":
        assert snapshot["parameters"]["allowed_assets"] == ["A股", "ETF基金"]


def test_report_rejects_strategy_snapshot_action_mismatch() -> None:
    built = report(candidates=(candidate("600001"),))
    parameters = dict(built.strategy_snapshot["parameters"])
    parameters["target_weight"] = {"热": "0.02", "沸": "0.02"}
    broken = replace(
        built,
        strategy_snapshot={**built.strategy_snapshot, "parameters": parameters},
    )

    with pytest.raises(
        ValueError, match="strategy snapshot does not match report actions"
    ):
        trend_module.validate_report_strategy_snapshot(broken)


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("use_available_cash", False),
        ("trailing_activation_signals", ["boiling"]),
    ],
)
def test_report_rejects_snapshot_that_changes_shared_execution_rules(
    parameter: str, value: object
) -> None:
    built = report(candidates=(candidate("600001"),))
    parameters = {**built.strategy_snapshot["parameters"], parameter: value}
    broken = replace(
        built,
        strategy_snapshot={**built.strategy_snapshot, "parameters": parameters},
    )

    with pytest.raises(
        ValueError, match="strategy snapshot does not match report actions"
    ):
        trend_module.validate_report_strategy_snapshot(broken)


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("min_strength", "94"),
        ("candidate_limit", 9),
        ("position_limit", 9),
        ("allowed_phases", ["谷雨"]),
    ],
)
def test_build_report_rejects_any_injected_snapshot_parameter_drift(
    parameter: str, value: object,
) -> None:
    canonical = trend_module.trend_strategy_snapshot("CN", "sha", ())
    changed = {
        **canonical,
        "parameters": {**canonical["parameters"], parameter: value},
    }

    with pytest.raises(ValueError, match="strategy snapshot"):
        build_report(
            as_of_date="2026-07-16",
            execution_date="2026-07-17",
            account=account(),
            candidates=(),
            holding_snapshots={},
            bars_by_symbol={},
            market="CN",
            process_version="sha",
            candidate_pool_ids=(),
            strategy_snapshot=changed,
        )


def test_build_report_upgrades_exact_repository_legacy_snapshot() -> None:
    legacy = json.loads(
        Path("data/trend_review/daily/CN/2026-07-16.json").read_text(
            encoding="utf-8"
        )
    )["strategy_snapshot"]
    pools = tuple(legacy["parameters"]["candidate_pool_ids"])

    built = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        market="CN",
        process_version=legacy["process_version"],
        candidate_pool_ids=pools,
        strategy_snapshot=legacy,
    )

    assert built.strategy_snapshot == trend_module.trend_strategy_snapshot(
        "CN", legacy["process_version"], pools
    )


def test_cn_v9_accepts_etf_without_rewriting_v8() -> None:
    item = candidate("511020", asset="ETF基金")

    def build(version: str) -> TrendReport:
        snapshot = trend_module.live_trend_strategy_snapshot(
            "CN",
            "abc123",
            (622466, 697199),
            strategy_version=version,
        )
        return build_report(
            as_of_date="2026-07-14",
            execution_date="2026-07-15",
            account=account(),
            candidates=(item,),
            holding_snapshots={},
            bars_by_symbol={},
            process_version="abc123",
            candidate_pool_ids=(622466, 697199),
            strategy_snapshot=snapshot,
        )

    current = build("v9")
    historical = build("v8")

    assert [candidate.symbol for candidate in current.candidates] == ["511020"]
    assert "511020" not in current.excluded
    assert historical.candidates == ()
    assert historical.excluded["511020"] == ["a_share_only"]


def test_candidates_filter_then_sort_deterministically() -> None:
    rows = [
        candidate("600004", strength="95", days=2, amount="3"),
        candidate("600003", strength="96", days=4, amount="2"),
        candidate("600002", strength="96", days=3, amount="2"),
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
    assert decisions.excluded["600005"] == ["strength_below_95"]
    assert decisions.excluded["600006"] == ["danger_signal"]


def _industry_context(
    industry_tm_id: int,
    *,
    temperature: str = "热",
    strength: str = "90",
    warm_to_hot_count: int = 5,
    right_share: str = "0.50",
    prior_temperature: str | None = "温",
    prior_right_share: str | None = "0.40",
    direction: str | None = "rising",
    change_pp: str | None = "10",
    valid: bool = True,
    invalid_reasons: tuple[str, ...] = (),
) -> IndustryContext:
    return IndustryContext(
        industry_tm_id=industry_tm_id,
        industry=f"行业{industry_tm_id}",
        as_of_date="2026-07-14",
        component_count=20,
        snapshot_count=20,
        tradable_count=20,
        valid_count=20,
        right_count=10,
        snapshot_coverage=Decimal("1"),
        right_state_coverage=Decimal("1"),
        right_share=Decimal(right_share),
        warm_to_hot_count=warm_to_hot_count,
        temperature=temperature,
        strength=Decimal(strength),
        valid=valid,
        invalid_reasons=invalid_reasons,
        prior_as_of_date="2026-07-13" if prior_temperature is not None else None,
        prior_temperature=prior_temperature,
        prior_right_share=(
            None if prior_right_share is None else Decimal(prior_right_share)
        ),
        temperature_direction=direction,
        right_share_change_pp=(
            None if change_pp is None else Decimal(change_pp)
        ),
    )


def test_candidate_industry_context_ordering_uses_report_wide_context_keys() -> None:
    rows = [
        candidate("600001", strength="99", days=1, amount="9", industry_tm_id=1),
        candidate("600002", strength="98", days=1, amount="9", industry_tm_id=2),
        candidate("600003", strength="97", days=1, amount="9", industry_tm_id=3),
    ]
    contexts = {
        1: _industry_context(1, temperature="热", strength="90", direction="falling"),
        2: _industry_context(2, temperature="沸", strength="95", direction="unchanged"),
        3: _industry_context(3, temperature="热", strength="95", direction="rising"),
    }

    decisions = build_candidate_list(
        rows, held_symbols=set(), industry_contexts=contexts
    )

    assert [item.symbol for item in decisions.eligible] == [
        "600003",
        "600002",
        "600001",
    ]


def test_candidate_context_missing_prior_uses_current_only_for_every_candidate() -> None:
    rows = [candidate("600001", industry_tm_id=1), candidate("600002", industry_tm_id=2)]
    contexts = {
        1: _industry_context(1, direction="rising", change_pp="10"),
        2: _industry_context(
            2,
            prior_temperature=None,
            prior_right_share=None,
            direction=None,
            change_pp=None,
        ),
    }

    decisions = build_candidate_list(
        rows, held_symbols=set(), industry_contexts=contexts
    )

    assert decisions.ordering_mode == "context_current_only"


def test_complete_small_context_keeps_industry_ordering_enabled() -> None:
    small = replace(
        _industry_context(1, temperature="平", strength="30"),
        component_count=2,
        snapshot_count=2,
        tradable_count=2,
        valid_count=2,
        right_count=2,
    )
    decisions = build_candidate_list(
        [
            candidate("600001", strength="99", industry_tm_id=1),
            candidate("600002", strength="96", industry_tm_id=2),
        ],
        held_symbols=set(),
        industry_contexts={
            1: small,
            2: _industry_context(2, temperature="热", strength="100"),
        },
    )

    assert decisions.ordering_mode == "context_with_history"
    assert [item.symbol for item in decisions.eligible] == ["600002", "600001"]


def test_invalid_current_industry_context_restores_legacy_order() -> None:
    rows = [
        candidate("600001", strength="99", days=5, amount="2", industry_tm_id=1),
        candidate("600002", strength="98", days=1, amount="2", industry_tm_id=2),
    ]
    contexts = {
        1: _industry_context(1, valid=False, invalid_reasons=("bad",)),
        2: _industry_context(2),
    }

    decisions = build_candidate_list(
        rows, held_symbols=set(), industry_contexts=contexts
    )

    assert decisions.ordering_mode == "legacy_invalid_current"
    assert [item.symbol for item in decisions.eligible] == ["600001", "600002"]


def test_missing_industry_id_triggers_report_wide_legacy_fallback() -> None:
    item = replace(
        candidate("600001", industry_tm_id=None),
        asset="US stock",
        exchange="US",
        temperature_prev=None,
        temperature_curr=None,
        phase=None,
        market_cap=None,
    )
    decision = build_candidate_list([item], held_symbols=set(), market="US")

    assert decision.eligible == (item,)
    assert decision.ordering_mode == "legacy_invalid_current"
    assert decision.industry_context_status["affected_industry_ids"] == ["unknown"]


def test_candidate_industry_context_hard_gate_still_excludes_candidate() -> None:
    item = candidate("600001", danger=True, industry_tm_id=1)
    decisions = build_candidate_list(
        [item],
        held_symbols=set(),
        industry_contexts={1: _industry_context(1, strength="100")},
    )

    assert decisions.eligible == ()
    assert decisions.excluded["600001"] == ["danger_signal"]


def test_report_payload_freezes_industry_context_and_ordering_facts() -> None:
    contexts = (_industry_context(621707, right_share="0.278688524590"),)
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(candidate("600001", industry_tm_id=621707),),
        holding_snapshots={},
        bars_by_symbol={},
        industry_contexts=contexts,
        estimated_api_cost=Decimal("0.479"),
        actual_api_cost=Decimal("0.610"),
        estimated_api_cost_complete=False,
    )

    payload = trend_module._report_payload(built)
    assert payload["industry_context_status"]["ordering_mode"] == "context_with_history"
    assert payload["industry_contexts"][0]["industry_tm_id"] == 621707
    assert payload["industry_contexts"][0]["right_share"] == "0.278688524590"
    assert payload["api_cost"] == {
        "actual": "0.610",
        "estimated": "0.479",
        "estimate_complete": False,
        "unit": "Trend Animals 余额单位",
        "label": "本报告 API 费用：实扣 0.610 Trend Animals 余额单位",
    }
    ordering = payload["strategy_judgments"]["top10_candidates"][0]["ordering_context"]
    assert ordering["applied"] is True
    assert ordering["industry_tm_id"] == 621707
    assert ordering["ordering_mode"] == "context_with_history"


@pytest.mark.parametrize(
    ("actual", "estimated", "estimate_complete", "expected"),
    [
        (
            Decimal("0.610"),
            Decimal("0.479"),
            False,
            "本报告 API 费用：实扣 0.610 Trend Animals 余额单位",
        ),
        (
            None,
            Decimal("0.479"),
            True,
            "本报告 API 费用：估算 0.479 Trend Animals 余额单位（实扣不可得）",
        ),
        (
            None,
            Decimal("0.479"),
            False,
            "本报告 API 费用：未知（快照估算 0.479 Trend Animals 余额单位；成分费用未计）",
        ),
        (
            Decimal("0.000"),
            None,
            False,
            "本报告 API 费用：实扣 0 Trend Animals 余额单位",
        ),
        (
            Decimal("1.2000"),
            None,
            False,
            "本报告 API 费用：实扣 1.2 Trend Animals 余额单位",
        ),
    ],
)
def test_trend_api_cost_label_has_one_canonical_branch(
    actual: Decimal | None,
    estimated: Decimal | None,
    estimate_complete: bool,
    expected: str,
) -> None:
    assert trend_module.trend_api_cost_label(
        actual=actual,
        estimated=estimated,
        estimate_complete=estimate_complete,
    ) == expected


@pytest.mark.parametrize("raw_balance", ["-0.001", "NaN", "Infinity"])
def test_balance_rejects_negative_or_nonfinite_values(raw_balance: str) -> None:
    with pytest.raises(TrendAnimalsError, match="valid balance"):
        trend_module._balance({"balance": raw_balance})


def test_report_cost_label_is_shared_by_markdown_feishu_and_json() -> None:
    built = replace(
        report(),
        estimated_api_cost=Decimal("0.479"),
        actual_api_cost=None,
        estimated_api_cost_complete=False,
    )
    expected = (
        "本报告 API 费用：未知（快照估算 0.479 Trend Animals 余额单位；成分费用未计）"
    )

    markdown = render_markdown(built)
    payload = trend_module._report_payload(built)
    _, feishu = render_trend_feishu_text(
        payload,
        broker_label="东方财富",
        market_label="A股",
    )

    assert markdown.count(expected) == 1
    assert feishu.count(expected) == 1
    assert payload["api_cost"]["label"] == expected
    assert payload["api_cost"] == {
        "actual": None,
        "estimated": "0.479",
        "estimate_complete": False,
        "unit": "Trend Animals 余额单位",
        "label": expected,
    }


def test_legacy_feishu_cost_uses_api_cost_completeness() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": serialized_account(fresh=True),
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "actual_api_cost": None,
        "estimated_api_cost": "0.479",
        "api_cost": {
            "actual": None,
            "estimated": "0.479",
            "estimate_complete": False,
            "unit": "Trend Animals 余额单位",
        },
        "strategy_judgments": {
            "holding_decisions": [],
            "formal_actions": [],
        },
    }

    _, message = render_trend_feishu_text(
        payload,
        broker_label="东方财富",
        market_label="A股",
    )

    assert (
        "本报告 API 费用：未知（快照估算 0.479 Trend Animals 余额单位；成分费用未计）"
        in message
    )


def test_build_report_rejects_external_context_status_when_contexts_are_missing() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(candidate("600001", industry_tm_id=621707),),
        holding_snapshots={},
        bars_by_symbol={},
        industry_context_status={
            "ordering_mode": "context_with_history",
            "current_complete": True,
            "history_complete": True,
            "fallback_reason": None,
        },
    )

    assert built.industry_context_status["ordering_mode"] == "legacy_invalid_current"
    payload = trend_module._report_payload(built)
    assert payload["industry_context_status"]["ordering_mode"] == (
        "legacy_invalid_current"
    )
    assert payload["strategy_judgments"]["top10_candidates"][0][
        "ordering_context"
    ] == {
        "applied": False,
        "industry_tm_id": 621707,
        "ordering_mode": "legacy_invalid_current",
        "fallback_reason": "industry_context_missing",
    }


@pytest.mark.parametrize(
    "extra",
    [
        _industry_context(2, valid=False, invalid_reasons=("bad",)),
        _industry_context(
            2,
            prior_temperature=None,
            prior_right_share=None,
            direction=None,
            change_pp=None,
        ),
    ],
)
def test_non_candidate_contexts_do_not_change_ordering_mode(
    extra: IndustryContext,
) -> None:
    decision = build_candidate_list(
        [candidate("600001", industry_tm_id=1)],
        held_symbols=set(),
        industry_contexts={
            1: _industry_context(1),
            extra.industry_tm_id: extra,
        },
    )

    assert decision.ordering_mode == "context_with_history"


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
        "industryTmId": 700001,
        "priceIndex": "10",
        "marketCap": "100",
        "trendTemperaturePrev": "温",
        "trendTemperatureCurr": "热",
        "trendPhaseCurr": "立夏",
    }

    item = evaluate_candidate(row, bars(), industry_temperature="热")

    assert (item.symbol, item.exchange) == ("920000", "BJ")
    assert build_candidate_list([item], held_symbols=set()).excluded["920000"] == [
        "excluded_security"
    ]


def test_stale_candidate_kline_is_unavailable_and_excluded() -> None:
    row = {
        "tmId": 600001,
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
        "industryTmId": 700001,
        "priceIndex": "10",
        "marketCap": "100",
        "trendTemperaturePrev": "温",
        "trendTemperatureCurr": "热",
        "trendPhaseCurr": "立夏",
    }

    item = evaluate_candidate(
        row, bars(end_date="2026-07-13"), industry_temperature="热"
    )

    assert (item.close, item.atr) == (None, None)
    assert build_candidate_list(
        [item], held_symbols=set(), expected_date="2026-07-14"
    ).excluded["600001"] == ["atr_unavailable"]


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
            "industryTmId": 700001,
            "priceIndex": "10",
            "marketCap": "100",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
            "trendPhaseCurr": "立夏",
        },
        bars(),
        industry_temperature="热",
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
            "industryTmId": 700001,
            "priceIndex": "10",
            "marketCap": "100",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
            "trendPhaseCurr": "立夏",
        },
        bars(),
    )
    assert (item.symbol, item.exchange) == ("600000", "SZ")


def test_candidate_serializes_paid_expansion_fields_and_kline_fallback(
    tmp_path: Path,
) -> None:
    row = {
        "tmId": 600001,
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
        "industryTmId": 700001,
        "priceIndex": "10",
        "marketCap": "100",
        "trendTemperaturePrev": "温",
        "trendTemperatureCurr": "热",
        "gainSinceTrendEntry": "0.048",
        "trendPhasePrev": " 谷雨 ",
        "trendPhaseCurr": " 立夏 ",
        "trendStrengthLocalChange": " ↑↑ ",
        "trendStrengthGlobalCurr": "91.8",
        "trendStrengthLocalPrevWeek": "86.0",
        "trendStrengthLocalPrevMonth": "77.4",
        "tickerLabels": "成交主力; 市值龙头",
        "stopwinFlagByBoilingTemperature": False,
        "stopwinFlagByPopChampagne": False,
    }
    assert set(row) == set(UNIFIED_TREND_FIELDS)
    supplement_bars = bars(50)
    supplement_bars[30:49] = [
        replace(bar, close=12, high=12) for bar in supplement_bars[30:49]
    ]
    supplement_bars[-1] = replace(
        supplement_bars[-1], close=13, high=14, low=11, volume=200
    )
    complete = evaluate_candidate(row, supplement_bars, industry_temperature="热")
    missing = evaluate_candidate(
        {key: value for key, value in row.items() if key != "trendStrengthGlobalCurr"},
        supplement_bars,
        industry_temperature="热",
    )

    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(complete,),
        holding_snapshots={},
        bars_by_symbol={},
    )
    _, json_path = write_frozen_report(built, tmp_path)
    signal = json.loads(json_path.read_text(encoding="utf-8"))["signal_snapshots"][
        "candidates"
    ][0]

    assert signal | {
        "gain_since_entry": "0.048",
        "phase_prev": "谷雨",
        "phase_curr": "立夏",
        "strength_change": "↑↑",
        "global_strength": "91.8",
        "strength_prev_week": "86.0",
        "strength_prev_month": "77.4",
        "labels": ["成交主力", "市值龙头"],
    } == signal
    assert missing.kline_supplement == {
        "pullback_to_sma20": True,
        "breakout_20d_with_volume": True,
        "sma50_breakdown": False,
    }
    assert complete.kline_supplement is None
    assert build_candidate_list([missing], held_symbols=set()).eligible == (missing,)
    missing_built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(missing,),
        holding_snapshots={},
        bars_by_symbol={},
    )
    assert [item.symbol for item in missing_built.buy_actions] == [
        item.symbol for item in built.buy_actions
    ]
    assert missing_built.signal_snapshots["candidates"][0]["source"] == (
        built.signal_snapshots["candidates"][0]["source"]
    ) == "Trend Animals"


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
            "industryTmId": 700001,
            "priceIndex": "10",
            "marketCap": "100",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
            "trendPhaseCurr": "立夏",
        },
        bars(),
    )

    assert (item.symbol, item.exchange) == (ticker_symbol, exchange)


@pytest.mark.parametrize("phase", ["谷雨", "立夏", "夏至"])
@pytest.mark.parametrize("industry_temperature", ["温", "热", "沸"])
def test_cn_candidate_accepts_allowed_phase_and_industry_temperature(
    phase: str, industry_temperature: str,
) -> None:
    item = candidate(
        "600001", phase=phase, industry_temperature=industry_temperature,
        filter_price="200", strength="95", market_cap="100", amount="2",
        days=15,
    )
    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"asset": "ETF基金"}, "a_share_only"),
        ({"temperature_prev": "平"}, "temperature_transition_not_entry"),
        ({"temperature_curr": "温"}, "temperature_transition_not_entry"),
        ({"strength": Decimal("94.99")}, "strength_below_95"),
        ({"industry_temperature": "平"}, "industry_temperature_not_hot"),
        ({"phase": "小暑"}, "phase_after_summer_solstice"),
        ({"market_cap": Decimal("99.99")}, "market_cap_below_100"),
        ({"amount": Decimal("1.99")}, "amount_below_2"),
        ({"exchange": "BJ"}, "excluded_security"),
    ],
)
def test_cn_candidate_rejects_failed_discipline(
    changes: dict[str, object], reason: str,
) -> None:
    decision = build_candidate_list(
        [replace(candidate("600001"), **changes)], held_symbols=set()
    )
    assert reason in decision.excluded["600001"]


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("temperature_prev", "temperature_missing"),
        ("temperature_curr", "temperature_missing"),
        ("strength", "strength_missing"),
        ("industry_tm_id", "industry_id_missing"),
        ("industry_temperature", "industry_temperature_missing"),
        ("phase", "phase_missing"),
        ("market_cap", "market_cap_missing"),
        ("amount", "amount_missing"),
        ("days", "right_side_days_missing"),
    ],
)
def test_cn_candidate_missing_required_fact_is_excluded(
    field: str, reason: str,
) -> None:
    decision = build_candidate_list(
        [replace(candidate("600001"), **{field: None})], held_symbols=set()
    )
    assert reason in decision.excluded["600001"]


@pytest.mark.parametrize("filter_price", [None, "200.01", "1500"])
def test_cn_candidate_does_not_gate_on_filter_price(
    filter_price: str | None,
) -> None:
    item = candidate("600001", filter_price=filter_price)

    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


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
        ("港股", "SH", "a_share_only"),
        ("期货", "SH", "a_share_only"),
        ("stock", "SH", "a_share_only"),
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


def test_candidate_accepts_days_amount_and_strength_boundaries() -> None:
    item = candidate("600001", days=15, amount="2", strength="95")
    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


@pytest.mark.parametrize(
    ("market", "ticker_symbol", "asset", "symbol", "exchange"),
    [
        ("US", "VIXY.US", "美股", "VIXY", "US"),
        ("HK", "0700.HK", "港股", "00700", "HK"),
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
            "industryTmId": 700001,
            "priceIndex": "10",
            "marketCap": "100",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
            "trendPhaseCurr": "立夏",
        },
        None,
        industry_temperature="热",
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
            "industryTmId": 700001,
            "priceIndex": "10",
            "marketCap": "100",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
            "trendPhaseCurr": "立夏",
        },
        invalid,
        industry_temperature="热",
    )
    assert item.atr is None
    assert build_candidate_list([item], held_symbols=set()).excluded["600001"] == [
        "atr_unavailable"
    ]


def test_atr14_requires_fifteen_valid_bars() -> None:
    assert atr14(bars(14)) is None
    assert atr14(bars(15)) == Decimal("2")


def test_buy_actions_respect_four_percent_cash_slots_and_round_lots() -> None:
    ranked = [candidate("600001"), candidate("600002")]

    actions = estimate_buy_actions(
        ranked=ranked,
        net_value=Decimal("676549.55"),
        available_cash=Decimal("7000"),
        current_position_count=9,
        position_weight=Decimal("0.04"),
    )

    assert [
        (item.symbol, item.target_amount, item.estimated_shares) for item in actions
    ] == [("600001", Decimal("7000"), 600)]


def test_buy_action_targets_never_reserve_more_than_available_cash() -> None:
    actions = estimate_buy_actions(
        ranked=[candidate("600001"), candidate("600002")],
        net_value=Decimal("676549.55"),
        available_cash=Decimal("7000"),
        current_position_count=8,
        position_weight=Decimal("0.04"),
    )

    assert [(item.symbol, item.target_amount) for item in actions] == [
        ("600001", Decimal("7000"))
    ]
    assert sum((item.target_amount for item in actions), Decimal("0")) <= Decimal(
        "7000"
    )


def test_report_plans_buys_after_all_sell_all_actions_release_cash_and_slots() -> None:
    discipline_account = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("0"),
        positions=(
            AccountPosition(
                "600001",
                "退出标的",
                "stock",
                Decimal("400"),
                Decimal("10"),
                Decimal("4000"),
            ),
        ),
        exceptions=(),
        position_count=10,
    )

    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=discipline_account,
        candidates=[candidate("600002")],
        holding_snapshots={"600001": holding("600001", danger=True)},
        bars_by_symbol={"600001": bars()},
    )

    assert [(item.symbol, item.action) for item in built.holdings] == [
        ("600001", "SELL_ALL")
    ]
    assert [
        (item.symbol, item.target_amount, item.estimated_shares)
        for item in built.buy_actions
    ] == [("600002", Decimal("4000.00"), 300)]


def test_buy_actions_use_four_percent_even_when_account_is_stale() -> None:
    actions = estimate_buy_actions(
        ranked=[candidate("600001")],
        net_value=Decimal("100000"),
        available_cash=Decimal("10000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
    )

    assert [
        (item.symbol, item.target_amount, item.estimated_shares)
        for item in actions
    ] == [("600001", Decimal("4000.00"), 300)]


def test_cn_buy_weight_follows_current_temperature() -> None:
    actions = estimate_buy_actions(
        ranked=[
            candidate("600001", temperature_curr="热"),
            candidate("600002", temperature_curr="沸"),
        ],
        net_value=Decimal("100000"),
        available_cash=Decimal("10000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
    )
    assert [
        (item.symbol, item.target_weight, item.target_amount, item.estimated_shares)
        for item in actions
    ] == [
        ("600001", Decimal("0.04"), Decimal("4000.00"), 300),
        ("600002", Decimal("0.04"), Decimal("4000.00"), 300),
    ]


@pytest.mark.parametrize(
    ("market", "exchange", "lot_sizes", "expected_quantity"),
    [
        ("CN", "SH", None, 300),
        ("HK", "HK", {"600001": 100}, 300),
        ("US", "US", None, 396),
    ],
)
def test_fixed_risk_sizing_includes_normal_costs_and_market_rounding(
    market: str,
    exchange: str,
    lot_sizes: dict[str, int] | None,
    expected_quantity: int,
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            positions=(),
            exceptions=(),
        ),
        candidates=[replace(candidate("600001"), exchange=exchange)],
        holding_snapshots={},
        bars_by_symbol={},
        market=market,
        lot_sizes=lot_sizes,
        metadata={"market": market, "broker": "futu"},
    )

    action = built.buy_actions[0]
    assert action.estimated_shares == expected_quantity
    assert action.normal_cost > 0
    assert action.planned_stop_risk == (
        Decimal(expected_quantity) * Decimal("1.01")
    )
    assert action.planned_stop_risk <= Decimal("400")
    assert action.decisive_constraint == "单笔风险上限"
    assert built.risk_summary["single_entry_risk_limit_pct"] == Decimal("0.004")
    assert built.risk_summary["portfolio_risk_limit_pct"] == Decimal("0.04")
    assert built.risk_summary["abnormal_loss_buffer_pct"] == Decimal("0.01")


def test_boiling_nominal_limit_stays_two_percent_below_risk_capacity() -> None:
    built = report(candidates=(candidate("600001", temperature_curr="沸"),))

    action = built.buy_actions[0]
    assert action.target_weight == Decimal("0.02")
    assert action.estimated_shares == 1300
    assert action.decisive_constraint == "名义仓位上限"


def _trend_kelly_rounds(*returns: str, market: str = "US") -> tuple[TrendKellyRound, ...]:
    return tuple(
        TrendKellyRound(
            round_id=f"round-{index:03d}",
            source="simulation",
            market=market,
            strategy_id=f"trend_animals_warm_to_hot/{market}/v3",
            opening_strategy_version="v3",
            closed_at=f"2026-07-{index // 24 + 1:02d}T{index % 24:02d}:00:00+00:00",
            net_return=Decimal(net_return),
            costs_complete=True,
            attribution_status="attributed",
            kelly_eligible=True,
        )
        for index, net_return in enumerate(returns)
    )


def test_cn_v7_report_keeps_v4_samples_without_admitting_v5_or_v6() -> None:
    identities = ("v4", "v5", "v6", "v7")
    rounds = tuple(
        replace(
            _trend_kelly_rounds("0.10", market="CN")[0],
            round_id=f"round-{version}",
            strategy_id=f"trend_animals_warm_to_hot/CN/{version}",
            opening_strategy_version=version,
        )
        for version in identities
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=[candidate("600001")],
        holding_snapshots={},
        bars_by_symbol={},
        market="CN",
        strategy_snapshot=trend_module.live_trend_strategy_snapshot(
            "CN", "abc123", (622466, 697199), strategy_version="v7"
        ),
        kelly_rounds=rounds,
    )

    assert built.strategy_snapshot["strategy_version"] == "v7"
    assert built.risk_summary["kelly_eligible_sample_count"] == 2
    assert built.risk_summary["kelly_selected_sample_count"] == 2


def _us_kelly_report(rounds: tuple[TrendKellyRound, ...]) -> TrendReport:
    return build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            positions=(),
            exceptions=(),
        ),
        candidates=[replace(candidate("600001"), exchange="US", close=Decimal("100"))],
        holding_snapshots={},
        bars_by_symbol={},
        market="US",
        metadata={"market": "US", "broker": "tiger"},
        kelly_rounds=rounds,
    )


def test_v3_kelly_cold_start_keeps_issue_4_nominal_sizing() -> None:
    built = _us_kelly_report(_trend_kelly_rounds(*(["0.10"] * 29)))

    assert built.strategy_snapshot["strategy_version"] == "v3"
    assert built.buy_actions[0].target_weight == Decimal("0.04")
    assert built.risk_summary["kelly_phase"] == "cold_start"
    assert built.risk_summary["kelly_eligible_sample_count"] == 29
    assert built.risk_summary["kelly_selected_sample_count"] == 29
    assert built.risk_summary["kelly_cap"] is None
    assert built.risk_summary["kelly_reason"] == (
        "Kelly 冷启动：29/30 个合格模拟闭环；继续使用固定风险仓位"
    )
    assert built.risk_summary["status"] == "active"


def test_v3_kelly_only_reduces_nominal_before_fixed_risk_constraints() -> None:
    rounds = _trend_kelly_rounds(*(["0.10"] * 15), *(["-0.099"] * 15))

    built = _us_kelly_report(rounds)

    action = built.buy_actions[0]
    assert action.target_weight == Decimal("0.012626")
    assert action.target_amount == Decimal("1262.60")
    assert action.estimated_shares == 12
    assert action.target_weight < Decimal("0.04")
    assert action.planned_stop_risk <= Decimal("400")
    assert built.risk_summary["kelly_phase"] == "active_all_samples"
    assert built.risk_summary["kelly_eligible_sample_count"] == 30
    assert built.risk_summary["kelly_cap"] == Decimal("0.012626")
    assert built.risk_summary["kelly_reason"] == ""


def test_v3_zero_kelly_pauses_only_future_entries_and_keeps_sell_decision() -> None:
    zero_rounds = _trend_kelly_rounds(*(["0.10"] * 15), *(["-0.10"] * 15))
    existing = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("99000"),
        positions=(
            AccountPosition(
                "OLD", "旧持仓", "stock", Decimal("10"), Decimal("90"), Decimal("1000")
            ),
        ),
        exceptions=(),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=existing,
        candidates=[replace(candidate("600001"), exchange="US", close=Decimal("100"))],
        holding_snapshots={
            "OLD": HoldingSnapshot(
                tm_id=1,
                symbol="OLD",
                exchange="US",
                name="旧持仓",
                as_of_date="2026-07-14",
                right_side=True,
                danger=True,
                boiling=False,
                champagne=False,
            )
        },
        bars_by_symbol={"OLD": bars(close=100, low=99)},
        market="US",
        metadata={"market": "US", "broker": "tiger"},
        kelly_rounds=zero_rounds,
    )

    assert [(item.symbol, item.action) for item in built.holdings] == [("OLD", "SELL_ALL")]
    assert built.buy_actions == ()
    assert built.risk_summary["status"] == "paused"
    assert built.risk_summary["kelly_cap"] == Decimal("0")
    assert built.risk_summary["pause_reason"] == "Kelly 上限为 0，仅暂停未来新开仓"
    assert built.risk_skips[0]["decisive_constraint"] == "Kelly 上限"


def test_frozen_v2_snapshot_does_not_enable_kelly() -> None:
    snapshot = trend_module.trend_strategy_snapshot("US", "abc123", (622460,))
    snapshot["strategy_id"] = "trend_animals_warm_to_hot/US/v2"
    snapshot["strategy_version"] = "v2"
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            positions=(),
            exceptions=(),
        ),
        candidates=[replace(candidate("600001"), exchange="US", close=Decimal("100"))],
        holding_snapshots={},
        bars_by_symbol={},
        market="US",
        metadata={"market": "US", "broker": "tiger"},
        strategy_snapshot=snapshot,
        kelly_rounds=_trend_kelly_rounds(*(["-0.10"] * 30)),
    )

    assert built.buy_actions[0].target_weight == Decimal("0.04")
    assert "kelly_phase" not in built.risk_summary


def test_v3_payload_and_compact_report_freeze_kelly_facts() -> None:
    built = _us_kelly_report(
        _trend_kelly_rounds(*(["0.10"] * 15), *(["-0.099"] * 15))
    )

    payload = trend_module._report_payload(built)
    markdown = render_markdown(built)

    assert payload["strategy_snapshot"]["strategy_version"] == "v3"
    assert payload["risk_summary"]["kelly_phase"] == "active_all_samples"
    assert payload["risk_summary"]["kelly_eligible_sample_count"] == 30
    assert payload["risk_summary"]["kelly_cap"] == "0.012626"
    assert "Kelly 阶段：全样本启用（30 个合格模拟闭环）" in markdown
    assert "当前 Kelly 上限：1.2626%" in markdown
    assert "实盘结果不参与计算" in markdown


def test_v3_payload_rejects_kelly_cap_action_mismatch() -> None:
    built = _us_kelly_report(
        _trend_kelly_rounds(*(["0.10"] * 15), *(["-0.099"] * 15))
    )
    summary = {**built.risk_summary, "kelly_cap": Decimal("0.02")}

    with pytest.raises(
        ValueError,
        match="^strategy snapshot does not match report actions$",
    ):
        trend_module._report_payload(replace(built, risk_summary=summary))


def test_v3_corrupt_stats_reason_pauses_entries_visibly() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            positions=(),
            exceptions=(),
        ),
        candidates=[replace(candidate("600001"), exchange="US", close=Decimal("100"))],
        holding_snapshots={},
        bars_by_symbol={},
        market="US",
        metadata={"market": "US", "broker": "tiger"},
        kelly_data_reason=(
            "Kelly 模拟闭环统计不可用，暂停新开仓："
            "trend_api_stats.json schema_version must be 'open_trader.trend_api_stats.v1'"
        ),
    )

    assert built.buy_actions == ()
    assert built.risk_summary["status"] == "paused"
    assert built.risk_summary["kelly_phase"] == "unavailable"
    assert built.risk_summary["kelly_cap"] is None
    assert built.risk_summary["kelly_reason"] == built.risk_summary["pause_reason"]
    assert "trend_api_stats.json schema_version" in render_markdown(built)


def test_v3_corrupt_stats_remain_visible_when_fixed_risk_also_pauses() -> None:
    kelly_reason = "Kelly 模拟闭环统计不可用，暂停新开仓：invalid stats"
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=Decimal("0"),
            available_cash=Decimal("0"),
            positions=(),
            exceptions=(),
        ),
        candidates=[replace(candidate("600001"), exchange="US")],
        holding_snapshots={},
        bars_by_symbol={},
        market="US",
        metadata={"market": "US", "broker": "tiger"},
        kelly_data_reason=kelly_reason,
    )

    payload = trend_module._report_payload(built)

    assert payload["risk_summary"]["pause_reason"] == "模拟盘净值缺失，暂停新开仓"
    assert payload["risk_summary"]["kelly_reason"] == kelly_reason


def test_sell_all_releases_cash_slot_and_planned_risk_before_new_entries() -> None:
    discipline_account = AccountSnapshot(
        source_date="2026-07-14",
        fresh=True,
        net_value=Decimal("100000"),
        available_cash=Decimal("62000"),
        positions=(
            AccountPosition(
                "600001", "退出标的", "stock", Decimal("3800"),
                Decimal("9.5"), Decimal("38000"),
            ),
        ),
        exceptions=(),
        position_count=1,
    )
    common = {
        "as_of_date": "2026-07-14",
        "execution_date": "2026-07-15",
        "account": discipline_account,
        "candidates": [candidate("600002")],
        "bars_by_symbol": {"600001": bars()},
        "prior_state": {
            "positions": {
                "600001": {
                    "initial_line": "9", "active_line": "9", "atr14": "0.5",
                    "position_started_for": "2026-07-01", "updated_for": "2026-07-13",
                }
            }
        },
    }

    held = build_report(
        **common,
        holding_snapshots={"600001": holding("600001")},
    )
    sold = build_report(
        **common,
        holding_snapshots={"600001": holding("600001", danger=True)},
    )

    assert held.risk_summary["existing_planned_risk"] == Decimal("3838.000")
    assert held.buy_actions[0].estimated_shares == 100
    assert held.buy_actions[0].decisive_constraint == "组合剩余风险"
    assert sold.risk_summary["existing_planned_risk"] == Decimal("0")
    assert sold.buy_actions[0].estimated_shares == 300
    assert sold.buy_actions[0].decisive_constraint == "单笔风险上限"


def test_full_existing_portfolio_risk_pauses_new_entries_explicitly() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("60000"),
            positions=(
                AccountPosition(
                    "600001", "持仓", "stock", Decimal("4000"),
                    Decimal("9.5"), Decimal("40000"),
                ),
            ),
            exceptions=(),
        ),
        candidates=[candidate("600002")],
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": bars()},
        prior_state={
            "positions": {
                "600001": {
                    "initial_line": "9", "active_line": "9", "atr14": "0.5",
                    "position_started_for": "2026-07-01", "updated_for": "2026-07-13",
                }
            }
        },
    )

    assert built.buy_actions == ()
    assert built.risk_summary["status"] == "paused"
    assert built.risk_summary["status_label"] == "组合风险已满"
    assert built.risk_summary["portfolio_remaining_risk"] == Decimal("0")
    assert built.risk_skips[0]["reason"] == "组合正常计划风险已达到净值 4%"


def test_v7_drawdown_pause_blocks_only_entries_and_keeps_sell_and_hold() -> None:
    strategy_snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "drawdown123", (622466, 697199), strategy_version="v7"
    )
    drawdown_summary = {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": "CN",
        "strategy_id": strategy_snapshot["strategy_id"],
        "strategy_version": "v7",
        "kelly_sample_key": (
            "CN|trend_animals_warm_to_hot/CN/v7|v7"
        ),
        "state_status": "ok",
        "status": "paused",
        "status_label": "暂停新开仓",
        "entry_allowed": False,
        "current_equity": "95000",
        "high_water_mark": "100000",
        "drawdown_pct": "0.05",
        "drawdown_limit_pct": "0.05",
        "pause_reason": "策略累计回撤已达到 5%，需人工解锁",
        "paused_at": "2026-07-14T18:00:00+08:00",
        "observed_at": "2026-07-14T18:00:00+08:00",
        "bootstrap_event": None,
        "recovery_event": None,
    }
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=Decimal("95000"),
            available_cash=Decimal("98000"),
            positions=(
                AccountPosition(
                    "600001", "卖出", "stock", Decimal("100"),
                    Decimal("9.5"), Decimal("1000"),
                ),
                AccountPosition(
                    "600002", "持有", "stock", Decimal("100"),
                    Decimal("9.5"), Decimal("1000"),
                ),
            ),
            exceptions=(),
            position_count=2,
        ),
        candidates=[candidate("600003")],
        holding_snapshots={
            "600001": holding("600001", danger=True),
            "600002": holding("600002"),
        },
        bars_by_symbol={"600001": bars(), "600002": bars()},
        prior_state={
            "positions": {
                symbol: {
                    "initial_line": "9",
                    "active_line": "9",
                    "atr14": "0.5",
                    "position_started_for": "2026-07-01",
                    "updated_for": "2026-07-13",
                }
                for symbol in ("600001", "600002")
            }
        },
        process_version="drawdown123",
        candidate_pool_ids=(622466, 697199),
        strategy_snapshot=strategy_snapshot,
        drawdown_summary=drawdown_summary,
    )

    assert [(item.symbol, item.action) for item in built.holdings] == [
        ("600001", "SELL_ALL"),
        ("600002", "HOLD"),
    ]
    assert built.buy_actions == ()
    assert built.risk_summary["status"] == "active"
    assert built.risk_summary["new_planned_risk"] == Decimal("0")
    assert built.risk_skips[0]["reason"] == drawdown_summary["pause_reason"]
    assert built.risk_skips[0]["decisive_constraint"] == "策略累计回撤"
    payload = trend_module._report_payload(built)
    assert payload["drawdown_summary"] == drawdown_summary
    markdown = render_markdown(built)
    assert "组合计划风险" in markdown
    assert "策略累计回撤" in markdown


def test_minimum_lot_skip_keeps_plain_integer_reason() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14", fresh=True,
            net_value=Decimal("100000"), available_cash=Decimal("100000"),
            positions=(), exceptions=(),
        ),
        candidates=[candidate("600001", atr="2")],
        holding_snapshots={},
        bars_by_symbol={},
    )

    assert built.buy_actions == ()
    assert built.risk_skips[0]["reason"] == "最小交易单位 100 股超过单笔风险上限"


@pytest.mark.parametrize("unknown", ["nav", "quantity", "price", "fx", "line"])
def test_unknown_critical_simulation_fact_pauses_the_whole_entry_batch(
    unknown: str,
) -> None:
    quantity = Decimal("NaN") if unknown == "quantity" else Decimal("100")
    nav = Decimal("NaN") if unknown == "nav" else Decimal("100000")
    prior_positions = {} if unknown == "line" else {
        "600001": {
            "initial_line": "9", "active_line": "9", "atr14": "0.5",
            "position_started_for": "2026-07-01", "updated_for": "2026-07-13",
        }
    }
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=AccountSnapshot(
            source_date="2026-07-14",
            fresh=True,
            net_value=nav,
            available_cash=Decimal("50000"),
            positions=(
                AccountPosition(
                    "600001", "持仓", "stock", quantity,
                    Decimal("9.5"), Decimal("1000"),
                ),
            ),
            exceptions=(),
        ),
        candidates=[candidate("600002"), candidate("600003")],
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={} if unknown == "price" else {"600001": bars()},
        prior_state={"positions": prior_positions},
        price_fx_to_account_currency=(
            Decimal("NaN") if unknown == "fx" else Decimal("1")
        ),
    )

    assert built.buy_actions == ()
    assert built.risk_summary["status"] == "paused"
    assert built.risk_summary["pause_reason"]
    assert [item["symbol"] for item in built.risk_skips] == ["600002", "600003"]
    assert all(item["reason"] == built.risk_summary["pause_reason"] for item in built.risk_skips)


def test_cn_buy_action_serializes_candidate_industry(tmp_path: Path) -> None:
    built = report(candidates=(candidate("600001", industry="电力"),))
    _, json_path = write_frozen_report(built, tmp_path)
    buy = json.loads(json_path.read_text(encoding="utf-8"))[
        "strategy_judgments"
    ]["formal_actions"][0]

    assert built.buy_actions[0].industry == "电力"
    assert buy["industry"] == "电力"


def test_market_buy_actions_use_whole_us_shares_and_hk_lot_sizes() -> None:
    us = replace(candidate("600001", close="123"), symbol="VIXY", exchange="US")
    hk = replace(candidate("600002", close="51"), symbol="00700", exchange="HK")

    us_actions = estimate_buy_actions(
        ranked=[us],
        net_value=Decimal("100000"),
        available_cash=Decimal("1000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
        market="US",
    )
    hk_actions = estimate_buy_actions(
        ranked=[hk],
        net_value=Decimal("1000000"),
        available_cash=Decimal("6000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
        market="HK",
        lot_sizes={"00700": 100},
    )

    assert us_actions[0].estimated_shares == 8
    assert hk_actions[0].estimated_shares == 100


def test_us_buy_actions_convert_usd_share_price_to_hkd() -> None:
    us = replace(candidate("600001", close="100", atr="5"), symbol="VIXY", exchange="US")

    actions = estimate_buy_actions(
        ranked=[us],
        net_value=Decimal("785000"),
        available_cash=Decimal("98500"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
        market="US",
        price_fx_to_account_currency=Decimal("7.85"),
    )

    assert actions[0].target_amount == Decimal("31400.00")
    assert actions[0].estimated_shares == 39


def test_hk_four_percent_weight_can_buy_one_board_lot() -> None:
    hk = replace(
        candidate("600002", close="127.6"),
        symbol="06821",
        exchange="HK",
    )

    actions = estimate_buy_actions(
        ranked=[hk],
        net_value=Decimal("628554.06"),
        available_cash=Decimal("55053.79"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
        market="HK",
        lot_sizes={"06821": 100},
    )

    assert len(actions) == 1
    assert actions[0].target_amount == Decimal("25142.16")
    assert actions[0].estimated_shares == 100


def test_more_than_ten_positions_has_no_formal_buys() -> None:
    assert (
        estimate_buy_actions(
            ranked=[candidate("600001")],
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            current_position_count=11,
            position_weight=Decimal("0.04"),
        )
        == []
    )


def test_unaffordable_candidate_does_not_consume_cash_or_slot() -> None:
    actions = estimate_buy_actions(
        ranked=[
            candidate("600001", close="20"),
            candidate("600002", close="1", atr="0.01"),
        ],
        net_value=Decimal("10000"),
        available_cash=Decimal("600"),
        current_position_count=9,
        position_weight=Decimal("0.04"),
    )
    assert [(item.symbol, item.target_amount, item.estimated_shares) for item in actions] == [
        ("600002", Decimal("400.00"), 400)
    ]


def test_unaffordable_top_ten_promotes_later_affordable_candidate() -> None:
    ranked = [candidate(f"6000{index:02d}", close="100") for index in range(1, 11)]
    ranked.append(candidate("600011", close="1", atr="0.01"))
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=replace(account(), net_value=Decimal("10000")),
        candidates=ranked,
        holding_snapshots={},
        bars_by_symbol={},
    )
    assert len(built.candidates) == 10
    assert [item.symbol for item in built.buy_actions] == ["600011"]


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
    assert built.metadata["position_weight"] == "0.04"
    assert built.metadata["position_weight_source"] == "fallback_4pct"


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


@pytest.mark.parametrize("previous", ["温", "热", "沸"])
def test_cn_holding_temperature_transition_to_flat_sells(previous: str) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding(
                "600001", temperature_prev=previous, temperature_curr="平"
            )
        },
        bars_by_symbol={"600001": bars()},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "SELL_ALL", "temperature_changed_to_flat"
    )


def test_cn_holding_continuous_flat_does_not_create_temperature_sell() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding(
                "600001", temperature_prev="平", temperature_curr="平"
            )
        },
        bars_by_symbol={"600001": bars()},
    )
    assert built.holdings[0].action == "HOLD"


def test_cn_holding_entry_failures_are_hints_not_sell_triggers() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding(
                "600001", strength="91.3", phase="小暑",
                temperature_prev="热", temperature_curr="热",
            )
        },
        bars_by_symbol={"600001": bars()},
    )
    decision = built.holdings[0]
    assert decision.action == "HOLD"
    assert decision.entry_hints == (
        "强度 91.3，低于入场线 95",
        "节气已到小暑",
        "不是新的温转热或温转沸入场信号",
    )


@pytest.mark.parametrize(
    ("market", "lot_sizes", "quantity", "expected_lot", "expected_shares"),
    [
        ("CN", None, Decimal("1000"), 100, 300),
        ("US", None, Decimal("7"), 1, 2),
        ("HK", {"600001": 200}, Decimal("1000"), 200, 200),
    ],
)
@pytest.mark.parametrize("boiling,champagne", [(True, False), (False, True), (True, True)])
def test_explicit_overheat_creates_one_partial_action(
    market: str,
    lot_sizes: Mapping[str, int] | None,
    quantity: Decimal,
    expected_lot: int,
    expected_shares: int,
    boiling: bool,
    champagne: bool,
) -> None:
    held_account = account("600001")
    held_account = replace(
        held_account,
        positions=(replace(held_account.positions[0], quantity=quantity),),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=held_account,
        candidates=(),
        holding_snapshots={
            "600001": holding("600001", boiling=boiling, champagne=champagne)
        },
        bars_by_symbol={"600001": bars(close=12, low=11)},
        market=market,
        lot_sizes=lot_sizes,
        prior_state={
            "schema_version": 1,
            "positions": {"600001": {
                "initial_line": "8", "active_line": "9", "atr14": "1",
                "tracking_active": False, "position_started_for": "2026-07-01",
                "updated_for": "2026-07-13",
            }},
        },
    )
    action = built.holdings[0]
    assert (action.action, action.reason) == (
        "SELL_PARTIAL", "overheat_take_profit"
    )
    assert action.target_fraction == Decimal("0.30")
    assert action.position_started_for == "2026-07-01"
    assert action.lot_size == expected_lot
    assert action.estimated_shares == expected_shares
    assert action.overheat_signals == tuple(
        signal
        for signal, enabled in (("boiling", boiling), ("champagne", champagne))
        if enabled
    )
    assert action.warnings == ()
    assert built.holdings[0].active_line == Decimal("11")


@pytest.mark.parametrize(
    ("market", "version"),
    [("CN", "v9"), ("US", "v6"), ("HK", "v6")],
)
def test_current_exit_discipline_ignores_overheat_and_sells_on_flat(
    market: str, version: str,
) -> None:
    pools = (
        (622466, 697199)
        if market == "CN"
        else (622460,)
        if market == "US"
        else (622494,)
    )
    strategy = trend_module.live_trend_strategy_snapshot(
        market, "abc123", pools, strategy_version=version
    )
    common = {
        "as_of_date": "2026-07-14",
        "execution_date": "2026-07-15",
        "account": account("600001"),
        "candidates": (),
        "bars_by_symbol": {"600001": bars(close=12, low=11)},
        "market": market,
        "strategy_snapshot": strategy,
    }
    overheated = build_report(
        **common,
        holding_snapshots={
            "600001": holding("600001", boiling=True, champagne=True)
        },
    )
    flat = build_report(
        **common,
        holding_snapshots={
            "600001": holding(
                "600001", temperature_prev="热", temperature_curr="平"
            )
        },
    )

    assert (overheated.holdings[0].action, overheated.holdings[0].reason) == (
        "HOLD", "trend_intact"
    )
    assert (flat.holdings[0].action, flat.holdings[0].reason) == (
        "SELL_ALL", "temperature_changed_to_flat"
    )


def test_current_exit_discipline_preserves_existing_line_without_trailing() -> None:
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v9"
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": bars(close=12, low=11)},
        strategy_snapshot=strategy,
        prior_state={
            "schema_version": 1,
            "positions": {
                "600001": {
                    "initial_line": "8",
                    "active_line": "9",
                    "atr14": "1",
                    "tracking_active": True,
                    "position_started_for": "2026-07-01",
                    "updated_for": "2026-07-13",
                }
            },
        },
    )
    assert built.holdings[0].active_line == Decimal("9")
    assert built.protection_state["positions"]["600001"]["tracking_active"] is False


def test_current_exit_discipline_does_not_require_overheat_fields() -> None:
    strategy = trend_module.live_trend_strategy_snapshot(
        "US", "abc123", (622460,), strategy_version="v6"
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding("600001", boiling=None, champagne=None)
        },
        bars_by_symbol={"600001": bars()},
        market="US",
        strategy_snapshot=strategy,
    )
    assert built.holdings[0].action == "HOLD"


@pytest.mark.parametrize(
    ("snapshot_changes", "triggered", "reason"),
    [
        ({"danger": True}, set(), "danger_signal"),
        ({"right_side": False}, set(), "left_trend_right_side"),
        ({}, {"600001"}, "protection_line_already_triggered"),
    ],
)
@pytest.mark.parametrize(
    ("market", "version"),
    [("CN", "v9"), ("US", "v6"), ("HK", "v6")],
)
def test_current_exit_discipline_keeps_all_existing_full_exit_triggers(
    market: str,
    version: str,
    snapshot_changes: dict[str, object],
    triggered: set[str],
    reason: str,
) -> None:
    snapshot = replace(holding("600001"), **snapshot_changes)
    assert trend_module._holding_action(
        symbol="600001",
        snapshot=snapshot,
        triggered=triggered,
        market=market,
        current_exit_discipline=True,
    ) == ("SELL_ALL", reason)


def test_full_exit_still_wins_over_overheat() -> None:
    snapshot = replace(holding("600001"), danger=True, boiling=True)
    assert trend_module._holding_action(
        symbol="600001", snapshot=snapshot, triggered=set()
    ) == ("SELL_ALL", "danger_signal")


def test_explicit_overheat_wins_over_unknown_non_exit_fields() -> None:
    snapshot = replace(
        holding("600001"), danger=None, right_side=None, boiling=True, champagne=None
    )
    assert trend_module._holding_action(
        symbol="600001", snapshot=snapshot, triggered=set()
    ) == ("SELL_PARTIAL", "overheat_take_profit")


@pytest.mark.parametrize(
    ("snapshot", "triggered", "market", "terminal", "expected"),
    [
        (
            holding("600001", boiling=True),
            {"600001"},
            "CN",
            False,
            ("SELL_ALL", "protection_line_already_triggered"),
        ),
        (
            holding("600001", boiling=True, right_side=False),
            set(),
            "CN",
            False,
            ("SELL_ALL", "left_trend_right_side"),
        ),
        (
            holding("600001", boiling=True, temperature_curr="平"),
            set(),
            "CN",
            False,
            ("SELL_ALL", "temperature_changed_to_flat"),
        ),
        (
            holding("600001", right_side=None),
            set(),
            "CN",
            False,
            ("MANUAL_REVIEW", "holding_signal_unknown"),
        ),
        (
            holding("600001", boiling=True),
            set(),
            "CN",
            True,
            ("HOLD", "trend_intact"),
        ),
    ],
)
def test_holding_action_preserves_exit_priority_and_terminal_trim(
    snapshot: HoldingSnapshot,
    triggered: set[str],
    market: str,
    terminal: bool,
    expected: tuple[str, str],
) -> None:
    assert trend_module._holding_action(
        symbol="600001",
        snapshot=snapshot,
        triggered=triggered,
        market=market,
        overheat_trim_terminal=terminal,
    ) == expected


def test_position_zero_allows_a_later_overheat_trim_lifecycle() -> None:
    terminal_state = {
        "schema_version": 1,
        "positions": {
            "600001": {
                "initial_line": "8",
                "active_line": "9",
                "atr14": "1",
                "position_started_for": "2026-07-01",
                "overheat_trim_status": "complete",
                "overheat_trim_target_qty": "300",
                "overheat_trim_filled_qty": "300",
                "overheat_trim_started_for": "2026-07-01",
                "updated_for": "2026-07-13",
            }
        },
    }
    terminal = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": bars()},
        prior_state=terminal_state,
    )
    zeroed = build_report(
        as_of_date="2026-07-15",
        execution_date="2026-07-16",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        prior_state=terminal.protection_state,
    )
    reentered = build_report(
        as_of_date="2026-07-16",
        execution_date="2026-07-17",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": replace(holding("600001", boiling=True), as_of_date="2026-07-16")
        },
        bars_by_symbol={"600001": bars(end_date="2026-07-16")},
        prior_state=zeroed.protection_state,
    )
    assert terminal.holdings[0].action == "HOLD"
    assert terminal.protection_state["positions"]["600001"] | {
        "overheat_trim_status": "complete",
        "overheat_trim_target_qty": "300",
        "overheat_trim_filled_qty": "300",
        "overheat_trim_started_for": "2026-07-01",
    } == terminal.protection_state["positions"]["600001"]
    assert zeroed.protection_state == {"schema_version": 1, "positions": {}}
    assert reentered.holdings[0].action == "SELL_PARTIAL"
    assert reentered.holdings[0].position_started_for == "2026-07-16"


@pytest.mark.parametrize("daily_bars", [None, bars(end_date="2026-07-13")])
def test_explicit_overheat_survives_unavailable_kline_and_preserves_old_line(
    daily_bars: list[DailyKlineBar] | None,
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": daily_bars},
        prior_state={
            "schema_version": 1,
            "positions": {
                "600001": {
                    "initial_line": "8",
                    "active_line": "8.5",
                    "atr14": "1",
                    "position_started_for": "2026-07-01",
                    "updated_for": "2026-07-13",
                }
            },
        },
    )
    action = built.holdings[0]
    assert (action.action, action.reason, action.active_line) == (
        "SELL_PARTIAL", "overheat_take_profit", Decimal("8.5")
    )
    assert action.warnings == ("holding_kline_unavailable",)


def test_explicit_overheat_without_line_persists_lifecycle_and_pauses_buys() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(candidate("600002"),),
        holding_snapshots={
            "600001": holding(
                "600001", boiling=True, danger=None, right_side=None, champagne=None
            )
        },
        bars_by_symbol={"600001": None},
    )
    action = built.holdings[0]
    assert (action.action, action.reason, action.initial_line, action.active_line) == (
        "SELL_PARTIAL", "overheat_take_profit", None, None
    )
    assert action.warnings == (
        "holding_signal_unknown", "holding_kline_unavailable"
    )
    assert built.protection_state["positions"]["600001"]["position_started_for"] == "2026-07-14"
    assert built.buy_actions == ()
    assert built.risk_summary["status"] == "paused"
    assert "活动保护线缺失" in str(built.risk_summary["pause_reason"])


@pytest.mark.parametrize("lot_size", [None, 0])
def test_hk_partial_without_a_valid_lot_size_requires_review(
    lot_size: int | None,
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": bars()},
        market="HK",
        lot_sizes={} if lot_size is None else {"600001": lot_size},
    )
    action = built.holdings[0]
    assert (action.action, action.reason) == (
        "MANUAL_REVIEW", "holding_lot_size_unavailable"
    )
    assert (
        action.position_started_for,
        action.target_fraction,
        action.estimated_shares,
        action.lot_size,
        action.overheat_signals,
        action.warnings,
    ) == (None, None, None, None, (), ())
    assert "overheat_trim_status" not in built.protection_state["positions"]["600001"]


@pytest.mark.parametrize(
    ("snapshot", "watch_events", "reason"),
    [
        (
            holding("600001", temperature_curr="平"),
            ({"event_type": "protection_triggered", "symbol": "600001"},),
            "protection_line_already_triggered",
        ),
        (holding("600001", danger=True, temperature_curr="平"), (), "danger_signal"),
        (
            holding("600001", right_side=False, temperature_curr="平"),
            (),
            "left_trend_right_side",
        ),
    ],
)
def test_cn_holding_stronger_sell_gates_beat_temperature_transition(
    snapshot: HoldingSnapshot,
    watch_events: tuple[dict[str, str], ...],
    reason: str,
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": snapshot},
        bars_by_symbol={"600001": bars()},
        watch_events=watch_events,
        prior_state={
            "schema_version": 1,
            "positions": {"600001": {
                "initial_line": "8", "active_line": "9", "atr14": "1",
                "updated_for": "2026-07-13",
            }},
        },
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == ("SELL_ALL", reason)


@pytest.mark.parametrize("field", ["temperature_prev", "temperature_curr"])
def test_cn_holding_missing_temperature_requires_review(field: str) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": replace(holding("600001"), **{field: None})
        },
        bars_by_symbol={"600001": bars()},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "MANUAL_REVIEW", "holding_signal_unknown"
    )


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


def test_stale_holding_kline_requires_review_even_with_old_line() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": bars(end_date="2026-07-13")},
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

    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "MANUAL_REVIEW",
        "holding_kline_unavailable",
    )


@pytest.mark.parametrize(
    ("snapshot", "watch_events", "reason"),
    [
        (
            holding("600001"),
            ({"event_type": "protection_triggered", "symbol": "600001"},),
            "protection_line_already_triggered",
        ),
        (holding("600001", danger=True), (), "danger_signal"),
        (holding("600001", right_side=False), (), "left_trend_right_side"),
    ],
)
def test_stale_holding_kline_preserves_stronger_sell_priority(
    snapshot: HoldingSnapshot,
    watch_events: tuple[dict[str, str], ...],
    reason: str,
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": snapshot},
        bars_by_symbol={"600001": bars(end_date="2026-07-13")},
        watch_events=watch_events,
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

    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "SELL_ALL",
        reason,
    )


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


def test_build_report_freezes_real_holding_decision_without_strategy_side_effect() -> None:
    held = account("600001")
    held = replace(
        held,
        positions=(
            replace(held.positions[0], avg_cost_price=Decimal("9.5")),
        ),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        real_holdings=RealHoldingInput(
            status="available",
            reason="",
            source={
                "broker": "eastmoney",
                "broker_label": "东方财富",
                "snapshot_period": "2026-07-14",
                "source_kind": "statement",
                "freshness_text": "非实时",
                "read_only_text": "只读，不自动下单",
            },
            positions=held.positions,
            holding_snapshots={"600001": holding("600001")},
            bars_by_symbol={"600001": bars()},
            prior_state=None,
        ),
    )

    assert built.real_holdings[0].action == "HOLD"
    assert built.real_holdings[0].active_line == Decimal("5.5")
    assert built.real_protection_state["positions"]["600001"]["active_line"] == "5.5"
    assert built.protection_state == {"schema_version": 1, "positions": {}}
    assert built.buy_actions == ()
    payload = trend_module._report_payload(built)
    judgments = payload["strategy_judgments"]
    assert judgments["real_holding_decisions"][0]["symbol"] == "600001"
    assert judgments["real_holding_decisions_status"] == "available"
    assert judgments["real_holding_decisions_source"]["broker"] == "eastmoney"
    assert judgments["formal_actions"] == []
    assert payload["signal_snapshots"]["real_holdings"]["600001"]["symbol"] == "600001"


def test_real_continuing_protection_line_never_decreases() -> None:
    held = account("600001")
    held = replace(
        held,
        positions=(
            replace(held.positions[0], avg_cost_price=Decimal("9.5")),
        ),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        real_holdings=RealHoldingInput(
            status="available",
            reason="",
            source={},
            positions=held.positions,
            holding_snapshots={"600001": holding("600001")},
            bars_by_symbol={"600001": bars()},
            prior_state={
                "schema_version": 1,
                "positions": {
                    "600001": {
                        "initial_line": "5.8",
                        "active_line": "6",
                        "atr14": "1",
                        "position_started_for": "2026-07-01",
                        "updated_for": "2026-07-13",
                    }
                },
            },
        ),
    )

    assert built.real_holdings[0].active_line == Decimal("6")
    assert built.real_holdings[0].position_started_for is None
    assert built.real_protection_state["positions"]["600001"]["position_started_for"] == (
        "2026-07-01"
    )


def test_real_missing_signal_does_not_invent_a_protection_line() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        real_holdings=RealHoldingInput(
            status="available",
            reason="",
            source={},
            positions=account("600001").positions,
            holding_snapshots={
                "600001": replace(holding("600001"), right_side=None)
            },
            bars_by_symbol={"600001": bars()},
            prior_state=None,
        ),
    )

    assert (built.real_holdings[0].action, built.real_holdings[0].reason) == (
        "MANUAL_REVIEW",
        "holding_signal_unknown",
    )
    assert built.real_holdings[0].active_line is None
    assert built.real_protection_state["positions"]["600001"].get("active_line") is None


def test_real_symbol_misses_are_per_position_and_agrz_skips_trend_lookup() -> None:
    class MissingApi:
        def __init__(self) -> None:
            self.searches: list[tuple[str, str, str]] = []

        def search_exact_symbol(
            self,
            symbol: str,
            *,
            market: str,
            expected_date: str,
        ) -> int:
            self.searches.append((symbol, market, expected_date))
            raise TrendAnimalsLookupError(f"missing {symbol}")

    class Quote:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, str]] = []

        def get_daily_kline(
            self,
            symbol: str,
            *,
            start: str,
            end: str,
        ) -> list[DailyKlineBar]:
            self.requests.append((symbol, start, end))
            return bars(end_date=end)

    real_input = RealHoldingInput(
        status="available",
        reason="",
        source={"broker": "tiger"},
        positions=(
            AccountPosition(
                symbol="US.AGRZ",
                name="AGRZ",
                asset_class="etf",
                quantity=Decimal("10"),
                avg_cost_price=Decimal("20"),
                market_value=Decimal("200"),
            ),
            AccountPosition(
                symbol="US.EUV",
                name="EUV",
                asset_class="etf",
                quantity=Decimal("10"),
                avg_cost_price=Decimal("20"),
                market_value=Decimal("200"),
            ),
        ),
        holding_snapshots={},
        bars_by_symbol={},
        prior_state=None,
    )
    api = MissingApi()
    quote = Quote()

    enriched, rows, real_bars, request_count = (
        trend_module.enrich_real_holding_input(
            real_input,
            api=api,
            quote=quote,
            market="US",
            as_of_date="2026-07-29",
            kline_start="2026-04-30",
            existing_holding_ids={},
            existing_rows_by_tm_id={},
            existing_holding_snapshots={},
            existing_bars_by_symbol={},
        )
    )

    assert enriched.status == "available"
    assert enriched.trend_excluded_symbols == ("US.AGRZ",)
    assert api.searches == [("US.EUV", "US", "2026-07-29")]
    assert enriched.holding_snapshots == {
        "US.AGRZ": None,
        "US.EUV": None,
    }
    assert set(enriched.bars_by_symbol) == {"US.AGRZ", "US.EUV"}
    assert [request[0] for request in quote.requests] == ["US.AGRZ", "US.EUV"]
    assert rows == {}
    assert set(real_bars) == {"US.AGRZ", "US.EUV"}
    assert request_count == 0

    built = build_report(
        as_of_date="2026-07-29",
        execution_date="2026-07-30",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        market="US",
        real_holdings=enriched,
    )
    decisions = {item.symbol: item for item in built.real_holdings}

    assert (
        decisions["US.AGRZ"].action,
        decisions["US.AGRZ"].reason,
    ) == ("MANUAL_REVIEW", "holding_trend_excluded")
    assert decisions["US.AGRZ"].industry == ""
    assert decisions["US.AGRZ"].temperature_prev is None
    assert decisions["US.AGRZ"].temperature_curr is None
    assert decisions["US.AGRZ"].strength is None
    assert decisions["US.AGRZ"].phase is None
    assert decisions["US.EUV"].reason == "holding_signal_unknown"


def test_real_symbol_system_failure_still_degrades_entire_real_input() -> None:
    class BrokenApi:
        def search_exact_symbol(
            self,
            symbol: str,
            *,
            market: str,
            expected_date: str,
        ) -> int:
            raise TrendAnimalsError("service unavailable")

    real_input = RealHoldingInput(
        status="available",
        reason="",
        source={"broker": "tiger"},
        positions=(
            AccountPosition(
                symbol="US.MSFT",
                name="Microsoft",
                asset_class="stock",
                quantity=Decimal("1"),
                avg_cost_price=Decimal("500"),
                market_value=Decimal("500"),
            ),
        ),
        holding_snapshots={},
        bars_by_symbol={},
        prior_state=None,
    )

    enriched, rows, real_bars, request_count = (
        trend_module.enrich_real_holding_input(
            real_input,
            api=BrokenApi(),
            quote=object(),
            market="US",
            as_of_date="2026-07-29",
            kline_start="2026-04-30",
            existing_holding_ids={},
            existing_rows_by_tm_id={},
            existing_holding_snapshots={},
            existing_bars_by_symbol={},
        )
    )

    assert enriched.status == "unavailable"
    assert enriched.reason == "真实持仓趋势服务不可用：service unavailable"
    assert rows == {}
    assert real_bars == {}
    assert request_count == 0


def test_unavailable_real_snapshot_is_distinct_from_empty_available_snapshot() -> None:
    source = {
        "broker": "phillips",
        "broker_label": "辉立",
        "snapshot_period": "",
        "source_kind": "statement",
        "freshness_text": "非实时",
        "read_only_text": "只读，不自动下单",
    }
    unavailable = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        real_holdings=RealHoldingInput(
            status="unavailable",
            reason="未找到可用的辉立持仓结单",
            source=source,
            positions=(),
            holding_snapshots={},
            bars_by_symbol={},
            prior_state={"schema_version": 1, "positions": {"00700": {}}},
        ),
    )
    available_empty = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        real_holdings=RealHoldingInput(
            status="available",
            reason="",
            source=source,
            positions=(),
            holding_snapshots={},
            bars_by_symbol={},
            prior_state=None,
        ),
    )

    assert unavailable.real_holdings_status == "unavailable"
    assert unavailable.real_holdings_reason == "未找到可用的辉立持仓结单"
    assert unavailable.real_holdings == ()
    assert unavailable.real_protection_state is None
    assert available_empty.real_holdings_status == "available"
    assert available_empty.real_holdings == ()
    assert available_empty.real_protection_state == {"schema_version": 1, "positions": {}}


def test_new_holding_protection_line_uses_merged_average_fill() -> None:
    held_account = account("600001")
    held_account = replace(
        held_account,
        positions=(
            replace(held_account.positions[0], avg_cost_price=Decimal("9.5")),
        ),
    )
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v9"
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=held_account,
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": bars(close=10, low=9)},
        strategy_snapshot=strategy,
    )
    assert built.holdings[0].initial_line == Decimal("5.5")
    assert built.protection_state["positions"]["600001"]["initial_line"] == "5.5"


def test_legacy_holding_protection_line_keeps_close_anchor() -> None:
    held_account = account("600001")
    held_account = replace(
        held_account,
        positions=(
            replace(held_account.positions[0], avg_cost_price=Decimal("9.5")),
        ),
    )
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v8"
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=held_account,
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": bars(close=10, low=9)},
        strategy_snapshot=strategy,
    )
    assert built.holdings[0].initial_line == Decimal("6")
    assert built.protection_state["positions"]["600001"]["initial_line"] == "6"


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
        bars_by_symbol={"600001": bars(close=12, low=11, end_date="2026-07-15")},
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
        bars_by_symbol={"600001": bars(end_date="2026-07-16")},
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
        bars_by_symbol={"600001": bars(end_date="2026-07-17")},
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
        "account": serialized_account(fresh=True),
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

    assert title == "【日报｜富途｜美股趋势报告｜2026-07-15】"
    assert message == "\n".join(
        [
            "数据截至：2026-07-14",
            "账户状态：已更新",
            "今日动作：卖出 1｜买入 1｜持有 1｜复核 2",
            "",
            "卖出",
            "1. AAPL 苹果｜右侧趋势已结束｜全部卖出｜保护线 190",
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


def test_partial_action_is_a_formal_trim_in_feishu_and_markdown() -> None:
    held_account = account("600001")
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=replace(
            held_account,
            positions=(replace(held_account.positions[0], quantity=Decimal("1000")),),
        ),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": bars()},
    )

    payload = trend_module._report_payload(built)
    _, message = render_trend_feishu_text(
        payload, broker_label="东方财富", market_label="A股"
    )
    markdown = render_markdown(built)

    assert payload["strategy_judgments"]["holding_decisions"][0]["action"] == "SELL_PARTIAL"
    assert payload["strategy_judgments"]["formal_actions"] == [
        payload["strategy_judgments"]["holding_decisions"][0]
    ]
    assert "今日动作：卖出 1｜买入 0｜持有 0｜复核 0" in message
    assert "沸腾/开香槟过热止盈｜止盈减仓 30%｜模拟预计数量 300 股" in message
    assert "今日无买卖动作" not in message
    assert "止盈减仓 30%" in markdown
    assert "全部卖出 0｜止盈减仓 30% 1" in markdown
    assert "模拟预计数量 300 股" in markdown
    assert "无需卖出" not in markdown


def test_partial_action_labels_simulated_target_signals_and_warnings() -> None:
    held_account = account("600001")
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=replace(
            held_account,
            positions=(replace(held_account.positions[0], quantity=Decimal("1000")),),
        ),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": None},
    )

    payload = trend_module._report_payload(built)
    _, message = render_trend_feishu_text(
        payload, broker_label="东方财富", market_label="A股"
    )
    markdown = render_markdown(built)

    for text in (
        "止盈减仓 30%",
        "模拟预计数量 300 股",
        "每手 100 股",
        "触发信号 沸腾",
        "持仓日线数据不可用",
    ):
        assert text in message
        assert text in markdown
    assert trend_module.REASON_LABELS["holding_lot_size_unavailable"] == "持仓整手信息不可用"


def test_current_exit_copy_uses_hard_stop_and_omits_partial_count() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={"600001": bars()},
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
        watch_events=({"symbol": "600001", "event_type": "protection_triggered"},),
    )
    current_snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v9"
    )
    current_payload = trend_module._report_payload(built)
    current_payload["strategy_snapshot"] = current_snapshot
    _, feishu = render_trend_feishu_text(
        current_payload, broker_label="东方财富", market_label="A股"
    )
    markdown = render_markdown(replace(built, strategy_snapshot=current_snapshot))

    for text in ("2×ATR14 硬止损",):
        assert text in markdown
        assert text in feishu
    assert "止盈减仓 30%" not in markdown
    assert "止盈减仓 30%" not in feishu
    assert "全部卖出 1｜允许买入 0" in markdown


def test_trend_feishu_text_uses_short_no_trade_template() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": serialized_account(fresh=False),
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
    assert title == "【日报｜辉立｜港股趋势报告｜2026-07-15】"
    assert message == (
        "数据截至：2026-07-14\n"
        "账户状态：账户数据非实时，执行前核对现金与持仓\n"
        "今日无买卖动作｜持有 1｜复核 0\n\n"
        "请人工确认，不自动下单。"
    )


@pytest.mark.parametrize("market", ["US", "HK"])
def test_trend_feishu_text_omits_raw_option_attention(market: str) -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": serialized_account(fresh=True),
        "metadata": {"market": market},
        "strategy_judgments": {
            "holding_decisions": [],
            "formal_actions": [],
        },
        "option_attention": [
            {
                "symbol": "QQQ",
                "right_side": {
                    "previous": False,
                    "current": True,
                    "changed": True,
                },
                "temperature": {
                    "previous": "温",
                    "current": "热",
                    "changed": True,
                },
                "phase": {
                    "previous": "谷雨",
                    "current": "立夏",
                    "changed": True,
                },
                "strength_change": {
                    "previous": None,
                    "current": None,
                    "changed": False,
                },
                "danger": {
                    "previous": False,
                    "current": False,
                    "changed": False,
                },
                "boiling": {
                    "previous": False,
                    "current": False,
                    "changed": False,
                },
                "champagne": {
                    "previous": False,
                    "current": False,
                    "changed": False,
                },
            }
        ],
    }

    _, message = render_trend_feishu_text(
        payload, broker_label="老虎" if market == "US" else "辉立", market_label=market
    )

    assert "期权关注" not in message
    assert "QQQ｜右侧" not in message


def test_trend_feishu_text_never_appends_option_attention_to_cn() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": serialized_account(fresh=True),
        "metadata": {"market": "CN"},
        "strategy_judgments": {
            "holding_decisions": [],
            "formal_actions": [],
        },
        "option_attention": [{"symbol": "600001"}],
    }

    _, message = render_trend_feishu_text(
        payload, broker_label="东方财富", market_label="A股"
    )

    assert "期权关注" not in message
    assert "600001" not in message


def test_trend_feishu_text_moves_unknown_hold_reason_to_review() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": serialized_account(fresh=True),
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
        "account": serialized_account(fresh=True),
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


@pytest.mark.parametrize(
    "fresh", [False, MISSING_FRESH, None, "yes"]
)
def test_trend_feishu_text_keeps_buy_for_non_realtime_account(
    fresh: object,
) -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": serialized_account(fresh=fresh),
        "metadata": {"market": "HK", "broker": "phillips"},
        "strategy_judgments": {
            "holding_decisions": [],
            "formal_actions": [
                {
                    "action": "BUY",
                    "symbol": "02800",
                    "name": "盈富基金",
                    "estimated_shares": 100,
                    "target_amount": "1000",
                    "estimated_initial_line": "9",
                }
            ],
        },
    }

    _, message = render_trend_feishu_text(
        payload, broker_label="辉立", market_label="港股"
    )

    assert "账户状态：账户数据非实时，执行前核对现金与持仓" in message
    assert "今日动作：卖出 0｜买入 1｜持有 0｜复核 0" in message
    assert "\n买入\n" in message
    assert "02800 盈富基金" in message
    assert "禁止买入" not in message


@pytest.mark.parametrize(
    "account",
    [
        None,
        {},
        {**serialized_account(), "source_date": ""},
        {**serialized_account(), "source_date": "not-a-date"},
        {**serialized_account(), "source_date": "2026-13"},
        {**serialized_account(), "source_date": "2026-02-30"},
        {**serialized_account(), "net_value": "NaN"},
        {**serialized_account(), "available_cash": None},
        {**serialized_account(), "positions": ["not-a-position"]},
        {**serialized_account(), "positions": [{}]},
        {
            **serialized_account(),
            "positions": [{**serialized_position(), "symbol": ""}],
        },
        {
            **serialized_account(),
            "positions": [{**serialized_position(), "name": ""}],
        },
        {
            **serialized_account(),
            "positions": [{**serialized_position(), "asset_class": ""}],
        },
        {
            **serialized_account(),
            "positions": [{**serialized_position(), "quantity": "NaN"}],
        },
        {
            **serialized_account(),
            "positions": [{**serialized_position(), "market_value": None}],
        },
        {
            **serialized_account(),
            "positions": [
                {**serialized_position(), "avg_cost_price": "Infinity"}
            ],
        },
        {**serialized_account(), "exceptions": [1]},
    ],
)
def test_trend_feishu_text_rejects_missing_or_malformed_account(
    account: object,
) -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "metadata": {"market": "HK", "broker": "phillips"},
        "strategy_judgments": {
            "holding_decisions": [],
            "formal_actions": [{"action": "BUY", "symbol": "02800"}],
        },
    }
    if account is not None:
        payload["account"] = account

    with pytest.raises(ValueError, match="账户快照无效"):
        render_trend_feishu_text(
            payload, broker_label="辉立", market_label="港股"
        )


def test_trend_feishu_text_lists_reviews_on_no_trade_days() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": serialized_account(fresh=True),
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
    assert title == "【需处理｜东方财富｜A股趋势报告生成失败｜2026-07-15】"
    assert message == (
        "发生：趋势报告未生成\n"
        "影响：不能依据旧报告交易\n"
        "现在做：确认 Trend Animals 数据状态后手动重跑东方财富报告\n"
        "原因：趋势数据在截止时间前仍未更新"
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


def test_cn_markdown_keeps_filter_and_execution_prices_distinct() -> None:
    markdown = render_markdown(
        report(candidates=(candidate("600001", filter_price="9.8", close="10"),))
    )

    assert "筛选价 9.80 元（Trend Animals）" in markdown
    assert "执行参考价 10.00 元（富途前复权日线）" in markdown
    assert "温→热" in markdown
    assert "目标仓位 4.00%" in markdown
    assert "实际股数按富途数据日前复权日线收盘价向下取整" in markdown
    assert "按东方财富实时价格" not in markdown


@pytest.mark.parametrize("market", ["US", "HK", "CN"])
def test_markdown_uses_shared_warning_for_stale_account(market: str) -> None:
    built = report()
    built = replace(
        built,
        account=account(fresh=False),
        metadata={**built.metadata, "market": market},
    )

    markdown = render_markdown(built)

    assert "账户：账户数据非实时，执行前核对现金与持仓" in markdown
    assert "已过期" not in markdown
    assert "日结单" not in markdown


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
            "忽略旧成分 1 条：NUVL（2026-07-14）",
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
    assert "忽略旧成分 1 条：NUVL（2026-07-14）" in markdown
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


def test_markdown_translates_missing_account_exception_fields() -> None:
    built = report()
    built = replace(
        built,
        account=replace(
            built.account,
            exceptions=(
                "unsupported Eastmoney asset: <missing-symbol> <missing-name> (CN/bond)",
            ),
        ),
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


def test_report_freezes_and_renders_aggregate_right_side_structure() -> None:
    context = replace(
        _industry_context(700001),
        industry="银行",
        aggregate_right_count_ratio=Decimal("0.191"),
        aggregate_right_market_cap_ratio=Decimal("0.650"),
        prior_aggregate_right_count_ratio=Decimal("0.150"),
        prior_aggregate_right_market_cap_ratio=Decimal("0.600"),
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        industry_contexts=(context,),
    )

    payload = trend_module._report_payload(built)
    markdown = render_markdown(built)

    assert payload["industry_contexts"][0]["aggregate_right_count_ratio"] == "0.191"
    assert payload["industry_contexts"][0]["aggregate_right_market_cap_ratio"] == "0.650"
    assert "右侧个数占比 15% → 19.1%" in markdown
    assert "右侧市值占比 60% → 65%" in markdown
    assert "高于右侧个数占比 45.9 个百分点" in markdown
    assert "不是账户仓位或上涨概率" in markdown


def test_no_action_report_uses_exact_cash_sentence() -> None:
    assert "现金也是有效仓位，本日无需交易。" in render_markdown(report())


def test_formal_buy_text_includes_window_estimates_target_and_line() -> None:
    markdown = render_markdown(report(candidates=(candidate("600001"),)))
    assert "09:30–10:00" in markdown
    assert "约 2600 股" in markdown
    assert "金额上限 27061.98 元" in markdown
    assert "预计保护线 9.00" in markdown
    assert "按富途数据日前复权日线收盘价向下取整为 100 股整数倍" in markdown


def test_candidate_row_shows_industry_slots_and_weight() -> None:
    markdown = render_markdown(report(candidates=(candidate("600001"),)))
    assert "行业 电力（已占 0 个席位，当前仓位 0.00%）" in markdown


def test_flat_bars_keep_zero_atr_in_state_and_render() -> None:
    flat = [
        replace(item, open=10, high=10, low=10, close=10)
        for item in bars()
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


def test_receipt_recovery_preserves_frozen_allocation_and_rotation_payload(
    tmp_path: Path,
) -> None:
    protection_state = {"schema_version": 1, "positions": {}}
    frozen_payload = {
        "allocation": {
            "daily_path": "data/trend_allocation/daily/2026-08-03.json",
            "sha256": "b" * 64,
        },
        "strategy_judgments": {
            "simulate_rotation_pairs": [{
                "sell_symbol": "SIM", "buy_symbol": "BUY",
                "execution_date": "2026-08-04", "execution_mode": "automatic",
            }],
            "real_rotation_pairs": [{
                "sell_symbol": "REAL", "buy_symbol": "BUY",
                "execution_date": "2026-08-04", "execution_mode": "manual",
            }],
        },
        "protection_state": protection_state,
    }
    receipt_path = tmp_path / "delivery/2026-08-03.json"
    trend_module._write_delivery_receipt(
        receipt_path,
        status="prepared",
        generated_at="2026-08-03T16:20:00+08:00",
        artifact_stem="2026-08-03",
        markdown="frozen",
        report_json=json.dumps(frozen_payload, ensure_ascii=False),
        protection_state=protection_state,
    )
    latest = tmp_path / "trend_allocation/latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps({"daily_path": "data/trend_allocation/daily/later.json"}),
        encoding="utf-8",
    )

    recovered = trend_module.read_delivery_receipt(
        receipt_path, artifact_stem="2026-08-03"
    )
    assert recovered is not None
    assert json.loads(str(recovered["report_json"])) == frozen_payload
    _, replayed_json = trend_module._freeze_receipt_report(
        receipt=recovered,
        reports_dir=tmp_path / "reports/trend_a_share",
        artifact_stem="2026-08-03",
    )
    assert json.loads(replayed_json.read_text(encoding="utf-8")) == frozen_payload


def test_frozen_revisions_choose_first_free_pair(tmp_path: Path) -> None:
    base = report()
    assert write_frozen_report(base, tmp_path, revision=True)[0].name == "2026-07-14-r1.md"
    assert write_frozen_report(base, tmp_path, revision=True)[0].name == "2026-07-14-r2.md"


def test_frozen_revision_writes_matching_industry_history_revision(
    tmp_path: Path,
) -> None:
    context = _industry_context(700001)
    receipt = {
        "artifact_stem": "2026-07-14-r2",
        "report_json": json.dumps(
            {
                "generated_at": "2026-07-14T19:00:00+08:00",
                "strategy_snapshot": {"strategy_version": "v10"},
                "industry_contexts": [_context_to_mapping(context)],
            }
        ),
    }

    path = trend_module._write_frozen_industry_context_history(
        receipt=receipt,
        history_root=tmp_path,
        market="CN",
    )

    assert path is not None
    assert path.name == "2026-07-14-r2.json"


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
        "risk_skips": [],
    }
    assert payload["risk_summary"]["portfolio_risk_limit_pct"] == "0.04"
    assert payload["risk_summary"]["abnormal_loss_buffer_pct"] == "0.01"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("parameters", "single_entry_risk_limit", "0.04"),
        ("parameters", "portfolio_risk_limit", "0.4"),
        ("parameters", "abnormal_loss_buffer", "0.1"),
        ("parameters", "normal_cost_rate", "0.009"),
        ("parameters", "normal_cost_model", "按心情估算"),
        ("summary", "single_entry_risk_limit_pct", Decimal("0.4")),
        ("summary", "normal_cost_rate", Decimal("0.009")),
        ("summary", "normal_cost_model", "按心情估算"),
        ("summary", "total_risk_budget_target_pct", Decimal("0.5")),
        ("summary", "disclaimer", "5% 保证最大损失"),
    ],
)
def test_v2_report_rejects_frozen_risk_contract_drift(
    section: str, key: str, value: object,
) -> None:
    built = report(candidates=(candidate("600001"),))
    snapshot = copy.deepcopy(built.strategy_snapshot)
    summary = copy.deepcopy(built.risk_summary)
    target = snapshot["parameters"] if section == "parameters" else summary
    assert isinstance(target, dict)
    target[key] = value
    tampered = replace(built, strategy_snapshot=snapshot, risk_summary=summary)

    with pytest.raises(ValueError, match="strategy snapshot does not match report actions"):
        trend_module._report_payload(tampered)


def test_v2_report_freezes_shared_remaining_risk_semantics() -> None:
    built = report(candidates=(candidate("600001"),))

    assert built.risk_summary["portfolio_remaining_risk_note"] == (
        "组合剩余风险供本报告后续新仓共享，不等于单标的仓位上限。"
    )


def test_v2_report_rejects_paused_state_with_new_buy_risk() -> None:
    built = report(candidates=(candidate("600001"),))
    summary = copy.deepcopy(built.risk_summary)
    summary.update({
        "status": "paused",
        "status_label": "暂停新开仓",
        "pause_reason": "测试暂停",
    })

    with pytest.raises(ValueError, match="strategy snapshot does not match report actions"):
        trend_module._report_payload(replace(built, risk_summary=summary))


def test_v2_report_rejects_active_state_over_portfolio_risk_limit() -> None:
    built = report()
    summary = copy.deepcopy(built.risk_summary)
    portfolio_limit = summary["portfolio_risk_limit"]
    assert isinstance(portfolio_limit, Decimal)
    nav = portfolio_limit / Decimal("0.04")
    planned_risk = portfolio_limit + Decimal("1")
    summary.update({
        "existing_planned_risk": planned_risk,
        "new_planned_risk": Decimal("0"),
        "portfolio_planned_risk": planned_risk,
        "portfolio_planned_risk_pct": planned_risk / nav,
        "portfolio_remaining_risk": Decimal("0"),
        "portfolio_remaining_risk_pct": Decimal("0"),
    })

    with pytest.raises(ValueError, match="strategy snapshot does not match report actions"):
        trend_module._report_payload(replace(built, risk_summary=summary))


def test_v2_report_rejects_risk_amounts_scaled_away_from_account_nav() -> None:
    built = report()
    summary = copy.deepcopy(built.risk_summary)
    for key in (
        "portfolio_risk_limit",
        "portfolio_remaining_risk",
        "single_entry_risk_limit",
        "abnormal_loss_buffer",
    ):
        summary[key] *= 2

    with pytest.raises(ValueError, match="strategy snapshot does not match report actions"):
        trend_module._report_payload(replace(built, risk_summary=summary))


@pytest.mark.parametrize("malformed", ["zero_risk", "partial_lot"])
def test_v2_report_rejects_impossible_buy_risk_facts(malformed: str) -> None:
    built = report(candidates=(candidate("600001"),))
    action = built.buy_actions[0]
    summary = copy.deepcopy(built.risk_summary)
    if malformed == "zero_risk":
        action = replace(
            action,
            planned_stop_risk=Decimal("0"),
            planned_stop_risk_pct=Decimal("0"),
            normal_cost=Decimal("0"),
        )
        summary.update({
            "new_planned_risk": Decimal("0"),
            "portfolio_planned_risk": Decimal("0"),
            "portfolio_planned_risk_pct": Decimal("0"),
            "portfolio_remaining_risk": summary["portfolio_risk_limit"],
            "portfolio_remaining_risk_pct": Decimal("0.04"),
        })
    else:
        action = replace(action, estimated_shares=350, lot_size=100)

    tampered = replace(
        built, buy_actions=(action,), risk_summary=summary
    )
    with pytest.raises(ValueError, match="strategy snapshot does not match report actions"):
        trend_module._report_payload(tampered)


def test_frozen_json_formal_actions_include_sells_and_buys(tmp_path: Path) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600009"),
        candidates=(
            replace(candidate("600001"), boiling=True, champagne=False),
        ),
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
    assert {"boiling", "champagne"}.isdisjoint(
        payload["signal_snapshots"]["candidates"][0]
    )
    assert {"boiling", "champagne"}.isdisjoint(judgments["top10_candidates"][0])
    assert "no_action" not in payload


def test_frozen_json_holding_decisions_include_phase(tmp_path: Path) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600009"),
        candidates=(),
        holding_snapshots={"600009": holding("600009", danger=True, phase="立夏")},
        bars_by_symbol={"600009": None},
    )

    _, json_path = write_frozen_report(built, tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert payload["strategy_judgments"]["holding_decisions"][0]["phase"] == "立夏"


@pytest.mark.parametrize("market", ["US", "HK"])
def test_us_hk_frozen_candidates_keep_attention_risk_fields(
    tmp_path: Path, market: str
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=(
            replace(
                candidate("600001", exchange=market),
                boiling=True,
                champagne=False,
            ),
        ),
        holding_snapshots={},
        bars_by_symbol={},
        metadata={"market": market},
        market=market,
    )

    _, json_path = write_frozen_report(built, tmp_path / market)
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert payload["signal_snapshots"]["candidates"][0]["boiling"] is True
    assert payload["signal_snapshots"]["candidates"][0]["champagne"] is False
    assert payload["strategy_judgments"]["top10_candidates"][0]["boiling"] is True
    assert payload["strategy_judgments"]["top10_candidates"][0]["champagne"] is False


def test_report_records_generation_time_and_whitelisted_signal_audit(
    tmp_path: Path,
) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        generated_at="2026-07-14T17:00:01+08:00",
        account=account("600009"),
        candidates=(candidate("600001", danger=True),),
        holding_snapshots={"600009": replace(holding("600009"), boiling=None, days=3)},
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
        "asset": "A股",
        "industry": "电力",
        "industry_tm_id": 700001,
        "industry_temperature": "热",
        "filter_price": "10",
        "market_cap": "100",
        "strength": "96",
        "temperature_prev": "温",
        "temperature_curr": "热",
        "phase": "立夏",
        "days": 3,
        "gain_since_entry": None,
        "phase_prev": None,
        "phase_curr": None,
        "strength_change": None,
        "global_strength": None,
        "strength_prev_week": None,
        "strength_prev_month": None,
        "labels": [],
        "kline_supplement": None,
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
        "filter_price",
        "close",
        "atr",
        "market_cap",
        "market_value_currency",
        "cny_per_local_currency",
        "market_cap_cny_100m",
        "amount_cny_100m",
        "market_cap_cny_threshold_met",
        "amount_cny_threshold_met",
        "industry_tm_id",
        "industry_temperature",
        "temperature_prev",
        "temperature_curr",
        "phase",
        "gain_since_entry",
        "phase_prev",
        "phase_curr",
        "strength_change",
        "global_strength",
        "strength_prev_week",
        "strength_prev_month",
        "labels",
        "kline_supplement",
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
        "filter_price",
        "close",
        "atr",
        "market_cap",
        "market_value_currency",
        "cny_per_local_currency",
        "market_cap_cny_100m",
        "amount_cny_100m",
        "market_cap_cny_threshold_met",
        "amount_cny_threshold_met",
        "industry_tm_id",
        "industry_temperature",
        "temperature_prev",
        "temperature_curr",
        "phase",
        "gain_since_entry",
        "phase_prev",
        "phase_curr",
        "strength_change",
        "global_strength",
        "strength_prev_week",
        "strength_prev_month",
        "labels",
        "kline_supplement",
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
        trend_review_cn_simulate_acc_id=101,
        trend_review_us_simulate_acc_id=102,
        trend_review_hk_simulate_acc_id=103,
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
        "account": serialized_account(fresh=False),
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
        catalog_unit_cost: str = "0.071",
        snapshot_ids: list[object] | None = None,
        missing_industry_ids: set[int] | None = None,
        industry_error: Exception | None = None,
        industry_ids: dict[int, int] | None = None,
        industry_state_temperature: str = "热",
        ignored_stale_components: tuple[dict[str, str], ...] = (),
    ) -> None:
        self.calls = calls
        self.ready = ready
        self.snapshot_date = snapshot_date
        self.holding_error = holding_error
        self.invalid_billing = invalid_billing
        self.catalog_unit_cost = catalog_unit_cost
        self.snapshot_ids = snapshot_ids
        self.missing_industry_ids = missing_industry_ids or set()
        self.industry_error = industry_error
        self.industry_ids = industry_ids or {}
        self.industry_state_temperature = industry_state_temperature
        self.ignored_stale_components = ignored_stale_components
        self.snapshot_requests: list[tuple[list[int], tuple[str, ...]]] = []
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
        if tm_id == 700001:
            return [
                {"tmId": member_id, "tickerSymbol": f"60000{member_id}.SH", "asOfDate": expected_date}
                for member_id in range(1, 11)
            ]
        component_id = 1 if tm_id == 622466 else 2
        return [{"tmId": component_id, "tickerSymbol": f"60000{component_id}.SH", "asOfDate": expected_date}]

    def search_exact_symbol(
        self, symbol: str, *, market: str, expected_date: str
    ) -> int:
        assert (market, expected_date) == ("CN", "2026-07-14")
        self.calls.append(f"api.search.{symbol}")
        if self.holding_error:
            raise self.holding_error
        return int(symbol)

    def get_snapshot_billing(self) -> list[dict[str, object]]:
        self.calls.append("api.billing")
        catalog_fields = tuple(
            dict.fromkeys((*UNIFIED_TREND_FIELDS, *trend_module.INDUSTRY_STATE_FIELDS))
        )
        return [
            {
                "columnName": field,
                "priceCost": (
                    "bad"
                    if self.invalid_billing
                    else self.catalog_unit_cost
                    if field == "tickerName"
                    else "0.004"
                    if field in {
                        "TrendRightSideCountRatio",
                        "TrendRightSideMktCapRatio",
                    }
                    else "0"
                ),
            }
            for field in catalog_fields
        ]

    def get_snapshots(self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str) -> list[dict[str, object]]:
        self.calls.append("api.snapshots")
        self.snapshot_requests.append((tm_ids, fields))
        if fields == A_SHARE_INDUSTRY_FIELDS:
            if self.industry_error:
                raise self.industry_error
            return [
                {
                    "tmId": tm_id,
                    "asOfDate": expected_date,
                    "trendTemperatureCurr": "热",
                }
                for tm_id in tm_ids
                if tm_id not in self.missing_industry_ids
            ]
        if fields == trend_module.INDUSTRY_MEMBER_FIELDS:
            return [
                {
                    "tmId": tm_id,
                    "asOfDate": expected_date,
                    "tradableFlag": True,
                    "isTrendRightSide": True,
                }
                for tm_id in tm_ids
            ]
        if fields == trend_module.INDUSTRY_STATE_FIELDS:
            if self.industry_error:
                raise self.industry_error
            return [
                {
                    "tmId": tm_id,
                    "asOfDate": expected_date,
                    "trendTemperatureCurr": self.industry_state_temperature,
                    "trendStrengthLocalCurr": "92",
                    "TrendRightSideCountRatio": "0.191",
                    "TrendRightSideMktCapRatio": "0.650",
                }
                for tm_id in tm_ids
            ]
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
                "industryTmId": self.industry_ids.get(tm_id, 700001),
                "priceIndex": "10",
                "marketCap": "100",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "热",
                "trendPhaseCurr": "立夏",
            })
        return rows


def test_report_runner_fetches_unique_industries_in_one_batch(tmp_path: Path) -> None:
    calls: list[str] = []
    api = ReadyApi(
        calls,
        ignored_stale_components=(
            {"tickerSymbol": "NUVL", "asOfDate": "2026-07-14"},
        ),
    )
    result = run_a_share_trend_report(
        config=trend_config(tmp_path),
        run_date="2026-07-14",
        api_factory=lambda **kwargs: api,
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        notifier=RecordingFeishu(),
    )
    assert api.snapshot_requests == [
        ([1, 2], A_SHARE_SNAPSHOT_FIELDS),
        ([700001], A_SHARE_INDUSTRY_FIELDS),
        (list(range(1, 11)), trend_module.INDUSTRY_MEMBER_FIELDS),
        ([700001], trend_module.INDUSTRY_STATE_FIELDS),
    ]
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["account_input"] == {
        "snapshot_generation": "sha256:" + "a" * 64,
        "account_generation": "sha256:" + "b" * 64,
        "status": "healthy",
    }
    assert "忽略旧成分 1 条：NUVL（2026-07-14）" in payload["api_facts"]
    assert payload["metadata"]["run_date"] == "2026-07-14"
    assert payload["strategy_snapshot"]["strategy_version"] == "v8"
    assert payload["risk_summary"]["kelly_phase"] == "cold_start"
    assert payload["risk_summary"]["kelly_eligible_sample_count"] == 0
    assert payload["risk_summary"]["kelly_cap"] is None
    audit = payload["signal_snapshots"]["candidates"]
    assert audit[0]["industry_tm_id"] == 700001
    assert audit[0]["industry_temperature"] == "热"
    assert (
        f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows=2 "
        "cache=client-managed"
    ) in payload["api_facts"]
    assert payload["estimated_api_cost"] == "0.150"
    assert payload["api_cost"]["estimate_complete"] is False
    evidence_path = trend_config(tmp_path).data_dir / payload["replay_evidence"]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["query"]["component_pool_ids"] == [622466, 697199]
    assert evidence["responses"]["snapshots"]
    assert evidence["rebuild_inputs"]["candidates"]


def test_a_share_report_pins_one_account_snapshot_through_internal_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = copy.deepcopy(ACCOUNT_SNAPSHOT)
    fetches = 0
    seen: list[object] = []

    def fetch() -> dict[str, object]:
        nonlocal fetches
        fetches += 1
        return snapshot

    def attempt(**kwargs: object) -> AShareTrendRunResult:
        seen.append(kwargs.get("account_snapshot"))
        return AShareTrendRunResult(
            "waiting" if len(seen) == 1 else "generated",
            None,
            None,
        )

    monkeypatch.setattr(trend_module, "fetch_account_snapshot", fetch, raising=False)
    monkeypatch.setattr(trend_module, "_attempt_report", attempt)

    result = run_a_share_trend_report(
        config=trend_config(tmp_path),
        run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 17, tzinfo=SHANGHAI),
        sleep_fn=lambda _seconds: None,
    )

    assert result.status == "generated"
    assert fetches == 1
    assert seen == [snapshot, snapshot]
    assert all(item is snapshot for item in seen)


def test_report_runner_includes_simulated_holding_only_industry(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    api = ReadyApi([], industry_ids={600009: 339103})
    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **_kwargs: api,
        quote_factory=lambda **_kwargs: ReadyQuote([]),
        account_factory=simulation_account_with_positions("SH.600009"),
        notifier=RecordingFeishu(),
    )

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))

    assert {item["industry_tm_id"] for item in payload["industry_contexts"]} == {
        339103,
        700001,
    }
    assert payload["strategy_judgments"]["holding_decisions"][0][
        "industry"
    ] == "电力"


def test_cn_entry_gate_keeps_early_temperature_when_industry_state_changes(
    tmp_path: Path,
) -> None:
    api = ReadyApi([], industry_state_temperature="平")
    result = run_a_share_trend_report(
        config=trend_config(tmp_path),
        run_date="2026-07-14",
        api_factory=lambda **kwargs: api,
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingFeishu(),
    )

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert [
        item["symbol"] for item in payload["strategy_judgments"]["top10_candidates"]
    ] == [
        "000001",
        "000002",
    ]
    assert payload["industry_contexts"][0]["temperature"] == "平"


def test_collect_industry_contexts_queries_only_eligible_industries_and_unions_members(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    class Api:
        def get_components(self, *, tm_id: int, expected_date: str) -> list[dict[str, object]]:
            calls.append(("components", tm_id))
            assert tm_id == 700001
            return [
                {"tmId": member_id, "asOfDate": expected_date}
                for member_id in range(1, 13)
            ] + [{"tmId": 1, "asOfDate": expected_date}]

        def get_snapshots(
            self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str
        ) -> list[dict[str, object]]:
            calls.append(("snapshots", (tm_ids, fields)))
            if fields == trend_module.INDUSTRY_MEMBER_FIELDS:
                return [
                    {
                        "tmId": member_id,
                        "asOfDate": expected_date,
                        "tradableFlag": True,
                        "isTrendRightSide": True,
                    }
                    for member_id in tm_ids
                ]
            assert fields == trend_module.INDUSTRY_STATE_FIELDS
            return [
                {
                    "tmId": 700001,
                    "asOfDate": expected_date,
                    "trendTemperatureCurr": "热",
                    "trendStrengthLocalCurr": "92",
                    "TrendRightSideCountRatio": "0.191",
                    "TrendRightSideMktCapRatio": "0.650",
                }
            ]

    candidate_rows = [
        {
            "tmId": member_id,
            "industryTmId": 700001,
            "industryName": "电力",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
        }
        for member_id in range(1, 13)
    ]
    contexts, status, facts = trend_module.collect_industry_contexts(
        api=Api(),
        candidates=(
            candidate("000001"),
            candidate("000002", danger=True),
        ),
        candidate_rows=candidate_rows,
        held_symbols=set(),
        expected_date="2026-07-14",
        market="CN",
        history_root=tmp_path / "trend_industry_context",
    )

    assert [context.industry_tm_id for context in contexts] == [700001]
    assert contexts[0].component_count == 12
    assert contexts[0].aggregate_right_count_ratio == Decimal("0.191")
    assert contexts[0].aggregate_right_market_cap_ratio == Decimal("0.650")
    assert contexts[0].warm_to_hot_count == 12
    assert status["ordering_mode"] == "context_current_only"
    assert facts["member_ids"] == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
    assert facts["member_fields"] == trend_module.INDUSTRY_MEMBER_FIELDS
    assert facts["state_fields"] == trend_module.INDUSTRY_STATE_FIELDS
    assert "TrendRightSideCountRatio" in trend_module.INDUSTRY_STATE_FIELDS
    assert "TrendRightSideMktCapRatio" in trend_module.INDUSTRY_STATE_FIELDS
    assert calls == [
        ("components", 700001),
        (
            "snapshots",
            (
                list(range(1, 13)),
                trend_module.INDUSTRY_MEMBER_FIELDS,
            ),
        ),
        (
            "snapshots",
            (
                [700001],
                trend_module.INDUSTRY_STATE_FIELDS,
            ),
        ),
    ]


def test_collect_industry_contexts_marks_stale_only_components_invalid(
    tmp_path: Path,
) -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            assert (tm_id, expected_date) == (700001, "2026-07-14")
            raise TrendAnimalsNoCurrentRowsError(
                "getComponentTicker returned no current-date rows"
            )

        def get_snapshots(
            self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str
        ) -> list[dict[str, object]]:
            assert tm_ids == [700001]
            assert fields == trend_module.INDUSTRY_STATE_FIELDS
            return [{
                "tmId": 700001,
                "asOfDate": expected_date,
                "trendTemperatureCurr": "热",
                "trendStrengthLocalCurr": "92",
            }]

    contexts, status, facts = trend_module.collect_industry_contexts(
        api=Api(),
        candidates=(candidate("600001"),),
        candidate_rows=[{
            "tmId": 600001,
            "industryTmId": 700001,
            "industryName": "电力",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
        }],
        held_symbols=set(),
        expected_date="2026-07-14",
        market="CN",
        history_root=tmp_path / "trend_industry_context",
    )

    assert len(contexts) == 1
    assert contexts[0].component_count == 0
    assert contexts[0].valid is False
    assert "snapshot_coverage_below_90pct" in contexts[0].invalid_reasons
    assert status["ordering_mode"] == "legacy_invalid_current"
    assert facts["component_rows"] == 0


def test_collect_industry_contexts_appends_holding_industries_in_strength_order(
    tmp_path: Path,
) -> None:
    candidate_industry_id = 700001
    invalid_industry_id = 800004

    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return [{"tmId": tm_id + 1, "asOfDate": expected_date}]

        def get_snapshots(
            self,
            *,
            tm_ids: list[int],
            fields: tuple[str, ...],
            expected_date: str,
        ) -> list[dict[str, object]]:
            if fields == trend_module.INDUSTRY_MEMBER_FIELDS:
                return [
                    {
                        "tmId": tm_id,
                        "asOfDate": expected_date,
                        "tradableFlag": True,
                        "isTrendRightSide": True,
                    }
                    for tm_id in tm_ids
                ]
            strengths = {
                candidate_industry_id: "95",
                339103: "92.4",
                621693: "98.7",
            }
            return [
                {
                    "tmId": tm_id,
                    "asOfDate": expected_date,
                    "trendTemperatureCurr": "热",
                    **(
                        {"trendStrengthLocalCurr": strengths[tm_id]}
                        if tm_id in strengths
                        else {}
                    ),
                }
                for tm_id in tm_ids
            ]

    contexts, status, facts = trend_module.collect_industry_contexts(
        api=Api(),
        candidates=(
            candidate(
                "600001",
                industry="候选行业",
                industry_tm_id=candidate_industry_id,
            ),
        ),
        candidate_rows=[
            {
                "tmId": 600001,
                "industryTmId": candidate_industry_id,
                "industryName": "候选行业",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "热",
            }
        ],
        held_symbols=set(),
        holding_snapshots=(
            holding("600010", industry="银行", industry_tm_id=339103),
            holding("600011", industry="电力", industry_tm_id=621693),
            holding("600012", industry="银行", industry_tm_id=339103),
            holding("600013", industry="无行业", industry_tm_id=None),
            holding(
                "600014",
                industry="异常行业",
                industry_tm_id=invalid_industry_id,
            ),
        ),
        expected_date="2026-07-14",
        market="CN",
        history_root=tmp_path / "trend_industry_context",
    )

    assert facts["eligible_industry_ids"] == (candidate_industry_id,)
    assert facts["holding_industry_ids"] == (
        339103,
        621693,
        invalid_industry_id,
    )
    assert facts["context_industry_ids"] == (
        candidate_industry_id,
        339103,
        621693,
        invalid_industry_id,
    )
    assert [item.industry_tm_id for item in contexts] == [
        621693,
        candidate_industry_id,
        339103,
        invalid_industry_id,
    ]
    assert contexts[-1].strength is None
    assert contexts[-1].valid is False
    assert facts["holding_errors"] == {}
    assert status["ordering_mode"] == "context_current_only"


def test_collect_industry_contexts_skips_holding_only_member_breadth(
    tmp_path: Path,
) -> None:
    component_calls: list[int] = []
    member_snapshot_calls: list[list[int]] = []
    state_snapshot_calls: list[list[int]] = []

    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            component_calls.append(tm_id)
            assert tm_id == 700001
            return [
                {"tmId": member_id, "asOfDate": expected_date}
                for member_id in range(1, 13)
            ]

        def get_snapshots(
            self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str
        ) -> list[dict[str, object]]:
            if fields == trend_module.INDUSTRY_MEMBER_FIELDS:
                member_snapshot_calls.append(list(tm_ids))
                return [
                    {
                        "tmId": member_id,
                        "asOfDate": expected_date,
                        "tradableFlag": True,
                        "isTrendRightSide": True,
                    }
                    for member_id in tm_ids
                ]
            assert fields == trend_module.INDUSTRY_STATE_FIELDS
            state_snapshot_calls.append(list(tm_ids))
            strengths = {700001: "95", 339103: "92.4", 621693: "98.7"}
            return [
                {
                    "tmId": industry_id,
                    "asOfDate": expected_date,
                    "trendTemperatureCurr": "热",
                    "trendStrengthLocalCurr": strengths[industry_id],
                    "TrendRightSideCountRatio": "0.191",
                    "TrendRightSideMktCapRatio": "0.650",
                }
                for industry_id in tm_ids
            ]

    contexts, status, facts = trend_module.collect_industry_contexts(
        api=Api(),
        candidates=(
            candidate("600001", industry="候选行业", industry_tm_id=700001),
        ),
        candidate_rows=[
            {
                "tmId": 600001,
                "industryTmId": 700001,
                "industryName": "候选行业",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "热",
            }
        ],
        held_symbols=set(),
        holding_snapshots=(
            holding("600010", industry="银行", industry_tm_id=339103),
            holding("600011", industry="电力", industry_tm_id=621693),
        ),
        expected_date="2026-07-14",
        market="CN",
        history_root=tmp_path / "trend_industry_context",
    )

    assert component_calls == [700001]
    assert member_snapshot_calls == [list(range(1, 13))]
    assert state_snapshot_calls == [[700001], [339103, 621693]]
    assert facts["component_requests"] == 1
    assert facts["member_ids"] == tuple(range(1, 13))
    assert facts["member_rows"] == 12

    holding_contexts = {
        item.industry_tm_id: item
        for item in contexts
        if item.industry_tm_id in {339103, 621693}
    }
    assert all(
        item.member_breadth_collected is False
        and item.component_count == 0
        and item.right_share is None
        for item in holding_contexts.values()
    )
    assert [item.industry_tm_id for item in contexts] == [621693, 700001, 339103]
    assert status["ordering_mode"] == "context_current_only"


def test_collect_industry_contexts_keeps_holding_state_failure_local(
    tmp_path: Path,
) -> None:
    component_calls: list[int] = []
    member_snapshot_calls: list[list[int]] = []
    state_snapshot_calls: list[list[int]] = []

    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            component_calls.append(tm_id)
            assert tm_id == 700001
            return [
                {"tmId": member_id, "asOfDate": expected_date}
                for member_id in range(1, 13)
            ]

        def get_snapshots(
            self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str
        ) -> list[dict[str, object]]:
            if fields == trend_module.INDUSTRY_MEMBER_FIELDS:
                member_snapshot_calls.append(list(tm_ids))
                return [
                    {
                        "tmId": member_id,
                        "asOfDate": expected_date,
                        "tradableFlag": True,
                        "isTrendRightSide": True,
                    }
                    for member_id in tm_ids
                ]
            assert fields == trend_module.INDUSTRY_STATE_FIELDS
            state_snapshot_calls.append(list(tm_ids))
            if tm_ids == [339103, 621693]:
                raise TrendAnimalsError("holding state unavailable")
            return [
                {
                    "tmId": 700001,
                    "asOfDate": expected_date,
                    "trendTemperatureCurr": "热",
                    "trendStrengthLocalCurr": "95",
                    "TrendRightSideCountRatio": "0.191",
                    "TrendRightSideMktCapRatio": "0.650",
                }
            ]

    contexts, _status, facts = trend_module.collect_industry_contexts(
        api=Api(),
        candidates=(
            candidate("600001", industry="候选行业", industry_tm_id=700001),
        ),
        candidate_rows=[
            {
                "tmId": 600001,
                "industryTmId": 700001,
                "industryName": "候选行业",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "热",
            }
        ],
        held_symbols=set(),
        holding_snapshots=(
            holding("600010", industry="银行", industry_tm_id=339103),
            holding("600011", industry="电力", industry_tm_id=621693),
        ),
        expected_date="2026-07-14",
        market="CN",
        history_root=tmp_path / "trend_industry_context",
    )

    assert component_calls == [700001]
    assert member_snapshot_calls == [list(range(1, 13))]
    assert state_snapshot_calls == [[700001], [339103, 621693]]
    assert facts["holding_errors"] == {"states": "holding state unavailable"}
    assert all(
        context.member_breadth_collected is False
        and context.component_count == 0
        and context.right_share is None
        for context in contexts
        if context.industry_tm_id in {339103, 621693}
    )


def test_frozen_2026_07_31_paid_scope_ledger() -> None:
    frozen = {
        "CN": {
            "candidate": {621715: 34, 621743: 68},
            "holding_only": {339103: 42, 328115: 51, 621693: 102},
        },
        "HK": {
            "candidate": {},
            "holding_only": {
                621783: 37,
                621784: 75,
                621772: 83,
                621781: 129,
                621766: 151,
                621779: 63,
                621768: 113,
                669417: 0,
            },
        },
        "US": {
            "candidate": {332177: 247, 332182: 862},
            "holding_only": {
                332176: 1260,
                692047: 2,
                332179: 171,
                692034: 3,
                332181: 655,
                692011: 3,
                332174: 670,
            },
        },
    }
    old_component_calls = sum(
        len(scope["candidate"]) + len(scope["holding_only"])
        for scope in frozen.values()
    )
    new_component_calls = sum(
        len(scope["candidate"]) for scope in frozen.values()
    )
    old_member_ids = sum(
        sum(scope["candidate"].values()) + sum(scope["holding_only"].values())
        for scope in frozen.values()
    )
    new_member_ids = sum(
        sum(scope["candidate"].values()) for scope in frozen.values()
    )

    assert (old_component_calls, new_component_calls) == (22, 4)
    assert (old_member_ids, new_member_ids) == (4821, 1211)
    assert old_component_calls - new_component_calls == 18
    assert old_member_ids - new_member_ids == 3610
    assert Decimal(old_member_ids - new_member_ids) * Decimal("0.003") == Decimal(
        "10.830"
    )


def test_report_runner_turns_corrupt_kelly_stats_into_visible_entry_pause(
    tmp_path: Path,
) -> None:
    cfg = trend_config(tmp_path)
    stats = cfg.data_dir / "latest/trend_api_stats.json"
    stats.parent.mkdir(parents=True, exist_ok=True)
    stats.write_text('{"schema_version":"broken","rounds":[]}', encoding="utf-8")

    result = run_a_share_trend_report(
        config=cfg,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingFeishu(),
    )

    assert result.json_path is not None
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["strategy_judgments"]["formal_actions"] == []
    assert payload["risk_summary"]["kelly_phase"] == "unavailable"
    assert payload["risk_summary"]["status"] == "paused"
    assert "trend_api_stats.json" in payload["risk_summary"]["pause_reason"]
    assert "schema_version" in payload["risk_summary"]["pause_reason"]


def test_missing_industry_row_excludes_only_affected_candidate(
    tmp_path: Path,
) -> None:
    api = ReadyApi(
        [],
        missing_industry_ids={700001},
        industry_ids={1: 700001, 2: 700002},
    )
    config = trend_config(tmp_path)
    unlock_live_drawdown(config.data_dir)
    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        api_factory=lambda **kwargs: api,
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingFeishu(),
    )
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["excluded"] == {
        "000001": ["industry_temperature_missing"],
    }
    assert [
        item["symbol"]
        for item in payload["strategy_judgments"]["formal_actions"]
        if item["action"] == "BUY"
    ] == ["000002"]


def test_industry_snapshot_failure_blocks_report(tmp_path: Path) -> None:
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 21, 10, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi(
            [], industry_error=TrendAnimalsError("industry unavailable")
        ),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.json"))


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
        return [replace(item, high=10.2, low=9.8) for item in bars()]

    def close(self) -> None:
        pass


def test_report_runner_uses_cn_simulation_account_and_ignores_actual_portfolio(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    config.portfolio.write_text("actual account changes are overlay-only", encoding="utf-8")
    account_calls: list[dict[str, object]] = []

    class SimAccountClient:
        def __init__(self, **kwargs: object) -> None:
            account_calls.append(dict(kwargs))
            self.acc_id = int(kwargs["simulate_acc_id"])

        def account_snapshot(self) -> dict[str, object]:
            return DefaultSimAccountClient(
                simulate_acc_id=self.acc_id
            ).account_snapshot()

        def close(self) -> None:
            pass

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        account_factory=SimAccountClient,
        notifier=RecordingFeishu(),
    )

    assert result.status == "generated"
    assert account_calls[0] | {
        "simulate_acc_id": 101,
        "trd_market": "CN",
    } == account_calls[0]
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["account"]["positions"] == []
    assert payload["metadata"]["simulate_acc_id"] == 101
    assert payload["strategy_snapshot"]["strategy_version"] == "v8"
    assert payload["drawdown_summary"]["state_status"] == "missing"
    assert payload["drawdown_summary"]["entry_allowed"] is False
    assert payload["strategy_judgments"]["formal_actions"] == []


def test_generated_report_keeps_v8_identity_kelly_scope_and_drawdown_continuity(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    unlock_live_drawdown(config.data_dir)
    state_path = config.data_dir / "trend_drawdown/state.json"
    before = json.loads(state_path.read_text(encoding="utf-8"))

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingFeishu(),
    )

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    snapshot = payload["strategy_snapshot"]
    assert snapshot["strategy_id"] == "trend_animals_warm_to_hot/CN/v8"
    assert snapshot["strategy_version"] == "v8"
    assert snapshot["parameters"]["kelly_sample_scope"] == (
        "market+strategy_id+opening_strategy_version"
    )
    assert snapshot["parameters"]["kelly_sample_inherits"] == [
        {
            "market": "CN",
            "strategy_id": f"trend_animals_warm_to_hot/CN/{version}",
            "opening_strategy_version": version,
        }
        for version in ("v4", "v7", "v8")
    ]
    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert after["audit_events"] == before["audit_events"]


def test_report_runner_sends_exact_broker_v7_text(tmp_path: Path) -> None:
    calls: list[str] = []
    api_kwargs: dict[str, object] = {}
    config = trend_config(tmp_path)
    unlock_live_drawdown(config.data_dir)
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
    assert calls.index("api.balance_after") > max(
        index for index, call in enumerate(calls) if call == "api.snapshots"
    )
    assert calls.index("api.billing") < calls.index("api.snapshots")
    assert api_kwargs["cache_dir"] == config.data_dir / "trend_animals/cache"
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert notifier.messages == [
        (
            "【日报｜东方财富｜A股趋势报告｜2026-07-15】",
            "数据截至：2026-07-14\n"
            "账户状态：已更新\n"
            "今日动作：卖出 0｜买入 2｜持有 0｜复核 0\n"
            "本报告 API 费用：实扣 1 Trend Animals 余额单位\n\n"
            "买入\n"
            "1. 000001 股票000001｜09:30–10:00｜约 400 股｜金额上限 4000｜保护线 9.2\n"
            "2. 000002 股票000002｜09:30–10:00｜约 400 股｜金额上限 4000｜保护线 9.2\n\n"
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


def test_report_runner_failure_owns_day_at_inclusive_1900_deadline(tmp_path: Path) -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    feishu = RecordingFeishu()
    macos = RecordingMacOS()
    notifier = CompositeNotifier([feishu, macos])
    config = trend_config(tmp_path)
    times = iter([
        datetime(2026, 7, 14, 17, 50, tzinfo=SHANGHAI),
        datetime(2026, 7, 14, 19, 0, tzinfo=SHANGHAI),
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
            "【需处理｜东方财富｜A股趋势报告生成失败｜2026-07-14】",
            "发生：趋势报告未生成\n"
            "影响：不能依据旧报告交易\n"
            "现在做：确认 Trend Animals 数据状态后手动重跑东方财富报告\n"
            "原因：趋势数据在截止时间前仍未更新",
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
        datetime(2026, 7, 14, 19, 0, tzinfo=SHANGHAI),
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
        report_json=(
            '{\n  "delivery_status": "delivery_failed",\n'
            '  "protection_state": {"positions": {}, "schema_version": 1}\n}\n'
        ),
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
            report_json=(
                '{\n  "delivery_status": "sent",\n'
                '  "protection_state": {"positions": {}, "schema_version": 1}\n}\n'
            ),
            protection_state={"schema_version": 1, "positions": {}},
        )

    assert json.loads(receipt_path.read_text(encoding="utf-8")) == old_receipt
    recovered = trend_module.read_delivery_receipt(
        receipt_path, artifact_stem="2026-07-14"
    )
    assert recovered is not None
    assert recovered["markdown"] == "old report"
    assert recovered["report_json"] == (
        '{\n  "delivery_status": "delivery_failed",\n'
        '  "protection_state": {"positions": {}, "schema_version": 1}\n}\n'
    )


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
    assert calls.count("api.snapshots") == 4


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
    assert feishu.messages[0][0] == "【日报｜东方财富｜A股趋势报告｜2026-07-15】"
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


@pytest.mark.parametrize(
    ("failed_klines", "expected_symbols"),
    [
        ({"SH.000001", "SH.000002"}, []),
        ({"SH.000001"}, ["SH.000002"]),
    ],
)
def test_report_runner_records_candidate_mapping_only_after_verified_futu_kline(
    tmp_path: Path,
    failed_klines: set[str],
    expected_symbols: list[str],
) -> None:
    mapping_calls: list[dict[str, object]] = []

    class MappingApi(ReadyApi):
        def remember_symbol_row(self, **kwargs: object) -> None:
            mapping_calls.append(dict(kwargs))

    run_a_share_trend_report(
        config=trend_config(tmp_path),
        run_date="2026-07-14",
        api_factory=lambda **kwargs: MappingApi([]),
        quote_factory=lambda **kwargs: ReadyQuote(
            [], failed_klines=failed_klines
        ),
        notifier=RecordingFeishu(),
    )

    assert [call["expected_futu_symbol"] for call in mapping_calls] == expected_symbols
    assert all(call["market"] == "CN" for call in mapping_calls)
    assert [call["row"]["tickerSymbol"] for call in mapping_calls] == [
        symbol.replace("SH.", "") + ".SH" for symbol in expected_symbols
    ]


@pytest.mark.parametrize(
    ("market", "expected_tm_id", "row", "expected_calls"),
    [
        (
            "CN",
            308052,
            {"tmId": 308052, "tickerSymbol": "600036.SH", "asset": "A股"},
            1,
        ),
        (
            "CN",
            999999,
            {"tmId": 308052, "tickerSymbol": "600036.SH", "asset": "A股"},
            0,
        ),
        (
            "CN",
            308052,
            {"tmId": 308052, "tickerSymbol": "600036.US", "asset": "A股"},
            0,
        ),
        (
            "CN",
            308052,
            {"tmId": 308052, "tickerSymbol": "600036.SH", "asset": "美股"},
            0,
        ),
    ],
)
def test_legacy_mapping_upgrade_requires_matching_snapshot_identity(
    market: str,
    expected_tm_id: int,
    row: dict[str, object],
    expected_calls: int,
) -> None:
    calls: list[dict[str, object]] = []

    class Api:
        def remember_symbol_row(self, **kwargs: object) -> None:
            calls.append(dict(kwargs))

    trend_module._remember_verified_symbol_row(
        Api(),
        market=market,
        expected_futu_symbol="SH.600036",
        expected_tm_id=expected_tm_id,
        row=row,
    )

    assert len(calls) == expected_calls


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
        account_factory=simulation_account_with_positions("SH.600009"),
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
        account_factory=simulation_account_with_positions("BJ.920000"),
        notifier=RecordingFeishu(),
    )

    decision = json.loads(result.json_path.read_text(encoding="utf-8"))[
        "strategy_judgments"
    ]["holding_decisions"][0]
    assert (decision["symbol"], decision["action"]) == ("920000", "MANUAL_REVIEW")


def test_report_runner_snapshot_date_mismatch_uses_deadline_contract(tmp_path: Path) -> None:
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 21, 10, tzinfo=SHANGHAI),
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
        now_fn=lambda: datetime(2026, 7, 14, 21, 10, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([], snapshot_ids=snapshot_ids),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.json"))


def test_report_runner_retries_systemic_kline_outage_without_formal_report(tmp_path: Path) -> None:
    outage = FutuQuoteError("network down", error_type="quote_server_interrupted")
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 21, 10, tzinfo=SHANGHAI),
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
        now_fn=lambda: datetime(2026, 7, 14, 21, 10, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([], invalid_billing=True),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.json"))


def test_report_runner_rejects_catalog_cost_drift_before_paid_snapshots(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    result = run_a_share_trend_report(
        config=trend_config(tmp_path),
        run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 21, 10, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi(
            calls, catalog_unit_cost="0.072"
        ),
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        notifier=RecordingMacOS(),
    )

    assert result.status == "failed"
    assert "api.snapshots" not in calls


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


def test_report_runner_mapping_failure_is_manual_with_independent_price(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    write_portfolio(config.portfolio, [portfolio_row(symbol="600009")])
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(config.portfolio, (timestamp, timestamp))
    calls: list[str] = []
    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        api_factory=lambda **kwargs: ReadyApi(calls, holding_error=TrendAnimalsLookupError("missing")),
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        account_factory=simulation_account_with_positions("SH.600009"),
        notifier=RecordingFeishu(),
    )
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    decision = payload["strategy_judgments"]["holding_decisions"][0]
    assert (decision["symbol"], decision["action"], decision["reason"]) == (
        "600009",
        "MANUAL_REVIEW",
        "holding_signal_unknown",
    )
    assert decision["close"] == "10"
    assert "价格缺失" not in str(payload["risk_summary"]["pause_reason"])
    assert "futu.kline.SH.600009" in calls

    transport = trend_config(tmp_path / "transport")
    write_portfolio(transport.portfolio, [portfolio_row(symbol="600009")])
    os.utime(transport.portfolio, (timestamp, timestamp))
    transport_calls: list[str] = []
    transport_result = run_a_share_trend_report(
        config=transport, run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi([], holding_error=TrendAnimalsError("transport")),
        quote_factory=lambda **kwargs: ReadyQuote(transport_calls),
        account_factory=simulation_account_with_positions("SH.600009"),
        notifier=RecordingMacOS(),
    )
    transport_payload = json.loads(
        transport_result.json_path.read_text(encoding="utf-8")
    )
    transport_decision = transport_payload[
        "strategy_judgments"
    ]["holding_decisions"][0]
    assert transport_result.status == "generated"
    assert (
        transport_decision["action"],
        transport_decision["reason"],
        transport_decision["close"],
    ) == ("MANUAL_REVIEW", "holding_signal_unknown", "10")
    assert "价格缺失" not in str(
        transport_payload["risk_summary"]["pause_reason"]
    )
    assert "futu.kline.SH.600009" in transport_calls


def test_candidate_rejects_cross_market_trend_symbol() -> None:
    row = {
        "tmId": 600036,
        "tickerSymbol": "600036.HK",
        "asOfDate": "2026-07-14",
    }

    with pytest.raises(ValueError, match="invalid CN Trend Animals symbol"):
        evaluate_candidate(row, bars(), market="CN")


def test_report_runner_rejects_cached_holding_symbol_mismatch(
    tmp_path: Path,
) -> None:
    config = trend_config(tmp_path)
    write_portfolio(config.portfolio, [portfolio_row(symbol="600009")])
    timestamp = datetime(2026, 7, 14, 12, tzinfo=SHANGHAI).timestamp()
    os.utime(config.portfolio, (timestamp, timestamp))
    calls: list[str] = []

    class MismatchedApi(ReadyApi):
        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            rows = super().get_snapshots(**kwargs)
            for row in rows:
                if row.get("tmId") == 600009:
                    row["tickerSymbol"] = "000001.SZ"
            return rows

    result = run_a_share_trend_report(
        config=config,
        run_date="2026-07-14",
        api_factory=lambda **kwargs: MismatchedApi(calls),
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        account_factory=simulation_account_with_positions("SH.600009"),
        notifier=RecordingFeishu(),
    )

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    decision = payload["strategy_judgments"]["holding_decisions"][0]
    assert (decision["action"], decision["reason"], decision["close"]) == (
        "MANUAL_REVIEW",
        "holding_signal_unknown",
        "10",
    )
    assert "futu.kline.SH.600009" in calls
    assert "futu.kline.SZ.600009" not in calls


def test_report_runner_redacts_api_key_from_all_outputs(tmp_path: Path) -> None:
    config = trend_config(tmp_path)
    notifier = RecordingMacOS()

    class SecretApi(ReadyApi):
        def get_update_status(self) -> list[dict[str, object]]:
            raise TrendAnimalsError(f"failed {config.trend_animals_api_key}")

    result = run_a_share_trend_report(
        config=config, run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 21, 10, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: SecretApi([]),
        quote_factory=lambda **kwargs: ReadyQuote([]), notifier=notifier,
    )
    captured = repr(result) + repr(notifier.messages)
    for path in [*config.logs_dir.rglob("*"), *config.reports_dir.rglob("*")]:
        if path.is_file():
            captured += path.read_text(encoding="utf-8")
    assert config.trend_animals_api_key not in captured
