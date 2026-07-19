from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Callable
from zoneinfo import ZoneInfo

from .daily_premarket import (
    DailyPremarketConfig,
    RunLock,
    require_trend_review_config,
    send_notification_with_results,
)
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import to_futu_symbol
from .kelly_order_execution import FutuSimulateOrderExecutionClient
from .kline_technical_facts import DailyKlineBar
from .notifications import Notifier, NullNotifier
from .portfolio_risk import size_entry_by_risk
from .strategy_drawdown import (
    DRAWDOWN_LIMIT,
    observe_strategy_equity,
    valid_drawdown_decision,
)
from .trend_kelly import (
    KELLY_MINIMUM_SAMPLES,
    KELLY_OPTIMIZER,
    KELLY_ROLLING_SAMPLES,
    TrendKellyRound,
    TrendKellyState,
    calculate_trend_kelly,
    load_trend_kelly_rounds,
)
from .trend_animals import (
    TrendAnimalsClient,
    TrendAnimalsError,
    TrendAnimalsLookupError,
)
from .trend_delivery import deliver_daily_trend_text
from .trend_review import freeze_report_evidence


NO_ACTION_TEXT = "现金也是有效仓位，本日无需交易。"
DISCLAIMER_TEXT = (
    "本报告是确定性纪律清单，不是订单或成交事实；所有交易由用户人工确认与执行。"
)
NON_REALTIME_ACCOUNT_WARNING = "账户数据非实时，执行前核对现金与持仓"
STALE_TIGER_ACCOUNT_WARNING = "账户数据非实时，禁止新增买入；持仓需复核"
SHANGHAI = ZoneInfo("Asia/Shanghai")
UNIFIED_TREND_FIELDS = (
    "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
    "tradableFlag", "industryTmId", "industryName", "priceIndex",
    "marketCap", "amount1d", "isTrendRightSide",
    "trendTemperatureCurr", "trendTemperaturePrev",
    "daysSinceTrendEntry", "gainSinceTrendEntry",
    "trendPhasePrev", "trendPhaseCurr", "trendStrengthLocalCurr",
    "trendStrengthLocalChange", "trendStrengthGlobalCurr",
    "trendStrengthLocalPrevWeek", "trendStrengthLocalPrevMonth",
    "stopwinFlagByDangerSignal", "stopwinFlagByBoilingTemperature",
    "stopwinFlagByPopChampagne", "tickerLabels",
)
UNIFIED_TREND_UNIT_COST = Decimal("0.071")
CANDIDATE_FIELDS = UNIFIED_TREND_FIELDS
HOLDING_FIELDS = UNIFIED_TREND_FIELDS
A_SHARE_SNAPSHOT_FIELDS = UNIFIED_TREND_FIELDS
A_SHARE_INDUSTRY_FIELDS = (
    "tmId",
    "asOfDate",
    "trendTemperatureCurr",
)
CN_MAX_FILTER_PRICE = Decimal("200")
CN_MIN_STRENGTH = Decimal("95")
CN_MIN_MARKET_CAP_100M = Decimal("100")
CN_MIN_AMOUNT_100M = Decimal("2")
MARKET_MIN_STRENGTH_EXCLUSIVE = Decimal("90")
MARKET_MAX_RIGHT_SIDE_DAYS_EXCLUSIVE = 10
MARKET_MIN_AMOUNT_100M = Decimal("1")
POSITION_LIMIT = 10
CANDIDATE_LIMIT = 10
CN_TARGET_WEIGHTS = {"热": Decimal("0.04"), "沸": Decimal("0.02")}
DEFAULT_TARGET_WEIGHT = Decimal("0.04")
SINGLE_ENTRY_RISK_LIMIT = Decimal("0.004")
PORTFOLIO_RISK_LIMIT = Decimal("0.04")
ABNORMAL_LOSS_BUFFER = Decimal("0.01")
NORMAL_COST_RATE = Decimal("0.001")
NORMAL_COST_MODEL = "预计完整开平仓正常成本按名义金额计提"
RISK_BUDGET_DISCLAIMER = "5% 是风险预算目标，不是最大损失保证。"
PORTFOLIO_REMAINING_RISK_NOTE = (
    "组合剩余风险供本报告后续新仓共享，不等于单标的仓位上限。"
)
KELLY_STRATEGY_PARAMETERS = {
    "kelly_sample_minimum": KELLY_MINIMUM_SAMPLES,
    "kelly_rolling_window": KELLY_ROLLING_SAMPLES,
    "kelly_fraction": "0.25",
    "kelly_optimizer": KELLY_OPTIMIZER,
    "kelly_sample_scope": "market+strategy_id+opening_strategy_version",
    "kelly_source": "cost_complete_attributed_simulation_closed_rounds",
}
INITIAL_PROTECTION_ATR_MULTIPLE = Decimal("2")
TRAILING_LOW_DAYS = 5
ALLOWED_ENTRY_PHASES = {"谷雨", "立夏", "夏至"}
HOT_TEMPERATURES = {"热", "沸"}
KNOWN_TEMPERATURES = {"凉", "平", "温", "热", "沸"}

V2_RISK_NUMERIC_FIELDS = (
    "existing_planned_risk",
    "new_planned_risk",
    "portfolio_planned_risk",
    "portfolio_planned_risk_pct",
    "portfolio_risk_limit",
    "portfolio_risk_limit_pct",
    "portfolio_remaining_risk",
    "portfolio_remaining_risk_pct",
    "single_entry_risk_limit",
    "single_entry_risk_limit_pct",
    "abnormal_loss_buffer",
    "abnormal_loss_buffer_pct",
    "total_risk_budget_target_pct",
    "normal_cost_rate",
)


def _nonnegative_risk_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def valid_v2_risk_contract(
    parameters: object,
    summary: object,
    *,
    expected_nav: object,
) -> bool:
    if not isinstance(parameters, Mapping) or not isinstance(summary, Mapping):
        return False
    fixed_parameters = {
        "single_entry_risk_limit": SINGLE_ENTRY_RISK_LIMIT,
        "portfolio_risk_limit": PORTFOLIO_RISK_LIMIT,
        "abnormal_loss_buffer": ABNORMAL_LOSS_BUFFER,
        "normal_cost_rate": NORMAL_COST_RATE,
    }
    if any(
        _nonnegative_risk_decimal(parameters.get(key)) != expected
        for key, expected in fixed_parameters.items()
    ) or parameters.get("normal_cost_model") != NORMAL_COST_MODEL:
        return False
    fixed_summary = {
        "single_entry_risk_limit_pct": SINGLE_ENTRY_RISK_LIMIT,
        "portfolio_risk_limit_pct": PORTFOLIO_RISK_LIMIT,
        "abnormal_loss_buffer_pct": ABNORMAL_LOSS_BUFFER,
        "total_risk_budget_target_pct": (
            PORTFOLIO_RISK_LIMIT + ABNORMAL_LOSS_BUFFER
        ),
        "normal_cost_rate": NORMAL_COST_RATE,
    }
    if any(
        _nonnegative_risk_decimal(summary.get(key)) != expected
        for key, expected in fixed_summary.items()
    ) or (
        summary.get("normal_cost_model") != NORMAL_COST_MODEL
        or summary.get("disclaimer") != RISK_BUDGET_DISCLAIMER
        or summary.get("portfolio_remaining_risk_note")
        != PORTFOLIO_REMAINING_RISK_NOTE
    ):
        return False

    values: dict[str, Decimal | None] = {}
    for key in V2_RISK_NUMERIC_FIELDS:
        if key not in summary:
            return False
        raw = summary[key]
        value = _nonnegative_risk_decimal(raw)
        if raw is not None and value is None:
            return False
        values[key] = value

    status = summary.get("status")
    pause_reason = summary.get("pause_reason")
    if status == "active":
        if (
            summary.get("status_label") != "风险预算内"
            or pause_reason != ""
            or any(values[key] is None for key in V2_RISK_NUMERIC_FIELDS)
        ):
            return False
    elif status == "paused":
        if (
            summary.get("status_label") not in {"暂停新开仓", "组合风险已满"}
            or not isinstance(pause_reason, str)
            or not pause_reason
            or values["new_planned_risk"] is None
            or values["new_planned_risk"] != 0
        ):
            return False
    else:
        return False

    existing = values["existing_planned_risk"]
    new = values["new_planned_risk"]
    planned = values["portfolio_planned_risk"]
    planned_pct = values["portfolio_planned_risk_pct"]
    remaining = values["portfolio_remaining_risk"]
    remaining_pct = values["portfolio_remaining_risk_pct"]
    portfolio_limit = values["portfolio_risk_limit"]
    single_limit = values["single_entry_risk_limit"]
    buffer = values["abnormal_loss_buffer"]
    assert new is not None

    if existing is None:
        if any(
            value is not None
            for value in (planned, planned_pct, remaining, remaining_pct)
        ):
            return False
    elif planned is None or planned != existing + new:
        return False

    account_nav = _nonnegative_risk_decimal(expected_nav)
    if account_nav is None or account_nav <= 0:
        return (
            status == "paused"
            and portfolio_limit is None
            and single_limit is None
            and buffer is None
            and planned is None
        )
    if (
        portfolio_limit != account_nav * PORTFOLIO_RISK_LIMIT
        or single_limit is None
        or buffer is None
    ):
        return False
    nav = account_nav
    if (
        single_limit != nav * SINGLE_ENTRY_RISK_LIMIT
        or buffer != nav * ABNORMAL_LOSS_BUFFER
    ):
        return False
    if planned is None:
        return planned_pct is None and remaining is None and remaining_pct is None
    if status == "active" and planned > portfolio_limit:
        return False
    expected_remaining = max(Decimal("0"), portfolio_limit - planned)
    return (
        planned_pct == planned / nav
        and remaining == expected_remaining
        and remaining_pct == expected_remaining / nav
    )


def valid_v3_risk_contract(
    parameters: object,
    summary: object,
    *,
    expected_nav: object,
) -> bool:
    if not valid_v2_risk_contract(
        parameters, summary, expected_nav=expected_nav
    ) or not isinstance(parameters, Mapping) or not isinstance(summary, Mapping):
        return False
    if {
        key: parameters.get(key) for key in KELLY_STRATEGY_PARAMETERS
    } != KELLY_STRATEGY_PARAMETERS:
        return False
    count = summary.get("kelly_eligible_sample_count")
    selected = summary.get("kelly_selected_sample_count")
    phase = summary.get("kelly_phase")
    cap_raw = summary.get("kelly_cap")
    cap = _nonnegative_risk_decimal(cap_raw)
    reason = summary.get("kelly_reason")
    last_closed_at = summary.get("kelly_last_closed_at")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or isinstance(selected, bool)
        or not isinstance(selected, int)
        or not isinstance(reason, str)
        or not isinstance(last_closed_at, str)
        or summary.get("kelly_source")
        != "合格的富途模拟闭环；实盘结果不参与计算"
    ):
        return False
    if count:
        try:
            closed = datetime.fromisoformat(last_closed_at)
        except ValueError:
            return False
        if (
            closed.tzinfo is None
            or closed.utcoffset() is None
            or closed.isoformat() != last_closed_at
        ):
            return False
    elif last_closed_at:
        return False
    if phase == "cold_start":
        return (
            0 <= count < KELLY_MINIMUM_SAMPLES
            and selected == count
            and cap_raw is None
            and reason
            == f"Kelly 冷启动：{count}/{KELLY_MINIMUM_SAMPLES} 个合格模拟闭环；"
            "继续使用固定风险仓位"
        )
    if phase == "unavailable":
        return (
            count == 0
            and selected == 0
            and cap_raw is None
            and bool(reason)
            and summary.get("status") == "paused"
            and not last_closed_at
        )
    if cap is None or cap > Decimal("0.25"):
        return False
    if phase == "active_all_samples":
        phase_valid = (
            KELLY_MINIMUM_SAMPLES <= count < KELLY_ROLLING_SAMPLES
            and selected == count
        )
    elif phase == "active_rolling_200":
        phase_valid = (
            count >= KELLY_ROLLING_SAMPLES
            and selected == KELLY_ROLLING_SAMPLES
        )
    else:
        return False
    if cap == 0:
        return (
            phase_valid
            and reason == "Kelly 上限为 0，仅暂停未来新开仓"
            and summary.get("status") == "paused"
            and summary.get("pause_reason") == reason
        )
    return phase_valid and reason == ""


def valid_v4_risk_contract(
    parameters: object,
    summary: object,
    *,
    expected_nav: object,
) -> bool:
    return (
        valid_v3_risk_contract(parameters, summary, expected_nav=expected_nav)
        and isinstance(parameters, Mapping)
        and parameters.get("drawdown_limit") == str(DRAWDOWN_LIMIT)
        and parameters.get("drawdown_equity_source")
        == "Futu SIMULATE strategy NAV"
        and parameters.get("drawdown_unlock") == "manual_same_version_rebase"
    )


def trend_strategy_snapshot(
    market: str,
    process_version: str,
    candidate_pool_ids: Sequence[int],
    *,
    normal_cost_rate: Decimal = NORMAL_COST_RATE,
) -> dict[str, object]:
    market = market.upper()
    if market not in {"CN", "US", "HK"}:
        raise ValueError(f"unsupported trend review market: {market}")
    if normal_cost_rate != NORMAL_COST_RATE:
        raise ValueError("v2 normal cost rate must remain 0.001")

    common = {
        "candidate_pool_ids": list(candidate_pool_ids),
        "requires_right_side": True,
        "requires_tradable": True,
        "requires_no_danger": True,
        "requires_matching_data_date": True,
        "requires_not_held": True,
        "requires_atr14": True,
        "sort": ["strength_desc", "days_asc", "amount_desc", "symbol_asc"],
        "candidate_limit": CANDIDATE_LIMIT,
        "position_limit": POSITION_LIMIT,
        "single_entry_risk_limit": str(SINGLE_ENTRY_RISK_LIMIT),
        "portfolio_risk_limit": str(PORTFOLIO_RISK_LIMIT),
        "abnormal_loss_buffer": str(ABNORMAL_LOSS_BUFFER),
        "normal_cost_rate": str(normal_cost_rate),
        "normal_cost_model": NORMAL_COST_MODEL,
        **KELLY_STRATEGY_PARAMETERS,
    }
    if market == "CN":
        parameters: dict[str, object] = {
            **common,
            "allowed_exchanges": ["SH", "SZ"],
            "excluded_name_markers": ["ST", "退"],
            "temperature_transition": {"from": ["温"], "to": ["热", "沸"]},
            "max_filter_price": str(CN_MAX_FILTER_PRICE),
            "min_strength": str(CN_MIN_STRENGTH),
            "allowed_industry_temperatures": ["热", "沸"],
            "allowed_phases": ["谷雨", "立夏", "夏至"],
            "min_market_cap_100m": str(CN_MIN_MARKET_CAP_100M),
            "min_amount_100m": str(CN_MIN_AMOUNT_100M),
            "requires_right_side_days": True,
            "target_weight": {key: str(value) for key, value in CN_TARGET_WEIGHTS.items()},
            "lot_size": 100,
            "buy_window": "09:30-10:00",
            "initial_protection_atr_multiple": str(INITIAL_PROTECTION_ATR_MULTIPLE),
            "exit_reasons": [
                "danger",
                "left_right_side",
                "temperature_to_flat",
                "protection",
            ],
            "trailing_low_days": TRAILING_LOW_DAYS,
            "protection_line_non_decreasing": True,
        }
        name = "A 股短线右侧趋势"
        rows = [
            ("候选来源", "趋势动物组合", "温转热（A 股）、温转热（ETF 基金个股）"),
            ("入场过滤", "交易市场", "沪深 A 股；排除北交所、ST、*ST 和退市标记"),
            ("入场过滤", "趋势温度", "前一状态为温；当前状态为热或沸"),
            ("入场过滤", "筛选价格", "不高于 200 元"),
            ("入场过滤", "趋势强度", "不低于 95"),
            ("入场过滤", "行业温度", "热或沸"),
            ("入场过滤", "趋势节气", "谷雨、立夏或夏至"),
            ("入场过滤", "总市值", "不低于 100 亿元"),
            ("入场过滤", "单日成交额", "不低于 2 亿元"),
            ("入场过滤", "趋势右侧", "必须明确为是"),
            ("入场过滤", "可交易状态", "必须明确为可交易"),
            ("入场过滤", "危险信号", "必须明确未触发"),
            ("入场过滤", "其他要求", "数据日期一致、非当前持仓、右侧天数存在、ATR14 可计算"),
            ("候选排序", "排序顺序", "趋势强度降序、右侧天数升序、成交额降序、股票代码升序"),
            ("候选排序", "候选数量", "展示前 10；按剩余持仓席位产生买入动作"),
            ("仓位执行", "持仓上限", "10 笔"),
            ("仓位执行", "单笔计划止损风险上限", "账户净值的 0.40%"),
            ("仓位执行", "组合正常计划风险上限", "账户净值的 4%"),
            ("仓位执行", "异常损失缓冲", "账户净值的 1%，不得用于开仓"),
            ("仓位执行", "热状态仓位", "账户净值的 4%"),
            ("仓位执行", "沸状态仓位", "账户净值的 2%"),
            ("仓位执行", "买入数量", "按 100 股整数倍向下取整"),
            ("仓位执行", "买入窗口", "下一交易日 09:30–10:00"),
            ("退出保护", "初始保护线", "成交均价减 2.0 倍 ATR14"),
            ("退出保护", "退出条件", "危险信号、离开趋势右侧、温度转平或触发保护线时全部卖出"),
            ("退出保护", "过热跟踪", "此前 5 个完整交易日最低价；保护线只升不降"),
        ]
    else:
        parameters = {
            **common,
            "min_strength_exclusive": str(MARKET_MIN_STRENGTH_EXCLUSIVE),
            "max_right_side_days_exclusive": MARKET_MAX_RIGHT_SIDE_DAYS_EXCLUSIVE,
            "min_amount_100m": str(MARKET_MIN_AMOUNT_100M),
            "target_weight": str(DEFAULT_TARGET_WEIGHT),
            "allowed_exchange": market,
            **(
                {"lot_size": 1, "buy_window": "美股常规交易时段"}
                if market == "US"
                else {
                    "lot_size_source": "Futu 每标的整手",
                    "buy_window": "09:30-10:00",
                }
            ),
            "initial_protection_atr_multiple": str(INITIAL_PROTECTION_ATR_MULTIPLE),
            "exit_reasons": ["danger", "left_right_side", "protection"],
            "trailing_low_days": TRAILING_LOW_DAYS,
            "protection_line_non_decreasing": True,
        }
        market_label = "美股" if market == "US" else "港股"
        name = f"{market_label}短线右侧趋势"
        rows = [
            ("候选来源", "趋势动物组合", "、".join(str(item) for item in candidate_pool_ids)),
            ("入场过滤", "交易市场", market_label),
            ("入场过滤", "趋势强度", "高于 90"),
            ("入场过滤", "右侧天数", "少于 10 天"),
            ("入场过滤", "单日成交额", "不低于 1 亿元"),
            ("入场过滤", "其他要求", "趋势右侧、可交易、无危险信号、日期一致、非当前持仓、ATR14 可计算"),
            ("候选排序", "排序顺序", "趋势强度降序、右侧天数升序、成交额降序、股票代码升序"),
            ("候选排序", "候选数量", "展示前 10；按剩余持仓席位产生买入动作"),
            ("仓位执行", "持仓上限", "10 笔"),
            ("仓位执行", "单笔计划止损风险上限", "账户净值的 0.40%"),
            ("仓位执行", "组合正常计划风险上限", "账户净值的 4%"),
            ("仓位执行", "异常损失缓冲", "账户净值的 1%，不得用于开仓"),
            ("仓位执行", "目标仓位", "账户净值的 4%"),
            (
                "仓位执行",
                "买入数量",
                "按 1 股整数倍向下取整" if market == "US" else "按 Futu 返回的每标的整手股数向下取整",
            ),
            (
                "仓位执行",
                "买入窗口",
                "美股常规交易时段" if market == "US" else "下一交易日 09:30–10:00",
            ),
            ("退出保护", "初始保护线", "成交均价减 2.0 倍 ATR14"),
            ("退出保护", "退出条件", "危险信号、离开趋势右侧或触发保护线时全部卖出"),
            ("退出保护", "过热跟踪", "此前 5 个完整交易日最低价；保护线只升不降"),
        ]

    return {
        "strategy_id": f"trend_animals_warm_to_hot/{market}/v3",
        "strategy_name": name,
        "strategy_version": "v3",
        "market": market,
        "effective_from": "2026-07-20",
        "process_version": process_version,
        "parameters": parameters,
        "parameter_rows": [
            {"group": group, "name": label, "value": value}
            for group, label, value in rows
        ],
    }


def live_trend_strategy_snapshot(
    market: str,
    process_version: str,
    candidate_pool_ids: Sequence[int],
    *,
    normal_cost_rate: Decimal = NORMAL_COST_RATE,
) -> dict[str, object]:
    snapshot = trend_strategy_snapshot(
        market,
        process_version,
        candidate_pool_ids,
        normal_cost_rate=normal_cost_rate,
    )
    market = market.upper()
    parameters = dict(snapshot["parameters"])
    parameters.update(
        {
            "drawdown_limit": str(DRAWDOWN_LIMIT),
            "drawdown_equity_source": "Futu SIMULATE strategy NAV",
            "drawdown_unlock": "manual_same_version_rebase",
        }
    )
    return {
        **snapshot,
        "strategy_id": f"trend_animals_warm_to_hot/{market}/v4",
        "strategy_version": "v4",
        "effective_from": "2026-07-20",
        "parameters": parameters,
        "parameter_rows": [
            *snapshot["parameter_rows"],
            {
                "group": "累计回撤",
                "name": "策略累计回撤暂停",
                "value": "纪律模拟策略净值从高点回撤达到 5% 时暂停新开仓，人工解锁后重设基准",
            },
        ],
    }


@dataclass(frozen=True)
class AShareTrendRunResult:
    status: str
    report_path: Path | None
    json_path: Path | None


@dataclass(frozen=True)
class AccountPosition:
    symbol: str
    name: str
    asset_class: str
    quantity: Decimal
    avg_cost_price: Decimal | None
    market_value: Decimal = Decimal("0")


@dataclass(frozen=True)
class AccountSnapshot:
    source_date: str
    fresh: bool
    net_value: Decimal
    available_cash: Decimal
    positions: tuple[AccountPosition, ...]
    exceptions: tuple[str, ...]
    position_count: int | None = None


def _finite_decimal(value: object) -> bool:
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, TypeError, ValueError):
        return False


def _valid_account_source_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    format_ = {7: "%Y-%m", 10: "%Y-%m-%d"}.get(len(value))
    if format_ is None:
        return False
    try:
        return datetime.strptime(value, format_).strftime(format_) == value
    except ValueError:
        return False


def _valid_serialized_position(value: object) -> bool:
    if not isinstance(value, Mapping) or any(
        not isinstance(value.get(field), str) or not value[field].strip()
        for field in ("symbol", "name", "asset_class")
    ):
        return False
    average_cost = value.get("avg_cost_price")
    return (
        _finite_decimal(value.get("quantity"))
        and _finite_decimal(value.get("market_value"))
        and "avg_cost_price" in value
        and (average_cost is None or _finite_decimal(average_cost))
    )


def valid_serialized_account(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    positions = value.get("positions")
    exceptions = value.get("exceptions")
    return (
        _valid_account_source_date(value.get("source_date"))
        and _finite_decimal(value.get("net_value"))
        and _finite_decimal(value.get("available_cash"))
        and isinstance(positions, list)
        and all(_valid_serialized_position(item) for item in positions)
        and isinstance(exceptions, list)
        and all(isinstance(item, str) for item in exceptions)
    )


@dataclass(frozen=True)
class CandidateInput:
    tm_id: int
    symbol: str
    exchange: str
    name: str
    asset: str
    industry: str
    as_of_date: str
    tradable: object
    amount: Decimal | None
    right_side: object
    days: int | None
    strength: Decimal | None
    danger: object
    close: Decimal | None
    atr: Decimal | None
    pools: tuple[str, ...] = ()
    industry_tm_id: int | None = None
    industry_temperature: str | None = None
    filter_price: Decimal | None = None
    market_cap: Decimal | None = None
    temperature_prev: str | None = None
    temperature_curr: str | None = None
    phase: str | None = None
    gain_since_entry: Decimal | None = None
    phase_prev: str | None = None
    phase_curr: str | None = None
    strength_change: str | None = None
    global_strength: Decimal | None = None
    strength_prev_week: Decimal | None = None
    strength_prev_month: Decimal | None = None
    labels: tuple[str, ...] = ()
    kline_supplement: dict[str, bool] | None = None
    boiling: object = None
    champagne: object = None


@dataclass(frozen=True)
class CandidateDecision:
    eligible: tuple[CandidateInput, ...]
    excluded: dict[str, list[str]]


@dataclass(frozen=True)
class BuyAction:
    symbol: str
    name: str
    industry: str
    target_weight: Decimal
    target_amount: Decimal
    estimated_shares: int
    lot_size: int
    filter_price: Decimal | None
    close: Decimal
    market_cap: Decimal | None
    industry_tm_id: int | None
    industry_temperature: str | None
    temperature_prev: str | None
    temperature_curr: str | None
    phase: str | None
    strength: Decimal | None
    amount: Decimal | None
    atr: Decimal
    estimated_initial_line: Decimal
    planned_stop_risk: Decimal
    planned_stop_risk_pct: Decimal
    normal_cost: Decimal
    decisive_constraint: str


@dataclass(frozen=True)
class HoldingSnapshot:
    tm_id: int
    symbol: str
    exchange: str
    name: str | None
    as_of_date: str
    right_side: bool | None
    danger: bool | None
    boiling: bool | None
    champagne: bool | None
    industry: str = ""
    industry_tm_id: int | None = None
    industry_temperature: str | None = None
    filter_price: Decimal | None = None
    market_cap: Decimal | None = None
    strength: Decimal | None = None
    temperature_prev: str | None = None
    temperature_curr: str | None = None
    phase: str | None = None
    days: int | None = None
    gain_since_entry: Decimal | None = None
    phase_prev: str | None = None
    phase_curr: str | None = None
    strength_change: str | None = None
    global_strength: Decimal | None = None
    strength_prev_week: Decimal | None = None
    strength_prev_month: Decimal | None = None
    labels: tuple[str, ...] = ()
    kline_supplement: dict[str, bool] | None = None


@dataclass(frozen=True)
class HoldingDecision:
    symbol: str
    name: str
    industry: str
    action: str
    reason: str
    initial_line: Decimal | None
    active_line: Decimal | None
    atr: Decimal | None
    historical: bool
    close: Decimal | None = None
    temperature_prev: str | None = None
    temperature_curr: str | None = None
    strength: Decimal | None = None
    entry_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrendReport:
    schema_version: int
    generated_at: str
    as_of_date: str
    execution_date: str
    account: AccountSnapshot
    api_facts: tuple[str, ...]
    holdings: tuple[HoldingDecision, ...]
    candidates: tuple[CandidateInput, ...]
    excluded: dict[str, list[str]]
    buy_actions: tuple[BuyAction, ...]
    risk_skips: tuple[dict[str, object], ...]
    risk_summary: dict[str, object]
    industry_concentration: tuple[tuple[str, int, Decimal], ...]
    data_sources: tuple[str, ...]
    estimated_api_cost: Decimal | None
    actual_api_cost: Decimal | None
    protection_state: dict[str, object]
    signal_snapshots: dict[str, object]
    metadata: dict[str, object]
    strategy_snapshot: dict[str, object]
    drawdown_summary: dict[str, object] | None = None
    replay_evidence: dict[str, str] | None = None


def _broker_set(value: str) -> set[str]:
    return {
        part.strip().lower()
        for chunk in value.split(",")
        for part in chunk.split(";")
        if part.strip()
    }


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"invalid decimal value: {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"invalid decimal value: {value!r}")
    return result


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None or str(value).strip() == "" else _decimal(value)


def _optional_int(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid integer value: {value!r}")
    try:
        return int(str(value).strip())
    except ValueError:
        raise ValueError(f"invalid integer value: {value!r}") from None


def _optional_text(value: object) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def _ticker_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in value.split(";") if part.strip())


def _account_exceptions(rows: Sequence[Mapping[str, str]]) -> list[str]:
    exceptions: list[str] = []
    for row in rows:
        market = row.get("market", "").strip().upper()
        asset_class = row.get("asset_class", "").strip().lower()
        currency = row.get("currency", "").strip().upper()
        if market == "CN" and asset_class in {"stock", "etf"}:
            continue
        if market == "CASH" and asset_class == "cash" and currency == "CNY":
            continue
        symbol = row.get("symbol", "").strip() or "<missing-symbol>"
        name = row.get("name", "").strip() or "<missing-name>"
        exceptions.append(
            f"unsupported Eastmoney asset: {symbol} {name} ({market}/{asset_class})"
        )
    return exceptions


def load_eastmoney_account(
    path: Path,
    *,
    expected_date: str,
    timezone: ZoneInfo = ZoneInfo("Asia/Shanghai"),
) -> AccountSnapshot:
    source_date = datetime.fromtimestamp(path.stat().st_mtime, timezone).date().isoformat()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        brokers = _broker_set(row.get("brokers", ""))
        if "eastmoney" in brokers and brokers != {"eastmoney"}:
            raise ValueError(
                f"portfolio row {row.get('symbol', '')} mixes Eastmoney with other brokers"
            )
    eastmoney = [
        row for row in rows if _broker_set(row.get("brokers", "")) == {"eastmoney"}
    ]
    net_value = sum((_decimal(row["market_value"]) for row in eastmoney), Decimal("0"))
    cash = sum(
        (
            _decimal(row["market_value"])
            for row in eastmoney
            if row.get("market", "").strip().upper() == "CASH"
            and row.get("currency", "").strip().upper() == "CNY"
        ),
        Decimal("0"),
    )
    positions = tuple(
        AccountPosition(
            symbol=row["symbol"].strip(),
            name=row["name"].strip(),
            asset_class=row["asset_class"].strip().lower(),
            quantity=_decimal(row["total_quantity"]),
            avg_cost_price=_optional_decimal(row.get("avg_cost_price", "")),
            market_value=_decimal(row["market_value"]),
        )
        for row in eastmoney
        if row.get("market", "").strip().upper() == "CN"
        and row.get("asset_class", "").strip().lower() in {"stock", "etf"}
        and _decimal(row.get("total_quantity", "")) > 0
    )
    return AccountSnapshot(
        source_date=source_date,
        fresh=source_date == expected_date,
        net_value=net_value,
        available_cash=cash,
        positions=positions,
        exceptions=tuple(_account_exceptions(eastmoney)),
        position_count=len(positions),
    )


def load_futu_simulate_trend_account(
    *,
    host: str,
    port: int,
    simulate_acc_id: int,
    market: str,
    expected_date: str,
    account_factory: Callable[..., object] = FutuSimulateOrderExecutionClient,
) -> AccountSnapshot:
    market = market.strip().upper()
    if market not in {"CN", "HK", "US"}:
        raise ValueError(f"unsupported trend review market: {market}")
    client = account_factory(
        host=host,
        port=port,
        simulate_acc_id=simulate_acc_id,
        trd_market=market,
    )
    try:
        snapshot = client.account_snapshot()
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    if not isinstance(snapshot, Mapping):
        raise ValueError("Futu simulation account snapshot must be an object")
    try:
        account_id = int(snapshot.get("acc_id"))
    except (TypeError, ValueError):
        raise ValueError("Futu simulation account snapshot ID is invalid") from None
    if account_id != simulate_acc_id:
        raise ValueError("Futu simulation account snapshot ID does not match config")
    try:
        net_value = _decimal(snapshot.get("net_value"))
    except ValueError:
        raise ValueError("Futu simulation account net value is invalid") from None
    if net_value <= 0:
        raise ValueError("Futu simulation account net value must be positive")
    try:
        cash = _decimal(snapshot.get("cash"))
    except ValueError:
        raise ValueError("Futu simulation account cash is invalid") from None
    if cash < 0:
        raise ValueError("Futu simulation account cash must be nonnegative")
    rows = snapshot.get("positions")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("Futu simulation account positions are invalid")
    prefixes = {"CN": {"SH", "SZ", "BJ"}, "HK": {"HK"}, "US": {"US"}}[market]
    positions: list[AccountPosition] = []
    for row in rows:
        try:
            quantity = _decimal(row.get("qty", row.get("quantity")))
        except ValueError:
            raise ValueError(
                "Futu simulation account position quantity is invalid"
            ) from None
        if quantity < 0:
            raise ValueError(
                "Futu simulation account position quantity must be nonnegative"
            )
        if quantity == 0:
            continue
        code = str(row.get("code") or row.get("futu_code") or "").strip().upper()
        prefix, separator, symbol = code.partition(".")
        if not separator or not prefix or not symbol:
            raise ValueError("Futu simulation account position code is invalid")
        if prefix not in prefixes:
            raise ValueError(
                f"Futu simulation account position market does not match {market}"
            )
        try:
            market_value = _decimal(
                row.get("market_val", row.get("market_value"))
            )
        except ValueError:
            raise ValueError(
                "Futu simulation account position market value is invalid"
            ) from None
        if market_value < 0:
            raise ValueError(
                "Futu simulation account position market value must be nonnegative"
            )
        stock_type = str(row.get("stock_type") or "").strip().upper()
        positions.append(
            AccountPosition(
                symbol=symbol,
                name=str(row.get("stock_name") or row.get("name") or symbol).strip(),
                asset_class="etf" if "ETF" in stock_type else "stock",
                quantity=quantity,
                avg_cost_price=_optional_decimal(
                    row.get("cost_price", row.get("avg_cost_price"))
                ),
                market_value=market_value,
            )
        )
    return AccountSnapshot(
        source_date=expected_date,
        fresh=True,
        net_value=net_value,
        available_cash=cash,
        positions=tuple(sorted(positions, key=lambda item: item.symbol)),
        exceptions=(),
        position_count=len(positions),
    )


def atr14(bars: Sequence[DailyKlineBar]) -> Decimal | None:
    valid = [bar for bar in bars if None not in (bar.high, bar.low)]
    if len(valid) < 15:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(valid[-15:-1], valid[-14:]):
        high = _decimal(current.high)
        low = _decimal(current.low)
        previous_close = _decimal(previous.close)
        ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return sum(ranges, Decimal("0")) / Decimal("14")


def _kline_metrics(
    bars: Sequence[DailyKlineBar],
    *,
    before: str | None = None,
    expected_date: str | None = None,
) -> tuple[Decimal | None, Decimal | None, tuple[Decimal, ...]]:
    if not bars or expected_date is not None and bars[-1].date != expected_date:
        return None, None, ()
    try:
        atr = atr14(bars)
        close = _decimal(bars[-1].close)
        lows = tuple(
            _decimal(bar.low)
            for bar in bars
            if before is not None and bar.date < before and bar.low is not None
        )[-5:]
    except ValueError:
        return None, None, ()
    return atr, close, lows


def _kline_supplement(
    bars: Sequence[DailyKlineBar],
) -> dict[str, bool] | None:
    if len(bars) < 50:
        return None
    try:
        closes = tuple(_decimal(bar.close) for bar in bars[-50:])
        low = _decimal(bars[-1].low)
        prior20_high = max(_decimal(bar.high) for bar in bars[-21:-1])
        current_volume = _decimal(bars[-1].volume)
        prior20_volume = tuple(_decimal(bar.volume) for bar in bars[-21:-1])
    except (TypeError, ValueError):
        return None
    sma20 = sum(closes[-20:], Decimal("0")) / Decimal("20")
    sma50 = sum(closes, Decimal("0")) / Decimal("50")
    close = closes[-1]
    average_volume = sum(prior20_volume, Decimal("0")) / Decimal("20")
    relative_volume = (
        current_volume / average_volume if average_volume > 0 else Decimal("0")
    )
    return {
        "pullback_to_sma20": sma20 > sma50 and low <= sma20 < close,
        "breakout_20d_with_volume": (
            close > prior20_high and relative_volume >= Decimal("1.5")
        ),
        "sma50_breakdown": close < sma50,
    }


def _paid_expansion_fields(
    row: Mapping[str, object], bars: Sequence[DailyKlineBar]
) -> dict[str, object]:
    fields: dict[str, object] = {
        "gain_since_entry": _optional_decimal(row.get("gainSinceTrendEntry")),
        "phase_prev": _optional_text(row.get("trendPhasePrev")),
        "phase_curr": _optional_text(row.get("trendPhaseCurr")),
        "strength_change": _optional_text(row.get("trendStrengthLocalChange")),
        "global_strength": _optional_decimal(row.get("trendStrengthGlobalCurr")),
        "strength_prev_week": _optional_decimal(
            row.get("trendStrengthLocalPrevWeek")
        ),
        "strength_prev_month": _optional_decimal(
            row.get("trendStrengthLocalPrevMonth")
        ),
        "labels": _ticker_labels(row.get("tickerLabels")),
    }
    paid_expansion_incomplete = any(
        fields[name] is None
        for name in (
            "gain_since_entry",
            "phase_prev",
            "phase_curr",
            "strength_change",
            "global_strength",
            "strength_prev_week",
            "strength_prev_month",
        )
    ) or not fields["labels"]
    fields["kline_supplement"] = (
        _kline_supplement(bars) if paid_expansion_incomplete else None
    )
    return fields


def _symbol_parts(value: object, *, market: str = "CN") -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("tickerSymbol must be a string")
    normalized_market = market.strip().upper()
    if normalized_market in {"US", "HK"}:
        text = value.strip().upper()
        suffix = f".{normalized_market}"
        raw_symbol = text[: -len(suffix)] if text.endswith(suffix) else text
        futu_symbol = to_futu_symbol(normalized_market, raw_symbol)
        exchange, symbol = futu_symbol.split(".", 1)
        return symbol, exchange
    parts = value.strip().upper().rsplit(".", 1)
    if len(parts) == 1:
        exchange, symbol = to_futu_symbol("CN", parts[0]).split(".", 1)
        return symbol, exchange
    if len(parts) != 2 or len(parts[0]) != 6 or not parts[0].isdigit() or not parts[1]:
        raise ValueError(f"invalid tickerSymbol: {value!r}")
    return parts[0], parts[1]


def evaluate_candidate(
    row: Mapping[str, object],
    bars: Sequence[DailyKlineBar] | None,
    *,
    pools: Sequence[str] = (),
    market: str = "CN",
    industry_temperature: str | None = None,
) -> CandidateInput:
    symbol, exchange = _symbol_parts(row.get("tickerSymbol"), market=market)
    daily_bars = tuple(bars or ())
    as_of_date = str(row.get("asOfDate") or "").strip()
    atr, close, _ = _kline_metrics(
        daily_bars, expected_date=as_of_date or None
    )
    tm_id = row.get("tmId")
    if isinstance(tm_id, bool) or not isinstance(tm_id, int):
        raise ValueError("tmId must be an integer")
    paid_expansion = _paid_expansion_fields(row, daily_bars)
    return CandidateInput(
        tm_id=tm_id,
        symbol=symbol,
        exchange=exchange,
        name=str(row.get("tickerName") or "").strip(),
        asset=str(row.get("asset") or "").strip(),
        industry=str(row.get("industryName") or "").strip(),
        as_of_date=str(row.get("asOfDate") or "").strip(),
        tradable=row.get("tradableFlag"),
        amount=_optional_decimal(row.get("amount1d")),
        right_side=row.get("isTrendRightSide"),
        days=_optional_int(row.get("daysSinceTrendEntry")),
        strength=_optional_decimal(row.get("trendStrengthLocalCurr")),
        danger=row.get("stopwinFlagByDangerSignal"),
        close=close,
        atr=atr,
        pools=tuple(sorted(set(pools))),
        industry_tm_id=_optional_int(row.get("industryTmId")),
        industry_temperature=industry_temperature,
        filter_price=_optional_decimal(row.get("priceIndex")),
        market_cap=_optional_decimal(row.get("marketCap")),
        temperature_prev=(
            str(row["trendTemperaturePrev"])
            if row.get("trendTemperaturePrev") in KNOWN_TEMPERATURES
            else None
        ),
        temperature_curr=(
            str(row["trendTemperatureCurr"])
            if row.get("trendTemperatureCurr") in KNOWN_TEMPERATURES
            else None
        ),
        phase=_optional_text(row.get("trendPhaseCurr")),
        boiling=(
            row.get("stopwinFlagByBoilingTemperature")
            if isinstance(row.get("stopwinFlagByBoilingTemperature"), bool)
            else None
        ),
        champagne=(
            row.get("stopwinFlagByPopChampagne")
            if isinstance(row.get("stopwinFlagByPopChampagne"), bool)
            else None
        ),
        **paid_expansion,
    )


def _excluded_name(name: str) -> bool:
    normalized = name.strip().upper()
    return "ST" in normalized or "退" in name


def _candidate_reasons(
    item: CandidateInput,
    held_symbols: set[str],
    expected_date: str | None = None,
    *,
    market: str = "CN",
) -> list[str]:
    reasons: list[str] = []
    if market == "CN":
        if item.asset != "A股":
            reasons.append("a_share_only")
        if item.temperature_prev is None or item.temperature_curr is None:
            reasons.append("temperature_missing")
        elif item.temperature_prev != "温" or item.temperature_curr not in HOT_TEMPERATURES:
            reasons.append("temperature_transition_not_entry")
        if item.filter_price is None:
            reasons.append("filter_price_missing")
        elif item.filter_price > CN_MAX_FILTER_PRICE:
            reasons.append("filter_price_above_200")
        if item.strength is None:
            reasons.append("strength_missing")
        elif item.strength < CN_MIN_STRENGTH:
            reasons.append("strength_below_95")
        if item.industry_tm_id is None:
            reasons.append("industry_id_missing")
        if item.industry_temperature is None:
            reasons.append("industry_temperature_missing")
        elif item.industry_temperature not in HOT_TEMPERATURES:
            reasons.append("industry_temperature_not_hot")
        if item.phase is None:
            reasons.append("phase_missing")
        elif item.phase not in ALLOWED_ENTRY_PHASES:
            reasons.append("phase_after_summer_solstice")
        if item.market_cap is None:
            reasons.append("market_cap_missing")
        elif item.market_cap < CN_MIN_MARKET_CAP_100M:
            reasons.append("market_cap_below_100")
        if item.amount is None:
            reasons.append("amount_missing")
        elif item.amount < CN_MIN_AMOUNT_100M:
            reasons.append("amount_below_2")
        if item.days is None:
            reasons.append("right_side_days_missing")
    else:
        if item.strength is None or item.strength <= MARKET_MIN_STRENGTH_EXCLUSIVE:
            reasons.append("strength_not_above_90")
        if item.days is None or item.days >= MARKET_MAX_RIGHT_SIDE_DAYS_EXCLUSIVE:
            reasons.append("right_side_days_not_below_10")
        if item.amount is None or item.amount < MARKET_MIN_AMOUNT_100M:
            reasons.append("amount_below_1")
    if item.right_side is not True:
        reasons.append("right_side_not_true")
    if item.tradable is not True:
        reasons.append("not_tradable")
    if item.danger is not False:
        reasons.append("danger_signal" if item.danger else "danger_unknown")
    if not item.name:
        reasons.append("name_missing")
    if not item.asset:
        reasons.append("asset_missing")
    if item.symbol in held_symbols:
        reasons.append("already_held")
    if market == "CN" and (item.exchange == "BJ" or _excluded_name(item.name)):
        reasons.append("excluded_security")
    elif item.exchange not in ({"SH", "SZ"} if market == "CN" else {market}):
        reasons.append("unsupported_exchange")
    if item.atr is None:
        reasons.append("atr_unavailable")
    if expected_date is not None and item.as_of_date != expected_date:
        reasons.append("data_date_mismatch")
    return reasons


def _candidate_sort_key(item: CandidateInput) -> tuple[Decimal, int, Decimal, str]:
    return (
        -item.strength,  # type: ignore[operator]
        item.days,  # type: ignore[return-value]
        -item.amount,  # type: ignore[operator]
        item.symbol,
    )


def build_candidate_list(
    rows: Sequence[CandidateInput],
    *,
    held_symbols: set[str],
    expected_date: str | None = None,
    market: str = "CN",
) -> CandidateDecision:
    eligible: list[CandidateInput] = []
    excluded: dict[str, list[str]] = {}
    grouped: dict[str, list[CandidateInput]] = defaultdict(list)
    for item in rows:
        grouped[item.symbol].append(item)
    for symbol in sorted(grouped):
        items = grouped[symbol]
        reasons = list(
            dict.fromkeys(
                reason
                for item in items
                for reason in _candidate_reasons(
                    item, held_symbols, expected_date, market=market
                )
            )
        )
        if reasons:
            excluded[symbol] = reasons
        else:
            eligible.append(min(items, key=_candidate_sort_key))
    eligible.sort(key=_candidate_sort_key)
    return CandidateDecision(tuple(eligible), excluded)


def estimate_buy_actions(
    *,
    ranked: Sequence[CandidateInput],
    net_value: Decimal,
    available_cash: Decimal,
    current_position_count: int,
    position_weight: Decimal,
    market: str = "CN",
    lot_sizes: Mapping[str, int] | None = None,
    price_fx_to_account_currency: Decimal = Decimal("1"),
    portfolio_planned_risk: Decimal = Decimal("0"),
    normal_cost_rate: Decimal = NORMAL_COST_RATE,
) -> list[BuyAction]:
    actions, _, _ = _plan_buy_actions(
        ranked=ranked,
        net_value=net_value,
        available_cash=available_cash,
        current_position_count=current_position_count,
        position_weight=position_weight,
        market=market,
        lot_sizes=lot_sizes,
        price_fx_to_account_currency=price_fx_to_account_currency,
        portfolio_planned_risk=portfolio_planned_risk,
        normal_cost_rate=normal_cost_rate,
    )
    return actions


def _estimate_buy_actions_v1(
    *,
    ranked: Sequence[CandidateInput],
    net_value: Decimal,
    available_cash: Decimal,
    current_position_count: int,
    position_weight: Decimal,
    market: str,
    lot_sizes: Mapping[str, int] | None,
    price_fx_to_account_currency: Decimal,
) -> list[BuyAction]:
    """Preserve the frozen v1 nominal/cash/slot sizing for evidence replay."""
    slots = max(0, POSITION_LIMIT - current_position_count)
    if slots == 0:
        return []
    remaining_cash = available_cash
    actions: list[BuyAction] = []
    for item in ranked:
        if slots == 0 or remaining_cash <= 0:
            break
        if item.close is None or item.close <= 0 or item.atr is None:
            continue
        weight = (
            CN_TARGET_WEIGHTS.get(item.temperature_curr)
            if market == "CN"
            else position_weight
        )
        if weight is None:
            continue
        target = (net_value * weight).quantize(Decimal("0.01"))
        amount = min(target, remaining_cash)
        lot_size = (
            100
            if market == "CN"
            else (lot_sizes or {}).get(item.symbol, 0)
            if market == "HK"
            else 1
        )
        share_price = item.close * price_fx_to_account_currency
        shares = int(amount / share_price / lot_size) * lot_size if lot_size > 0 else 0
        if shares <= 0:
            continue
        actions.append(
            BuyAction(
                symbol=item.symbol,
                name=item.name,
                industry=item.industry,
                target_weight=weight,
                target_amount=amount,
                estimated_shares=shares,
                lot_size=lot_size,
                filter_price=item.filter_price,
                close=item.close,
                market_cap=item.market_cap,
                industry_tm_id=item.industry_tm_id,
                industry_temperature=item.industry_temperature,
                temperature_prev=item.temperature_prev,
                temperature_curr=item.temperature_curr,
                phase=item.phase,
                strength=item.strength,
                amount=item.amount,
                atr=item.atr,
                estimated_initial_line=(
                    item.close - INITIAL_PROTECTION_ATR_MULTIPLE * item.atr
                ),
                planned_stop_risk=Decimal("0"),
                planned_stop_risk_pct=Decimal("0"),
                normal_cost=Decimal("0"),
                decisive_constraint="",
            )
        )
        remaining_cash -= amount
        slots -= 1
    return actions


def _risk_skip(
    item: CandidateInput,
    *,
    weight: Decimal | None,
    target_amount: Decimal | None,
    reason: str,
    decisive_constraint: str,
) -> dict[str, object]:
    return {
        **asdict(item),
        "target_weight": weight,
        "target_amount": target_amount,
        "estimated_shares": 0,
        "reason": reason,
        "decisive_constraint": decisive_constraint,
    }


def _risk_summary(
    *,
    net_value: Decimal,
    existing_planned_risk: Decimal | None,
    new_planned_risk: Decimal,
    normal_cost_rate: Decimal,
    pause_reason: str = "",
    kelly_state: TrendKellyState | None = None,
) -> dict[str, object]:
    valid_nav = net_value.is_finite() and net_value > 0
    portfolio_limit = net_value * PORTFOLIO_RISK_LIMIT if valid_nav else None
    planned_risk = (
        existing_planned_risk + new_planned_risk
        if existing_planned_risk is not None
        else None
    )
    remaining_risk = (
        max(Decimal("0"), portfolio_limit - planned_risk)
        if portfolio_limit is not None and planned_risk is not None
        else None
    )
    summary = {
        "status": "paused" if pause_reason else "active",
        "status_label": (
            "组合风险已满"
            if pause_reason == "组合正常计划风险已达到净值 4%"
            else "暂停新开仓"
            if pause_reason
            else "风险预算内"
        ),
        "pause_reason": pause_reason,
        "existing_planned_risk": existing_planned_risk,
        "new_planned_risk": new_planned_risk,
        "portfolio_planned_risk": planned_risk,
        "portfolio_planned_risk_pct": (
            planned_risk / net_value
            if valid_nav and planned_risk is not None
            else None
        ),
        "portfolio_risk_limit": portfolio_limit,
        "portfolio_risk_limit_pct": PORTFOLIO_RISK_LIMIT,
        "portfolio_remaining_risk": remaining_risk,
        "portfolio_remaining_risk_pct": (
            remaining_risk / net_value
            if valid_nav and remaining_risk is not None
            else None
        ),
        "single_entry_risk_limit": (
            net_value * SINGLE_ENTRY_RISK_LIMIT if valid_nav else None
        ),
        "single_entry_risk_limit_pct": SINGLE_ENTRY_RISK_LIMIT,
        "abnormal_loss_buffer": (
            net_value * ABNORMAL_LOSS_BUFFER if valid_nav else None
        ),
        "abnormal_loss_buffer_pct": ABNORMAL_LOSS_BUFFER,
        "total_risk_budget_target_pct": PORTFOLIO_RISK_LIMIT + ABNORMAL_LOSS_BUFFER,
        "normal_cost_rate": normal_cost_rate,
        "normal_cost_model": NORMAL_COST_MODEL,
        "disclaimer": RISK_BUDGET_DISCLAIMER,
        "portfolio_remaining_risk_note": PORTFOLIO_REMAINING_RISK_NOTE,
    }
    if kelly_state is not None:
        reason = (
            kelly_state.reason
            if kelly_state.phase == "unavailable"
            else (
                f"Kelly 冷启动：{kelly_state.eligible_sample_count}/"
                f"{KELLY_MINIMUM_SAMPLES} 个合格模拟闭环；"
                "继续使用固定风险仓位"
            )
            if not kelly_state.enabled
            else "Kelly 上限为 0，仅暂停未来新开仓"
            if kelly_state.quarter_kelly_cap == 0
            else ""
        )
        summary.update(
            {
                "kelly_phase": kelly_state.phase,
                "kelly_eligible_sample_count": kelly_state.eligible_sample_count,
                "kelly_selected_sample_count": kelly_state.selected_sample_count,
                "kelly_cap": kelly_state.quarter_kelly_cap,
                "kelly_reason": reason,
                "kelly_last_closed_at": kelly_state.last_closed_at,
                "kelly_source": "合格的富途模拟闭环；实盘结果不参与计算",
            }
        )
    return summary


def _plan_buy_actions(
    *,
    ranked: Sequence[CandidateInput],
    net_value: Decimal,
    available_cash: Decimal,
    current_position_count: int,
    position_weight: Decimal,
    market: str,
    lot_sizes: Mapping[str, int] | None,
    price_fx_to_account_currency: Decimal,
    portfolio_planned_risk: Decimal | None,
    normal_cost_rate: Decimal,
    critical_data_reason: str = "",
    kelly_state: TrendKellyState | None = None,
) -> tuple[list[BuyAction], list[dict[str, object]], dict[str, object]]:
    def entry_weight(item: CandidateInput) -> Decimal | None:
        nominal = (
            CN_TARGET_WEIGHTS.get(item.temperature_curr)
            if market == "CN"
            else position_weight
        )
        if (
            nominal is not None
            and kelly_state is not None
            and kelly_state.enabled
            and kelly_state.quarter_kelly_cap is not None
        ):
            return min(nominal, kelly_state.quarter_kelly_cap)
        return nominal

    if not critical_data_reason:
        if not net_value.is_finite() or net_value <= 0:
            critical_data_reason = "模拟盘净值缺失，暂停新开仓"
        elif not available_cash.is_finite() or available_cash < 0:
            critical_data_reason = "模拟盘现金缺失，暂停新开仓"
        elif (
            not price_fx_to_account_currency.is_finite()
            or price_fx_to_account_currency <= 0
        ):
            critical_data_reason = "汇率缺失，暂停新开仓"
        elif portfolio_planned_risk is None:
            critical_data_reason = "模拟持仓风险事实缺失，暂停新开仓"
        elif any(
            item.close is None
            or not item.close.is_finite()
            or item.close <= 0
            or item.atr is None
            or not item.atr.is_finite()
            or item.atr <= 0
            for item in ranked
        ):
            critical_data_reason = "候选价格或活动保护线缺失，暂停新开仓"

    if critical_data_reason:
        skips = [
            _risk_skip(
                item,
                weight=entry_weight(item),
                target_amount=None,
                reason=critical_data_reason,
                decisive_constraint="关键风险数据",
            )
            for item in ranked
        ]
        return [], skips, _risk_summary(
            net_value=net_value,
            existing_planned_risk=portfolio_planned_risk,
            new_planned_risk=Decimal("0"),
            normal_cost_rate=normal_cost_rate,
            pause_reason=critical_data_reason,
            kelly_state=kelly_state,
        )

    assert portfolio_planned_risk is not None
    if (
        kelly_state is not None
        and kelly_state.enabled
        and kelly_state.quarter_kelly_cap == 0
    ):
        pause_reason = "Kelly 上限为 0，仅暂停未来新开仓"
        skips = [
            _risk_skip(
                item,
                weight=Decimal("0"),
                target_amount=Decimal("0"),
                reason=pause_reason,
                decisive_constraint="Kelly 上限",
            )
            for item in ranked
        ]
        return [], skips, _risk_summary(
            net_value=net_value,
            existing_planned_risk=portfolio_planned_risk,
            new_planned_risk=Decimal("0"),
            normal_cost_rate=normal_cost_rate,
            pause_reason=pause_reason,
            kelly_state=kelly_state,
        )
    if portfolio_planned_risk >= net_value * PORTFOLIO_RISK_LIMIT:
        pause_reason = "组合正常计划风险已达到净值 4%"
        skips = [
            _risk_skip(
                item,
                weight=entry_weight(item),
                target_amount=None,
                reason=pause_reason,
                decisive_constraint="组合剩余风险",
            )
            for item in ranked
        ]
        return [], skips, _risk_summary(
            net_value=net_value,
            existing_planned_risk=portfolio_planned_risk,
            new_planned_risk=Decimal("0"),
            normal_cost_rate=normal_cost_rate,
            pause_reason=pause_reason,
            kelly_state=kelly_state,
        )
    remaining_cash = available_cash
    remaining_risk = max(
        Decimal("0"),
        net_value * PORTFOLIO_RISK_LIMIT - portfolio_planned_risk,
    )
    single_entry_limit = net_value * SINGLE_ENTRY_RISK_LIMIT
    slots = max(0, POSITION_LIMIT - current_position_count)
    actions: list[BuyAction] = []
    skips: list[dict[str, object]] = []
    for item in ranked:
        nominal_weight = (
            CN_TARGET_WEIGHTS.get(item.temperature_curr)
            if market == "CN"
            else position_weight
        )
        weight = entry_weight(item)
        if weight is None:
            continue
        target_amount = min(net_value * weight, remaining_cash).quantize(
            Decimal("0.01")
        )
        if slots == 0:
            skips.append(
                _risk_skip(
                    item,
                    weight=weight,
                    target_amount=target_amount,
                    reason="10 个持仓席位已满",
                    decisive_constraint="持仓席位",
                )
            )
            continue
        lot_size = (
            100
            if market == "CN"
            else (lot_sizes or {}).get(item.symbol, 0)
            if market == "HK"
            else 1
        )
        if lot_size <= 0:
            skips.append(
                _risk_skip(
                    item,
                    weight=weight,
                    target_amount=target_amount,
                    reason="缺少实际每手股数",
                    decisive_constraint="交易单位",
                )
            )
            continue
        assert item.close is not None and item.atr is not None
        protection_line = item.close - INITIAL_PROTECTION_ATR_MULTIPLE * item.atr
        sized = size_entry_by_risk(
            entry_price=item.close,
            protection_line=protection_line,
            fx_to_account_currency=price_fx_to_account_currency,
            portfolio_nav=net_value,
            nominal_weight_limit=weight,
            single_entry_risk_limit=single_entry_limit,
            portfolio_remaining_risk=remaining_risk,
            available_cash=remaining_cash,
            lot_size=Decimal(lot_size),
            normal_cost_rate=normal_cost_rate,
        )
        if sized.final_quantity <= 0:
            skips.append(
                _risk_skip(
                    item,
                    weight=weight,
                    target_amount=target_amount,
                    reason=sized.reason,
                    decisive_constraint=sized.decisive_constraint,
                )
            )
            continue
        quantity = int(sized.final_quantity)
        actions.append(
            BuyAction(
                symbol=item.symbol,
                name=item.name,
                industry=item.industry,
                target_weight=weight,
                target_amount=target_amount,
                estimated_shares=quantity,
                lot_size=lot_size,
                filter_price=item.filter_price,
                close=item.close,
                market_cap=item.market_cap,
                industry_tm_id=item.industry_tm_id,
                industry_temperature=item.industry_temperature,
                temperature_prev=item.temperature_prev,
                temperature_curr=item.temperature_curr,
                phase=item.phase,
                strength=item.strength,
                amount=item.amount,
                atr=item.atr,
                estimated_initial_line=protection_line,
                planned_stop_risk=sized.planned_stop_risk,
                planned_stop_risk_pct=sized.planned_stop_risk / net_value,
                normal_cost=sized.normal_cost,
                decisive_constraint=(
                    "Kelly 上限"
                    if nominal_weight is not None
                    and weight < nominal_weight
                    and sized.decisive_constraint == "名义仓位上限"
                    else sized.decisive_constraint
                ),
            )
        )
        remaining_cash -= sized.cash_required
        remaining_risk -= sized.planned_stop_risk
        slots -= 1

    new_planned_risk = sum(
        (item.planned_stop_risk for item in actions), Decimal("0")
    )
    return actions, skips, _risk_summary(
        net_value=net_value,
        existing_planned_risk=portfolio_planned_risk,
        new_planned_risk=new_planned_risk,
        normal_cost_rate=normal_cost_rate,
        kelly_state=kelly_state,
    )


def update_protection_line(
    *,
    old_line: Decimal,
    boiling: bool,
    champagne: bool,
    prior_five_lows: Sequence[Decimal],
) -> Decimal:
    if not (boiling or champagne) or len(prior_five_lows) != TRAILING_LOW_DAYS:
        return old_line
    return max(old_line, min(prior_five_lows))


def _state_positions(prior_state: Mapping[str, object] | None) -> Mapping[str, object]:
    if not prior_state:
        return {}
    positions = prior_state.get("positions", {})
    return positions if isinstance(positions, Mapping) else {}


def _state_decimal(state: Mapping[str, object], key: str) -> Decimal | None:
    return _optional_decimal(state.get(key))


def _holding_action(
    *,
    symbol: str,
    snapshot: HoldingSnapshot | None,
    triggered: set[str],
    market: str = "CN",
) -> tuple[str, str]:
    if symbol in triggered:
        return "SELL_ALL", "protection_line_already_triggered"
    if snapshot is not None and snapshot.danger is True:
        return "SELL_ALL", "danger_signal"
    if snapshot is not None and snapshot.right_side is False:
        return "SELL_ALL", "left_trend_right_side"
    if (
        market == "CN"
        and snapshot is not None
        and snapshot.temperature_prev in {"温", "热", "沸"}
        and snapshot.temperature_curr == "平"
    ):
        return "SELL_ALL", "temperature_changed_to_flat"
    if snapshot is None or any(
        signal is None
        for signal in (
            snapshot.right_side,
            snapshot.danger,
            snapshot.boiling,
            snapshot.champagne,
        )
    ) or (
        market == "CN"
        and (
            snapshot.temperature_prev not in KNOWN_TEMPERATURES
            or snapshot.temperature_curr not in KNOWN_TEMPERATURES
        )
    ):
        return "MANUAL_REVIEW", "holding_signal_unknown"
    return "HOLD", "trend_intact"


def _holding_entry_hints(snapshot: HoldingSnapshot | None) -> tuple[str, ...]:
    if snapshot is None:
        return ("数据不可用",)
    hints: list[str] = []
    if snapshot.filter_price is None:
        hints.append("筛选价数据不可用")
    elif snapshot.filter_price > 200:
        hints.append(f"筛选价 {snapshot.filter_price}，高于入场上限 200")
    if snapshot.strength is None:
        hints.append("强度数据不可用")
    elif snapshot.strength < 95:
        hints.append(f"强度 {snapshot.strength}，低于入场线 95")
    if snapshot.industry_temperature is None:
        hints.append("行业温度数据不可用")
    elif snapshot.industry_temperature not in HOT_TEMPERATURES:
        hints.append(f"行业温度为{snapshot.industry_temperature}，未达到热或沸")
    if snapshot.phase is None:
        hints.append("节气数据不可用")
    elif snapshot.phase not in ALLOWED_ENTRY_PHASES:
        hints.append(f"节气已到{snapshot.phase}")
    if snapshot.market_cap is None:
        hints.append("市值数据不可用")
    elif snapshot.market_cap < 100:
        hints.append(f"市值 {snapshot.market_cap} 亿元，低于入场线 100")
    if (
        snapshot.temperature_prev != "温"
        or snapshot.temperature_curr not in HOT_TEMPERATURES
    ):
        hints.append("不是新的温转热或温转沸入场信号")
    return tuple(hints)


def _protection_was_triggered(
    symbol: str,
    old_state: Mapping[str, object],
    watch_events: Sequence[Mapping[str, object]],
) -> bool:
    if not old_state:
        return False
    started_for = old_state.get("position_started_for")
    for event in watch_events:
        if event.get("event_type") != "protection_triggered" or str(
            event.get("symbol", "")
        ).strip() != symbol:
            continue
        event_date = event.get("trading_date")
        if not isinstance(event_date, str):
            occurred_at = event.get("occurred_at")
            event_date = occurred_at[:10] if isinstance(occurred_at, str) else ""
        if not isinstance(started_for, str) or not started_for or not event_date:
            return True
        if event_date >= started_for:
            return True
    return False


def _post_sell_planned_risk(
    *,
    account: AccountSnapshot,
    holdings: Sequence[HoldingDecision],
    sell_symbols: set[str],
    price_fx_to_account_currency: Decimal,
    normal_cost_rate: Decimal,
) -> tuple[Decimal | None, str]:
    if not account.net_value.is_finite() or account.net_value <= 0:
        return None, "模拟盘净值缺失，暂停新开仓"
    if not account.available_cash.is_finite() or account.available_cash < 0:
        return None, "模拟盘现金缺失，暂停新开仓"
    if (
        not price_fx_to_account_currency.is_finite()
        or price_fx_to_account_currency <= 0
    ):
        return None, "汇率缺失，暂停新开仓"
    if not normal_cost_rate.is_finite() or normal_cost_rate < 0:
        return None, "正常成本模型缺失，暂停新开仓"

    holding_by_symbol = {item.symbol: item for item in holdings}
    planned_risk = Decimal("0")
    for position in account.positions:
        if position.symbol in sell_symbols:
            continue
        if not position.quantity.is_finite() or position.quantity <= 0:
            return None, f"模拟持仓 {position.symbol} 数量缺失，暂停新开仓"
        holding = holding_by_symbol.get(position.symbol)
        if (
            holding is None
            or holding.close is None
            or not holding.close.is_finite()
            or holding.close <= 0
        ):
            return None, f"模拟持仓 {position.symbol} 价格缺失，暂停新开仓"
        if (
            holding.historical
            or holding.active_line is None
            or not holding.active_line.is_finite()
            or holding.active_line < 0
        ):
            return None, f"模拟持仓 {position.symbol} 活动保护线缺失，暂停新开仓"
        planned_risk += position.quantity * (
            max(Decimal("0"), holding.close - holding.active_line)
            * price_fx_to_account_currency
            + holding.close * price_fx_to_account_currency * normal_cost_rate
        )
    return planned_risk, ""


def build_report(
    *,
    as_of_date: str,
    execution_date: str,
    account: AccountSnapshot,
    candidates: Sequence[CandidateInput],
    holding_snapshots: Mapping[str, HoldingSnapshot | None],
    bars_by_symbol: Mapping[str, Sequence[DailyKlineBar] | None],
    prior_state: Mapping[str, object] | None = None,
    watch_events: Sequence[Mapping[str, object]] = (),
    api_facts: Sequence[str] = (),
    data_sources: Sequence[str] = (),
    estimated_api_cost: Decimal | None = None,
    actual_api_cost: Decimal | None = None,
    generated_at: str | None = None,
    metadata: Mapping[str, object] | None = None,
    market: str = "CN",
    lot_sizes: Mapping[str, int] | None = None,
    position_weight: Decimal = DEFAULT_TARGET_WEIGHT,
    position_weight_source: str = "fallback_4pct",
    price_fx_to_account_currency: Decimal = Decimal("1"),
    normal_cost_rate: Decimal = NORMAL_COST_RATE,
    process_version: str = "",
    candidate_pool_ids: Sequence[int] = (),
    strategy_snapshot: Mapping[str, object] | None = None,
    kelly_rounds: Sequence[TrendKellyRound] = (),
    kelly_data_reason: str = "",
    drawdown_summary: Mapping[str, object] | None = None,
) -> TrendReport:
    resolved_strategy_snapshot = (
        {
            **dict(strategy_snapshot),
            "process_version": process_version
            or str((metadata or {}).get("process_version") or ""),
        }
        if strategy_snapshot is not None
        else trend_strategy_snapshot(
            market,
            process_version
            or str((metadata or {}).get("process_version") or ""),
            candidate_pool_ids,
            normal_cost_rate=normal_cost_rate,
        )
    )
    snapshot_version = str(resolved_strategy_snapshot.get("strategy_version") or "")
    kelly_state = (
        TrendKellyState(
            phase="unavailable",
            eligible_sample_count=0,
            selected_sample_count=0,
            enabled=False,
            full_kelly=None,
            quarter_kelly_cap=None,
            reason=kelly_data_reason,
            last_closed_at="",
            selected_round_ids=(),
        )
        if snapshot_version in {"v3", "v4"} and kelly_data_reason
        else calculate_trend_kelly(
            kelly_rounds,
            market=market,
            strategy_id=str(resolved_strategy_snapshot.get("strategy_id") or ""),
            opening_strategy_version=snapshot_version,
        )
        if snapshot_version in {"v3", "v4"}
        else None
    )
    held_symbols = {position.symbol for position in account.positions}
    candidate_decision = build_candidate_list(
        candidates,
        held_symbols=held_symbols,
        expected_date=as_of_date,
        market=market,
    )
    displayed_candidates = candidate_decision.eligible[:CANDIDATE_LIMIT]
    old_positions = _state_positions(prior_state)
    holdings: list[HoldingDecision] = []
    new_positions: dict[str, object] = {}
    industries: Counter[str] = Counter()
    industry_values: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for position in account.positions:
        symbol = position.symbol
        returned_snapshot = holding_snapshots.get(symbol)
        snapshot = (
            returned_snapshot
            if returned_snapshot is not None
            and returned_snapshot.as_of_date == as_of_date
            else None
        )
        old = old_positions.get(symbol)
        old_state = old if isinstance(old, Mapping) else {}
        triggered = (
            {symbol}
            if _protection_was_triggered(symbol, old_state, watch_events)
            else set()
        )
        action, reason = _holding_action(
            symbol=symbol, snapshot=snapshot, triggered=triggered, market=market
        )
        initial_line = _state_decimal(old_state, "initial_line")
        active_line = _state_decimal(old_state, "active_line")
        old_atr = _state_decimal(old_state, "atr14")
        position_started_for = old_state.get("position_started_for")
        if not isinstance(position_started_for, str) or not position_started_for:
            position_started_for = as_of_date
        tracking_active = old_state.get("tracking_active") is True
        if snapshot is not None and (
            snapshot.boiling is True or snapshot.champagne is True
        ):
            tracking_active = True
        historical = not old_state
        daily_bars = tuple(bars_by_symbol.get(symbol) or ())
        current_atr, close, lows = _kline_metrics(
            daily_bars, before=as_of_date, expected_date=as_of_date
        )
        stale_kline = bool(daily_bars) and daily_bars[-1].date != as_of_date
        if active_line is None and current_atr is not None and close is not None:
            initial_line = active_line = (
                close - INITIAL_PROTECTION_ATR_MULTIPLE * current_atr
            )
        if active_line is not None and tracking_active and action == "HOLD":
            active_line = update_protection_line(
                old_line=active_line,
                boiling=True,
                champagne=False,
                prior_five_lows=lows,
            )
        if (active_line is None or stale_kline) and action == "HOLD":
            action, reason = "MANUAL_REVIEW", "holding_kline_unavailable"
        effective_atr = current_atr if current_atr is not None else old_atr
        industry = snapshot.industry if snapshot else ""
        if industry:
            industries[industry] += 1
            industry_values[industry] += position.market_value
        holdings.append(
            HoldingDecision(
                symbol=symbol,
                name=position.name,
                industry=industry,
                action=action,
                reason=reason,
                initial_line=initial_line,
                active_line=active_line,
                atr=effective_atr,
                close=close,
                temperature_prev=snapshot.temperature_prev if snapshot else None,
                temperature_curr=snapshot.temperature_curr if snapshot else None,
                strength=snapshot.strength if snapshot else None,
                entry_hints=(
                    _holding_entry_hints(snapshot) if market == "CN" else ()
                ),
                historical=historical,
            )
        )
        if active_line is not None:
            new_positions[symbol] = {
                "initial_line": str(initial_line),
                "active_line": str(active_line),
                "atr14": str(effective_atr) if effective_atr is not None else "",
                "position_started_for": position_started_for,
                "tracking_active": tracking_active,
                "updated_for": as_of_date,
            }
    sell_symbols = {
        holding.symbol for holding in holdings if holding.action == "SELL_ALL"
    }
    post_sell_cash = account.available_cash + sum(
        (
            position.market_value
            for position in account.positions
            if position.symbol in sell_symbols
        ),
        Decimal("0"),
    )
    post_sell_position_count = max(
        0,
        (
            account.position_count
            if account.position_count is not None
            else len(account.positions)
        )
        - len(sell_symbols),
    )
    if snapshot_version == "v1":
        buy_actions = _estimate_buy_actions_v1(
            ranked=candidate_decision.eligible,
            net_value=account.net_value,
            available_cash=post_sell_cash,
            current_position_count=post_sell_position_count,
            position_weight=position_weight,
            market=market,
            lot_sizes=lot_sizes,
            price_fx_to_account_currency=price_fx_to_account_currency,
        )
        risk_skips: list[dict[str, object]] = []
        risk_summary: dict[str, object] = {}
    else:
        existing_planned_risk, critical_data_reason = _post_sell_planned_risk(
            account=account,
            holdings=holdings,
            sell_symbols=sell_symbols,
            price_fx_to_account_currency=price_fx_to_account_currency,
            normal_cost_rate=normal_cost_rate,
        )
        critical_data_reason = critical_data_reason or kelly_data_reason
        buy_actions, risk_skips, risk_summary = _plan_buy_actions(
            ranked=candidate_decision.eligible,
            net_value=account.net_value,
            available_cash=post_sell_cash,
            current_position_count=post_sell_position_count,
            position_weight=position_weight,
            market=market,
            lot_sizes=lot_sizes,
            price_fx_to_account_currency=price_fx_to_account_currency,
            portfolio_planned_risk=existing_planned_risk,
            normal_cost_rate=normal_cost_rate,
            critical_data_reason=critical_data_reason,
            kelly_state=kelly_state,
        )
        if snapshot_version == "v4" and (
            not valid_drawdown_decision(
                drawdown_summary,
                expected_market=market,
                expected_strategy_id=str(
                    resolved_strategy_snapshot.get("strategy_id") or ""
                ),
                expected_strategy_version=snapshot_version,
                expected_equity=account.net_value,
            )
            or drawdown_summary.get("entry_allowed") is not True
        ):
            valid_summary = isinstance(drawdown_summary, Mapping)
            pause_reason = (
                str(drawdown_summary.get("pause_reason") or "")
                if valid_summary
                else ""
            ) or "策略累计回撤状态无效，暂停新开仓"
            risk_skips = [
                _risk_skip(
                    item,
                    weight=(
                        CN_TARGET_WEIGHTS.get(item.temperature_curr)
                        if market == "CN"
                        else position_weight
                    ),
                    target_amount=None,
                    reason=pause_reason,
                    decisive_constraint="策略累计回撤",
                )
                for item in candidate_decision.eligible
            ]
            buy_actions = []
            if risk_summary.get("status") == "active":
                risk_summary = _risk_summary(
                    net_value=account.net_value,
                    existing_planned_risk=existing_planned_risk,
                    new_planned_risk=Decimal("0"),
                    normal_cost_rate=normal_cost_rate,
                    kelly_state=kelly_state,
                )
    industry_concentration = tuple(
        (
            industry,
            count,
            (
                industry_values[industry] * Decimal("100") / account.net_value
                if account.net_value.is_finite() and account.net_value > 0
                else Decimal("0")
            ),
        )
        for industry, count in sorted(industries.items())
    )
    holding_signals = {
        position.symbol: (
            _holding_signal(holding_snapshots[position.symbol], market=market)
            if holding_snapshots.get(position.symbol) is not None
            else None
        )
        for position in account.positions
    }
    excluded_signals = {
        symbol: [
            _candidate_signal(item, market=market)
            for item in candidates
            if item.symbol == symbol
        ]
        for symbol in candidate_decision.excluded
    }
    ranks = {
        (item.tm_id, item.symbol): rank
        for rank, item in enumerate(candidate_decision.eligible, 1)
    }
    candidate_signals = [
        {
            **_candidate_signal(item, market=market),
            "eligible": (item.tm_id, item.symbol) in ranks,
            "excluded_reasons": _candidate_reasons(
                item, held_symbols, as_of_date, market=market
            ),
            "rank": ranks.get((item.tm_id, item.symbol)),
            "pools": list(item.pools),
            "source": "Trend Animals",
        }
        for item in candidates
    ]
    return TrendReport(
        schema_version=1,
        generated_at=generated_at
        or datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        as_of_date=as_of_date,
        execution_date=execution_date,
        account=account,
        api_facts=tuple(api_facts),
        holdings=tuple(holdings),
        candidates=displayed_candidates,
        excluded=candidate_decision.excluded,
        buy_actions=tuple(buy_actions),
        risk_skips=tuple(risk_skips),
        risk_summary=risk_summary,
        industry_concentration=industry_concentration,
        data_sources=tuple(data_sources),
        estimated_api_cost=estimated_api_cost,
        actual_api_cost=actual_api_cost,
        protection_state={"schema_version": 1, "positions": new_positions},
        signal_snapshots={
            "holdings": holding_signals,
            "excluded": excluded_signals,
            "candidates": candidate_signals,
        },
        metadata={
            **dict(metadata or {}),
            "position_weight": str(position_weight),
            "position_weight_source": position_weight_source,
        },
        strategy_snapshot=resolved_strategy_snapshot,
        drawdown_summary=(
            dict(drawdown_summary) if drawdown_summary is not None else None
        ),
        replay_evidence=None,
    )


def _finalize_market_report(
    report: TrendReport, *, managed_symbols: Sequence[str]
) -> TrendReport:
    market = str(report.metadata.get("market") or "").upper()
    if market == "US" and not report.account.fresh:
        report = replace(
            report,
            buy_actions=(),
            holdings=tuple(
                replace(
                    holding,
                    action="MANUAL_REVIEW",
                    reason="stale_tiger_account",
                )
                for holding in report.holdings
            ),
        )
    managed = set(managed_symbols)
    managed.update(item.symbol for item in report.account.positions)
    managed.update(item.symbol for item in report.buy_actions)
    return replace(
        report,
        protection_state={
            **report.protection_state,
            "managed_symbols": sorted(managed),
        },
        metadata={**report.metadata, "delivery_status": "prepared"},
    )


def _paid_expansion_signal(
    item: CandidateInput | HoldingSnapshot,
) -> dict[str, object]:
    return {
        "gain_since_entry": item.gain_since_entry,
        "phase_prev": item.phase_prev,
        "phase_curr": item.phase_curr,
        "strength_change": item.strength_change,
        "global_strength": item.global_strength,
        "strength_prev_week": item.strength_prev_week,
        "strength_prev_month": item.strength_prev_month,
        "labels": list(item.labels),
        "kline_supplement": item.kline_supplement,
    }


def _holding_signal(item: HoldingSnapshot, *, market: str) -> dict[str, object]:
    signal = {
        "tm_id": item.tm_id,
        "symbol": item.symbol,
        "as_of_date": item.as_of_date,
        "right_side": item.right_side,
        "danger": item.danger,
        "boiling": item.boiling,
        "champagne": item.champagne,
        "industry": item.industry,
        "industry_tm_id": item.industry_tm_id,
        "industry_temperature": item.industry_temperature,
        "filter_price": item.filter_price,
        "market_cap": item.market_cap,
        "strength": item.strength,
        "temperature_prev": item.temperature_prev,
        "temperature_curr": item.temperature_curr,
        "phase": item.phase,
        **_paid_expansion_signal(item),
    }
    if market.upper() in {"US", "HK"}:
        signal.update(name=item.name, days=item.days)
    return signal


def _candidate_signal(item: CandidateInput, *, market: str) -> dict[str, object]:
    signal = {
        "tm_id": item.tm_id,
        "symbol": item.symbol,
        "exchange": item.exchange,
        "name": item.name,
        "asset": item.asset,
        "industry": item.industry,
        "as_of_date": item.as_of_date,
        "tradable": item.tradable,
        "amount": item.amount,
        "right_side": item.right_side,
        "days": item.days,
        "strength": item.strength,
        "danger": item.danger,
        "filter_price": item.filter_price,
        "close": item.close,
        "atr": item.atr,
        "market_cap": item.market_cap,
        "industry_tm_id": item.industry_tm_id,
        "industry_temperature": item.industry_temperature,
        "temperature_prev": item.temperature_prev,
        "temperature_curr": item.temperature_curr,
        "phase": item.phase,
        **_paid_expansion_signal(item),
    }
    if market.upper() in {"US", "HK"}:
        signal.update(boiling=item.boiling, champagne=item.champagne)
    return signal


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _risk_percent(value: object) -> str:
    parsed = _nonnegative_risk_decimal(value)
    return "未知" if parsed is None else f"{_money(parsed * Decimal('100'))}%"


ACTION_LABELS = {
    "SELL_ALL": "全部卖出",
    "HOLD": "继续持有",
    "MANUAL_REVIEW": "人工复核",
}

REASON_LABELS = {
    "protection_line_already_triggered": "活动保护线已触发",
    "danger_signal": "危险信号触发",
    "left_trend_right_side": "右侧趋势已结束",
    "holding_signal_unknown": "趋势信号不完整",
    "holding_kline_unavailable": "持仓日线数据不可用",
    "stale_tiger_account": "老虎账户数据非实时，禁止新增买入；持仓需复核",
    "trend_intact": "趋势保持完好",
    "temperature_changed_to_flat": "趋势温度转平",
    "a_share_only": "仅限 A 股股票",
    "temperature_missing": "个股趋势温度缺失",
    "temperature_transition_not_entry": "不是温转热或温转沸",
    "filter_price_missing": "筛选价缺失",
    "filter_price_above_200": "筛选价高于 200 元",
    "strength_missing": "趋势强度缺失",
    "strength_below_95": "趋势强度低于 95",
    "industry_id_missing": "行业 ID 缺失",
    "industry_temperature_missing": "行业温度缺失",
    "industry_temperature_not_hot": "行业温度未达到热或沸",
    "phase_missing": "趋势节气缺失",
    "phase_after_summer_solstice": "趋势节气晚于夏至",
    "market_cap_missing": "市值缺失",
    "market_cap_below_100": "市值低于 100 亿元",
    "amount_missing": "日成交额缺失",
    "amount_below_2": "日成交额不足 2 亿元",
    "right_side_days_missing": "右侧天数缺失",
    "right_side_not_true": "尚未进入右侧趋势",
    "strength_not_above_90": "趋势强度未超过 90",
    "right_side_days_not_below_10": "进入右侧趋势已满 10 天",
    "not_tradable": "当前不可交易",
    "amount_below_1": "日成交额不足 1 亿元",
    "danger_unknown": "危险信号未知",
    "name_missing": "标的名称缺失",
    "asset_missing": "资产类型缺失",
    "unsupported_asset": "不属于 A 股股票或境内 ETF",
    "already_held": "当前账户已经持有",
    "excluded_security": "北交所、ST 或退市标的",
    "unsupported_exchange": "不属于沪深市场",
    "atr_unavailable": "缺少 ATR 数据",
    "data_date_mismatch": "数据日期不一致",
}


def _action_label(value: str) -> str:
    return ACTION_LABELS.get(value, f"未知动作（{value}）")


def _reason_label(value: str) -> str:
    return REASON_LABELS.get(value, f"未知原因（{value}）")


def _component_api_facts(api: object, row_count: int) -> tuple[str, ...]:
    facts = [f"getComponentTicker rows={row_count} cache=client-managed"]
    ignored = tuple(getattr(api, "ignored_stale_components", ()))
    if ignored:
        details = "、".join(
            f"{row['tickerSymbol']}（{row['asOfDate']}）" for row in ignored
        )
        facts.append(f"忽略旧成分 {len(ignored)} 条：{details}")
    return tuple(facts)


def _api_fact_label(value: str) -> str:
    if value.startswith("忽略旧成分 "):
        return value
    if value.startswith("getUpdateStatus rows="):
        return f"数据更新状态：已检查 {value.rsplit('=', 1)[-1]} 条"
    if value.startswith("getComponentTicker rows="):
        count = value.split(" rows=", 1)[1].split(" ", 1)[0]
        return f"候选池成分：{count} 条"
    if value.startswith("getTickerSnapshot fields=") and " rows=" in value:
        count = value.split(" rows=", 1)[1].split(" ", 1)[0]
        return f"趋势快照：{count} 条"
    return "其他接口事实：详见 JSON 审计文件"


def _account_exception_label(value: str) -> str:
    prefix = "unsupported Eastmoney asset: "
    if value.startswith(prefix):
        identity, separator, details = value[len(prefix) :].rpartition(" (")
        if identity and separator and details.endswith(")"):
            identity = identity.replace("<missing-symbol>", "代码缺失").replace(
                "<missing-name>", "名称缺失"
            )
            return f"东方财富账户不支持的资产：{identity}"
    return "其他账户例外：详见 JSON 审计文件"


def _data_source_label(value: str) -> str:
    if Path(value).is_absolute():
        return "东方财富账户快照"
    return {
        "Trend Animals": "趋势动物",
        "Futu CN calendar/QFQ daily K-line": "富途 A 股交易日历与前复权日线",
    }.get(value, value)


TREND_BUY_WINDOWS = {
    "US": "美股常规交易时段",
    "HK": "09:30–10:00",
    "CN": "09:30–10:00",
}


def _feishu_identity(item: Mapping[str, object]) -> str:
    return " ".join(
        part
        for part in (
            str(item.get("symbol") or "-").strip(),
            str(item.get("name") or "").strip(),
        )
        if part
    )


def _feishu_reason(item: Mapping[str, object]) -> str:
    reason = str(item.get("reason") or "")
    if reason not in REASON_LABELS:
        return "未知动作或原因，需人工确认"
    return _reason_label(reason)


def _feishu_money(value: object) -> str:
    return _money(Decimal(str(value))).rstrip("0").rstrip(".")


def _append_feishu_action_sections(
    lines: list[str],
    sells: Sequence[Mapping[str, object]],
    buys: Sequence[Mapping[str, object]],
    reviews: Sequence[Mapping[str, object]],
    *,
    market: str,
) -> None:
    if sells:
        lines.extend(["", "卖出"])
        for index, item in enumerate(sells, 1):
            line = f"{index}. {_feishu_identity(item)}｜{_feishu_reason(item)}"
            if item.get("active_line") not in {None, ""}:
                line += f"｜保护线 {_feishu_money(item['active_line'])}"
            lines.append(line)
    if buys:
        lines.extend(["", "买入"])
        for index, item in enumerate(buys, 1):
            lines.append(
                f"{index}. {_feishu_identity(item)}｜{TREND_BUY_WINDOWS[market]}｜"
                f"约 {item.get('estimated_shares', '-')} 股｜"
                f"金额上限 {_feishu_money(item.get('target_amount') or '0')}｜"
                f"保护线 {_feishu_money(item.get('estimated_initial_line') or '0')}"
            )
    if reviews:
        lines.extend(["", "人工复核"])
        lines.extend(
            f"{index}. {_feishu_identity(item)}｜{_feishu_reason(item)}"
            for index, item in enumerate(reviews, 1)
        )


ATTENTION_FEISHU_FIELDS = (
    ("right_side", "右侧"),
    ("temperature", "温度"),
    ("phase", "节气"),
    ("strength_change", "周强度"),
    ("danger", "危险"),
    ("boiling", "沸腾"),
    ("champagne", "开香槟"),
)


def _attention_value(value: object) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    if value is None:
        return "未提供"
    return str(value)


def _append_feishu_attention(
    lines: list[str], attention: object, *, market: str
) -> None:
    if market not in {"US", "HK"} or not isinstance(attention, list):
        return
    items = [item for item in attention if isinstance(item, Mapping)]
    if not items:
        return
    lines.extend(["", "期权关注"])
    for index, item in enumerate(items, 1):
        fields = []
        for key, label in ATTENTION_FEISHU_FIELDS:
            transition = item.get(key)
            if not isinstance(transition, Mapping) or transition.get("changed") is not True:
                continue
            fields.append(
                f"{label} {_attention_value(transition.get('previous'))}"
                f"→{_attention_value(transition.get('current'))}"
            )
        suffix = f"｜{'｜'.join(fields)}" if fields else ""
        lines.append(f"{index}. {item.get('symbol') or '-'}{suffix}")


def render_trend_feishu_text(
    payload: Mapping[str, object], *, broker_label: str, market_label: str
) -> tuple[str, str]:
    execution_date = str(payload.get("execution_date") or "-")
    as_of_date = str(payload.get("as_of_date") or "-")
    account = payload.get("account")
    if not valid_serialized_account(account):
        raise ValueError("趋势报告账户快照无效")
    assert isinstance(account, Mapping)
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    market = str(metadata.get("market") or "CN").upper()
    judgments = payload.get("strategy_judgments")
    judgments = judgments if isinstance(judgments, dict) else {}
    holdings = [
        item
        for item in judgments.get("holding_decisions", [])
        if isinstance(item, dict)
    ]
    formal = [
        item
        for item in judgments.get("formal_actions", [])
        if isinstance(item, dict)
    ]
    fresh = account.get("fresh") is True
    sells = [
        item
        for item in formal
        if item.get("action") == "SELL_ALL" and not _trend_action_needs_review(item)
    ]
    buys = [
        item
        for item in formal
        if item.get("action") == "BUY"
        and not _trend_action_needs_review(item)
    ]
    holds = [
        item
        for item in holdings
        if item.get("action") == "HOLD" and not _trend_action_needs_review(item)
    ]
    reviews: list[dict[str, object]] = []
    for item in formal + holdings:
        if _trend_action_needs_review(item) and item not in reviews:
            reviews.append(item)
    title = f"【{broker_label}｜{market_label}趋势报告｜{execution_date}】"
    status = (
        "已更新"
        if fresh
        else STALE_TIGER_ACCOUNT_WARNING
        if metadata.get("broker") == "tiger"
        else NON_REALTIME_ACCOUNT_WARNING
    )
    summary = (
        f"今日动作：卖出 {len(sells)}｜买入 {len(buys)}｜持有 {len(holds)}｜复核 {len(reviews)}"
        if sells or buys
        else f"今日无买卖动作｜持有 {len(holds)}｜复核 {len(reviews)}"
    )
    lines = [
        f"数据截至：{as_of_date}",
        f"账户状态：{status}",
        summary,
    ]
    _append_feishu_action_sections(lines, sells, buys, reviews, market=market)
    _append_feishu_attention(lines, payload.get("option_attention"), market=market)
    lines.extend(["", "请人工确认，不自动下单。"])
    return title, "\n".join(lines)


def _trend_action_needs_review(item: Mapping[str, object]) -> bool:
    action = item.get("action")
    reason = item.get("reason")
    known_reason = isinstance(reason, str) and reason in REASON_LABELS
    if action == "BUY":
        return reason not in (None, "") and not known_reason
    return (
        action == "MANUAL_REVIEW"
        or action not in ACTION_LABELS
        or action in {"SELL_ALL", "HOLD"} and not known_reason
    )


def render_trend_failure_text(
    *,
    broker_label: str,
    market_label: str,
    report_date: str,
    reason: str,
    recovery_action: str,
) -> tuple[str, str]:
    return (
        f"【{broker_label}｜{market_label}趋势报告生成失败｜{report_date}】",
        f"原因：{reason}\n现在做：{recovery_action}\n\n报告未生成，请勿依据旧报告交易。",
    )


def render_markdown(report: TrendReport) -> str:
    market = str(report.metadata.get("market") or "CN").upper()
    market_label = {"CN": "A股", "US": "美股", "HK": "港股"}.get(market, market)
    account_currency = str(report.metadata.get("account_currency") or "")
    currency = (
        {"CNY": "元", "USD": "美元", "HKD": "港元"}.get(account_currency)
        or {"CN": "元", "US": "美元", "HK": "港元"}.get(market, "")
    )
    freshness = (
        "已更新"
        if report.account.fresh is True
        else STALE_TIGER_ACCOUNT_WARNING
        if report.metadata.get("broker") == "tiger"
        else NON_REALTIME_ACCOUNT_WARNING
    )
    sells = [item for item in report.holdings if item.action == "SELL_ALL"]
    holds = [item for item in report.holdings if item.action == "HOLD"]
    reviews = [item for item in report.holdings if item.action == "MANUAL_REVIEW"]
    others = [item for item in report.holdings if item.action not in ACTION_LABELS]
    industry_facts = {
        industry: (count, weight)
        for industry, count, weight in report.industry_concentration
    }
    lines = [
        f"# {market_label}趋势操作计划 · {report.execution_date}",
        "",
        "## 操作摘要",
        "",
        f"数据日期：{report.as_of_date}｜生成时间：{report.generated_at}｜账户：{freshness}",
        f"全部卖出 {len(sells)}｜允许买入 {len(report.buy_actions)}｜"
        f"继续持有 {len(holds)}｜人工复核 {len(reviews)}｜其他动作 {len(others)}",
    ]
    if report.strategy_snapshot.get("strategy_version") in {"v3", "v4"}:
        phase = {
            "cold_start": "冷启动",
            "active_all_samples": "全样本启用",
            "active_rolling_200": "最近 200 个样本启用",
        }.get(str(report.risk_summary.get("kelly_phase") or ""), "未知")
        count = report.risk_summary.get("kelly_eligible_sample_count", 0)
        cap = report.risk_summary.get("kelly_cap")
        cap_text = (
            "禁用（固定风险仓位）"
            if cap is None
            else f"{format((Decimal(str(cap)) * Decimal('100')).normalize(), 'f')}%"
        )
        lines.extend(
            [
                f"Kelly 阶段：{phase}（{count} 个合格模拟闭环）｜"
                f"当前 Kelly 上限：{cap_text}",
                f"Kelly 说明：{report.risk_summary.get('kelly_reason') or '仅向下约束未来新仓'}；"
                "实盘结果不参与计算",
            ]
        )
    if report.risk_summary:
        lines.extend([
            "",
            "## 组合计划风险",
            "",
            "- 正常计划风险："
            f"{_risk_percent(report.risk_summary['portfolio_planned_risk_pct'])}"
            f" / {_risk_percent(report.risk_summary['portfolio_risk_limit_pct'])}",
            "- 异常损失缓冲："
            f"{_risk_percent(report.risk_summary['abnormal_loss_buffer_pct'])}（不得用于开仓）",
            "",
        ])
    if report.drawdown_summary is not None:
        drawdown = report.drawdown_summary.get("drawdown_pct")
        drawdown_text = _risk_percent(drawdown)
        lines.extend([
            "## 策略累计回撤",
            "",
            f"- 当前累计回撤：{drawdown_text}｜暂停阈值 5%｜{report.drawdown_summary.get('status_label', '未知')}",
            *(
                [f"- {report.drawdown_summary['pause_reason']}"]
                if report.drawdown_summary.get("pause_reason")
                else []
            ),
            "",
        ])
    lines.extend(["## 开盘前：确认卖出", ""])
    if sells:
        for item in sells:
            line = f"- {item.symbol} {item.name}｜{_reason_label(item.reason)}"
            if item.active_line is not None:
                line += f"｜活动保护线 {_money(item.active_line)}"
            lines.append(line)
    else:
        lines.append("- 无需卖出。")

    buy_window = "09:30–10:00" if market == "CN" else "下个常规交易时段"
    lines.extend(["", f"## {buy_window}：按顺序考虑买入", ""])
    if report.buy_actions:
        for index, item in enumerate(report.buy_actions, 1):
            if market == "CN":
                lines.append(
                    f"- {index}. {item.symbol} {item.name}｜"
                    f"筛选价 {_money(item.filter_price)} 元（Trend Animals）｜"  # type: ignore[arg-type]
                    f"执行参考价 {_money(item.close)} 元（富途前复权日线）｜"
                    f"温度 {item.temperature_prev or '未知'}→{item.temperature_curr or '未知'}｜"
                    f"节气 {item.phase or '未知'}｜强度 {item.strength}｜"
                    f"行业温度 {item.industry_temperature or '未知'}｜"
                    f"市值 {item.market_cap} 亿元｜成交额 {item.amount} 亿元｜"
                    f"目标仓位 {_money(item.target_weight * Decimal('100'))}%｜"
                    f"金额上限 {_money(item.target_amount)} 元｜约 {item.estimated_shares} 股｜"
                    f"预计保护线 {_money(item.estimated_initial_line)}"
                )
            else:
                lines.append(
                    f"- {index}. {item.symbol} {item.name}｜约 {item.estimated_shares} 股｜"
                    f"金额上限 {_money(item.target_amount)} {currency}｜"
                    f"预计保护线 {_money(item.estimated_initial_line)}"
                )
        if market == "CN":
            quantity_rule = "按富途数据日前复权日线收盘价向下取整为 100 股整数倍"
        elif market == "HK":
            quantity_rule = "按富途 lot size 向下取整为整手"
        else:
            quantity_rule = "按富途实时价格向下取整为整股，不使用碎股"
        lines.append(f"- 实际股数{quantity_rule}，不得超过金额上限。")
    else:
        lines.append("- 无允许买入标的。")
    if not sells and not report.buy_actions:
        lines.extend(["", NO_ACTION_TEXT])

    lines.extend(["", "## 继续持有与人工复核", ""])
    for item in [*holds, *reviews, *others]:
        line = (
            f"- {item.symbol} {item.name}｜{_action_label(item.action)}｜"
            f"{_reason_label(item.reason)}"
        )
        if item.active_line is not None:
            line += f"｜活动保护线 {_money(item.active_line)}"
        if market == "CN":
            line += (
                f"｜执行参考价 "
                f"{_money(item.close) + ' 元（富途前复权日线）' if item.close is not None else '不可用'}"
                f"｜温度 {item.temperature_prev or '未知'}→{item.temperature_curr or '未知'}"
                f"｜强度 {item.strength if item.strength is not None else '不可用'}"
            )
            if item.entry_hints:
                line += f"｜持仓提示 {'；'.join(item.entry_hints)}"
        lines.append(line)
    if not holds and not reviews and not others:
        lines.append("- 无。")

    lines.extend(["", "## 中文附录", "", "### 前 10 名候选", ""])
    if report.candidates:
        for index, item in enumerate(report.candidates[:10], 1):
            industry_count, industry_weight = industry_facts.get(
                item.industry, (0, Decimal("0"))
            )
            lines.append(
                f"- {index}. {item.symbol} {item.name}｜强度 {item.strength}｜"
                f"右侧 {item.days} 天｜成交额 {item.amount} 亿元｜"
                + (
                    f"筛选价 {item.filter_price} 元｜执行参考价 {item.close} 元｜"
                    f"温度 {item.temperature_prev or '未知'}→{item.temperature_curr or '未知'}｜"
                    f"节气 {item.phase or '未知'}｜行业 ID {item.industry_tm_id}｜"
                    f"行业温度 {item.industry_temperature or '未知'}｜市值 {item.market_cap} 亿元｜"
                    if market == "CN"
                    else ""
                )
                +
                f"行业 {item.industry or '未知'}（已占 {industry_count} 个席位，"
                f"当前仓位 {_money(industry_weight)}%）"
            )
    else:
        lines.append("- 无合格候选。")

    lines.extend(["", "### 行业集中度", ""])
    if report.industry_concentration:
        lines.extend(
            f"- {industry}：当前持仓 {count} 个席位，当前仓位 {_money(weight)}%"
            for industry, count, weight in report.industry_concentration
        )
    else:
        lines.append("- 当前无行业持仓集中事实。")

    lines.extend(["", "### 排除项", ""])
    for symbol, reasons in report.excluded.items():
        lines.append(f"- {symbol}｜{'、'.join(_reason_label(reason) for reason in reasons)}")
    lines.extend(
        f"- 账户例外｜{_account_exception_label(item)}"
        for item in report.account.exceptions
    )
    if not report.excluded and not report.account.exceptions:
        lines.append("- 无。")

    lines.extend(["", "### 数据与成本", ""])
    lines.extend(f"- {_api_fact_label(fact)}" for fact in report.api_facts)
    if not report.api_facts:
        lines.append("- 无可用接口事实。")
    lines.extend(
        f"- 数据来源：{_data_source_label(source)}" for source in report.data_sources
    )
    lines.append(
        "- API 计费估算："
        + ("未知" if report.estimated_api_cost is None else str(report.estimated_api_cost))
    )
    lines.append(
        "- 本次余额变化："
        + ("未知" if report.actual_api_cost is None else str(report.actual_api_cost))
    )
    lines.extend(
        [
            "",
            "## 免责声明",
            "",
            DISCLAIMER_TEXT,
            "",
        ]
    )
    return "\n".join(lines)


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def validate_report_strategy_snapshot(report: TrendReport) -> None:
    snapshot = report.strategy_snapshot
    parameters = snapshot.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("strategy snapshot does not match report actions")
    market = str(report.metadata.get("market") or "CN").upper()
    expected_window = "美股常规交易时段" if market == "US" else "09:30-10:00"
    if parameters.get("buy_window") != expected_window:
        raise ValueError("strategy snapshot does not match report actions")
    version = snapshot.get("strategy_version")
    if version not in {"v1", "v2", "v3", "v4"}:
        raise ValueError("strategy snapshot does not match report actions")
    if version in {"v2", "v3", "v4"}:
        valid_contract = {
            "v2": valid_v2_risk_contract,
            "v3": valid_v3_risk_contract,
            "v4": valid_v4_risk_contract,
        }[version]
        if not valid_contract(
            parameters,
            report.risk_summary,
            expected_nav=report.account.net_value,
        ):
            raise ValueError("strategy snapshot does not match report actions")
        if report.risk_summary.get("status") == "paused" and report.buy_actions:
            raise ValueError("strategy snapshot does not match report actions")
        portfolio_limit = _nonnegative_risk_decimal(
            report.risk_summary.get("portfolio_risk_limit")
        )
        new_planned_risk = Decimal("0")
        if portfolio_limit is None:
            if report.buy_actions:
                raise ValueError("strategy snapshot does not match report actions")
            nav = None
        elif portfolio_limit > 0:
            nav = portfolio_limit / PORTFOLIO_RISK_LIMIT
        else:
            raise ValueError("strategy snapshot does not match report actions")
        for action in report.buy_actions:
            assert nav is not None
            if (
                action.estimated_shares <= 0
                or action.lot_size <= 0
                or action.estimated_shares % action.lot_size != 0
                or not action.planned_stop_risk.is_finite()
                or action.planned_stop_risk <= 0
                or not action.planned_stop_risk_pct.is_finite()
                or action.planned_stop_risk_pct <= 0
                or action.planned_stop_risk_pct
                != action.planned_stop_risk / nav
                or action.planned_stop_risk > nav * SINGLE_ENTRY_RISK_LIMIT
                or not action.normal_cost.is_finite()
                or action.normal_cost <= 0
                or action.normal_cost > action.planned_stop_risk
                or action.decisive_constraint
                not in {
                    "名义仓位上限",
                    "Kelly 上限",
                    "单笔风险上限",
                    "组合剩余风险",
                    "现金",
                }
            ):
                raise ValueError("strategy snapshot does not match report actions")
            new_planned_risk += action.planned_stop_risk
        if _nonnegative_risk_decimal(
            report.risk_summary.get("new_planned_risk")
        ) != new_planned_risk:
            raise ValueError("strategy snapshot does not match report actions")
    if version == "v4":
        if (
            not valid_drawdown_decision(
                report.drawdown_summary,
                expected_market=market,
                expected_strategy_id=str(snapshot.get("strategy_id") or ""),
                expected_strategy_version="v4",
                expected_equity=report.account.net_value,
            )
            or report.drawdown_summary.get("entry_allowed") is not True
            and report.buy_actions
        ):
            raise ValueError("strategy snapshot does not match report actions")
    try:
        protection_multiple = Decimal(str(parameters["initial_protection_atr_multiple"]))
    except (InvalidOperation, KeyError, ValueError):
        raise ValueError("strategy snapshot does not match report actions") from None
    for action in report.buy_actions:
        target = parameters.get("target_weight")
        nominal_weight = (
            target.get(action.temperature_curr)
            if isinstance(target, Mapping)
            else target
        )
        expected_weight = Decimal(str(nominal_weight))
        if version in {"v3", "v4"} and report.risk_summary.get("kelly_phase") != "cold_start":
            cap = _nonnegative_risk_decimal(report.risk_summary.get("kelly_cap"))
            if cap is None:
                raise ValueError("strategy snapshot does not match report actions")
            expected_weight = min(expected_weight, cap)
        expected_lot = parameters.get("lot_size")
        if (
            action.target_weight != expected_weight
            or (expected_lot is not None and action.lot_size != expected_lot)
            or action.lot_size <= 0
            or action.estimated_initial_line
            != action.close - protection_multiple * action.atr
        ):
            raise ValueError("strategy snapshot does not match report actions")


def _report_payload(report: TrendReport) -> dict[str, object]:
    validate_report_strategy_snapshot(report)
    market = str(report.metadata.get("market") or "CN").upper()
    legacy_v1 = report.strategy_snapshot.get("strategy_version") == "v1"
    buy_window = (
        f"{report.execution_date} 09:30–10:00"
        if market in {"CN", "HK"}
        else f"{report.execution_date} regular session"
    )
    holding_decisions = [_json_value(asdict(item)) for item in report.holdings]
    top10_candidates = []
    for item in report.candidates:
        candidate = asdict(item)
        if market == "CN":
            candidate.pop("boiling")
            candidate.pop("champagne")
        top10_candidates.append(_json_value(candidate))
    formal_actions = [
        _json_value(asdict(item))
        for item in report.holdings
        if item.action == "SELL_ALL"
    ]
    for item in report.buy_actions:
        action = _json_value(asdict(item))
        assert isinstance(action, dict)
        if legacy_v1:
            for key in (
                "planned_stop_risk",
                "planned_stop_risk_pct",
                "normal_cost",
                "decisive_constraint",
            ):
                action.pop(key)
        formal_actions.append(
            {**action, "action": "BUY", "valid_window": buy_window}
        )
    strategy_judgments = {
        "holding_decisions": holding_decisions,
        "top10_candidates": top10_candidates,
        "formal_actions": formal_actions,
    }
    if not legacy_v1:
        strategy_judgments["risk_skips"] = _json_value(report.risk_skips)
    payload = {
        "schema_version": report.schema_version,
        "generated_at": report.generated_at,
        "as_of_date": report.as_of_date,
        "execution_date": report.execution_date,
        "account": _json_value(asdict(report.account)),
        "api_facts": list(report.api_facts),
        "strategy_judgments": strategy_judgments,
        "industry_concentration": _json_value(report.industry_concentration),
        "excluded": report.excluded,
        "data_sources": list(report.data_sources),
        "estimated_api_cost": _json_value(report.estimated_api_cost),
        "actual_api_cost": _json_value(report.actual_api_cost),
        "protection_state": report.protection_state,
        "signal_snapshots": _json_value(report.signal_snapshots),
        "metadata": _json_value(report.metadata),
        "strategy_snapshot": _json_value(report.strategy_snapshot),
        "disclaimer": DISCLAIMER_TEXT,
    }
    if report.drawdown_summary is not None:
        payload["drawdown_summary"] = _json_value(report.drawdown_summary)
    if not legacy_v1:
        payload["risk_summary"] = _json_value(report.risk_summary)
    if report.replay_evidence is not None:
        payload["replay_evidence"] = dict(report.replay_evidence)
    for key in ("delivery_status", "process_version"):
        value = report.metadata.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    if not formal_actions:
        payload["no_action"] = NO_ACTION_TEXT
    return payload


def load_protection_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "positions": {}}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("protection state is unreadable or malformed") from None
    return _validate_protection_state(payload)


def _validate_protection_state(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("protection state has an invalid schema")
    positions = payload.get("positions")
    if not isinstance(positions, dict):
        raise ValueError("protection state positions must be an object")
    for symbol, state in positions.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("protection state symbol must be non-empty")
        if not isinstance(state, dict):
            raise ValueError(f"protection state for {symbol} must be an object")
        if _optional_decimal(state.get("initial_line")) is None or _optional_decimal(
            state.get("active_line")
        ) is None:
            raise ValueError(f"protection state for {symbol} has no active line")
        _optional_decimal(state.get("atr14"))
        tracking_active = state.get("tracking_active")
        if tracking_active is not None and not isinstance(tracking_active, bool):
            raise ValueError(f"protection state for {symbol} has invalid tracking state")
        position_started_for = state.get("position_started_for")
        if position_started_for is not None and not isinstance(position_started_for, str):
            raise ValueError(f"protection state for {symbol} has invalid start date")
        if not isinstance(state.get("updated_for"), str):
            raise ValueError(f"protection state for {symbol} has no update date")
    return payload


def write_protection_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=path.parent
        ) as handle:
            json.dump(
                _json_value(dict(state)),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_watch_events(path: Path) -> tuple[dict[str, object], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError):
        raise ValueError("watch events are unreadable") from None
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError(f"watch event line {line_number} is malformed") from None
        if not isinstance(event, dict):
            raise ValueError(f"watch event line {line_number} is not an object")
        events.append(event)
    return tuple(events)


def write_frozen_report(
    report: TrendReport, reports_dir: Path, revision: bool = False
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = report.as_of_date
    if revision:
        revision_number = 1
        while (reports_dir / f"{stem}-r{revision_number}.md").exists() or (
            reports_dir / f"{stem}-r{revision_number}.json"
        ).exists():
            revision_number += 1
        stem = f"{stem}-r{revision_number}"
    markdown_path = reports_dir / f"{stem}.md"
    json_path = reports_dir / f"{stem}.json"
    if not revision and markdown_path.exists() and json_path.exists():
        json.loads(json_path.read_text(encoding="utf-8"))
        markdown_path.read_text(encoding="utf-8")
        return markdown_path, json_path

    markdown_temp: Path | None = None
    json_temp: Path | None = None
    markdown_backup: Path | None = None
    json_backup: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=reports_dir
        ) as handle:
            handle.write(render_markdown(report))
            markdown_temp = Path(handle.name)
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=reports_dir
        ) as handle:
            json.dump(
                _report_payload(report),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            json_temp = Path(handle.name)
        if markdown_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(markdown_path.read_bytes())
                markdown_backup = Path(handle.name)
        if json_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(json_path.read_bytes())
                json_backup = Path(handle.name)
        try:
            markdown_temp.replace(markdown_path)
            json_temp.replace(json_path)
        except Exception as replace_error:
            rollback_error: Exception | None = None
            for final_path, backup_path in (
                (markdown_path, markdown_backup),
                (json_path, json_backup),
            ):
                try:
                    if backup_path is None:
                        final_path.unlink(missing_ok=True)
                    else:
                        backup_path.replace(final_path)
                except Exception as exc:
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise rollback_error from replace_error
            raise
        return markdown_path, json_path
    finally:
        if markdown_temp is not None:
            markdown_temp.unlink(missing_ok=True)
        if json_temp is not None:
            json_temp.unlink(missing_ok=True)
        if markdown_backup is not None:
            markdown_backup.unlink(missing_ok=True)
        if json_backup is not None:
            json_backup.unlink(missing_ok=True)


def _process_version(repo: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _redact_api_key(value: object, secret: str) -> str:
    text = str(value)
    return text.replace(secret, "<redacted>") if secret else text


def _write_run_log(path: Path, payload: Mapping[str, object], *, append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def _payload_hashes(
    markdown: str,
    report_json: str,
    protection_state: Mapping[str, object] | None = None,
) -> dict[str, str]:
    markdown_bytes = markdown.encode("utf-8")
    json_bytes = report_json.encode("utf-8")
    payload = {
        "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "json_sha256": hashlib.sha256(json_bytes).hexdigest(),
    }
    content = markdown_bytes + b"\0" + json_bytes
    if protection_state is not None:
        state_bytes = json.dumps(
            protection_state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload["protection_state_sha256"] = hashlib.sha256(state_bytes).hexdigest()
        content += b"\0" + state_bytes
    payload["content_hash"] = hashlib.sha256(content).hexdigest()
    return payload


def _write_delivery_receipt(
    path: Path,
    *,
    status: str,
    generated_at: str,
    artifact_stem: str,
    markdown: str,
    report_json: str,
    protection_state: Mapping[str, object],
) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    frozen_state = json.loads(
        json.dumps(protection_state, ensure_ascii=False, sort_keys=True)
    )
    payload = {
        "status": status,
        "generated_at": generated_at,
        "artifact_stem": artifact_stem,
        "markdown": markdown,
        "report_json": report_json,
        "protection_state": frozen_state,
        **_payload_hashes(markdown, report_json, frozen_state),
    }
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return payload


def _read_delivery_receipt(
    path: Path,
    *,
    artifact_stem: str,
) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("delivery receipt is unreadable or malformed") from None
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {
        "prepared", "pending", "sent", "delivery_failed", "delivery_unknown"
    }:
        raise ValueError("delivery receipt has an invalid status")
    if payload.get("artifact_stem") != artifact_stem:
        raise ValueError("delivery receipt artifact stem mismatch")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("delivery receipt has no generation timestamp")
    markdown = payload.get("markdown")
    report_json = payload.get("report_json")
    if not isinstance(markdown, str) or not isinstance(report_json, str):
        raise ValueError("delivery receipt has no embedded report payload")
    try:
        report_payload = json.loads(report_json)
    except json.JSONDecodeError:
        raise ValueError("delivery receipt report JSON is malformed") from None
    if not isinstance(report_payload, dict):
        raise ValueError("delivery receipt report JSON must be an object")
    protection_state = payload.get("protection_state")
    if "protection_state" not in payload and "protection_state_sha256" not in payload:
        if status == "prepared":
            raise ValueError("delivery receipt has no embedded protection state")
        legacy_hashes = _payload_hashes(markdown, report_json)
        if any(payload.get(key) != value for key, value in legacy_hashes.items()):
            raise ValueError("delivery receipt content hash mismatch")
        protection_state = _validate_protection_state(
            report_payload.get("protection_state")
        )
        return _write_delivery_receipt(
            path,
            status=str(status),
            generated_at=generated_at,
            artifact_stem=artifact_stem,
            markdown=markdown,
            report_json=report_json,
            protection_state=protection_state,
        )
    if not isinstance(protection_state, dict):
        raise ValueError("delivery receipt has no embedded protection state")
    hashes = _payload_hashes(markdown, report_json, protection_state)
    if any(payload.get(key) != value for key, value in hashes.items()):
        raise ValueError("delivery receipt content hash mismatch")
    return payload


def _transition_delivery_receipt(
    path: Path,
    receipt: Mapping[str, object],
    *,
    status: str,
    delivery_status: str,
) -> dict[str, object]:
    payload = json.loads(str(receipt["report_json"]))
    if not isinstance(payload, dict):
        raise ValueError("delivery receipt report JSON must be an object")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    metadata["delivery_status"] = delivery_status
    payload["delivery_status"] = delivery_status
    return _write_delivery_receipt(
        path,
        status=status,
        generated_at=str(receipt["generated_at"]),
        artifact_stem=str(receipt["artifact_stem"]),
        markdown=str(receipt["markdown"]),
        report_json=(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ),
        protection_state=receipt["protection_state"],  # type: ignore[arg-type]
    )


def _freeze_receipt_report(
    *,
    receipt: Mapping[str, object],
    reports_dir: Path,
    artifact_stem: str,
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = reports_dir / f"{artifact_stem}.md"
    json_path = reports_dir / f"{artifact_stem}.json"
    markdown_temp: Path | None = None
    json_temp: Path | None = None
    markdown_backup: Path | None = None
    json_backup: Path | None = None
    try:
        with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
            handle.write(str(receipt["markdown"]).encode("utf-8"))
            markdown_temp = Path(handle.name)
        with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
            handle.write(str(receipt["report_json"]).encode("utf-8"))
            json_temp = Path(handle.name)
        if markdown_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(markdown_path.read_bytes())
                markdown_backup = Path(handle.name)
        if json_path.exists():
            with NamedTemporaryFile("wb", delete=False, dir=reports_dir) as handle:
                handle.write(json_path.read_bytes())
                json_backup = Path(handle.name)
        try:
            markdown_temp.replace(markdown_path)
            json_temp.replace(json_path)
        except Exception:
            for final_path, backup_path in (
                (markdown_path, markdown_backup),
                (json_path, json_backup),
            ):
                if backup_path is None:
                    final_path.unlink(missing_ok=True)
                else:
                    backup_path.replace(final_path)
            raise
        return markdown_path, json_path
    finally:
        if markdown_temp is not None:
            markdown_temp.unlink(missing_ok=True)
        if json_temp is not None:
            json_temp.unlink(missing_ok=True)
        if markdown_backup is not None:
            markdown_backup.unlink(missing_ok=True)
        if json_backup is not None:
            json_backup.unlink(missing_ok=True)


def _artifact_stem(
    *, run_date: str, revision: bool, reports_dir: Path, data_dir: Path
) -> str:
    if not revision:
        return run_date
    number = 1
    while True:
        stem = f"{run_date}-r{number}"
        receipt_path = _receipt_path(data_dir, stem)
        markdown_path = reports_dir / f"{stem}.md"
        json_path = reports_dir / f"{stem}.json"
        if _legacy_sent_pair_matches(
            receipt_path, stem, markdown_path, json_path
        ):
            number += 1
            continue
        receipt = _read_delivery_receipt(receipt_path, artifact_stem=stem)
        if receipt is not None:
            if receipt["status"] != "sent" or not _final_pair_matches(
                receipt, markdown_path, json_path
            ):
                return stem
        elif markdown_path.exists() and json_path.exists():
            markdown_path.read_text(encoding="utf-8")
            json.loads(json_path.read_text(encoding="utf-8"))
        else:
            return stem
        number += 1


def _receipt_path(data_dir: Path, artifact_stem: str) -> Path:
    return data_dir / "trend_a_share/delivery" / f"{artifact_stem}.json"


def _final_pair_matches(
    receipt: Mapping[str, object], markdown_path: Path, json_path: Path
) -> bool:
    try:
        return (
            markdown_path.read_text(encoding="utf-8") == receipt["markdown"]
            and json_path.read_text(encoding="utf-8") == receipt["report_json"]
        )
    except (OSError, UnicodeError):
        return False


def _legacy_sent_pair_matches(
    receipt_path: Path,
    artifact_stem: str,
    markdown_path: Path,
    json_path: Path,
) -> bool:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        report_json = json_path.read_text(encoding="utf-8")
        json.loads(report_json)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict) or any(
        key in receipt for key in ("markdown", "report_json")
    ):
        return False
    return (
        receipt.get("status") == "sent"
        and receipt.get("artifact_stem") == artifact_stem
        and all(
            receipt.get(key) == value
            for key, value in _payload_hashes(markdown, report_json).items()
        )
    )


def _notify_status(notifier: Notifier, title: str, message: str) -> None:
    send_notification_with_results(
        notifier,
        title,
        message,
        channels={"macos"},
    )


def _notify_delivery_status(
    notifier: Notifier, *, run_date: str, delivery_status: str
) -> None:
    if delivery_status in {"sent", "sent_prior_attempt", "sent_prior_message"}:
        title = "A股趋势计划已生成"
    elif delivery_status == "delivery_unknown":
        title = "A股趋势计划交付状态未知"
    else:
        title = "A股趋势计划发送失败"
    _notify_status(
        notifier,
        title,
        f"{run_date} 本地报告已冻结；飞书状态：{delivery_status}",
    )


def _deliver_a_share_daily_text(
    *,
    config: DailyPremarketConfig,
    notifier: Notifier,
    run_date: str,
    payload: Mapping[str, object],
) -> str:
    title, message = render_trend_feishu_text(
        payload, broker_label="东方财富", market_label="A股"
    )
    return deliver_daily_trend_text(
        notifier,
        ledger_path=(
            config.data_dir / "trend_a_share/daily_delivery" / f"{run_date}.json"
        ),
        title=title,
        message=message,
    )


def _recover_receipt_report(
    *,
    config: DailyPremarketConfig,
    run_date: str,
    artifact_stem: str,
    notifier: Notifier,
) -> AShareTrendRunResult | None:
    receipt_path = _receipt_path(config.data_dir, artifact_stem)
    receipt = _read_delivery_receipt(
        receipt_path,
        artifact_stem=artifact_stem,
    )
    if receipt is None:
        return None
    prior_status = str(receipt["status"])
    if prior_status in {"prepared", "pending", "delivery_failed"}:
        if prior_status == "prepared":
            write_protection_state(
                config.data_dir / "trend_a_share/protection_state.json",
                receipt["protection_state"],  # type: ignore[arg-type]
            )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status="pending",
            delivery_status="pending",
        )
        payload = json.loads(str(receipt["report_json"]))
        if not isinstance(payload, dict):
            raise ValueError("delivery receipt report JSON must be an object")
        delivery_status = _deliver_a_share_daily_text(
            config=config,
            notifier=notifier,
            run_date=run_date,
            payload=payload,
        )
        receipt_status = (
            "sent"
            if delivery_status in {"sent", "sent_prior_message"}
            else delivery_status
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status=receipt_status,
            delivery_status=delivery_status,
        )
    elif prior_status == "sent":
        delivery_status = "sent_prior_attempt"
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status="sent",
            delivery_status=delivery_status,
        )
    else:
        delivery_status = "delivery_unknown"
        receipt = _transition_delivery_receipt(
            receipt_path, receipt, status=prior_status, delivery_status=delivery_status
        )
    markdown_path, json_path = _freeze_receipt_report(
        receipt=receipt,
        reports_dir=config.reports_dir / "trend_a_share",
        artifact_stem=artifact_stem,
    )
    _notify_delivery_status(
        notifier,
        run_date=run_date,
        delivery_status=delivery_status,
    )
    return AShareTrendRunResult("generated", markdown_path, json_path)


def _status_date(row: Mapping[str, object]) -> str:
    for key in ("asOfDate", "updateDate", "latestDate", "date"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _updates_ready(rows: Sequence[Mapping[str, object]], run_date: str) -> bool:
    dates = {
        row.get("asset"): _status_date(row)
        for row in rows
        if row.get("asset") in {"A股", "ETF基金"}
    }
    return dates == {"A股": run_date, "ETF基金": run_date}


def _row_tm_id(row: Mapping[str, object]) -> int:
    value = row.get("tmId")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TrendAnimalsError("Trend Animals returned an invalid tmId")
    return value


def _billing_field(row: Mapping[str, object]) -> str:
    for key in ("field", "fieldName", "column", "columnName"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _billing_price(row: Mapping[str, object]) -> Decimal:
    for key in ("priceCost", "price", "cost", "unitPrice", "billing"):
        if key in row:
            try:
                value = _decimal(row[key])
            except ValueError:
                raise TrendAnimalsError("snapshot billing returned an invalid price") from None
            if value < 0:
                raise TrendAnimalsError("snapshot billing returned a negative price")
            return value
    raise TrendAnimalsError("snapshot billing returned no price")


def _unified_trend_unit_cost(
    billing: Mapping[str, Mapping[str, object]],
) -> Decimal:
    cost = sum(
        (_billing_price(billing[field]) for field in UNIFIED_TREND_FIELDS),
        Decimal("0"),
    )
    if cost != UNIFIED_TREND_UNIT_COST:
        raise TrendAnimalsError(
            "unified Trend Animals catalog cost must be "
            f"{UNIFIED_TREND_UNIT_COST}, got {cost}"
        )
    return cost


def _is_systemic_futu_error(exc: FutuQuoteError) -> bool:
    return exc.error_type in {
        "opend_unreachable",
        "context_failed",
        "quote_server_interrupted",
    }


def _balance(row: Mapping[str, object]) -> Decimal:
    for key in ("balance", "remainingBalance", "amount"):
        if key in row:
            try:
                return _decimal(row[key])
            except ValueError:
                break
    raise TrendAnimalsError("getAccountBalance returned no valid balance")


def _holding_snapshot(
    row: Mapping[str, object],
    *,
    market: str = "CN",
    industry_temperature: str | None = None,
    bars: Sequence[DailyKlineBar] = (),
) -> HoldingSnapshot:
    symbol, exchange = _symbol_parts(row.get("tickerSymbol"), market=market)
    paid_expansion = _paid_expansion_fields(row, bars)
    return HoldingSnapshot(
        tm_id=_row_tm_id(row),
        symbol=symbol,
        exchange=exchange,
        name=_optional_text(row.get("tickerName")),
        as_of_date=str(row.get("asOfDate") or "").strip(),
        right_side=(
            row.get("isTrendRightSide")
            if isinstance(row.get("isTrendRightSide"), bool)
            else None
        ),
        danger=(
            row.get("stopwinFlagByDangerSignal")
            if isinstance(row.get("stopwinFlagByDangerSignal"), bool)
            else None
        ),
        boiling=(
            row.get("stopwinFlagByBoilingTemperature")
            if isinstance(row.get("stopwinFlagByBoilingTemperature"), bool)
            else None
        ),
        champagne=(
            row.get("stopwinFlagByPopChampagne")
            if isinstance(row.get("stopwinFlagByPopChampagne"), bool)
            else None
        ),
        industry=str(row.get("industryName") or "").strip(),
        industry_tm_id=_optional_int(row.get("industryTmId")),
        industry_temperature=industry_temperature,
        filter_price=_optional_decimal(row.get("priceIndex")),
        market_cap=_optional_decimal(row.get("marketCap")),
        strength=_optional_decimal(row.get("trendStrengthLocalCurr")),
        temperature_prev=(
            str(row["trendTemperaturePrev"])
            if row.get("trendTemperaturePrev") in KNOWN_TEMPERATURES
            else None
        ),
        temperature_curr=(
            str(row["trendTemperatureCurr"])
            if row.get("trendTemperatureCurr") in KNOWN_TEMPERATURES
            else None
        ),
        phase=_optional_text(row.get("trendPhaseCurr")),
        days=_optional_int(row.get("daysSinceTrendEntry")),
        **paid_expansion,
    )


def _attempt_report(
    *,
    config: DailyPremarketConfig,
    run_date: str,
    artifact_stem: str,
    process_version: str,
    api_factory: Callable[..., object],
    quote_factory: Callable[..., object],
    account_factory: Callable[..., object],
    notifier: Notifier,
) -> AShareTrendRunResult:
    run_day = date.fromisoformat(run_date)
    quote = quote_factory(host=config.futu_host, port=config.futu_port)
    try:
        calendar = quote.get_cn_trading_days(
            start=run_date,
            end=(run_day + timedelta(days=14)).isoformat(),
        )
        if run_date not in calendar:
            return AShareTrendRunResult("holiday", None, None)
        execution_dates = sorted(item for item in calendar if item > run_date)
        if not execution_dates:
            raise FutuQuoteError("Futu CN calendar has no later trading day")
        execution_date = execution_dates[0]

        api = api_factory(
            api_key=config.trend_animals_api_key,
            cache_dir=config.data_dir / "trend_animals/cache",
        )
        update_rows = api.get_update_status()
        if not _updates_ready(update_rows, run_date):
            return AShareTrendRunResult("waiting", None, None)

        balance_before = _balance(api.get_account_balance())
        component_rows = []
        component_pools: defaultdict[int, set[str]] = defaultdict(set)
        for tm_id in (
            config.trend_animals_a_share_tm_id,
            config.trend_animals_etf_tm_id,
        ):
            rows = api.get_components(tm_id=tm_id, expected_date=run_date)
            component_rows.extend(rows)
            for row in rows:
                component_pools[_row_tm_id(row)].add(str(tm_id))
        component_ids = {_row_tm_id(row) for row in component_rows}

        simulate_acc_id = require_trend_review_config(config, "CN")
        account = load_futu_simulate_trend_account(
            host=config.futu_host,
            port=config.futu_port,
            simulate_acc_id=simulate_acc_id,
            market="CN",
            expected_date=run_date,
            account_factory=account_factory,
        )
        holding_ids: dict[str, int] = {}
        for position in account.positions:
            try:
                holding_ids[position.symbol] = api.search_exact_symbol(position.symbol)
            except TrendAnimalsLookupError:
                continue

        requested_ids = sorted(component_ids | set(holding_ids.values()))
        fields = UNIFIED_TREND_FIELDS
        billing_rows = api.get_snapshot_billing()
        billing = {_billing_field(row): row for row in billing_rows}
        requested_fields = tuple(dict.fromkeys(fields + A_SHARE_INDUSTRY_FIELDS))
        missing_billing = [field for field in requested_fields if field not in billing]
        if missing_billing:
            raise TrendAnimalsError(
                "getSnapshotColumnBilling missing requested field(s): "
                + ", ".join(missing_billing)
            )
        unified_unit_cost = _unified_trend_unit_cost(billing)
        snapshot_rows = (
            api.get_snapshots(
                tm_ids=requested_ids,
                fields=fields,
                expected_date=run_date,
            )
            if requested_ids
            else []
        )
        returned_ids = [_row_tm_id(row) for row in snapshot_rows]
        if len(returned_ids) != len(set(returned_ids)) or sorted(
            returned_ids
        ) != requested_ids:
            raise TrendAnimalsError("getTickerSnapshot returned mismatched tmIds")
        if any(row.get("asOfDate") != run_date for row in snapshot_rows):
            raise TrendAnimalsError("getTickerSnapshot returned a stale data date")
        industry_ids = sorted(
            {
                value
                for row in snapshot_rows
                if isinstance((value := row.get("industryTmId")), int)
                and not isinstance(value, bool)
                and value > 0
            }
        )
        industry_rows = (
            api.get_snapshots(
                tm_ids=industry_ids,
                fields=A_SHARE_INDUSTRY_FIELDS,
                expected_date=run_date,
            )
            if industry_ids
            else []
        )
        returned_industry_ids = [_row_tm_id(row) for row in industry_rows]
        if (
            len(returned_industry_ids) != len(set(returned_industry_ids))
            or any(tm_id not in industry_ids for tm_id in returned_industry_ids)
        ):
            raise TrendAnimalsError("industry snapshot returned mismatched tmIds")
        if any(row.get("asOfDate") != run_date for row in industry_rows):
            raise TrendAnimalsError("industry snapshot returned a stale data date")
        industry_temperatures = {
            _row_tm_id(row): (
                str(row["trendTemperatureCurr"])
                if row.get("trendTemperatureCurr") in KNOWN_TEMPERATURES
                else None
            )
            for row in industry_rows
        }
        balance_after = _balance(api.get_account_balance())

        candidates: list[CandidateInput] = []
        holding_snapshots: dict[str, HoldingSnapshot | None] = {
            position.symbol: None for position in account.positions
        }
        rows_by_tm_id = {_row_tm_id(row): row for row in snapshot_rows}
        kline_start = (run_day - timedelta(days=90)).isoformat()
        bars_by_symbol: dict[str, Sequence[DailyKlineBar] | None] = {}
        for tm_id in sorted(component_ids):
            row = rows_by_tm_id.get(tm_id)
            if row is None:
                continue
            try:
                symbol, exchange = _symbol_parts(row.get("tickerSymbol"))
                daily_bars = quote.get_daily_kline(
                    f"{exchange}.{symbol}", start=kline_start, end=run_date
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                daily_bars = None
            except ValueError:
                daily_bars = None
            candidates.append(
                evaluate_candidate(
                    row,
                    daily_bars,
                    pools=component_pools[tm_id],
                    industry_temperature=industry_temperatures.get(
                        _optional_int(row.get("industryTmId"))
                    ),
                )
            )
        for symbol, tm_id in holding_ids.items():
            row = rows_by_tm_id.get(tm_id)
            daily_bars = None
            try:
                futu_symbol = to_futu_symbol("CN", symbol)
                if row is not None:
                    _, exchange = _symbol_parts(row.get("tickerSymbol"))
                    futu_symbol = f"{exchange}.{symbol}"
                daily_bars = quote.get_daily_kline(
                    futu_symbol, start=kline_start, end=run_date
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                daily_bars = None
            except ValueError:
                daily_bars = None
            bars_by_symbol[symbol] = daily_bars
            if row is not None:
                try:
                    holding_snapshots[symbol] = _holding_snapshot(
                        row,
                        industry_temperature=industry_temperatures.get(
                            _optional_int(row.get("industryTmId"))
                        ),
                        bars=tuple(daily_bars or ()),
                    )
                except ValueError:
                    holding_snapshots[symbol] = None

        estimated_cost = unified_unit_cost * len(requested_ids) + sum(
            (
                _billing_price(billing[field])
                for field in A_SHARE_INDUSTRY_FIELDS
            ),
            Decimal("0"),
        ) * len(industry_ids)
        balance_delta = balance_before - balance_after
        actual_cost = balance_delta if balance_delta >= 0 else None
        cache_events = tuple(getattr(api, "paid_cache_events", ()))
        cache_metadata = {
            "hits": sum(event.get("cache") == "hit" for event in cache_events),
            "misses": sum(event.get("cache") == "miss" for event in cache_events),
            "events": [dict(event) for event in cache_events],
        }
        prior_state = load_protection_state(
            config.data_dir / "trend_a_share/protection_state.json"
        )
        watch_events = load_watch_events(
            config.data_dir / "trend_a_share/watch_events.jsonl"
        )
        try:
            kelly_rounds = load_trend_kelly_rounds(config.data_dir)
            kelly_data_reason = ""
        except ValueError as exc:
            kelly_rounds = ()
            kelly_data_reason = f"Kelly 模拟闭环统计不可用，暂停新开仓：{exc}"
        generated_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        strategy_snapshot = live_trend_strategy_snapshot(
            "CN",
            process_version,
            (
                config.trend_animals_a_share_tm_id,
                config.trend_animals_etf_tm_id,
            ),
        )
        drawdown_summary = observe_strategy_equity(
            config.data_dir,
            market="CN",
            strategy_id=str(strategy_snapshot["strategy_id"]),
            strategy_version=str(strategy_snapshot["strategy_version"]),
            current_equity=account.net_value,
            observed_at=generated_at,
        )
        report = build_report(
            as_of_date=run_date,
            execution_date=execution_date,
            account=account,
            candidates=candidates,
            holding_snapshots=holding_snapshots,
            bars_by_symbol=bars_by_symbol,
            prior_state=prior_state,
            watch_events=watch_events,
            api_facts=(
                f"getUpdateStatus rows={len(update_rows)}",
                *_component_api_facts(api, len(component_rows)),
                f"getTickerSnapshot fields={','.join(fields)} rows={len(snapshot_rows)} cache=client-managed",
                f"getTickerSnapshot industries fields={','.join(A_SHARE_INDUSTRY_FIELDS)} rows={len(industry_rows)} cache=client-managed",
            ),
            data_sources=(
                "Trend Animals",
                "Futu CN calendar/QFQ daily K-line",
                "Futu CN SIMULATE account",
            ),
            estimated_api_cost=estimated_cost,
            actual_api_cost=actual_cost,
            generated_at=generated_at,
            position_weight=Decimal("0.04"),
            position_weight_source="fallback_4pct",
            process_version=process_version,
            candidate_pool_ids=(
                config.trend_animals_a_share_tm_id,
                config.trend_animals_etf_tm_id,
            ),
            strategy_snapshot=strategy_snapshot,
            drawdown_summary=drawdown_summary,
            metadata={
                "market": "CN",
                "broker": "eastmoney",
                "simulate_acc_id": simulate_acc_id,
                "run_date": run_date,
                "paid_response_cache": cache_metadata,
            },
            kelly_rounds=kelly_rounds,
            kelly_data_reason=kelly_data_reason,
        )
        report = replace(
            report,
            metadata={
                **report.metadata,
                "delivery_status": "prepared",
                "process_version": process_version,
            },
        )
        evidence = freeze_report_evidence(
            data_dir=config.data_dir,
            report=report,
            candidates=candidates,
            holding_snapshots=holding_snapshots,
            bars_by_symbol=bars_by_symbol,
            prior_state=prior_state,
            watch_events=watch_events,
            query={
                "component_pool_ids": [
                    config.trend_animals_a_share_tm_id,
                    config.trend_animals_etf_tm_id,
                ],
                "snapshot_fields": list(fields),
                "industry_fields": list(A_SHARE_INDUSTRY_FIELDS),
            },
            responses={
                "update_status": update_rows,
                "components": component_rows,
                "snapshots": snapshot_rows,
                "industries": industry_rows,
            },
            candidate_pool_ids=(
                config.trend_animals_a_share_tm_id,
                config.trend_animals_etf_tm_id,
            ),
            lot_sizes={},
            price_fx_to_account_currency=Decimal("1"),
            previous_attention_rows=(),
            option_attention_broker_label=None,
            kelly_rounds=kelly_rounds,
            kelly_data_reason=kelly_data_reason,
        )
        report = replace(
            report,
            replay_evidence={
                "path": str(Path(evidence["path"]).relative_to(config.data_dir)),
                "sha256": evidence["sha256"],
            },
        )
        receipt_path = _receipt_path(config.data_dir, artifact_stem)
        payload = _report_payload(report)
        receipt = _write_delivery_receipt(
            receipt_path,
            status="prepared",
            generated_at=report.generated_at,
            artifact_stem=artifact_stem,
            markdown=render_markdown(report),
            report_json=(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ),
            protection_state=report.protection_state,
        )
        write_protection_state(
            config.data_dir / "trend_a_share/protection_state.json",
            report.protection_state,
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status="pending",
            delivery_status="pending",
        )
        delivery_status = _deliver_a_share_daily_text(
            config=config,
            notifier=notifier,
            run_date=run_date,
            payload=payload,
        )
        receipt_status = (
            "sent"
            if delivery_status in {"sent", "sent_prior_message"}
            else delivery_status
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status=receipt_status,
            delivery_status=delivery_status,
        )
        markdown_path, json_path = _freeze_receipt_report(
            receipt=receipt,
            reports_dir=config.reports_dir / "trend_a_share",
            artifact_stem=artifact_stem,
        )
        _notify_delivery_status(
            notifier,
            run_date=run_date,
            delivery_status=delivery_status,
        )
        return AShareTrendRunResult("generated", markdown_path, json_path)
    finally:
        close = getattr(quote, "close", None)
        if callable(close):
            close()


def run_a_share_trend_report(
    *,
    config: DailyPremarketConfig,
    run_date: str,
    revision: bool = False,
    now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
    sleep_fn: Callable[[float], None] = sleep,
    api_factory: Callable[..., object] = TrendAnimalsClient,
    quote_factory: Callable[..., object] = FutuQuoteClient,
    account_factory: Callable[..., object] | None = None,
    notifier: Notifier | None = None,
) -> AShareTrendRunResult:
    run_day = date.fromisoformat(run_date)
    notifier = notifier or NullNotifier()
    if config.trend_animals_a_share_tm_id != 622466:
        raise ValueError("TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID must be 622466")
    if config.trend_animals_etf_tm_id != 697199:
        raise ValueError("TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID must be 697199")
    report_dir = config.reports_dir / "trend_a_share"
    base_markdown = report_dir / f"{run_date}.md"
    base_json = report_dir / f"{run_date}.json"
    with RunLock(config.data_dir / "runs/.trend_a_share_report.lock"):
        artifact_stem = _artifact_stem(
            run_date=run_date,
            revision=revision,
            reports_dir=report_dir,
            data_dir=config.data_dir,
        )
        if not revision and _legacy_sent_pair_matches(
            _receipt_path(config.data_dir, artifact_stem),
            artifact_stem,
            base_markdown,
            base_json,
        ):
            return AShareTrendRunResult("existing", base_markdown, base_json)
        receipt_path = _receipt_path(config.data_dir, artifact_stem)
        receipt = _read_delivery_receipt(
            receipt_path,
            artifact_stem=artifact_stem,
        )
        if not revision and base_markdown.exists() and base_json.exists():
            if receipt is None:
                base_markdown.read_text(encoding="utf-8")
                json.loads(base_json.read_text(encoding="utf-8"))
                return AShareTrendRunResult("existing", base_markdown, base_json)
            if receipt["status"] == "sent" and _final_pair_matches(
                receipt, base_markdown, base_json
            ):
                return AShareTrendRunResult("existing", base_markdown, base_json)
        recovered = _recover_receipt_report(
            config=config,
            run_date=run_date,
            artifact_stem=artifact_stem,
            notifier=notifier,
        )
        if recovered is not None:
            return recovered
        version = _process_version(config.repo)
        log_path = config.logs_dir / "trend_a_share" / f"{run_date}.log"
        deadline = datetime.combine(run_day, time(18, 0), tzinfo=SHANGHAI)
        notified_waiting = False
        last_error = "Trend Animals update status is not ready"
        _write_run_log(
            log_path,
            {"event": "start", "process_version": version, "run_date": run_date},
            append=False,
        )
        while True:
            try:
                attempt = _attempt_report(
                    config=config,
                    run_date=run_date,
                    artifact_stem=artifact_stem,
                    process_version=version,
                    api_factory=api_factory,
                    quote_factory=quote_factory,
                    account_factory=(
                        account_factory or FutuSimulateOrderExecutionClient
                    ),
                    notifier=notifier,
                )
                if attempt.status in {"generated", "existing", "holiday"}:
                    return attempt
                last_error = "Trend Animals update status is not ready"
            except (TrendAnimalsError, FutuQuoteError, ValueError, RuntimeError) as exc:
                last_error = _redact_api_key(exc, config.trend_animals_api_key)
            _write_run_log(
                log_path,
                {"event": "retry", "error": last_error, "run_date": run_date},
                append=True,
            )
            now = now_fn()
            if now >= deadline:
                title, message = render_trend_failure_text(
                    broker_label="东方财富",
                    market_label="A股",
                    report_date=run_date,
                    reason=(
                        "趋势数据在截止时间前仍未更新"
                        if "not ready" in last_error.lower()
                        else "趋势报告生成失败，需检查运行日志"
                    ),
                    recovery_action=(
                        "确认 Trend Animals 数据状态后手动重跑东方财富报告"
                    ),
                )
                deliver_daily_trend_text(
                    notifier,
                    ledger_path=(
                        config.data_dir
                        / "trend_a_share/daily_delivery"
                        / f"{run_date}.json"
                    ),
                    title=title,
                    message=message,
                )
                _notify_status(notifier, "A股趋势计划失败", last_error)
                return AShareTrendRunResult("failed", None, None)
            if not notified_waiting:
                _notify_status(notifier, "A股趋势数据等待中", last_error)
                notified_waiting = True
            sleep_fn(min(600.0, max(1.0, (deadline - now).total_seconds())))
