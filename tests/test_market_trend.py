from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import open_trader.a_share_trend as trend_module

from open_trader import market_trend, trend_review
from open_trader.a_share_trend import (
    AShareTrendRunResult,
    write_protection_state,
)
from open_trader.daily_premarket import DailyPremarketConfig
from open_trader.a_share_trend import favorite_candidate_ids
from open_trader.market_trend import (
    MARKET_NOTIFICATION_LABELS,
    MARKET_SETTINGS,
    MarketHoliday,
    _candidate_pool_components,
    market_paths,
    resolve_market_dates,
    run_market_trend_report,
    updates_ready,
    update_status_gap,
)
from open_trader.trend_animals import (
    TrendAnimalsError,
    TrendAnimalsLookupError,
    TrendAnimalsNoCurrentRowsError,
)
from open_trader.notifications import (
    FeishuWebhookNotifier,
    NotificationError,
    NullNotifier,
)
from open_trader.kline_technical_facts import DailyKlineBar
from open_trader.a_share_trend import (
    A_SHARE_INDUSTRY_FIELDS,
    INDUSTRY_MEMBER_FIELDS,
    INDUSTRY_STATE_FIELDS,
    UNIFIED_TREND_FIELDS,
)
from open_trader.strategy_drawdown import automatic_bootstrap_strategy_drawdown
from open_trader.trend_api_stats import (
    build_trend_api_stats_payload,
    write_trend_api_stats,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
ACCOUNT_SNAPSHOT = {
    "snapshot_generation": "sha256:" + "c" * 64,
    "account_generation": "sha256:" + "d" * 64,
    "status": "healthy",
    "sources": {
        "account": {
            "status": "healthy",
            "as_of": "2026-07-15T12:00:00+08:00",
            "reason": None,
            "brokers": {
                broker: {
                    "source_kind": "live" if broker == "tiger" else "statement",
                    "data_as_of": "2026-07-15T12:00:00+08:00",
                    "last_success_at": "2026-07-15T12:00:00+08:00",
                    "status": "healthy",
                    "reason": None,
                }
                for broker in ("eastmoney", "futu", "phillips", "tiger")
            },
        },
        "quotes": {
            "status": "healthy",
            "as_of": "2026-07-15T12:00:00+08:00",
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


@pytest.fixture(autouse=True)
def account_http_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        market_trend,
        "fetch_account_snapshot",
        lambda: copy.deepcopy(ACCOUNT_SNAPSHOT),
        raising=False,
    )


def unlock_live_drawdown(
    data_dir: Path,
    market: str,
    *,
    version: str = "v4",
) -> None:
    automatic_bootstrap_strategy_drawdown(
        data_dir,
        market=market,
        strategy_id=f"trend_animals_warm_to_hot/{market}/{version}",
        strategy_version=version,
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("100000"),
        source_date="2026-07-13",
        accepted_git_sha="a" * 40,
        occurred_at="2026-07-14T08:00:00+08:00",
        actor="pytest",
        reason="first_activation",
        entry_eligible_from="2026-07-14",
    )


def allocation_for(market: str, *, rank: int, entry_weight: str) -> dict[str, object]:
    return {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": "b" * 64,
        "snapshot": {
            "markets": {
                market: {
                    "rank": rank,
                    "score": "95.2",
                    "score_source": "美国ETF",
                    "entry_weight": entry_weight,
                    "nominal_weight": {2: "0.40", 3: "0.20"}[rank],
                },
            },
        },
    }


def allocation_reference_for_runner() -> dict[str, object]:
    assets = {
        "CN": ("A股", "ETF基金"),
        "HK": ("港股", "香港ETF"),
        "US": ("美股", "美国ETF"),
    }
    ranks = {"CN": 3, "HK": 2, "US": 1}
    strengths = {1: ("90", "80"), 2: ("70", "60"), 3: ("50", "40")}
    roots = {
        market: {
            role: {
                "asset": asset,
                "tm_id": (index + 1) * 10 + role_index,
                "as_of_date": "2026-08-03",
                "global_strength": strengths[ranks[market]][role_index],
            }
            for role_index, (role, asset) in enumerate(
                zip(("stock", "etf"), assets[market])
            )
        }
        for index, market in enumerate(("CN", "HK", "US"))
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
                market: {
                    "rank": ranks[market],
                    "score": strengths[ranks[market]][0],
                    "score_source": assets[market][0],
                    "entry_weight": {1: "0.06", 2: "0.04", 3: "0.02"}[ranks[market]],
                    "nominal_weight": {1: "0.60", 2: "0.40", 3: "0.20"}[ranks[market]],
                }
                for market in ("CN", "HK", "US")
            },
        },
    }


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
                        "cost_price": "10",
                        "market_val": "1000",
                    }
                    for code in codes
                ],
            }

    return SimAccountClient


@pytest.fixture(autouse=True)
def default_simulation_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        market_trend,
        "FutuSimulateOrderExecutionClient",
        DefaultSimAccountClient,
    )


class RecordingFeishu(FeishuWebhookNotifier):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(webhook_url="https://example.invalid")
        self.fail = fail
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
        if self.fail:
            raise NotificationError("network down")


@pytest.mark.parametrize(
    ("market", "name", "pool_ids", "market_parameters"),
    [
        (
            "US",
            "美股短线右侧趋势",
            (622460,),
            {"allowed_exchange": "US", "lot_size": 1, "buy_window": "美股常规交易时段"},
        ),
        (
            "HK",
            "港股短线右侧趋势",
            (622494,),
            {
                "allowed_exchange": "HK",
                "lot_size_source": "Futu 每标的整手",
                "buy_window": "09:30-10:00",
            },
        ),
    ],
)
def test_market_strategy_snapshot_matches_runtime_rules(
    market: str,
    name: str,
    pool_ids: tuple[int, ...],
    market_parameters: dict[str, object],
) -> None:
    snapshot = trend_module.trend_strategy_snapshot(market, "abc123", pool_ids)

    assert snapshot["strategy_name"] == name
    assert snapshot["strategy_version"] == "v3"
    assert snapshot["parameters"] == {
        "candidate_pool_ids": list(pool_ids),
        "min_strength_exclusive": "90",
        "max_right_side_days_exclusive": 10,
        "min_amount_100m": "1",
        "requires_right_side": True,
        "requires_tradable": True,
        "requires_no_danger": True,
        "requires_matching_data_date": True,
        "requires_not_held": True,
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
        "target_weight": "0.04",
        **market_parameters,
        "initial_protection_atr_multiple": "2",
        "exit_reasons": ["danger", "left_right_side", "protection"],
        "trailing_low_days": 5,
        "protection_line_non_decreasing": True,
    }
    assert snapshot["parameter_rows"]
    assert all(
        set(row) == {"group", "name", "value"}
        for row in snapshot["parameter_rows"]
    )
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}
    assert rows["买入数量"].startswith("使用已有现金，")
    assert rows["过热跟踪"] == (
        "沸腾或开香槟触发后，活动保护线取原值与此前 5 个完整交易日最低价的较高者，只升不降"
    )
    live = trend_module.live_trend_strategy_snapshot(market, "abc123", pool_ids)
    assert (live["strategy_id"], live["strategy_version"]) == (
        f"trend_animals_warm_to_hot/{market}/v8", "v8"
    )
    assert "overheat_trim_fraction" not in live["parameters"]
    assert not {
        "过热止盈比例",
        "过热止盈信号",
        "过热止盈次数",
        "过热止盈取整",
        "不足一手处理",
        "清仓优先级",
        "过热跟踪",
    } & {row["name"] for row in live["parameter_rows"]}


@pytest.mark.parametrize("market", ["US", "HK"])
def test_live_market_strategy_snapshot_defaults_to_v8_with_exact_inheritance(
    market: str,
) -> None:
    pools = (622460,) if market == "US" else (622494,)
    snapshot = trend_module.live_trend_strategy_snapshot(market, "abc123", pools)

    assert snapshot["strategy_id"] == f"trend_animals_warm_to_hot/{market}/v8"
    assert snapshot["strategy_version"] == "v8"
    assert snapshot["parameters"]["kelly_sample_inherits"] == [
        {
            "market": market,
            "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
            "opening_strategy_version": version,
        }
        for version in ("v4", "v5", "v6", "v7", "v8")
    ]


@pytest.mark.parametrize(
    ("market", "rank", "weight", "pools"),
    [
        ("HK", 2, "0.04", (622494,)),
        ("US", 3, "0.02", (622460,)),
    ],
)
def test_allocation_market_v11_freezes_rank_weight(
    market: str, rank: int, weight: str, pools: tuple[int, ...],
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market,
        "abc123",
        pools,
        allocation=allocation_for(market, rank=rank, entry_weight=weight),
    )

    assert snapshot["strategy_version"] == "v11"
    assert snapshot["parameters"]["target_weight"] == weight
    assert snapshot["parameters"]["allocation_rank"] == rank
    assert snapshot["parameters"]["min_strength"] == "95"


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
        trend_review_cn_simulate_acc_id=101,
        trend_review_us_simulate_acc_id=102,
        trend_review_hk_simulate_acc_id=103,
    )


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


@pytest.mark.parametrize(
    ("market", "as_of_date", "rows"),
    [
        (
            "US",
            "2026-07-24",
            [
                {"asset": "美股", "asOfDate": "2026-07-24"},
                {"asset": "美国ETF", "asOfDate": "2026-07-24"},
            ],
        ),
        (
            "HK",
            "2026-07-24",
            [
                {"asset": "港股", "asOfDate": "2026-07-24"},
                {"asset": "香港ETF", "asOfDate": "2026-07-24"},
            ],
        ),
    ],
)
def test_updates_ready_requires_stock_and_etf_dates(
    market: str,
    as_of_date: str,
    rows: list[dict[str, object]],
) -> None:
    assert updates_ready(rows, market=market, as_of_date=as_of_date) is True
    assert updates_ready(rows[:-1], market=market, as_of_date=as_of_date) is False
    rows[-1]["asOfDate"] = "2026-07-23"
    assert updates_ready(rows, market=market, as_of_date=as_of_date) is False


@pytest.mark.parametrize(
    ("market", "rows"),
    [
        (
            "US",
            [
                {"asset": "美股", "asOfDate": "2026-07-24"},
                {"asset": "美国ETF", "asOfDate": "2026-07-24"},
                {"asset": "美国ETF", "asOfDate": "2026-07-23"},
            ],
        ),
        (
            "US",
            [
                {"asset": "美股", "asOfDate": "2026-07-24"},
                {"asset": "美国ETF", "asOfDate": "2026-07-23"},
                {"asset": "美国ETF", "asOfDate": "2026-07-24"},
            ],
        ),
        (
            "HK",
            [
                {"asset": "港股", "asOfDate": "2026-07-24"},
                {"asset": "香港ETF", "asOfDate": "not-a-date"},
            ],
        ),
    ],
)
def test_updates_ready_rejects_duplicate_or_malformed_required_statuses(
    market: str,
    rows: list[dict[str, object]],
) -> None:
    assert updates_ready(rows, market=market, as_of_date="2026-07-24") is False


def test_update_status_gap_reports_stale_assets_for_both_markets() -> None:
    us_stale = [
        {"asset": "美股", "asOfDate": "2026-07-23"},
        {"asset": "美国ETF", "asOfDate": "2026-07-24"},
    ]
    assert update_status_gap(
        us_stale, market="US", as_of_date="2026-07-24"
    ) == "美股 2026-07-23 → 2026-07-24"
    assert updates_ready(us_stale, market="US", as_of_date="2026-07-24") is False

    hk_stale = [
        {"asset": "港股", "asOfDate": "2026-07-23"},
        {"asset": "香港ETF", "asOfDate": "2026-07-22"},
    ]
    assert update_status_gap(
        hk_stale, market="HK", as_of_date="2026-07-24"
    ) == "港股 2026-07-23 → 2026-07-24，香港ETF 2026-07-22 → 2026-07-24"

    ready = [
        {"asset": "港股", "asOfDate": "2026-07-24"},
        {"asset": "香港ETF", "asOfDate": "2026-07-24"},
    ]
    assert update_status_gap(ready, market="HK", as_of_date="2026-07-24") is None
    assert updates_ready(ready, market="HK", as_of_date="2026-07-24") is True


def test_hk_etf_root_missing_warm_to_hot_is_empty() -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            assert (tm_id, expected_date) == (707617, "2026-07-24")
            return [{
                "tmId": 707815,
                "tickerName": "行业趋势龙头(香港ETF)",
                "asOfDate": expected_date,
            }]

    assert _candidate_pool_components(
        Api(),
        market="HK",
        pool_id=707617,
        expected_date="2026-07-24",
    ) == ([], None)


def test_hk_etf_root_with_only_stale_children_is_empty() -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            assert (tm_id, expected_date) == (707617, "2026-07-30")
            raise TrendAnimalsNoCurrentRowsError(
                "getComponentTicker returned no current-date rows"
            )

    assert _candidate_pool_components(
        Api(),
        market="HK",
        pool_id=707617,
        expected_date="2026-07-30",
    ) == ([], None)


@pytest.mark.parametrize(
    "row",
    [
        {"tmId": 707815},
        {"tmId": 0, "tickerName": "行业趋势龙头(香港ETF)"},
    ],
)
def test_hk_etf_root_rejects_malformed_child_rows(
    row: dict[str, object],
) -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return [row]

    with pytest.raises(TrendAnimalsError, match="invalid HK ETF root row"):
        _candidate_pool_components(
            Api(),
            market="HK",
            pool_id=707617,
            expected_date="2026-07-24",
        )


def test_hk_etf_root_loads_unique_warm_to_hot_child() -> None:
    security = {
        "tmId": 708001,
        "tickerSymbol": "2800.HK",
        "asOfDate": "2026-07-24",
    }

    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return {
                707617: [{
                    "tmId": 707900,
                    "tickerName": "温转热(香港ETF)",
                    "asOfDate": expected_date,
                }],
                707900: [security],
            }[tm_id]

    assert _candidate_pool_components(
        Api(),
        market="HK",
        pool_id=707617,
        expected_date="2026-07-24",
    ) == ([security], 707900)


def test_favorite_candidate_ids_only_add_deduplicated_security_rows() -> None:
    favorites = [
        {"tmId": 2, "tickerSymbol": "AAPL.US", "asset": "美股"},
        {"tmId": 2, "tickerSymbol": "AAPL.US", "asset": "美股"},
        {"tmId": 3, "tickerSymbol": "0700.HK", "asset": "港股"},
        {"tmId": 622460, "tickerName": "美股", "asset": "美股"},
    ]

    assert favorite_candidate_ids(favorites, market="US") == {2}


def test_hk_etf_root_keeps_resolved_child_when_current_members_are_empty() -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            if tm_id == 707617:
                return [{
                    "tmId": 707824,
                    "tickerName": "温转热(香港ETF)",
                    "asOfDate": expected_date,
                }]
            assert tm_id == 707824
            raise TrendAnimalsNoCurrentRowsError(
                "getComponentTicker tmId=707824 returned no current-date rows"
            )

    assert _candidate_pool_components(
        Api(),
        market="HK",
        pool_id=707617,
        expected_date="2026-07-30",
    ) == ([], 707824)


def test_hk_etf_root_rejects_duplicate_warm_to_hot_children() -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return [
                {
                    "tmId": child_id,
                    "tickerName": "温转热(香港ETF)",
                    "asOfDate": expected_date,
                }
                for child_id in (707900, 707901)
            ]

    with pytest.raises(
        TrendAnimalsError,
        match="HK ETF warm-to-hot pool is not unique",
    ):
        _candidate_pool_components(
            Api(),
            market="HK",
            pool_id=707617,
            expected_date="2026-07-24",
        )


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


def test_market_report_pins_one_account_snapshot_through_internal_retries(
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

    monkeypatch.setattr(market_trend, "fetch_account_snapshot", fetch, raising=False)

    result = run_market_trend_report(
        config=config(tmp_path),
        market="US",
        run_date="2026-07-15",
        notifier=NullNotifier(),
        attempt_fn=attempt,
        now_fn=lambda: datetime(2026, 7, 15, 9, tzinfo=SHANGHAI),
        sleep_fn=lambda _seconds: None,
    )

    assert result.status == "generated"
    assert fetches == 1
    assert seen == [snapshot, snapshot]
    assert all(item is snapshot for item in seen)


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


def test_market_report_failure_owns_day_at_19_shanghai_deadline(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, 19, 0, tzinfo=SHANGHAI)
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
            "【需处理｜老虎｜美股趋势报告生成失败｜2026-07-15】",
            "发生：趋势报告未生成\n"
            "影响：不能依据旧报告交易\n"
            "现在做：确认 Trend Animals 与老虎账户状态后手动重跑老虎报告\n"
            "原因：趋势数据在截止时间前仍未更新",
        )
    ]
    ledger = cfg.data_dir / "trend_us_tiger/daily_delivery/2026-07-15.json"
    assert __import__("json").loads(ledger.read_text(encoding="utf-8"))["status"] == "sent"


def test_market_report_failure_carries_waiting_gap_at_deadline(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 15, 19, 0, tzinfo=SHANGHAI)
    result = run_market_trend_report(
        config=config(tmp_path),
        market="US",
        run_date="2026-07-15",
        notifier=NullNotifier(),
        attempt_fn=lambda **kwargs: AShareTrendRunResult(
            "waiting", None, None,
            waiting_reason="美股 2026-07-14 → 2026-07-15",
        ),
        now_fn=lambda: now,
        sleep_fn=lambda seconds: None,
    )

    assert result.status == "failed"
    assert result.waiting_reason == "美股 2026-07-14 → 2026-07-15"


def test_hk_report_uses_simulation_holdings_when_actual_statement_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    unlock_live_drawdown(cfg.data_dir, "HK")
    write_protection_state(
        market_paths(cfg.data_dir, cfg.reports_dir, "HK").state,
        {
            "schema_version": 1,
            "positions": {
                "00700": {
                    "initial_line": "9.6",
                    "active_line": "9.6",
                    "atr14": "0.2",
                    "updated_for": "2026-07-14",
                }
            },
        },
    )
    account_snapshot = copy.deepcopy(ACCOUNT_SNAPSHOT)
    account_snapshot["sources"]["account"]["brokers"]["phillips"].update({
        "data_as_of": "2026-06-30T00:00:00+08:00",
        "status": "stale",
        "reason": "statement_stale",
    })
    account_snapshot["positions"] = [{
        "instrument_id": "phillips:HK:00700",
        "broker": "phillips",
        "market": "HK",
        "asset_class": "stock",
        "symbol": "00700",
        "name": "腾讯",
        "currency": "HKD",
        "quantity": "100",
        "cost_price": "400",
        "market_value": "50000",
    }]
    account_snapshot["cash_balances"] = [{
        "broker": "phillips",
        "account_alias": "phillips_main",
        "currency": "HKD",
        "cash_balance": "50000",
        "available_balance": "50000",
    }]
    monkeypatch.setattr(
        market_trend,
        "fetch_account_snapshot",
        lambda: account_snapshot,
    )

    def snapshot(tm_id: int, symbol: str, name: str) -> dict[str, object]:
        return {
            "tmId": tm_id, "tickerName": name, "tickerSymbol": symbol,
            "asset": "港股", "asOfDate": "2026-07-15", "tradableFlag": True,
            "industryName": "科技", "industryTmId": 700001,
            "amount1d": "2", "isTrendRightSide": True,
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
    lot_requests: list[list[str]] = []
    snapshot_calls: list[dict[str, object]] = []

    class Api:
        ignored_stale_components = (
            {"tickerSymbol": "NUVL", "asOfDate": "2026-07-14"},
        )

        def __init__(self, **kwargs: object) -> None:
            nonlocal api_instances
            api_instances += 1

        def get_update_status(self) -> list[dict[str, object]]:
            return [
                {"asset": "港股", "asOfDate": "2026-07-15"},
                {"asset": "香港ETF", "asOfDate": "2026-07-15"},
            ]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def symbol_mapping(self, *_args: object, **_kwargs: object) -> None:
            return None

        def remember_symbol_row(self, **_kwargs: object) -> None:
            pass

        def get_components(self, *, tm_id: int, expected_date: str) -> list[dict[str, object]]:
            if tm_id == 700001:
                return [
                    {"tmId": member_id, "tickerSymbol": f"28{member_id:02d}.HK", "asOfDate": expected_date}
                    for member_id in range(1, 11)
                ]
            assert tm_id == 622494
            return [{"tmId": 1, "tickerSymbol": "2800.HK", "asOfDate": expected_date}]

        def search_exact_symbol(
            self, symbol: str, *, market: str, expected_date: str
        ) -> int:
            assert (symbol, market, expected_date) == (
                "00700",
                "HK",
                "2026-07-15",
            )
            return 2

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            catalog_fields = tuple(
                dict.fromkeys((*UNIFIED_TREND_FIELDS, *INDUSTRY_STATE_FIELDS))
            )
            return [
                {
                    "field": field,
                    "priceCost": (
                        "0.071"
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

        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            snapshot_calls.append(dict(kwargs))
            if kwargs["fields"] == A_SHARE_INDUSTRY_FIELDS:
                assert kwargs["tm_ids"] == [700001]
                return [{
                    "tmId": 700001,
                    "asOfDate": kwargs["expected_date"],
                    "trendTemperatureCurr": "温",
                }]
            if kwargs["fields"] == INDUSTRY_MEMBER_FIELDS:
                return [
                    {
                        "tmId": tm_id,
                        "asOfDate": kwargs["expected_date"],
                        "tradableFlag": True,
                        "isTrendRightSide": True,
                    }
                    for tm_id in kwargs["tm_ids"]
                ]
            if kwargs["fields"] == INDUSTRY_STATE_FIELDS:
                return [{
                    "tmId": 700001,
                    "asOfDate": kwargs["expected_date"],
                    "trendTemperatureCurr": "热",
                    "trendStrengthLocalCurr": "92",
                    "TrendRightSideCountRatio": "0.191",
                    "TrendRightSideMktCapRatio": "0.650",
                }]
            assert kwargs["tm_ids"] == [1, 2]
            assert kwargs["fields"] == UNIFIED_TREND_FIELDS
            return [
                snapshot(1, "2800.HK", "盈富基金"),
                snapshot(2, "0700.HK", "腾讯"),
            ]

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            self.closed = False

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-15", "2026-07-16"]

        def get_daily_kline(self, *args: object, **kwargs: object) -> list[DailyKlineBar]:
            return [
                    DailyKlineBar(
                        date=f"2026-07-{index + 1:02d}", open=10, high=10.1,
                        low=9.9, close=10, volume=100,
                )
                for index in range(15)
            ]

        def get_lot_sizes(self, symbols: list[str]) -> dict[str, int]:
            lot_requests.append(symbols)
            return {symbol: 100 for symbol in symbols}

        def close(self) -> None:
            self.closed = True

    notifier = RecordingFeishu()
    holding_context_snapshots: list[object] = []
    original_collect = market_trend.collect_industry_contexts

    def collect_with_holding_context(**kwargs: object) -> object:
        holding_context_snapshots.extend(kwargs["holding_snapshots"])  # type: ignore[arg-type]
        return original_collect(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        market_trend,
        "collect_industry_contexts",
        collect_with_holding_context,
    )

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
        account_factory=simulation_account_with_positions("HK.00700"),
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
        account_factory=simulation_account_with_positions("HK.00700"),
    )

    assert result.status == recovered.status == revised.status == "generated"
    assert recovered.json_path is not None
    assert recovered.json_path.read_text(encoding="utf-8") == frozen_json
    assert len(notifier.messages) == 1
    assert api_instances == 2  # initial report plus explicit revision; recovery did not refetch
    title, message = notifier.messages[0]
    assert title == "【日报｜辉立｜港股趋势报告｜2026-07-16】"
    assert "账户状态：已更新" in message
    assert "今日动作：卖出 0｜买入 1｜持有 1｜复核 0" in message
    assert "\n买入\n" in message
    assert "02800 盈富基金" in message
    assert "禁止买入" not in message
    assert "http" not in message.lower()
    payload = __import__("json").loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["strategy_snapshot"]["strategy_version"] == "v4"
    assert [item["symbol"] for item in payload["option_attention"]] == [
        "00700",
        "02800",
    ]
    assert payload["option_attention"][0]["name"] == "腾讯"
    assert payload["option_attention"][0]["days"] == 3
    assert payload["signal_snapshots"]["holdings"]["00700"]["name"] == "腾讯"
    assert payload["signal_snapshots"]["holdings"]["00700"]["days"] == 3
    assert "\n期权关注\n" not in message
    assert payload["option_attention"][0]["source_broker"] == "辉立"
    candidate_snapshot = payload["signal_snapshots"]["candidates"][0]
    assert candidate_snapshot["boiling"] is False
    assert candidate_snapshot["champagne"] is False
    assert candidate_snapshot["industry_temperature"] is None
    assert len([
        call
        for call in snapshot_calls
        if call["fields"] == A_SHARE_INDUSTRY_FIELDS
    ]) == 0
    assert "忽略旧成分 1 条：NUVL（2026-07-14）" in payload["api_facts"]
    assert (
        f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows=2 "
        "cache=client-managed"
    ) in payload["api_facts"]
    actions = payload["strategy_judgments"]["formal_actions"]
    assert actions[0]["action"] == "BUY"
    assert actions[0]["symbol"] == "02800"
    assert actions[0]["futu_symbol"] == "HK.02800"
    assert payload["metadata"]["symbol_mapping_schema"] == (
        "open_trader.trend_symbol_mapping.v1"
    )
    assert actions[0]["target_amount"] == "4000.00"
    assert actions[0]["estimated_shares"] == 400
    assert payload["account"]["fresh"] is True
    assert payload["metadata"]["simulate_acc_id"] == 103
    assert payload["metadata"]["position_weight"] == "0.04"
    assert payload["metadata"]["position_weight_source"] == "fallback_4pct"
    assert any(
        getattr(snapshot, "symbol", None) == "00700"
        for snapshot in holding_context_snapshots
    )
    assert payload["strategy_snapshot"]["strategy_version"] == "v4"
    assert payload["risk_summary"]["kelly_phase"] == "unavailable"
    assert payload["risk_summary"]["kelly_eligible_sample_count"] == 0
    assert payload["risk_summary"]["kelly_cap"] is None
    assert payload["metadata"]["trend_statistics"] == {
        "status": "unavailable",
        "artifact_sha256": None,
        "statistics_cutoff_at": None,
        "eligible_sample_count": 0,
        "selected_sample_count": 0,
    }
    assert payload["estimated_api_cost"] == "0.150"
    assert payload["industry_contexts"][0]["aggregate_right_count_ratio"] == "0.191"
    assert payload["industry_contexts"][0]["aggregate_right_market_cap_ratio"] == "0.650"
    assert payload["api_cost"]["label"] == "本报告 API 费用：实扣 0 Trend Animals 余额单位"
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
    evidence_path = cfg.data_dir / payload["replay_evidence"]["path"]
    evidence = __import__("json").loads(evidence_path.read_text(encoding="utf-8"))
    assert "industry_fields" not in evidence["query"]
    assert "industries" not in evidence["responses"]
    assert evidence["market"] == "HK"
    assert evidence["query"]["component_pool_ids"] == [622494]
    assert evidence["rebuild_inputs"]["lot_sizes"] == {"00700": 100, "02800": 100}
    assert lot_requests == [["HK.00700", "HK.02800"], ["HK.00700", "HK.02800"]]


@pytest.mark.parametrize(
    (
        "market",
        "run_date",
        "as_of_date",
        "symbol",
        "wire_symbol",
        "asset",
        "industry_error",
        "expected_reason",
    ),
    [
        (
            "US", "2026-07-30", "2026-07-29", "GRMN", "GRMN.US", "美股",
            False, "industry_temperature_not_hot",
        ),
        (
            "HK", "2026-07-30", "2026-07-30", "00322", "0322.HK", "港股",
            False, "industry_temperature_not_hot",
        ),
        (
            "US", "2026-07-30", "2026-07-29", "GRMN", "GRMN.US", "美股",
            True, "industry_temperature_missing",
        ),
    ],
)
def test_corrupt_statistics_do_not_weaken_current_market_industry_gate(
    tmp_path: Path,
    market: str,
    run_date: str,
    as_of_date: str,
    symbol: str,
    wire_symbol: str,
    asset: str,
    industry_error: bool,
    expected_reason: str,
) -> None:
    cfg = config(tmp_path)
    stats_path = cfg.data_dir / "latest/trend_api_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        '{"schema_version":"broken","rounds":[]}', encoding="utf-8"
    )
    unlock_live_drawdown(cfg.data_dir, market, version="v8")
    industry_calls: list[dict[str, object]] = []
    mapping_calls: list[dict[str, object]] = []

    class Api:
        ignored_stale_components: tuple[object, ...] = ()

        def __init__(self, **kwargs: object) -> None:
            pass

        def get_update_status(self) -> list[dict[str, object]]:
            return [
                {"asset": update_asset, "asOfDate": as_of_date}
                for update_asset in market_trend.MARKET_UPDATE_ASSETS[market]
            ]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            assert expected_date == as_of_date
            return [{
                "tmId": 1,
                "tickerSymbol": wire_symbol,
                "asOfDate": expected_date,
            }]

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            fields = tuple(
                dict.fromkeys(
                    (
                        *UNIFIED_TREND_FIELDS,
                        *A_SHARE_INDUSTRY_FIELDS,
                        *INDUSTRY_MEMBER_FIELDS,
                        *INDUSTRY_STATE_FIELDS,
                    )
                )
            )
            return [
                {
                    "field": field,
                    "priceCost": (
                        "0.061"
                        if field == "tickerName"
                        else "0.01"
                        if field == "trendTemperatureCurr"
                        else "0"
                    ),
                }
                for field in fields
            ]

        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            if kwargs["fields"] == A_SHARE_INDUSTRY_FIELDS:
                industry_calls.append(dict(kwargs))
                if industry_error:
                    raise TrendAnimalsError("industry endpoint unavailable")
                return [{
                    "tmId": 700001,
                    "asOfDate": as_of_date,
                    "trendTemperatureCurr": "凉",
                }]
            assert kwargs["fields"] == UNIFIED_TREND_FIELDS
            return [{
                "tmId": 1,
                "tickerName": symbol,
                "tickerSymbol": wire_symbol,
                "asset": asset,
                "asOfDate": as_of_date,
                "tradableFlag": True,
                "industryTmId": 700001,
                "industryName": "可选消费",
                "priceIndex": "10",
                "marketCap": "200",
                "amount1d": "3",
                "isTrendRightSide": True,
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "热",
                "daysSinceTrendEntry": 3,
                "trendPhaseCurr": "立夏",
                "trendStrengthLocalCurr": "98",
                "stopwinFlagByDangerSignal": False,
                "stopwinFlagByBoilingTemperature": False,
                "stopwinFlagByPopChampagne": False,
            }]

        def remember_symbol_row(self, **kwargs: object) -> None:
            mapping_calls.append(dict(kwargs))

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return sorted({as_of_date, "2026-07-30", "2026-07-31"})

        def get_daily_kline(
            self, *args: object, **kwargs: object
        ) -> list[DailyKlineBar]:
            end = datetime.fromisoformat(as_of_date)
            return [
                DailyKlineBar(
                    date=(end - timedelta(days=14 - index)).date().isoformat(),
                    open=10,
                    high=10.1,
                    low=9.9,
                    close=10,
                    volume=100,
                )
                for index in range(15)
            ]

        def get_lot_sizes(self, symbols: list[str]) -> dict[str, int]:
            return {item: 100 for item in symbols}

        def close(self) -> None:
            pass

    result = run_market_trend_report(
        config=cfg,
        market=market,
        run_date=run_date,
        notifier=NullNotifier(),
        api_factory=Api,
        quote_factory=Quote,
    )

    assert result.status == "generated"
    assert result.json_path is not None
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["account_input"] == {
        "snapshot_generation": "sha256:" + "c" * 64,
        "account_generation": "sha256:" + "d" * 64,
        "status": "healthy",
    }
    judgments = payload["strategy_judgments"]
    assert payload["strategy_snapshot"]["strategy_version"] == "v8"
    assert payload["estimated_api_cost"] == "0.081"
    assert payload["risk_summary"]["kelly_phase"] == "unavailable"
    assert payload["metadata"]["trend_statistics"]["status"] == "unavailable"
    assert (payload["risk_summary"]["status"] == "paused") is industry_error
    evidence_path = cfg.data_dir / payload["replay_evidence"]["path"]
    replayed = trend_review.rebuild_trend_report_from_evidence(
        json.loads(evidence_path.read_text(encoding="utf-8"))
    )
    assert replayed["risk_summary"] == payload["risk_summary"]
    assert replayed["metadata"]["trend_statistics"] == (
        payload["metadata"]["trend_statistics"]
    )
    assert payload["excluded"][symbol] == [expected_reason]
    assert judgments["formal_actions"] == []
    assert judgments["risk_skips"] == []
    assert judgments["top10_candidates"] == []
    assert len(industry_calls) == 1
    assert industry_calls[0]["tm_ids"] == [700001]
    assert bool(payload["metadata"]["industry_data_reason"]) is industry_error
    assert len(mapping_calls) == 1
    assert mapping_calls[0]["market"] == market
    assert mapping_calls[0]["expected_futu_symbol"] == f"{market}.{symbol}"
    assert mapping_calls[0]["row"]["tickerSymbol"] == wire_symbol


@pytest.mark.parametrize(
    ("lookup_fails", "returned_symbol"),
    [
        (True, None),
        (False, "AAPL.US"),
    ],
    ids=("holding_lookup_miss", "cached_holding_symbol_mismatch"),
)
def test_market_report_keeps_futu_holding_price_when_trend_mapping_is_unavailable(
    tmp_path: Path,
    lookup_fails: bool,
    returned_symbol: str | None,
) -> None:
    cfg = config(tmp_path)
    unlock_live_drawdown(cfg.data_dir, "US")
    quote_requests: list[str] = []

    class Api:
        ignored_stale_components: tuple[object, ...] = ()

        def __init__(self, **kwargs: object) -> None:
            pass

        def get_update_status(self) -> list[dict[str, object]]:
            return [
                {"asset": "美股", "asOfDate": "2026-07-14"},
                {"asset": "美国ETF", "asOfDate": "2026-07-14"},
            ]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return []

        def search_exact_symbol(
            self, symbol: str, *, market: str, expected_date: str
        ) -> int:
            assert (symbol, market, expected_date) == (
                "VIXY",
                "US",
                "2026-07-14",
            )
            if lookup_fails:
                raise TrendAnimalsLookupError("missing")
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
            assert kwargs["tm_ids"] == [2]
            return [{
                "tmId": 2,
                "tickerName": "Wrong cached symbol",
                "tickerSymbol": returned_symbol,
                "asset": "美股",
                "asOfDate": "2026-07-14",
                "tradableFlag": True,
                "industryName": "ETF",
                "amount1d": "2",
                "isTrendRightSide": True,
                "daysSinceTrendEntry": 3,
                "trendStrengthLocalCurr": "96",
                "stopwinFlagByDangerSignal": False,
                "stopwinFlagByBoilingTemperature": False,
                "stopwinFlagByPopChampagne": False,
            }]

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_trading_days(self, **kwargs: object) -> list[str]:
            return ["2026-07-14", "2026-07-15"]

        def get_daily_kline(
            self, symbol: str, **kwargs: object
        ) -> list[DailyKlineBar]:
            quote_requests.append(symbol)
            end = datetime(2026, 7, 14)
            return [
                DailyKlineBar(
                    date=(end - timedelta(days=14 - index)).date().isoformat(),
                    open=10,
                    high=10.1,
                    low=9.9,
                    close=10,
                    volume=100,
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
        account_factory=simulation_account_with_positions("US.VIXY"),
    )

    assert result.json_path is not None
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    decision = payload["strategy_judgments"]["holding_decisions"][0]
    assert (decision["action"], decision["reason"]) == (
        "MANUAL_REVIEW",
        "holding_signal_unknown",
    )
    assert decision["close"] == "10"
    assert "价格缺失" not in str(payload["risk_summary"]["pause_reason"])
    assert "US.VIXY" in quote_requests


def test_account_snapshot_does_not_change_us_simulation_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    fills = []
    for index in range(30):
        common = {
            "source": "simulation",
            "source_id": "simulation:futu:102",
            "broker": "futu",
            "account_id": "102",
            "market": "US",
            "symbol": f"ROUND{index:03d}",
            "currency": "USD",
            "quantity": "1",
            "fee": "0",
            "costs_complete": True,
            "strategy_id": "trend_animals_warm_to_hot/US/v4",
            "strategy_version": "v4",
            "normal_cost_rate": "0.001",
            "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
            "report_sha256": "a" * 64,
            "attribution_status": "attributed",
            "exclusion_reason": "",
        }
        fills.extend(
            [
                {
                    **common,
                    "fill_id": f"buy-{index:03d}",
                    "order_id": f"buy-order-{index:03d}",
                    "side": "buy",
                    "price": "100",
                    "filled_at": f"2026-06-{index + 1:02d}T10:00:00+00:00",
                },
                {
                    **common,
                    "fill_id": f"sell-{index:03d}",
                    "order_id": f"sell-order-{index:03d}",
                    "side": "sell",
                    "price": "110.11" if index < 15 else "90.09",
                    "filled_at": f"2026-06-{index + 1:02d}T11:00:00+00:00",
                },
            ]
        )
    stats_payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[
            {
                "market": "US",
                "strategy_id": "trend_animals_warm_to_hot/US/v4",
                "strategy_version": "v4",
            }
        ],
        generated_at="2026-07-15T00:00:00+00:00",
        statistics_cutoff_at="2026-07-14T23:59:59+00:00",
    )
    stats_path = write_trend_api_stats(cfg.data_dir, stats_payload)
    unlock_live_drawdown(cfg.data_dir, "US")
    write_protection_state(
        market_paths(cfg.data_dir, cfg.reports_dir, "US").state,
        {
            "schema_version": 1,
            "positions": {
                "VIXY": {
                    "initial_line": "9.6",
                    "active_line": "9.6",
                    "atr14": "0.2",
                    "updated_for": "2026-07-13",
                }
            },
        },
    )
    account_snapshot = copy.deepcopy(ACCOUNT_SNAPSHOT)
    account_snapshot["positions"] = [{
        "instrument_id": "tiger:US:VIXY",
        "broker": "tiger",
        "market": "US",
        "asset_class": "etf",
        "symbol": "VIXY",
        "name": "VIX Short ETF",
        "currency": "USD",
        "quantity": "10",
        "cost_price": "40",
        "market_value": "200000",
    }]
    account_snapshot["cash_balances"] = [{
        "broker": "tiger",
        "account_alias": "tiger_main",
        "currency": "USD",
        "cash_balance": "20000",
        "available_balance": "20000",
    }]
    monkeypatch.setattr(
        market_trend,
        "fetch_account_snapshot",
        lambda: account_snapshot,
    )

    class Api:
        ignored_stale_components: tuple[object, ...] = ()

        def __init__(self, **kwargs: object) -> None:
            pass

        def get_update_status(self) -> list[dict[str, object]]:
            return [
                {"asset": "美股", "asOfDate": "2026-07-14"},
                {"asset": "美国ETF", "asOfDate": "2026-07-14"},
            ]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            if tm_id == 700001:
                return [
                    {"tmId": member_id, "tickerSymbol": f"QQ{member_id}.US", "asOfDate": expected_date}
                    for member_id in range(1, 11)
                ]
            assert (tm_id, expected_date) == (622460, "2026-07-14")
            return [{"tmId": 1, "tickerSymbol": "QQQ.US", "asOfDate": expected_date}]

        def search_exact_symbol(
            self, symbol: str, *, market: str, expected_date: str
        ) -> int:
            assert (symbol, market, expected_date) == (
                "VIXY",
                "US",
                "2026-07-14",
            )
            return 2

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            catalog_fields = tuple(
                dict.fromkeys((*UNIFIED_TREND_FIELDS, *INDUSTRY_STATE_FIELDS))
            )
            return [
                {
                    "field": field,
                    "priceCost": (
                        "0.071"
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

        def get_snapshots(self, **kwargs: object) -> list[dict[str, object]]:
            if kwargs["fields"] == A_SHARE_INDUSTRY_FIELDS:
                return [{
                    "tmId": 700001,
                    "asOfDate": kwargs["expected_date"],
                    "trendTemperatureCurr": "热",
                }]
            if kwargs["fields"] == INDUSTRY_MEMBER_FIELDS:
                return [
                    {
                        "tmId": tm_id,
                        "asOfDate": kwargs["expected_date"],
                        "tradableFlag": True,
                        "isTrendRightSide": True,
                    }
                    for tm_id in kwargs["tm_ids"]
                ]
            if kwargs["fields"] == INDUSTRY_STATE_FIELDS:
                return [{
                    "tmId": 700001,
                    "asOfDate": kwargs["expected_date"],
                    "trendTemperatureCurr": "热",
                    "trendStrengthLocalCurr": "92",
                    "TrendRightSideCountRatio": "0.191",
                    "TrendRightSideMktCapRatio": "0.650",
                }]
            assert kwargs["fields"] == UNIFIED_TREND_FIELDS
            return [
                {
                    "tmId": tm_id, "tickerName": name, "tickerSymbol": f"{symbol}.US",
                    "asset": "美股", "asOfDate": "2026-07-14", "tradableFlag": True,
                    "industryName": "ETF", "industryTmId": 700001,
                    "amount1d": "2", "isTrendRightSide": True,
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
                    open=10, high=10.1, low=9.9, close=10, volume=100,
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
        account_factory=simulation_account_with_positions("US.VIXY"),
    )

    assert result.json_path is not None
    assert result.report_path is not None
    payload = __import__("json").loads(result.json_path.read_text(encoding="utf-8"))
    assert (
        f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows=2 "
        "cache=client-managed"
    ) in payload["api_facts"]
    assert payload["estimated_api_cost"] == "0.150"
    assert payload["industry_contexts"][0]["aggregate_right_count_ratio"] == "0.191"
    assert payload["industry_contexts"][0]["aggregate_right_market_cap_ratio"] == "0.650"
    assert payload["api_cost"]["label"] == "本报告 API 费用：实扣 0 Trend Animals 余额单位"
    assert payload["account"]["fresh"] is True
    assert payload["metadata"]["simulate_acc_id"] == 102
    assert payload["account"]["source_date"] == "2026-07-14"
    assert payload["strategy_snapshot"]["strategy_version"] == "v4"
    assert payload["strategy_judgments"]["formal_actions"] == []
    assert payload["risk_summary"]["kelly_phase"] == "active_all_samples"
    assert payload["risk_summary"]["kelly_eligible_sample_count"] == 30
    assert payload["risk_summary"]["kelly_cap"] == "0.000000"
    assert payload["risk_summary"]["pause_reason"] == (
        "Kelly 上限为 0，仅暂停未来新开仓"
    )
    assert payload["metadata"]["trend_statistics"] == {
        "status": "available",
        "artifact_sha256": hashlib.sha256(stats_path.read_bytes()).hexdigest(),
        "statistics_cutoff_at": "2026-07-14T23:59:59+00:00",
        "eligible_sample_count": 30,
        "selected_sample_count": 30,
    }
    assert payload["strategy_judgments"]["holding_decisions"][0]["action"] == "HOLD"
    evidence_path = cfg.data_dir / payload["replay_evidence"]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    replayed = trend_review.rebuild_trend_report_from_evidence(evidence)
    for key in (
        "account",
        "strategy_judgments",
        "protection_state",
        "signal_snapshots",
        "strategy_snapshot",
    ):
        assert replayed[key] == payload[key]
    assert replayed["metadata"]["trend_statistics"] == (
        payload["metadata"]["trend_statistics"]
    )
    assert replayed["strategy_judgments"] == payload["strategy_judgments"]
    tampered_evidence = json.loads(json.dumps(evidence))
    tampered_evidence["rebuild_inputs"]["kelly_rounds"][-1][
        "net_return"
    ] = "0.10"
    tampered = trend_review.rebuild_trend_report_from_evidence(tampered_evidence)
    assert tampered["risk_summary"]["kelly_cap"] != "0.000000"
    assert tampered["strategy_judgments"]["formal_actions"] != []
    shortened_evidence = json.loads(json.dumps(evidence))
    shortened_evidence["rebuild_inputs"]["kelly_rounds"].pop()
    shortened = trend_review.rebuild_trend_report_from_evidence(shortened_evidence)
    assert shortened["risk_summary"]["kelly_phase"] == "cold_start"
    assert shortened["risk_summary"]["kelly_eligible_sample_count"] == 29
    assert shortened["risk_summary"]["kelly_cap"] is None
    invalid_evidence = json.loads(json.dumps(evidence))
    invalid_evidence["rebuild_inputs"]["kelly_rounds"][0][
        "costs_complete"
    ] = 1
    with pytest.raises(
        trend_review.TrendReplayIncompleteError,
        match="invalid original input: kelly_rounds",
    ):
        trend_review.rebuild_trend_report_from_evidence(invalid_evidence)
    assert payload["metadata"]["account_currency"] == "USD"
    assert payload["metadata"]["price_fx_to_account_currency"] == "1"
    assert "price_fx_to_hkd" not in payload["metadata"]
    assert evidence["rebuild_inputs"]["price_fx_to_account_currency"] == "1"
    assert len(evidence["rebuild_inputs"]["kelly_rounds"]) == 30
    assert "account_refresh_error" not in payload["metadata"]
    assert "账户状态：已更新" in notifier.messages[0][1]
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
    assert "200000" not in output


def test_market_report_rejects_catalog_cost_drift_before_paid_snapshots(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    snapshot_calls: list[object] = []

    class Api:
        ignored_stale_components: tuple[object, ...] = ()

        def __init__(self, **kwargs: object) -> None:
            pass

        def get_update_status(self) -> list[dict[str, object]]:
            return [
                {"asset": "美股", "asOfDate": "2026-07-14"},
                {"asset": "美国ETF", "asOfDate": "2026-07-14"},
            ]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return [{"tmId": 1, "tickerSymbol": "VIXY.US", "asOfDate": expected_date}]

        def search_exact_symbol(
            self, symbol: str, *, market: str, expected_date: str
        ) -> int:
            assert (symbol, market, expected_date) == (
                "VIXY",
                "US",
                "2026-07-14",
            )
            return 2

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            catalog_fields = tuple(
                dict.fromkeys((*UNIFIED_TREND_FIELDS, *INDUSTRY_STATE_FIELDS))
            )
            return [
                {
                    "field": field,
                    "priceCost": (
                        "0.072"
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
        now_fn=lambda: datetime(2026, 7, 15, 19, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
        api_factory=Api,
        quote_factory=Quote,
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
    assert attention[0]["days"] is None
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


@pytest.mark.parametrize("market", ["HK", "US"])
def test_allocation_market_runner_ledger_excludes_real_only_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, market: str,
) -> None:
    cfg = config(tmp_path)
    allocation = allocation_reference_for_runner()
    allocation_path = cfg.data_dir / "trend_allocation/daily/2026-08-03.json"
    allocation_path.parent.mkdir(parents=True, exist_ok=True)
    allocation_body = (
        json.dumps(
            allocation["snapshot"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    allocation_path.write_bytes(allocation_body)
    allocation["sha256"] = hashlib.sha256(allocation_body).hexdigest()
    # Freeze the signal/data date across both markets.  The US resolver uses
    # the following run date to roll back to the prior trading day (8/7).
    as_of_date = "2026-08-07"
    execution_date = "2026-08-08"
    run_date = "2026-08-07" if market == "HK" else "2026-08-08"
    pool_id = 622494 if market == "HK" else 622460
    complete_snapshot_ids: list[int] = []
    snapshot_request_ledger: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
    api_expected_dates: list[str] = []
    eligible_industry_component_calls: list[int] = []
    industry_member_snapshot_calls: list[list[int]] = []
    industry_state_snapshot_calls: list[list[int]] = []

    def ledger_row(tm_id: int, expected_date: str) -> dict[str, object]:
        if market == "HK":
            codes = {1: "2800", 2: "2801", 3: "2802"}
            assets = "港股"
            ticker_symbol = f"{codes[tm_id]}.HK"
        else:
            codes = {1: "QQQ", 2: "VIXY", 3: "SPY"}
            assets = "美股"
            ticker_symbol = f"{codes[tm_id]}.US"
        return {
            "tmId": tm_id,
            "tickerName": f"标的{codes[tm_id]}",
            "tickerSymbol": ticker_symbol,
            "asset": assets,
            "asOfDate": expected_date,
            "tradableFlag": True,
            "industryTmId": 700001,
            "industryName": "行业ETF",
            "priceIndex": "10",
            "marketCap": "200",
            "amount1d": "3",
            "isTrendRightSide": True,
            "trendTemperatureCurr": "热",
            "trendTemperaturePrev": "温",
            "daysSinceTrendEntry": 3,
            "gainSinceEntry": "0.1",
            "trendPhasePrev": "谷雨",
            "trendPhaseCurr": "立夏",
            "trendStrengthLocalCurr": {1: "98", 2: "94", 3: "97"}[tm_id],
            "trendStrengthLocalChange": "1",
            "trendStrengthGlobalCurr": {1: "98", 2: "94", 3: "97"}[tm_id],
            "trendStrengthLocalPrevWeek": "96",
            "trendStrengthLocalPrevMonth": "95",
            "stopwinFlagByDangerSignal": False,
            "stopwinFlagByBoilingTemperature": False,
            "stopwinFlagByPopChampagne": False,
            "tickerLabels": "成交主力",
        }

    class LedgerApi:
        paid_cache_events: tuple[dict[str, object], ...] = ()

        def get_update_status(self) -> list[dict[str, object]]:
            assets = ("港股", "香港ETF") if market == "HK" else ("美股", "美国ETF")
            return [{"asset": asset, "asOfDate": as_of_date} for asset in assets]

        def get_account_balance(self) -> dict[str, object]:
            return {"balance": "100"}

        def get_components(
            self, *, tm_id: int, expected_date: str,
        ) -> list[dict[str, object]]:
            api_expected_dates.append(expected_date)
            if tm_id != pool_id:
                eligible_industry_component_calls.append(tm_id)
            return [
                {
                    "tmId": candidate_id,
                    "tickerSymbol": (
                        f"{2800 + candidate_id - 1:04d}.HK"
                        if market == "HK"
                        else {1: "QQQ.US", 2: "VIXY.US", 3: "SPY.US"}[candidate_id]
                    ),
                    "asOfDate": expected_date,
                }
                for candidate_id in (1, 2, 3)
            ]

        def get_favorites_tickers(self) -> list[dict[str, object]]:
            return []

        def search_exact_symbol(
            self, symbol: str, *, market: str, expected_date: str,
        ) -> int:
            assert market == self_market
            api_expected_dates.append(expected_date)
            return 3

        def get_snapshot_billing(self) -> list[dict[str, object]]:
            fields = tuple(dict.fromkeys((*UNIFIED_TREND_FIELDS, *INDUSTRY_STATE_FIELDS)))
            return [
                {
                    "field": field,
                    "priceCost": (
                        "0.045"
                        if field == "tickerName"
                        else "0.001"
                        if field in UNIFIED_TREND_FIELDS
                        else "0.002"
                    ),
                }
                for field in fields
            ]

        def get_snapshots(
            self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str,
        ) -> list[dict[str, object]]:
            api_expected_dates.append(expected_date)
            if fields == INDUSTRY_MEMBER_FIELDS:
                industry_member_snapshot_calls.append(list(tm_ids))
            if fields == INDUSTRY_STATE_FIELDS:
                industry_state_snapshot_calls.append(list(tm_ids))
            if fields == UNIFIED_TREND_FIELDS:
                complete_snapshot_ids.extend(tm_ids)
            snapshot_request_ledger.append((tuple(fields), tuple(tm_ids)))
            if fields == A_SHARE_INDUSTRY_FIELDS:
                return [
                    {
                        "tmId": tm_id,
                        "asOfDate": expected_date,
                        "trendTemperatureCurr": "热",
                    }
                    for tm_id in tm_ids
                ]
            return [ledger_row(tm_id, expected_date) for tm_id in tm_ids]

        def remember_symbol_row(self, **kwargs: object) -> None:
            return None

        def symbol_mapping(self, *args: object, **kwargs: object) -> None:
            return None

    self_market = market
    broker = "phillips" if market == "HK" else "tiger"
    currency = "HKD" if market == "HK" else "USD"
    real_symbol = "02802" if market == "HK" else "SPY"
    real_snapshot = copy.deepcopy(ACCOUNT_SNAPSHOT)
    real_snapshot["positions"] = [{
        "instrument_id": f"{broker}:{market}:{real_symbol}",
        "broker": broker,
        "market": market,
        "asset_class": "etf",
        "symbol": real_symbol,
        "name": f"真实{real_symbol}",
        "currency": currency,
        "quantity": "10",
        "cost_price": "10",
        "market_value": "100",
    }]
    real_snapshot["cash_balances"] = [{
        "broker": broker,
        "account_alias": f"{broker}_main",
        "currency": currency,
        "cash_balance": "100",
        "available_balance": "100",
    }]
    monkeypatch.setattr(market_trend, "fetch_account_snapshot", lambda: real_snapshot)

    class Quote:
        def __init__(self, **kwargs: object) -> None:
            return None

        def get_trading_days(self, *, market: str, start: str, end: str) -> list[str]:
            return [as_of_date, execution_date]

        def get_daily_kline(
            self, symbol: str, *, start: str, end: str,
        ) -> list[object]:
            end_day = datetime.fromisoformat(as_of_date)
            return [
                DailyKlineBar(
                    date=(end_day - timedelta(days=14 - index)).date().isoformat(),
                    open=10,
                    high=10.2,
                    low=9.8,
                    close=10,
                    volume=100,
                )
                for index in range(15)
            ]

        def get_lot_sizes(self, symbols: list[str]) -> dict[str, int]:
            return {symbol: 100 for symbol in symbols}

        def close(self) -> None:
            return None

    api = LedgerApi()
    result = run_market_trend_report(
        config=cfg,
        market=market,
        run_date=run_date,
        allocation_reference=allocation,
        notifier=NullNotifier(),
        api_factory=lambda **kwargs: api,
        quote_factory=Quote,
    )
    assert result.json_path is not None
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    evidence_path = cfg.data_dir / payload["replay_evidence"]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    trace = evidence["query"]["staged_snapshot_requests"]

    assert sorted(complete_snapshot_ids) == [3]
    assert len(complete_snapshot_ids) == len(set(complete_snapshot_ids))
    assert api_expected_dates and set(api_expected_dates) == {as_of_date}
    expected_unified_fields = (
        "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
        "tradableFlag", "industryTmId", "industryName", "priceIndex",
        "marketCap", "amount1d", "isTrendRightSide", "trendTemperatureCurr",
        "trendTemperaturePrev", "daysSinceTrendEntry", "gainSinceTrendEntry",
        "trendPhasePrev", "trendPhaseCurr", "trendStrengthLocalCurr",
        "trendStrengthLocalChange", "trendStrengthGlobalCurr",
        "trendStrengthLocalPrevWeek", "trendStrengthLocalPrevMonth",
        "stopwinFlagByDangerSignal", "stopwinFlagByBoilingTemperature",
        "stopwinFlagByPopChampagne", "tickerLabels",
    )
    expected_ledger = (
        (expected_unified_fields, (3,)),
        (("tmId", "tickerName", "tickerSymbol", "asset", "asOfDate"), (1, 2)),
        (("tmId", "asOfDate", "trendStrengthLocalCurr"), (1, 2)),
        (("tmId", "asOfDate", "marketCap"), (1,)),
        (("tmId", "asOfDate", "trendTemperaturePrev", "trendTemperatureCurr"), (1,)),
        (("tmId", "asOfDate", "tradableFlag", "industryTmId", "industryName",
          "amount1d", "isTrendRightSide", "daysSinceTrendEntry", "trendPhaseCurr",
          "stopwinFlagByDangerSignal"), (1,)),
        (("tmId", "asOfDate", "trendTemperatureCurr"), (700001,)),
        (("priceIndex", "gainSinceTrendEntry", "trendPhasePrev",
          "trendStrengthLocalChange", "trendStrengthGlobalCurr",
          "trendStrengthLocalPrevWeek", "trendStrengthLocalPrevMonth",
          "stopwinFlagByBoilingTemperature", "stopwinFlagByPopChampagne",
          "tickerLabels"), (1,)),
    )
    assert tuple(snapshot_request_ledger) == expected_ledger
    assert tuple(
        (tuple(item["fields"]), tuple(item["tm_ids"])) for item in trace
    ) == expected_ledger[1:]
    assert all(item["tm_ids"] == sorted(set(item["tm_ids"])) for item in trace)
    assert all(3 not in ids for _, ids in expected_ledger[1:])
    assert eligible_industry_component_calls == []
    assert industry_member_snapshot_calls == []
    assert industry_state_snapshot_calls == []
    assert payload["industry_context_status"]["ordering_mode"] == "individual_global"
    assert sum(
        "fields=tmId,asOfDate,trendTemperatureCurr" in fact
        for fact in payload["api_facts"]
    ) == 1

    assert all(
        Decimal(str(row["priceCost"])) > 0
        for row in api.get_snapshot_billing()
    )
    expected_cost = Decimal("0.205")
    assert Decimal(payload["estimated_api_cost"]) == expected_cost
    if market == "US":
        assert Decimal(payload["estimated_api_cost"]) <= Decimal("2.852")
