from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
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
from .broker_details import load_broker_detail_snapshot
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import from_trend_animals_symbol, to_futu_symbol
from .kelly_order_execution import FutuSimulateOrderExecutionClient
from .kline_technical_facts import DailyKlineBar
from .notifications import Notifier, NullNotifier
from .notification_policy import render_attention, render_daily_title
from .portfolio_risk import size_entry_by_risk
from .parsers.base import detect_asset_class
from .strategy_drawdown import (
    ALLOCATION_DYNAMIC_PARAMETER_NAMES,
    ALLOCATION_PROJECTION_VERSIONS,
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
from .trend_industry_context import (
    IndustryContext,
    KNOWN_TEMPERATURES as INDUSTRY_KNOWN_TEMPERATURES,
    attach_prior_context,
    calculate_industry_context,
    load_latest_prior_context,
    write_industry_context_history,
    _context_from_mapping,
)
from .trend_animals import (
    SEARCH_ASSETS_BY_MARKET,
    TREND_SYMBOL_MAPPING_SCHEMA,
    TrendAnimalsClient,
    TrendAnimalsError,
    TrendAnimalsLookupError,
    TrendAnimalsNoCurrentRowsError,
)
from .trend_delivery import deliver_daily_trend_text
from .trend_review import (
    _floor_to_lot,
    freeze_report_evidence,
    normalize_trend_strategy_snapshot,
    rebuild_overheat_trim_projection,
    reserve_rotation_pairs,
    TREND_V1_EFFECTIVE_FROM,
)


NO_ACTION_TEXT = "现金也是有效仓位，本日无需交易。"
DISCLAIMER_TEXT = (
    "本报告是确定性纪律清单，不是订单或成交事实；所有交易由用户人工确认与执行。"
)
NON_REALTIME_ACCOUNT_WARNING = "账户数据非实时，执行前核对现金与持仓"
STALE_TIGER_ACCOUNT_WARNING = "账户数据非实时，禁止新增买入；持仓需复核"
TREND_API_COST_UNIT = "Trend Animals 余额单位"
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
INDUSTRY_MEMBER_FIELDS = (
    "tmId",
    "asOfDate",
    "tradableFlag",
    "isTrendRightSide",
)
INDUSTRY_STATE_FIELDS = (
    "tmId",
    "asOfDate",
    "trendTemperatureCurr",
    "trendStrengthLocalCurr",
    "TrendRightSideCountRatio",
    "TrendRightSideMktCapRatio",
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
LEGACY_CN_TARGET_WEIGHTS = {"热": Decimal("0.04"), "沸": Decimal("0.02")}
CN_TARGET_WEIGHTS = {"热": Decimal("0.04"), "沸": Decimal("0.04")}
CURRENT_TREND_STRATEGY_VERSIONS = {"CN": "v10", "US": "v8", "HK": "v8"}
ALLOCATION_REPORT_VERSIONS = {
    "CN": frozenset({"v11", "v12"}),
    "HK": frozenset({"v9", "v10"}),
    "US": frozenset({"v9", "v10"}),
}
CURRENT_TREND_EFFECTIVE_FROM = "2026-07-27"
CURRENT_ENTRY_DISCIPLINES = frozenset({
    ("US", "v8"),
    ("US", "v9"),
    ("US", "v10"),
    ("HK", "v8"),
    ("HK", "v9"),
    ("HK", "v10"),
})
CURRENT_EXIT_DISCIPLINES = frozenset({
    ("CN", "v9"),
    ("CN", "v10"),
    ("CN", "v11"),
    ("CN", "v12"),
    ("US", "v6"),
    ("US", "v7"),
    ("US", "v8"),
    ("US", "v9"),
    ("US", "v10"),
    ("HK", "v6"),
    ("HK", "v7"),
    ("HK", "v8"),
    ("HK", "v9"),
    ("HK", "v10"),
})
REAL_HOLDING_TREND_EXCLUDED_SYMBOLS = frozenset({"US.AGRZ"})
OVERHEAT_PARAMETER_NAMES = frozenset({
    "overheat_trim_fraction",
    "overheat_trim_once_per_position",
    "overheat_trim_signals",
    "overheat_trim_rounding",
    "overheat_trim_below_lot",
    "full_exit_precedes_partial_exit",
    "trailing_low_days",
})
OVERHEAT_ROW_NAMES = frozenset({
    "过热止盈比例",
    "过热止盈信号",
    "过热止盈次数",
    "过热止盈取整",
    "不足一手处理",
    "清仓优先级",
    "过热跟踪",
})
DEFAULT_TARGET_WEIGHT = Decimal("0.04")
SINGLE_ENTRY_RISK_LIMIT = Decimal("0.004")
PORTFOLIO_RISK_LIMIT = Decimal("0.04")
ABNORMAL_LOSS_BUFFER = Decimal("0.01")
NORMAL_COST_RATE = Decimal("0.001")
NORMAL_COST_MODEL = "预计完整开平仓正常成本按名义金额计提"
OVERHEAT_TRIM_FRACTION = Decimal("0.30")
OVERHEAT_TRIM_SIGNALS = ("boiling", "champagne")
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
CN_ALLOWED_INDUSTRY_TEMPERATURES = {"温", "热", "沸"}
KNOWN_TEMPERATURES = {"凉", "平", "温", "热", "沸"}
CNY_PER_LOCAL_CURRENCY = {
    "CN": Decimal("1"),
    "US": Decimal("7.85") / Decimal("1.08"),
    "HK": Decimal("1") / Decimal("1.08"),
}
MARKET_CURRENCY = {"CN": "CNY", "US": "USD", "HK": "HKD"}
MARKET_V5_EFFECTIVE_FROM = {"US": "2026-07-24", "HK": "2026-07-27"}
KNOWN_TEMPERATURE_ORDER = {
    value: index for index, value in enumerate(INDUSTRY_KNOWN_TEMPERATURES)
}
TEMPERATURE_DIRECTION_ORDER = {"rising": 0, "unchanged": 1, "falling": 2}
INDUSTRY_ORDERING_MODES = {
    "context_with_history",
    "context_current_only",
    "legacy_invalid_current",
    "legacy_no_eligible_candidates",
}

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


def _uses_shared_entry_discipline(
    market: str,
    strategy_version: str | None,
) -> bool:
    normalized_market = market.upper()
    return normalized_market == "CN" or (
        normalized_market,
        strategy_version,
    ) in CURRENT_ENTRY_DISCIPLINES


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
        "overheat_trim_fraction": str(OVERHEAT_TRIM_FRACTION),
        "overheat_trim_once_per_position": True,
        "overheat_trim_signals": list(OVERHEAT_TRIM_SIGNALS),
        "overheat_trim_rounding": "floor_to_market_lot",
        "overheat_trim_below_lot": "no_order_terminal",
        "full_exit_precedes_partial_exit": True,
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
            "target_weight": {
                key: str(value) for key, value in LEGACY_CN_TARGET_WEIGHTS.items()
            },
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
            ("仓位执行", "买入数量", "使用已有现金，按 100 股整数倍向下取整"),
            ("仓位执行", "买入窗口", "下一交易日 09:30–10:00"),
            ("退出保护", "初始保护线", "成交均价减 2.0 倍 ATR14"),
            ("退出保护", "退出条件", "危险信号、离开趋势右侧、温度转平或触发保护线时全部卖出"),
            ("退出保护", "过热止盈比例", "沸腾或开香槟时减仓 30%"),
            ("退出保护", "过热止盈信号", "沸腾、开香槟合并为一次机会"),
            ("退出保护", "过热止盈次数", "每个完整持仓生命周期最多一次"),
            ("退出保护", "过热止盈取整", "按市场最小交易单位向下取整"),
            ("退出保护", "不足一手处理", "不下单并记为本生命周期终态"),
            ("退出保护", "清仓优先级", "强制清仓优先于过热止盈"),
            (
                "退出保护",
                "过热跟踪",
                "沸腾或开香槟触发后，活动保护线取原值与此前 5 个完整交易日最低价的较高者，只升不降",
            ),
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
                "使用已有现金，按 1 股整数倍向下取整"
                if market == "US"
                else "使用已有现金，按 Futu 返回的每标的整手股数向下取整",
            ),
            (
                "仓位执行",
                "买入窗口",
                "美股常规交易时段" if market == "US" else "下一交易日 09:30–10:00",
            ),
            ("退出保护", "初始保护线", "成交均价减 2.0 倍 ATR14"),
            ("退出保护", "退出条件", "危险信号、离开趋势右侧或触发保护线时全部卖出"),
            ("退出保护", "过热止盈比例", "沸腾或开香槟时减仓 30%"),
            ("退出保护", "过热止盈信号", "沸腾、开香槟合并为一次机会"),
            ("退出保护", "过热止盈次数", "每个完整持仓生命周期最多一次"),
            ("退出保护", "过热止盈取整", "按市场最小交易单位向下取整"),
            ("退出保护", "不足一手处理", "不下单并记为本生命周期终态"),
            ("退出保护", "清仓优先级", "强制清仓优先于过热止盈"),
            (
                "退出保护",
                "过热跟踪",
                "沸腾或开香槟触发后，活动保护线取原值与此前 5 个完整交易日最低价的较高者，只升不降",
            ),
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


def _allocation_market_for(
    allocation: Mapping[str, object] | None, market: str,
) -> dict[str, object] | None:
    if allocation is None:
        return None
    snapshot = allocation.get("snapshot")
    markets = snapshot.get("markets") if isinstance(snapshot, Mapping) else None
    market_data = markets.get(market) if isinstance(markets, Mapping) else None
    daily_path = allocation.get("daily_path")
    sha256 = allocation.get("sha256")
    if not (
        isinstance(daily_path, str)
        and daily_path.startswith("data/trend_allocation/daily/")
        and isinstance(sha256, str)
        and len(sha256) == 64
        and all(character in "0123456789abcdef" for character in sha256)
        and isinstance(market_data, Mapping)
    ):
        raise ValueError("allocation reference is invalid")
    rank = market_data.get("rank")
    entry_weight = market_data.get("entry_weight")
    nominal_weight = market_data.get("nominal_weight")
    score = market_data.get("score")
    score_source = market_data.get("score_source")
    try:
        entry = Decimal(str(entry_weight))
        nominal = Decimal(str(nominal_weight))
        parsed_score = Decimal(str(score))
    except (InvalidOperation, ValueError):
        raise ValueError("allocation reference is invalid") from None
    expected_weights = {
        1: (Decimal("0.06"), Decimal("0.60")),
        2: (Decimal("0.04"), Decimal("0.40")),
        3: (Decimal("0.02"), Decimal("0.20")),
    }
    if (
        isinstance(rank, bool)
        or rank not in expected_weights
        or not entry.is_finite()
        or not nominal.is_finite()
        or not parsed_score.is_finite()
        or (entry, nominal) != expected_weights[rank]
        or not isinstance(score_source, str)
        or not score_source
    ):
        raise ValueError("allocation reference is invalid")
    return {
        "daily_path": daily_path,
        "sha256": sha256,
        "rank": rank,
        "score": str(score),
        "score_source": score_source,
        "entry_weight": format(entry, "f"),
        "nominal_weight": format(nominal, "f"),
    }


def freeze_allocation_reference(
    allocation: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Freeze the immutable allocation fact; a moving latest pointer is never valid."""
    if allocation is None:
        return None
    from .trend_allocation import _reference, _validate_snapshot

    try:
        daily_path, sha256 = _reference({
            "daily_path": allocation.get("daily_path"),
            "sha256": allocation.get("sha256"),
        })
        snapshot = allocation.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise ValueError
        _validate_snapshot(snapshot)
        reused = allocation.get("reused", False)
        stale_days = allocation.get("stale_a_trading_days", 0)
        failure_reason = allocation.get("failure_reason")
        if failure_reason is None:
            failure_reason = ""
        if (
            type(reused) is not bool
            or isinstance(stale_days, bool)
            or not isinstance(stale_days, int)
            or stale_days < 0
        ):
            raise ValueError
        if not isinstance(failure_reason, str):
            raise ValueError
        allocation_date = str(snapshot["allocation_date"])
        if re.fullmatch(r"data/trend_allocation/daily/(\d{4}-\d{2}-\d{2})(?:-r[1-9]\d*)?\.json", daily_path).group(1) != allocation_date:  # type: ignore[union-attr]
            raise ValueError
    except Exception:
        raise ValueError("allocation reference is invalid") from None
    return {
        "daily_path": daily_path,
        "sha256": sha256,
        "allocation_date": allocation_date,
        "generated_at": str(snapshot["generated_at"]),
        "reused": reused,
        "stale_a_trading_days": stale_days,
        "failure_reason": failure_reason,
        "roots": dict(snapshot["roots"]),
        "markets": dict(snapshot["markets"]),
    }


def valid_frozen_allocation(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "daily_path", "sha256", "allocation_date", "generated_at", "reused",
        "stale_a_trading_days", "failure_reason", "roots", "markets",
    } or not isinstance(value.get("failure_reason"), str):
        return False
    snapshot = {
        "version": 1,
        "allocation_date": value.get("allocation_date"),
        "generated_at": value.get("generated_at"),
        "generator_version": "trend-allocation-v1",
        "git_sha": "0" * 40,
        "roots": value.get("roots"),
        "markets": value.get("markets"),
    }
    try:
        freeze_allocation_reference({
            "daily_path": value.get("daily_path"),
            "sha256": value.get("sha256"),
            "snapshot": snapshot,
            "reused": value.get("reused"),
            "stale_a_trading_days": value.get("stale_a_trading_days"),
            "failure_reason": value.get("failure_reason"),
        })
    except ValueError:
        return False
    return True


ROTATION_COMPARISON_OUTCOMES = frozenset({
    "planned", "gap_below_threshold", "sizing_blocked", "data_unavailable",
})


def _valid_rotation_comparison(
    value: object,
    *,
    market: str,
    pair: Mapping[str, object] | None,
    holding_signals: Mapping[str, object],
    candidate_signals: Mapping[str, object],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    required_text = (
        "sell_symbol", "sell_name", "buy_symbol", "buy_name", "reason",
    )
    if any(
        not isinstance(value.get(field), str) or not str(value[field]).strip()
        for field in required_text
    ):
        return False
    pair_index = value.get("pair_index")
    if isinstance(pair_index, bool) or pair_index not in {0, 1}:
        return False
    sell_symbol = str(value["sell_symbol"])
    buy_symbol = str(value["buy_symbol"])
    sell_asset = str(value.get("sell_asset") or "")
    buy_asset = str(value.get("buy_asset") or "")
    allowed_assets = SEARCH_ASSETS_BY_MARKET.get(market, frozenset())
    outcome = str(value.get("outcome") or "")
    basis = value.get("strength_basis")
    if basis not in {None, "local", "global"} or outcome not in ROTATION_COMPARISON_OUTCOMES:
        return False
    if outcome != "data_unavailable" and (
        sell_asset not in allowed_assets or buy_asset not in allowed_assets
    ):
        return False
    if sell_symbol == buy_symbol:
        return False
    try:
        threshold = _decimal(value.get("threshold"))
        sell_local = _optional_decimal(value.get("sell_local_strength"))
        sell_global = _optional_decimal(value.get("sell_global_strength"))
        buy_local = _optional_decimal(value.get("buy_local_strength"))
        buy_global = _optional_decimal(value.get("buy_global_strength"))
        sell_compared = _optional_decimal(value.get("sell_compared_strength"))
        buy_compared = _optional_decimal(value.get("buy_compared_strength"))
        gap = _optional_decimal(value.get("strength_gap"))
    except ValueError:
        return False
    all_values = (
        sell_local, sell_global, buy_local, buy_global,
        sell_compared, buy_compared, gap,
    )
    if any(item is not None and not item.is_finite() for item in all_values):
        return False
    if threshold != Decimal("20"):
        return False
    if basis == "local":
        if sell_asset != buy_asset or sell_compared != sell_local or buy_compared != buy_local:
            return False
    elif basis == "global":
        if sell_asset == buy_asset or sell_compared != sell_global or buy_compared != buy_global:
            return False
    elif sell_compared is not None or buy_compared is not None or gap is not None:
        return False
    if sell_compared is not None and not Decimal("0") <= sell_compared <= Decimal("100"):
        return False
    if buy_compared is not None and not Decimal("0") <= buy_compared <= Decimal("100"):
        return False
    if sell_compared is not None or buy_compared is not None:
        if sell_compared is None or buy_compared is None or gap != buy_compared - sell_compared:
            return False
    if outcome == "data_unavailable":
        if sell_compared is not None or buy_compared is not None or gap is not None:
            return False
    elif gap is None:
        return False
    elif outcome == "gap_below_threshold" and gap >= threshold:
        return False
    elif outcome in {"planned", "sizing_blocked"} and gap < threshold:
        return False
    if pair is not None and outcome == "planned":
        if (
            pair.get("pair_index") != pair_index
            or pair.get("sell_symbol") != sell_symbol
            or pair.get("buy_symbol") != buy_symbol
            or pair.get("strength_basis") != basis
        ):
            return False
        try:
            if _decimal(pair.get("strength_gap")) != gap:
                return False
        except ValueError:
            return False
    for symbol, signals in (
        (sell_symbol, holding_signals), (buy_symbol, candidate_signals),
    ):
        signal = signals.get(symbol)
        if not isinstance(signal, Mapping):
            continue
        expected_asset = signal.get("asset")
        if expected_asset is not None and str(expected_asset or "") != (
            sell_asset if symbol == sell_symbol else buy_asset
        ):
            return False
    return True


def valid_frozen_report_contract(payload: Mapping[str, object]) -> bool:
    """Validate the allocation-era fields once for every frozen-report reader."""
    allocation = payload.get("allocation")
    judgments = payload.get("strategy_judgments")
    metadata = payload.get("metadata")
    strategy_snapshot = payload.get("strategy_snapshot")
    parameters = (
        strategy_snapshot.get("parameters")
        if isinstance(strategy_snapshot, Mapping)
        else None
    )
    if allocation is None:
        if isinstance(strategy_snapshot, Mapping):
            snapshot_market = str(strategy_snapshot.get("market") or "").upper()
            metadata_market = str(
                metadata.get("market") or ""
            ).upper() if isinstance(metadata, Mapping) else ""
            if "market" in strategy_snapshot and snapshot_market not in {
                "CN", "HK", "US",
            }:
                return False
            if snapshot_market and metadata_market and snapshot_market != metadata_market:
                return False
            identity_market = snapshot_market or metadata_market
            identity_version = str(
                strategy_snapshot.get("strategy_version") or ""
            )
            current_identity = identity_version in {
                *CURRENT_TREND_STRATEGY_VERSIONS.values(),
                *ALLOCATION_PROJECTION_VERSIONS.values(),
            }
            if ("strategy_id" in strategy_snapshot or current_identity) and (
                identity_market not in {"CN", "HK", "US"}
                or not identity_version
                or strategy_snapshot.get("strategy_id")
                != f"trend_animals_warm_to_hot/{identity_market}/{identity_version}"
            ):
                return False
            if ALLOCATION_PROJECTION_VERSIONS.get(identity_market) == identity_version:
                return False
        if isinstance(parameters, Mapping) and (
            ALLOCATION_DYNAMIC_PARAMETER_NAMES - {"target_weight"}
        ) & parameters.keys():
            return False
        if isinstance(judgments, Mapping) and any(
            field in judgments
            for field in ("simulate_rotation_pairs", "real_rotation_pairs")
        ):
            return False
        return True
    if not valid_frozen_allocation(allocation):
        return False
    market_value = strategy_snapshot.get("market") if isinstance(
        strategy_snapshot, Mapping
    ) else None
    metadata_market = metadata.get("market") if isinstance(metadata, Mapping) else None
    snapshot_market = (
        market_value.upper()
        if isinstance(market_value, str)
        and market_value.upper() in {"CN", "HK", "US"}
        else None
    )
    metadata_market_text = (
        str(metadata_market).upper()
        if metadata_market is not None
        and str(metadata_market).upper() in {"CN", "HK", "US"}
        else None
    )
    if market_value is not None and snapshot_market is None:
        return False
    if snapshot_market is not None and (
        metadata_market is not None and metadata_market_text != snapshot_market
    ):
        return False
    market = snapshot_market or metadata_market_text
    if market is None:
        return False
    allocation_markets = allocation.get("markets")
    allocation_market = (
        allocation_markets.get(market)
        if isinstance(allocation_markets, Mapping)
        else None
    )
    allocation_version = ALLOCATION_PROJECTION_VERSIONS[market]
    if (
        not isinstance(strategy_snapshot, Mapping)
        or strategy_snapshot.get("strategy_version") not in ALLOCATION_REPORT_VERSIONS[market]
        or strategy_snapshot.get("strategy_id")
        != f"trend_animals_warm_to_hot/{market}/{strategy_snapshot.get('strategy_version')}"
        or snapshot_market is None
        or not isinstance(parameters, Mapping)
        or not isinstance(allocation_market, Mapping)
        or any(
            type(parameters.get(parameter_name)) is not type(expected)
            or parameters.get(parameter_name) != expected
            for parameter_name, expected in (
                ("allocation_snapshot_path", allocation.get("daily_path")),
                ("allocation_snapshot_sha256", allocation.get("sha256")),
                ("allocation_rank", allocation_market.get("rank")),
                ("allocation_score", allocation_market.get("score")),
                ("allocation_score_source", allocation_market.get("score_source")),
                ("target_weight", allocation_market.get("entry_weight")),
                ("nominal_weight", allocation_market.get("nominal_weight")),
            )
        )
    ):
        return False
    if not isinstance(judgments, Mapping):
        return False
    try:
        execution_date = str(payload["execution_date"])
        date.fromisoformat(execution_date)
    except (KeyError, TypeError, ValueError):
        return False
    holdings = judgments.get("holding_decisions")
    candidates = judgments.get("top10_candidates")
    if not isinstance(holdings, list) or not isinstance(candidates, list):
        return False
    simulate_holding_symbols = {
        item.get("symbol")
        for item in holdings
        if isinstance(item, Mapping) and isinstance(item.get("symbol"), str)
    }
    real_holdings = judgments.get("real_holding_decisions")
    real_holding_symbols = {
        item.get("symbol")
        for item in real_holdings
        if isinstance(item, Mapping) and isinstance(item.get("symbol"), str)
    } if isinstance(real_holdings, list) else set()
    candidate_symbols = {
        item.get("symbol")
        for item in candidates
        if isinstance(item, Mapping) and isinstance(item.get("symbol"), str)
    }
    strategy_version = str(
        strategy_snapshot.get("strategy_version") or ""
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    signal_snapshots = payload.get("signal_snapshots")
    signal_snapshots = signal_snapshots if isinstance(signal_snapshots, Mapping) else {}
    holding_signals = signal_snapshots.get("holdings")
    holding_signals = holding_signals if isinstance(holding_signals, Mapping) else {}
    real_holding_signals = signal_snapshots.get("real_holdings")
    real_holding_signals = (
        real_holding_signals if isinstance(real_holding_signals, Mapping) else {}
    )
    candidate_signals = {
        str(item.get("symbol")): item
        for item in candidates
        if isinstance(item, Mapping) and isinstance(item.get("symbol"), str)
    }
    current_allocation_version = ALLOCATION_PROJECTION_VERSIONS[market]
    require_comparisons = strategy_version == current_allocation_version
    for field, mode, holding_symbols in (
        ("simulate_rotation_pairs", "automatic", simulate_holding_symbols),
        ("real_rotation_pairs", "manual", real_holding_symbols),
    ):
        pairs = judgments.get(field)
        if not isinstance(pairs, list) or len(pairs) > 2:
            return False
        if field == "real_rotation_pairs" and pairs and (
            judgments.get("real_holding_decisions_status") != "available"
            or not isinstance(real_holdings, list)
        ):
            return False
        seen_indices: set[int] = set()
        seen_symbols: set[str] = set()
        for pair in pairs:
            if not isinstance(pair, Mapping):
                return False
            pair_index = pair.get("pair_index")
            if (
                isinstance(pair_index, bool)
                or not isinstance(pair_index, int)
                or pair_index not in {0, 1}
                or pair_index in seen_indices
                or pair.get("execution_mode") != mode
                or pair.get("execution_date") != execution_date
                or pair.get("reason") != "relative_rotation"
            ):
                return False
            sell_symbol, buy_symbol = pair.get("sell_symbol"), pair.get("buy_symbol")
            if (
                not isinstance(sell_symbol, str)
                or not isinstance(buy_symbol, str)
                or any(
                    not isinstance(pair.get(field), str)
                    or not pair[field].strip()
                    for field in (
                        "sell_name", "sell_futu_symbol", "buy_name", "buy_futu_symbol",
                    )
                )
                or sell_symbol not in holding_symbols
                or buy_symbol not in candidate_symbols
                or sell_symbol == buy_symbol
                or {sell_symbol, buy_symbol} & seen_symbols
            ):
                return False
            try:
                sell_strength = Decimal(str(pair.get("sell_global_strength")))
                buy_strength = Decimal(str(pair.get("buy_global_strength")))
                gap = Decimal(str(pair.get("strength_gap")))
                weight = Decimal(str(pair.get("target_weight")))
                amount = Decimal(str(pair.get("target_amount")))
                atr = Decimal(str(pair.get("atr")))
                shares = pair.get("estimated_shares")
                lot_size = pair.get("lot_size")
            except (InvalidOperation, TypeError, ValueError):
                return False
            if (
                not all(value.is_finite() for value in (
                    sell_strength, buy_strength, gap, weight, amount, atr,
                ))
                or not Decimal("0") <= sell_strength <= Decimal("100")
                or not Decimal("0") <= buy_strength <= Decimal("100")
                or gap != buy_strength - sell_strength
                or gap < Decimal("20")
                or not Decimal("0") < weight <= Decimal("1")
                or amount <= 0
                or atr <= 0
                or isinstance(shares, bool)
                or not isinstance(shares, int)
                or shares <= 0
                or isinstance(lot_size, bool)
                or not isinstance(lot_size, int)
                or lot_size <= 0
                or shares % lot_size
            ):
                return False
            if pair.get("strength_basis") is not None:
                pair_signals = (
                    real_holding_signals if field == "real_rotation_pairs"
                    else holding_signals
                )
                pair_contract = {
                    **dict(pair),
                    "outcome": "planned",
                }
                if not _valid_rotation_comparison(
                    pair_contract,
                    market=market,
                    pair=pair,
                    holding_signals=pair_signals,
                    candidate_signals=candidate_signals,
                ):
                    return False
            seen_indices.add(pair_index)
            seen_symbols.update((sell_symbol, buy_symbol))
    for comparison_field, pair_field, holding_symbols, signals in (
        (
            "simulate_rotation_comparisons",
            "simulate_rotation_pairs",
            simulate_holding_symbols,
            holding_signals,
        ),
        (
            "real_rotation_comparisons",
            "real_rotation_pairs",
            real_holding_symbols,
            real_holding_signals,
        ),
    ):
        comparisons = judgments.get(comparison_field)
        if comparisons is None:
            if require_comparisons:
                return False
            comparisons = []
        if not isinstance(comparisons, list) or len(comparisons) > 2:
            return False
        pairs = judgments.get(pair_field)
        pairs_by_index = {
            item.get("pair_index"): item
            for item in pairs
            if isinstance(item, Mapping)
        } if isinstance(pairs, list) else {}
        seen_indices: set[int] = set()
        planned_indices: set[int] = set()
        for comparison in comparisons:
            if not isinstance(comparison, Mapping):
                return False
            pair_index = comparison.get("pair_index")
            if (
                isinstance(pair_index, bool)
                or not isinstance(pair_index, int)
                or pair_index in seen_indices
                or comparison.get("sell_symbol") not in holding_symbols
                or comparison.get("buy_symbol") not in candidate_symbols
            ):
                return False
            if comparison.get("outcome") == "planned":
                planned_indices.add(pair_index)
            if not _valid_rotation_comparison(
                comparison,
                market=market,
                pair=pairs_by_index.get(pair_index),
                holding_signals=signals,
                candidate_signals=candidate_signals,
            ):
                return False
            seen_indices.add(pair_index)
        if require_comparisons and set(pairs_by_index) != planned_indices:
            return False
    return True


def live_trend_strategy_snapshot(
    market: str,
    process_version: str,
    candidate_pool_ids: Sequence[int],
    *,
    normal_cost_rate: Decimal = NORMAL_COST_RATE,
    strategy_version: str | None = None,
    execution_date: str | None = None,
    allocation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    market = market.upper()
    if market not in {"CN", "US", "HK"}:
        raise ValueError(f"unsupported trend review market: {market}")
    allocation_market = _allocation_market_for(allocation, market)
    allocation_version = ALLOCATION_PROJECTION_VERSIONS[market]
    if strategy_version is not None:
        version = strategy_version
    elif allocation_market is not None:
        version = allocation_version
    elif execution_date is None or execution_date >= CURRENT_TREND_EFFECTIVE_FROM:
        version = CURRENT_TREND_STRATEGY_VERSIONS[market]
    elif market == "CN":
        version = "v8"
    elif market in MARKET_V5_EFFECTIVE_FROM and execution_date >= MARKET_V5_EFFECTIVE_FROM[market]:
        version = "v5"
    else:
        version = "v4"
    if (
        version not in {"v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"}
        or version in {"v11", "v12"} and market != "CN"
        or version == "v5" and market == "CN"
        or version == allocation_version and allocation_market is None
    ):
        raise ValueError("unsupported live trend strategy version")
    snapshot = trend_strategy_snapshot(
        market,
        process_version,
        candidate_pool_ids,
        normal_cost_rate=normal_cost_rate,
    )
    parameters = dict(snapshot["parameters"])
    rows = [dict(row) for row in snapshot["parameter_rows"]]
    if market in {"US", "HK"} and _uses_shared_entry_discipline(market, version):
        rate = CNY_PER_LOCAL_CURRENCY[market]
        parameters.pop("min_strength_exclusive", None)
        parameters.pop("max_right_side_days_exclusive", None)
        parameters.pop("min_amount_100m", None)
        parameters.update(
            {
                "temperature_transition": {"from": ["温"], "to": ["热", "沸"]},
                "min_strength": str(CN_MIN_STRENGTH),
                "allowed_industry_temperatures": ["温", "热", "沸"],
                "allowed_phases": ["谷雨", "立夏", "夏至"],
                "min_market_cap_cny_100m": str(CN_MIN_MARKET_CAP_100M),
                "min_amount_cny_100m": str(CN_MIN_AMOUNT_100M),
                "market_value_currency": MARKET_CURRENCY[market],
                "cny_per_local_currency": str(rate),
                "requires_right_side_days": True,
            }
        )
        rows = [
            row
            for row in rows
            if row["name"]
            not in {
                "趋势温度",
                "趋势强度",
                "行业温度",
                "趋势节气",
                "总市值",
                "右侧天数",
                "单日成交额",
            }
        ]
        insert_at = next(
            (index for index, row in enumerate(rows) if row["name"] == "其他要求"),
            len(rows),
        )
        rows[insert_at:insert_at] = [
            {
                "group": "入场过滤",
                "name": "趋势温度",
                "value": "前一状态为温；当前状态为热或沸",
            },
            {"group": "入场过滤", "name": "趋势强度", "value": "不低于 95"},
            {
                "group": "入场过滤",
                "name": "行业温度",
                "value": "温、热或沸",
            },
            {
                "group": "入场过滤",
                "name": "趋势节气",
                "value": "谷雨、立夏或夏至",
            },
            {
                "group": "入场过滤",
                "name": "总市值",
                "value": "不低于人民币 100 亿元（按冻结汇率换算）",
            },
            {
                "group": "入场过滤",
                "name": "单日成交额",
                "value": "不低于人民币 2 亿元（按冻结汇率换算）",
            },
        ]
        for row in rows:
            if row["name"] == "其他要求":
                row["value"] = (
                    "趋势右侧、可交易、无危险信号、日期一致、非当前持仓、"
                    "右侧天数存在、ATR14 可计算"
                )
    if market == "CN" and version in {"v6", "v7", "v8", "v9", "v10", "v11", "v12"}:
        parameters.pop("max_filter_price", None)
        parameters["allowed_industry_temperatures"] = ["温", "热", "沸"]
        rows = [row for row in rows if row["name"] != "筛选价格"]
        for row in rows:
            if row["name"] == "行业温度":
                row["value"] = "温、热或沸"
    if market == "CN" and version in {"v9", "v10", "v11", "v12"}:
        parameters["allowed_assets"] = ["A股", "ETF基金"]
        for row in rows:
            if row["name"] == "交易市场":
                row["value"] = "沪深 A 股及境内 ETF；排除北交所、ST、*ST 和退市标记"
    if market == "CN" and version == "v7":
        parameters["kelly_sample_inherits"] = [{
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v4",
            "opening_strategy_version": "v4",
        }]
    if version == "v8" and market == "CN":
        parameters["kelly_sample_inherits"] = [
            {
                "market": "CN",
                "strategy_id": f"trend_animals_warm_to_hot/CN/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v7", "v8")
        ]
    if version == "v5":
        parameters["kelly_sample_inherits"] = [
            {
                "market": market,
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v5")
        ]
    if version in {"v5", "v8", "v9", "v10", "v11", "v12"} or (
        version in {"v6", "v7"} and market in {"US", "HK"}
    ):
        for row in rows:
            if row["name"] == "排序顺序":
                row["value"] = (
                    "行业优先（变化、温度、强度、温转热数量、右侧占比），"
                    "再按个股趋势强度、右侧天数、成交额、代码；"
                    "缺历史省略变化键，行业上下文无效时回退个股排序"
                )
        rows.extend(
            {
                "group": group,
                "name": name,
                "value": value,
            }
            for group, name, value in [
                ("候选排序", "行业温度变化", "上升、持平、下降"),
                ("候选排序", "行业温度", "冻、寒、凉、平、温、热、沸，按温度降序"),
                ("候选排序", "行业趋势强度", "行业趋势强度降序"),
                ("候选排序", "行业温转热数量", "温转热数量降序"),
                ("候选排序", "行业右侧占比变化", "右侧占比变化百分点降序"),
                ("候选排序", "行业右侧占比", "当前右侧占比降序"),
                ("候选排序", "个股趋势强度", "趋势强度降序"),
                ("候选排序", "个股右侧天数", "右侧天数升序"),
                ("候选排序", "个股成交额", "成交额降序"),
                ("候选排序", "股票代码", "股票代码升序"),
                (
                    "候选排序",
                    "行业上下文回退",
                    "当前行业上下文缺失或无效时整份报告回退旧排序；缺历史时整份报告省略历史排序键",
                ),
                (
                    "入场过滤",
                    "行业成员字段",
                    "tmId、asOfDate、tradableFlag、isTrendRightSide",
                ),
                (
                    "入场过滤",
                    "行业状态字段",
                    "tmId、asOfDate、trendTemperatureCurr、trendStrengthLocalCurr",
                ),
                (
                    "仓位执行",
                    "费用标签",
                    "实扣为余额前后差额（非负）；估算为计费目录字段价格之和；估算不完整时明确标记",
                ),
                ("仓位执行", "费用单位", "Trend Animals 余额单位"),
            ]
        )
    if market == "CN" and version == "v9":
        parameters["kelly_sample_inherits"] = [
            {
                "market": "CN",
                "strategy_id": f"trend_animals_warm_to_hot/CN/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v7", "v8", "v9")
        ]
    if market == "CN" and version == "v10":
        parameters["kelly_sample_inherits"] = [
            {
                "market": "CN",
                "strategy_id": f"trend_animals_warm_to_hot/CN/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v7", "v8", "v9", "v10")
        ]
    if market == "CN" and version == "v11":
        parameters["kelly_sample_inherits"] = [
            {
                "market": "CN",
                "strategy_id": f"trend_animals_warm_to_hot/CN/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v7", "v8", "v9", "v10", "v11")
        ]
    if market == "CN" and version == "v12":
        parameters["kelly_sample_inherits"] = [
            {
                "market": "CN",
                "strategy_id": f"trend_animals_warm_to_hot/CN/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v7", "v8", "v9", "v10", "v11")
        ]
    if version == "v6" and market in {"US", "HK"}:
        parameters["kelly_sample_inherits"] = [
            {
                "market": market,
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v5", "v6")
        ]
    if version == "v7" and market in {"US", "HK"}:
        parameters["kelly_sample_inherits"] = [
            {
                "market": market,
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v5", "v6", "v7")
        ]
    if version == "v8" and market in {"US", "HK"}:
        parameters["kelly_sample_inherits"] = [
            {
                "market": market,
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v5", "v6", "v7", "v8")
        ]
    if version == "v9" and market in {"US", "HK"}:
        parameters["kelly_sample_inherits"] = [
            {
                "market": market,
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v5", "v6", "v7", "v8", "v9")
        ]
    if version == "v10" and market in {"US", "HK"}:
        parameters["kelly_sample_inherits"] = [
            {
                "market": market,
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
                "opening_strategy_version": item,
            }
            for item in ("v4", "v5", "v6", "v7", "v8", "v9")
        ]
    current_discipline = (market, version) in CURRENT_EXIT_DISCIPLINES
    if current_discipline:
        for name in OVERHEAT_PARAMETER_NAMES:
            parameters.pop(name, None)
        parameters["exit_reasons"] = [
            "danger",
            "left_right_side",
            "temperature_to_flat",
            "protection",
        ]
        rows = [row for row in rows if row["name"] not in OVERHEAT_ROW_NAMES]
        if market == "CN":
            parameters["target_weight"] = {
                key: str(value) for key, value in CN_TARGET_WEIGHTS.items()
            }
            rows = [
                row
                for row in rows
                if row["name"] not in {"热状态仓位", "沸状态仓位"}
            ]
            buy_index = next(
                index for index, row in enumerate(rows) if row["name"] == "买入数量"
            )
            rows.insert(
                buy_index,
                {
                    "group": "仓位执行",
                    "name": "目标仓位",
                    "value": "账户净值的 4%",
                },
            )
        for row in rows:
            if row["name"] == "退出条件":
                row["value"] = "危险信号、离开趋势右侧、温度转平或触发保护线时全部卖出"
    parameters.update(
        {
            "drawdown_limit": str(DRAWDOWN_LIMIT),
            "drawdown_equity_source": "Futu SIMULATE strategy NAV",
            "drawdown_unlock": "manual_same_version_rebase",
        }
    )
    if allocation_market is not None and version in ALLOCATION_REPORT_VERSIONS[market]:
        parameters.update(
            {
                "allocation_snapshot_path": allocation_market["daily_path"],
                "allocation_snapshot_sha256": allocation_market["sha256"],
                "allocation_rank": allocation_market["rank"],
                "allocation_score": allocation_market["score"],
                "allocation_score_source": allocation_market["score_source"],
                "target_weight": allocation_market["entry_weight"],
                "nominal_weight": allocation_market["nominal_weight"],
            }
        )
        for row in rows:
            if row["name"] == "目标仓位":
                row["value"] = f"账户净值的 {Decimal(str(allocation_market['entry_weight'])):.0%}"
        rows.extend(
            {
                "group": "市场资源配置",
                "name": name,
                "value": value,
            }
            for name, value in [
                ("资源排名", str(allocation_market["rank"])),
                ("市场分数", str(allocation_market["score"])),
                ("分数来源", str(allocation_market["score_source"])),
                ("10 席位名义仓位", str(allocation_market["nominal_weight"])),
                ("配置快照", str(allocation_market["daily_path"])),
                ("配置快照 SHA-256", str(allocation_market["sha256"])),
            ]
        )
    return {
        **snapshot,
        "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
        "strategy_version": version,
        "effective_from": (
            CURRENT_TREND_EFFECTIVE_FROM
            if current_discipline
            else "2026-07-24"
            if version in {"v5", "v6", "v7", "v8"}
            else "2026-07-20"
        ),
        "parameters": parameters,
        "parameter_rows": [
            *rows,
            {
                "group": "累计回撤",
                "name": "策略累计回撤暂停",
                "value": "纪律模拟策略净值从高点回撤达到 5% 时暂停新开仓，人工解锁后重设基准",
            },
        ],
    }


def _expected_report_strategy_snapshot(
    market: str,
    process_version: str,
    candidate_pool_ids: Sequence[int],
    supplied: Mapping[str, object] | None,
) -> dict[str, object]:
    requested_version = (
        str(supplied.get("strategy_version") or "")
        if supplied is not None
        else ""
    )
    parameters = supplied.get("parameters") if supplied is not None else None
    allocation = None
    if (
        (market.upper(), requested_version) in {
            ("CN", "v11"), ("CN", "v12"),
            ("HK", "v9"), ("HK", "v10"),
            ("US", "v9"), ("US", "v10"),
        }
        and isinstance(parameters, Mapping)
    ):
        allocation = {
            "daily_path": parameters.get("allocation_snapshot_path"),
            "sha256": parameters.get("allocation_snapshot_sha256"),
            "snapshot": {
                "markets": {
                    market.upper(): {
                        "rank": parameters.get("allocation_rank"),
                        "score": parameters.get("allocation_score"),
                        "score_source": parameters.get("allocation_score_source"),
                        "entry_weight": parameters.get("target_weight"),
                        "nominal_weight": parameters.get("nominal_weight"),
                    },
                },
            },
        }
    if requested_version in {"v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"}:
        return live_trend_strategy_snapshot(
            market,
            process_version,
            candidate_pool_ids,
            strategy_version=requested_version,
            allocation=allocation,
        )
    snapshot = trend_strategy_snapshot(market, process_version, candidate_pool_ids)
    if requested_version == "v2":
        snapshot = {
            **snapshot,
            "strategy_id": f"trend_animals_warm_to_hot/{market.upper()}/v2",
            "strategy_version": "v2",
        }
    return snapshot


def _preserve_v1_replay_snapshot(snapshot: Mapping[str, object]) -> bool:
    """Keep the main-branch nominal v1 shape intact during replay."""
    if snapshot.get("strategy_version") != "v1":
        return False
    parameters = snapshot.get("parameters")
    rows = snapshot.get("parameter_rows")
    if not isinstance(parameters, Mapping) or not isinstance(rows, list):
        return False
    row_names = {
        row.get("name")
        for row in rows
        if isinstance(row, Mapping)
    }
    return (
        "single_entry_risk_limit" not in parameters
        and "portfolio_risk_limit" not in parameters
        and "normal_cost_rate" not in parameters
        and "单笔计划止损风险上限" in row_names
        and "过热止盈比例" in row_names
    )


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
    futu_symbol: str | None = None


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
    futu_symbol: str | None = None


@dataclass(frozen=True)
class CandidateDecision:
    eligible: tuple[CandidateInput, ...]
    excluded: dict[str, list[str]]
    ordering_mode: str = "legacy_no_eligible_candidates"
    industry_context_status: dict[str, object] = field(default_factory=dict)


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
    futu_symbol: str | None = None


@dataclass(frozen=True)
class RotationPair:
    pair_index: int
    sell_symbol: str
    sell_name: str
    sell_futu_symbol: str
    sell_global_strength: Decimal | None
    buy_symbol: str
    buy_name: str
    buy_futu_symbol: str
    buy_global_strength: Decimal | None
    strength_gap: Decimal
    target_weight: Decimal
    target_amount: Decimal
    estimated_shares: int
    lot_size: int
    atr: Decimal
    reason: str = "relative_rotation"
    execution_date: str = ""
    execution_mode: str = "automatic"
    sell_asset: str = ""
    sell_local_strength: Decimal | None = None
    buy_asset: str = ""
    buy_local_strength: Decimal | None = None
    strength_basis: str | None = None
    sell_compared_strength: Decimal | None = None
    buy_compared_strength: Decimal | None = None
    threshold: Decimal = Decimal("20")


@dataclass(frozen=True)
class RotationComparison:
    pair_index: int
    sell_symbol: str
    sell_name: str
    sell_asset: str
    sell_local_strength: Decimal | None
    sell_global_strength: Decimal | None
    buy_symbol: str
    buy_name: str
    buy_asset: str
    buy_local_strength: Decimal | None
    buy_global_strength: Decimal | None
    strength_basis: str | None
    sell_compared_strength: Decimal | None
    buy_compared_strength: Decimal | None
    strength_gap: Decimal | None
    threshold: Decimal = Decimal("20")
    outcome: str = "data_unavailable"
    reason: str = ""


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
    asset: str = ""
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
    phase: str | None = None
    entry_hints: tuple[str, ...] = ()
    position_started_for: str | None = None
    target_fraction: Decimal | None = None
    estimated_shares: int | None = None
    lot_size: int | None = None
    overheat_signals: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    futu_symbol: str | None = None


@dataclass(frozen=True)
class RealHoldingInput:
    status: str
    reason: str
    source: dict[str, str]
    positions: tuple[AccountPosition, ...]
    holding_snapshots: Mapping[str, HoldingSnapshot | None]
    bars_by_symbol: Mapping[str, Sequence[DailyKlineBar] | None]
    prior_state: Mapping[str, object] | None
    trend_excluded_symbols: tuple[str, ...] = ()
    net_value: Decimal = Decimal("0")
    available_cash: Decimal = Decimal("0")
    position_count: int | None = None


@dataclass(frozen=True)
class HoldingEvaluation:
    decisions: tuple[HoldingDecision, ...]
    protection_state: dict[str, object]
    industry_counts: Counter[str]
    industry_values: dict[str, Decimal]


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
    industry_contexts: tuple[IndustryContext, ...]
    industry_context_status: dict[str, object]
    estimated_api_cost_complete: bool
    drawdown_summary: dict[str, object] | None = None
    replay_evidence: dict[str, str] | None = None
    real_holdings: tuple[HoldingDecision, ...] = ()
    real_holdings_status: str | None = None
    real_holdings_reason: str = ""
    real_holdings_source: dict[str, str] = field(default_factory=dict)
    real_protection_state: dict[str, object] | None = None
    allocation: dict[str, object] | None = None
    simulate_rotation_pairs: tuple[RotationPair, ...] = ()
    simulate_rotation_comparisons: tuple[RotationComparison, ...] = ()
    real_rotation_pairs: tuple[RotationPair, ...] = ()
    real_rotation_comparisons: tuple[RotationComparison, ...] = ()


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


def _format_api_cost(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." not in text:
        return text
    fraction = text.partition(".")[2]
    # Trend Animals catalog values are millesimal; preserve that published
    # precision for sub-unit costs while dropping unrelated padding.
    if abs(value) < 1 and len(fraction) == 3:
        return text
    return text.rstrip("0").rstrip(".")


def trend_api_cost_label(
    *,
    actual: Decimal | None,
    estimated: Decimal | None,
    estimate_complete: bool,
) -> str:
    if actual is not None:
        return (
            f"本报告 API 费用：实扣 {_format_api_cost(actual)} "
            f"{TREND_API_COST_UNIT}"
        )
    if estimated is not None and estimate_complete:
        return (
            f"本报告 API 费用：估算 {_format_api_cost(estimated)} "
            f"{TREND_API_COST_UNIT}（实扣不可得）"
        )
    if estimated is not None:
        return (
            f"本报告 API 费用：未知（快照估算 {_format_api_cost(estimated)} "
            f"{TREND_API_COST_UNIT}；成分费用未计）"
        )
    return "本报告 API 费用：未知"


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


def load_real_holding_input(
    data_dir: Path,
    market: str,
    *,
    state_path: Path,
) -> RealHoldingInput:
    normalized_market = market.strip().upper()
    broker_by_market = {
        "CN": ("eastmoney", "东方财富"),
        "HK": ("phillips", "辉立"),
        "US": ("tiger", "老虎"),
    }
    try:
        broker, broker_label = broker_by_market[normalized_market]
    except KeyError:
        raise ValueError(f"unsupported real holding market: {market}") from None
    detail = load_broker_detail_snapshot(data_dir, broker)
    source = {
        "broker": broker,
        "broker_label": broker_label,
        "snapshot_period": detail.snapshot_period,
        "source_kind": detail.source_kind,
        "freshness_text": (
            "实时" if detail.source_kind == "live_account" else "非实时"
        ),
        "read_only_text": "只读，不自动下单",
    }
    if not detail.available:
        return RealHoldingInput(
            status="unavailable",
            reason=detail.reason,
            source=source,
            positions=(),
            holding_snapshots={},
            bars_by_symbol={},
            prior_state=None,
        )
    try:
        prior_state = load_protection_state(state_path)
    except ValueError as exc:
        return RealHoldingInput(
            status="unavailable",
            reason=f"真实持仓保护线不可用：{exc}",
            source=source,
            positions=(),
            holding_snapshots={},
            bars_by_symbol={},
            prior_state=None,
        )
    positions: list[AccountPosition] = []
    for row in detail.positions:
        if row.get("market", "").strip().upper() != normalized_market:
            continue
        symbol_text = row.get("symbol", "").strip()
        name = row.get("name", "").strip() or symbol_text
        asset_class = row.get("asset_class", "").strip().lower()
        if not asset_class:
            asset_class = detect_asset_class(symbol_text, name).value
        if asset_class not in {"stock", "etf"}:
            continue
        try:
            quantity = _decimal(row.get("quantity", ""))
            market_value_text = row.get("market_value", "").strip()
            market_value = (
                Decimal("0")
                if not market_value_text
                else _decimal(market_value_text)
            )
            avg_cost_price = _optional_decimal(row.get("cost_price", ""))
            if quantity <= 0:
                continue
            futu_symbol = to_futu_symbol(normalized_market, symbol_text)
            symbol = futu_symbol.split(".", 1)[1]
        except (ValueError, InvalidOperation) as exc:
            return RealHoldingInput(
                status="unavailable",
                reason=f"真实持仓数量或标的不可用：{symbol_text or '<missing>'}（{exc}）",
                source=source,
                positions=(),
                holding_snapshots={},
                bars_by_symbol={},
                prior_state=None,
            )
        if market_value < 0:
            return RealHoldingInput(
                status="unavailable",
                reason=f"真实持仓市值不可用：{symbol}",
                source=source,
                positions=(),
                holding_snapshots={},
                bars_by_symbol={},
                prior_state=None,
            )
        positions.append(
            AccountPosition(
                symbol=symbol,
                name=name,
                asset_class=asset_class,
                quantity=quantity,
                avg_cost_price=avg_cost_price,
                market_value=market_value,
                futu_symbol=futu_symbol,
            )
        )
    account_currency = {"CN": "CNY", "HK": "HKD", "US": "USD"}[
        normalized_market
    ]
    cash_values: list[Decimal] = []
    cash_reason = ""
    for row in detail.cash:
        value = _optional_decimal(
            row.get("available_balance", row.get("cash_balance", ""))
        )
        currency = row.get("currency", "").strip().upper()
        if value is None or not value.is_finite() or value < 0:
            cash_reason = "真实账户可用现金不可用"
            break
        if currency == account_currency:
            cash_values.append(value)
        elif value != 0:
            cash_reason = "真实账户存在未换算的多币种现金"
            break
    if cash_reason or len(cash_values) != 1:
        return RealHoldingInput(
            status="unavailable",
            reason=cash_reason or f"真实账户缺少唯一 {account_currency} 可用现金",
            source=source,
            positions=(),
            holding_snapshots={},
            bars_by_symbol={},
            prior_state=None,
        )
    available_cash = cash_values[0]
    return RealHoldingInput(
        status="available",
        reason="",
        source=source,
        positions=tuple(sorted(positions, key=lambda item: item.symbol)),
        holding_snapshots={},
        bars_by_symbol={},
        prior_state=prior_state,
        net_value=sum((position.market_value for position in positions), available_cash),
        available_cash=available_cash,
        position_count=len(positions),
    )


def enrich_real_holding_input(
    real_input: RealHoldingInput,
    *,
    api: object,
    quote: object,
    market: str,
    as_of_date: str,
    kline_start: str,
    existing_holding_ids: Mapping[str, int],
    existing_rows_by_tm_id: Mapping[int, Mapping[str, object]],
    existing_holding_snapshots: Mapping[str, HoldingSnapshot | None],
    existing_bars_by_symbol: Mapping[
        str, Sequence[DailyKlineBar] | None
    ],
) -> tuple[
    RealHoldingInput,
    dict[int, Mapping[str, object]],
    dict[str, Sequence[DailyKlineBar] | None],
    int,
]:
    """Best-effort Trend Animals/Futu enrichment for the frozen real snapshot.

    Simulated account requests are deliberately passed in separately. A failure
    while resolving or loading real-only symbols degrades the real tab to
    ``unavailable`` and leaves the simulated report path untouched. An exact
    symbol miss only degrades that real position.
    """
    if real_input.status != "available":
        return real_input, {}, {}, 0
    real_ids: dict[str, int] = {}
    unresolved: set[str] = set()
    excluded = set(real_input.trend_excluded_symbols)
    search_exact_symbol = getattr(api, "search_exact_symbol", None)
    if not callable(search_exact_symbol):
        degraded = replace(
            real_input,
            status="unavailable",
            reason="Trend Animals 标的搜索接口不可用",
            holding_snapshots={},
            bars_by_symbol={},
        )
        return degraded, {}, {}, 0
    for position in real_input.positions:
        canonical_symbol = to_futu_symbol(market, position.symbol)
        if canonical_symbol in REAL_HOLDING_TREND_EXCLUDED_SYMBOLS:
            excluded.add(position.symbol)
            continue
        if position.symbol in existing_holding_ids:
            real_ids[position.symbol] = existing_holding_ids[position.symbol]
            continue
        try:
            real_ids[position.symbol] = int(
                search_exact_symbol(
                    position.symbol,
                    market=market,
                    expected_date=as_of_date,
                )
            )
        except TrendAnimalsLookupError:
            unresolved.add(position.symbol)
        except Exception as exc:
            degraded = replace(
                real_input,
                status="unavailable",
                reason=f"真实持仓趋势服务不可用：{exc}",
                holding_snapshots={},
                bars_by_symbol={},
            )
            return degraded, {}, {}, 0

    real_only_ids = sorted(
        set(real_ids.values()) - set(existing_rows_by_tm_id)
    )
    real_rows: dict[int, Mapping[str, object]] = {}
    if real_only_ids:
        get_snapshots = getattr(api, "get_snapshots", None)
        if not callable(get_snapshots):
            degraded = replace(
                real_input,
                status="unavailable",
                reason="Trend Animals 持仓快照接口不可用",
                holding_snapshots={},
                bars_by_symbol={},
            )
            return degraded, {}, {}, len(real_only_ids)
        try:
            response = get_snapshots(
                tm_ids=real_only_ids,
                fields=UNIFIED_TREND_FIELDS,
                expected_date=as_of_date,
            )
            response_ids = [_row_tm_id(row) for row in response]
            if (
                len(response_ids) != len(set(response_ids))
                or sorted(response_ids) != real_only_ids
                or any(row.get("asOfDate") != as_of_date for row in response)
            ):
                raise ValueError("真实持仓快照日期或 tmId 不一致")
            real_rows = {_row_tm_id(row): row for row in response}
        except Exception as exc:
            degraded = replace(
                real_input,
                status="unavailable",
                reason=f"真实持仓快照不可用：{exc}",
                holding_snapshots={},
                bars_by_symbol={},
            )
            return degraded, {}, {}, len(real_only_ids)

    real_bars: dict[str, Sequence[DailyKlineBar] | None] = {}
    get_daily_kline = getattr(quote, "get_daily_kline", None)
    for position in real_input.positions:
        if position.symbol in existing_bars_by_symbol:
            real_bars[position.symbol] = existing_bars_by_symbol[position.symbol]
            continue
        if not callable(get_daily_kline):
            real_bars[position.symbol] = None
            continue
        try:
            real_bars[position.symbol] = get_daily_kline(
                to_futu_symbol(market, position.symbol),
                start=kline_start,
                end=as_of_date,
            )
        except Exception:
            # Real holdings are read-only enrichment; a quote outage must not
            # turn a valid simulated report into a failed run.
            real_bars[position.symbol] = None

    real_snapshots: dict[str, HoldingSnapshot | None] = {}
    for position in real_input.positions:
        if position.symbol in excluded or position.symbol in unresolved:
            real_snapshots[position.symbol] = None
            continue
        if position.symbol in existing_holding_snapshots:
            real_snapshots[position.symbol] = existing_holding_snapshots[
                position.symbol
            ]
            continue
        tm_id = real_ids.get(position.symbol)
        if tm_id is None:
            real_snapshots[position.symbol] = None
            continue
        row = real_rows.get(tm_id)
        if row is None:
            real_snapshots[position.symbol] = None
            continue
        try:
            if from_trend_animals_symbol(
                market, str(row.get("tickerSymbol") or "")
            ) != to_futu_symbol(market, position.symbol):
                real_snapshots[position.symbol] = None
                continue
            _remember_verified_symbol_row(
                api,
                market=market,
                expected_futu_symbol=position.symbol,
                expected_tm_id=tm_id,
                row=row,
                require_unmapped=True,
            )
            real_snapshots[position.symbol] = _holding_snapshot(
                row,
                market=market,
                bars=tuple(real_bars.get(position.symbol) or ()),
            )
        except (TypeError, ValueError):
            real_snapshots[position.symbol] = None
    return (
        replace(
            real_input,
            holding_snapshots=real_snapshots,
            bars_by_symbol=real_bars,
            trend_excluded_symbols=tuple(sorted(excluded)),
        ),
        real_rows,
        real_bars,
        len(real_only_ids),
    )


def _remember_verified_symbol_row(
    api: object,
    *,
    market: str,
    expected_futu_symbol: str,
    expected_tm_id: int,
    row: Mapping[str, object],
    require_unmapped: bool = False,
) -> bool:
    recorder = getattr(api, "remember_symbol_row", None)
    if not callable(recorder):
        return False
    normalized_market = market.strip().upper()
    if normalized_market not in SEARCH_ASSETS_BY_MARKET:
        return False
    tm_id = row.get("tmId")
    ticker_symbol = row.get("tickerSymbol")
    asset = row.get("asset")
    if (
        not isinstance(tm_id, int)
        or isinstance(tm_id, bool)
        or tm_id != expected_tm_id
        or not isinstance(ticker_symbol, str)
        or not isinstance(asset, str)
        or asset.strip() not in SEARCH_ASSETS_BY_MARKET[normalized_market]
    ):
        return False
    try:
        futu_symbol = to_futu_symbol(normalized_market, expected_futu_symbol)
        trend_futu_symbol = from_trend_animals_symbol(
            normalized_market, ticker_symbol
        )
    except ValueError:
        return False
    if futu_symbol != trend_futu_symbol:
        return False
    if require_unmapped:
        lookup = getattr(api, "symbol_mapping", None)
        if not callable(lookup) or lookup(
            futu_symbol, market=normalized_market
        ) is not None:
            return False
    recorder(
        market=normalized_market,
        expected_futu_symbol=futu_symbol,
        row=row,
    )
    return True


def _supports_symbol_mapping_contract(api: object) -> bool:
    return callable(getattr(api, "remember_symbol_row", None)) and callable(
        getattr(api, "symbol_mapping", None)
    )


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
    account_client: object | None = None,
    account_factory: Callable[..., object] = FutuSimulateOrderExecutionClient,
) -> AccountSnapshot:
    market = market.strip().upper()
    if market not in {"CN", "HK", "US"}:
        raise ValueError(f"unsupported trend review market: {market}")
    owns_client = account_client is None
    client = account_client
    if client is None:
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
        if owns_client and callable(close):
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
                futu_symbol=code,
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
    exchange, symbol = from_trend_animals_symbol(market, value).split(".", 1)
    return symbol, exchange


def evaluate_candidate(
    row: Mapping[str, object],
    bars: Sequence[DailyKlineBar] | None,
    *,
    pools: Sequence[str] = (),
    market: str = "CN",
    industry_temperature: str | None = None,
    futu_symbol: str | None = None,
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
        futu_symbol=futu_symbol,
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
    strategy_version: str | None = None,
    cny_per_local_currency: Decimal | None = None,
) -> list[str]:
    reasons: list[str] = []
    shared_discipline = _uses_shared_entry_discipline(market, strategy_version)
    cny_rate = (
        cny_per_local_currency
        if cny_per_local_currency is not None
        else CNY_PER_LOCAL_CURRENCY.get(market, Decimal("1"))
    )
    if shared_discipline:
        allowed_assets = (
            {"A股", "ETF基金"}
            if strategy_version in {"v9", "v10", "v11", "v12"}
            else {"A股"}
        )
        if market == "CN" and item.asset not in allowed_assets:
            reasons.append("a_share_only")
        if item.temperature_prev is None or item.temperature_curr is None:
            reasons.append("temperature_missing")
        elif item.temperature_prev != "温" or item.temperature_curr not in HOT_TEMPERATURES:
            reasons.append("temperature_transition_not_entry")
        if item.strength is None:
            reasons.append("strength_missing")
        elif item.strength < CN_MIN_STRENGTH:
            reasons.append("strength_below_95")
        if item.industry_tm_id is None:
            reasons.append("industry_id_missing")
        if item.industry_temperature is None:
            reasons.append("industry_temperature_missing")
        elif item.industry_temperature not in CN_ALLOWED_INDUSTRY_TEMPERATURES:
            reasons.append("industry_temperature_not_hot")
        if item.phase is None:
            reasons.append("phase_missing")
        elif item.phase not in ALLOWED_ENTRY_PHASES:
            reasons.append("phase_after_summer_solstice")
        if item.market_cap is None:
            reasons.append("market_cap_missing")
        elif item.market_cap * cny_rate < CN_MIN_MARKET_CAP_100M:
            reasons.append(
                "market_cap_below_100"
                if market == "CN"
                else "market_cap_below_100_cny"
            )
        if item.amount is None:
            reasons.append("amount_missing")
        elif item.amount * cny_rate < CN_MIN_AMOUNT_100M:
            reasons.append(
                "amount_below_2" if market == "CN" else "amount_below_2_cny"
            )
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


def _candidate_context_sort_key(
    item: CandidateInput,
    context: IndustryContext,
    *,
    include_history: bool,
) -> tuple[object, ...]:
    temperature = context.temperature
    strength = context.strength
    right_share = context.right_share
    assert temperature in KNOWN_TEMPERATURE_ORDER
    assert strength is not None
    assert right_share is not None
    history = (
        (
            TEMPERATURE_DIRECTION_ORDER[context.temperature_direction],
            -context.right_share_change_pp,
        )
        if include_history
        else ()
    )
    return (
        *(history[:1]),
        -KNOWN_TEMPERATURE_ORDER[temperature],
        -strength,
        -context.warm_to_hot_count,
        *(history[1:]),
        -right_share,
        *_candidate_sort_key(item),
    )


def _context_current_reasons(context: object) -> list[str]:
    if not isinstance(context, IndustryContext):
        return ["industry_context_missing"]
    reasons = list(context.invalid_reasons)
    if not context.valid and not reasons:
        reasons.append("industry_context_invalid")
    if not isinstance(context.temperature, str) or context.temperature not in KNOWN_TEMPERATURE_ORDER:
        reasons.append("industry_temperature_invalid")
    if (
        not isinstance(context.strength, Decimal)
        or not context.strength.is_finite()
        or not Decimal("0") <= context.strength <= Decimal("100")
    ):
        reasons.append("industry_strength_invalid")
    if (
        not isinstance(context.right_share, Decimal)
        or not context.right_share.is_finite()
        or not Decimal("0") <= context.right_share <= Decimal("1")
    ):
        reasons.append("industry_right_share_invalid")
    if (
        isinstance(context.warm_to_hot_count, bool)
        or not isinstance(context.warm_to_hot_count, int)
        or context.warm_to_hot_count < 0
    ):
        reasons.append("warm_to_hot_count_invalid")
    return list(dict.fromkeys(reasons))


def _context_has_history(context: IndustryContext) -> bool:
    return (
        context.prior_as_of_date is not None
        and isinstance(context.prior_temperature, str)
        and context.prior_temperature in KNOWN_TEMPERATURE_ORDER
        and isinstance(context.prior_right_share, Decimal)
        and context.prior_right_share.is_finite()
        and isinstance(context.temperature_direction, str)
        and context.temperature_direction in TEMPERATURE_DIRECTION_ORDER
        and isinstance(context.right_share_change_pp, Decimal)
        and context.right_share_change_pp.is_finite()
    )


def _industry_context_state(
    eligible: Sequence[CandidateInput],
    industry_contexts: Mapping[int, IndustryContext] | None,
    *,
    expected_date: str | None = None,
) -> tuple[str, dict[str, object], dict[int, IndustryContext]]:
    if not eligible:
        return (
            "legacy_no_eligible_candidates",
            {
                "ordering_mode": "legacy_no_eligible_candidates",
                "current_complete": False,
                "history_complete": False,
                "fallback_reason": None,
            },
            dict(industry_contexts or {}),
        )

    contexts = dict(industry_contexts or {})
    eligible_industry_ids = {
        item.industry_tm_id
        for item in eligible
        if item.industry_tm_id is not None
    }
    affected: set[int | str] = set()
    reasons_by_id: dict[str, list[str]] = {}
    fallback_reason: str | None = None

    def add_failure(industry_id: int | str, reasons: Sequence[str]) -> None:
        normalized = list(dict.fromkeys(reasons)) or ["industry_context_invalid"]
        affected.add(industry_id)
        reasons_by_id[str(industry_id)] = normalized

    for item in eligible:
        industry_id = item.industry_tm_id
        if industry_id is None:
            add_failure("unknown", ("industry_id_missing",))
            fallback_reason = fallback_reason or "industry_id_missing"
            continue
        context = contexts.get(industry_id)
        reasons = _context_current_reasons(context)
        if isinstance(context, IndustryContext):
            if context.industry_tm_id != industry_id:
                reasons.append("industry_context_id_mismatch")
            if expected_date is not None and context.as_of_date != expected_date:
                reasons.append("industry_context_date_mismatch")
        if reasons:
            add_failure(industry_id, reasons)
            if reasons == ["industry_context_missing"]:
                fallback_reason = fallback_reason or "industry_context_missing"
            else:
                fallback_reason = fallback_reason or "industry_context_invalid"

    if affected:
        affected_ids = sorted(
            affected,
            key=lambda value: (value == "unknown", str(value)),
        )
        return (
            "legacy_invalid_current",
            {
                "ordering_mode": "legacy_invalid_current",
                "current_complete": False,
                "history_complete": False,
                "fallback_reason": fallback_reason or "industry_context_invalid",
                "affected_industry_ids": affected_ids,
                "validation_reasons": reasons_by_id,
            },
            contexts,
        )

    history_complete = all(
        _context_has_history(contexts[industry_id])
        for industry_id in eligible_industry_ids
    )
    mode = "context_with_history" if history_complete else "context_current_only"
    return (
        mode,
        {
            "ordering_mode": mode,
            "current_complete": True,
            "history_complete": history_complete,
            "fallback_reason": None,
        },
        contexts,
    )


def _sort_candidates_for_mode(
    eligible: Sequence[CandidateInput],
    *,
    mode: str,
    contexts: Mapping[int, IndustryContext],
) -> list[CandidateInput]:
    ranked = list(eligible)
    if mode in {"context_with_history", "context_current_only"}:
        include_history = mode == "context_with_history"
        ranked.sort(
            key=lambda item: _candidate_context_sort_key(
                item,
                contexts[item.industry_tm_id],
                include_history=include_history,
            )
        )
    else:
        ranked.sort(key=_candidate_sort_key)
    return ranked


def build_candidate_list(
    rows: Sequence[CandidateInput],
    *,
    held_symbols: set[str],
    expected_date: str | None = None,
    market: str = "CN",
    industry_contexts: Mapping[int, IndustryContext] | None = None,
    strategy_version: str | None = None,
    cny_per_local_currency: Decimal | None = None,
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
                    item,
                    held_symbols,
                    expected_date,
                    market=market,
                    strategy_version=strategy_version,
                    cny_per_local_currency=cny_per_local_currency,
                )
            )
        )
        if reasons:
            excluded[symbol] = reasons
        else:
            eligible.append(min(items, key=_candidate_sort_key))
    ordering_mode, context_status, contexts = _industry_context_state(
        eligible, industry_contexts, expected_date=expected_date
    )
    eligible = _sort_candidates_for_mode(
        eligible, mode=ordering_mode, contexts=contexts
    )
    return CandidateDecision(
        tuple(eligible),
        excluded,
        ordering_mode=ordering_mode,
        industry_context_status=context_status,
    )


def plan_rotation_pairs(
    *,
    holdings: Sequence[HoldingSnapshot],
    candidates: Sequence[CandidateInput],
    entry_weight: Decimal,
    available_slots: int,
    pair_slots: Sequence[int],
    net_value: Decimal = Decimal("0"),
    available_cash: Decimal | None = None,
    market: str = "CN",
    lot_sizes: Mapping[str, int] | None = None,
    require_mapping: bool = False,
) -> tuple[RotationPair, ...]:
    """Match eligible holdings with stronger candidates using frozen basis rules."""
    pairs, _ = plan_rotation_pairs_with_comparisons(
        holdings=holdings,
        candidates=candidates,
        entry_weight=entry_weight,
        available_slots=available_slots,
        pair_slots=pair_slots,
        net_value=net_value,
        available_cash=available_cash,
        market=market,
        lot_sizes=lot_sizes,
        require_mapping=require_mapping,
    )
    return pairs


def _rotation_comparison_values(
    holding: HoldingSnapshot,
    candidate: CandidateInput,
    *,
    market: str,
) -> tuple[str | None, Decimal | None, Decimal | None, str]:
    allowed_assets = SEARCH_ASSETS_BY_MARKET.get(market.upper(), frozenset())
    if (
        not holding.asset
        or not candidate.asset
        or holding.asset not in allowed_assets
        or candidate.asset not in allowed_assets
    ):
        return None, None, None, "大类未提供或不属于当前市场"
    same_category = holding.asset == candidate.asset
    basis = "local" if same_category else "global"
    sell_value = holding.strength if same_category else holding.global_strength
    buy_value = candidate.strength if same_category else candidate.global_strength
    label = "大类内强度" if same_category else "全局强度"
    if (
        sell_value is None
        or buy_value is None
        or not sell_value.is_finite()
        or not buy_value.is_finite()
        or not Decimal("0") <= sell_value <= Decimal("100")
        or not Decimal("0") <= buy_value <= Decimal("100")
    ):
        return basis, sell_value, buy_value, f"{label}未提供或无效"
    return basis, sell_value, buy_value, ""


def plan_rotation_pairs_with_comparisons(
    *,
    holdings: Sequence[HoldingSnapshot],
    candidates: Sequence[CandidateInput],
    entry_weight: Decimal,
    available_slots: int,
    pair_slots: Sequence[int],
    net_value: Decimal = Decimal("0"),
    available_cash: Decimal | None = None,
    market: str = "CN",
    lot_sizes: Mapping[str, int] | None = None,
    require_mapping: bool = False,
) -> tuple[tuple[RotationPair, ...], tuple[RotationComparison, ...]]:
    """Return executable pairs plus the frozen explanations used to choose them."""
    if available_slots > 0:
        return (), ()
    slots = tuple(pair_slots[:2])
    if len(set(slots)) != len(slots) or any(slot not in {0, 1} for slot in slots):
        raise ValueError("rotation pair slots must be unique 0/1 values")
    unique_holdings = {item.symbol: item for item in holdings}
    held_symbols = set(unique_holdings)
    unique_candidates = {item.symbol: item for item in candidates}
    eligible_candidates = sorted(
        (
            item for item in unique_candidates.values()
            if item.symbol not in held_symbols
            and item.close is not None
            and item.close.is_finite()
            and item.close > 0
            and item.atr is not None
            and item.atr.is_finite()
            and item.atr > 0
            and (not require_mapping or bool(item.futu_symbol))
        ),
        key=lambda item: item.symbol,
    )
    rows: list[tuple[HoldingSnapshot, CandidateInput, RotationComparison]] = []
    for holding in unique_holdings.values():
        for candidate in eligible_candidates:
            basis, sell_compared, buy_compared, data_reason = _rotation_comparison_values(
                holding, candidate, market=market,
            )
            gap = (
                buy_compared - sell_compared
                if sell_compared is not None and buy_compared is not None
                else None
            )
            comparison = RotationComparison(
                pair_index=-1,
                sell_symbol=holding.symbol,
                sell_name=holding.name or holding.symbol,
                sell_asset=holding.asset,
                sell_local_strength=holding.strength,
                sell_global_strength=holding.global_strength,
                buy_symbol=candidate.symbol,
                buy_name=candidate.name,
                buy_asset=candidate.asset,
                buy_local_strength=candidate.strength,
                buy_global_strength=candidate.global_strength,
                strength_basis=basis,
                sell_compared_strength=sell_compared,
                buy_compared_strength=buy_compared,
                strength_gap=gap,
                outcome=(
                    "data_unavailable"
                    if data_reason
                    else "gap_below_threshold"
                    if gap < Decimal("20")
                    else "planned"
                ),
                reason=(
                    data_reason
                    if data_reason
                    else f"强度差 {gap} 小于门槛 20"
                    if gap < Decimal("20")
                    else "relative_rotation"
                ),
            )
            rows.append((holding, candidate, comparison))
    rows.sort(
        key=lambda item: (
            item[2].strength_gap is None,
            -(item[2].strength_gap or Decimal("0")),
            0 if item[2].strength_basis == "local" else 1,
            item[1].symbol,
            item[0].symbol,
        )
    )
    result: list[RotationPair] = []
    comparisons: list[RotationComparison] = []
    used_symbols: set[str] = set()
    for pair_index, (held, candidate, comparison) in zip(
        slots,
        (
            row for row in rows
            if not ({row[0].symbol, row[1].symbol} & used_symbols)
        ),
    ):
        comparison = replace(comparison, pair_index=pair_index)
        used_symbols.update((held.symbol, candidate.symbol))
        comparisons.append(comparison)
        if comparison.outcome != "planned":
            continue
        assert candidate.close is not None
        assert candidate.atr is not None
        lot_size = (
            100
            if market == "CN"
            else (lot_sizes or {}).get(candidate.symbol, 0)
            if market == "HK"
            else 1
        )
        if lot_size <= 0:
            comparisons[-1] = replace(
                comparison,
                outcome="sizing_blocked",
                reason="买入手数缺失",
            )
            continue
        target_amount = min(
            net_value * entry_weight,
            available_cash if available_cash is not None else net_value * entry_weight,
        )
        estimated_shares = _floor_to_lot(target_amount / candidate.close, lot_size)
        if net_value > 0 and estimated_shares <= 0:
            comparisons[-1] = replace(
                comparison,
                outcome="sizing_blocked",
                reason="按现有资金与手数规则无法生成买单",
            )
            continue
        result.append(
            RotationPair(
                pair_index=pair_index,
                sell_symbol=held.symbol,
                sell_name=held.name or held.symbol,
                sell_futu_symbol=to_futu_symbol(market, held.symbol),
                sell_global_strength=held.global_strength,
                buy_symbol=candidate.symbol,
                buy_name=candidate.name,
                buy_futu_symbol=(
                    candidate.futu_symbol
                    or to_futu_symbol(market, candidate.symbol)
                ),
                buy_global_strength=candidate.global_strength,
                strength_gap=comparison.strength_gap or Decimal("0"),
                target_weight=entry_weight,
                target_amount=target_amount,
                estimated_shares=estimated_shares,
                lot_size=lot_size,
                atr=candidate.atr,
                sell_asset=held.asset,
                sell_local_strength=held.strength,
                buy_asset=candidate.asset,
                buy_local_strength=candidate.strength,
                strength_basis=comparison.strength_basis,
                sell_compared_strength=comparison.sell_compared_strength,
                buy_compared_strength=comparison.buy_compared_strength,
                threshold=comparison.threshold,
            )
        )
    return tuple(result), tuple(comparisons)


def _plan_account_rotation_pairs(
    *,
    account: AccountSnapshot,
    holdings: Sequence[HoldingDecision],
    holding_snapshots: Mapping[str, HoldingSnapshot | None],
    candidates: Sequence[CandidateInput],
    entry_weight: Decimal,
    forced_sell_symbols: set[str],
    market: str,
    lot_sizes: Mapping[str, int] | None,
    price_fx_to_account_currency: Decimal,
    normal_cost_rate: Decimal,
    cn_target_weights: Mapping[str, Decimal],
    kelly_state: TrendKellyState | None,
    critical_data_reason: str,
    require_mapping: bool,
) -> tuple[tuple[RotationPair, ...], tuple[RotationComparison, ...]]:
    """Pair first, then reuse ordinary entry sizing with each sell's proceeds."""
    positions_by_symbol = {item.symbol: item for item in account.positions}
    snapshots = [
        snapshot
        for decision in holdings
        if decision.action == "HOLD"
        and (snapshot := holding_snapshots.get(decision.symbol)) is not None
    ]
    proposed, comparisons = plan_rotation_pairs_with_comparisons(
        holdings=snapshots,
        candidates=[
            item for item in candidates if item.symbol not in positions_by_symbol
        ],
        entry_weight=entry_weight,
        available_slots=0,
        pair_slots=(0, 1),
        market=market,
        lot_sizes=lot_sizes,
        require_mapping=require_mapping,
    )
    candidates_by_symbol = {item.symbol: item for item in candidates}
    selected: list[RotationPair] = []
    comparisons_by_index = {
        item.pair_index: item for item in comparisons
    }
    selected_sell_symbols = set(forced_sell_symbols)
    sale_factor = max(Decimal("0"), Decimal("1") - normal_cost_rate)
    remaining_cash = account.available_cash + sum(
        (
            item.market_value * sale_factor
            for item in account.positions
            if item.symbol in forced_sell_symbols
        ),
        Decimal("0"),
    )
    replacement_risk = Decimal("0")
    for pair in proposed:
        position = positions_by_symbol.get(pair.sell_symbol)
        candidate = candidates_by_symbol[pair.buy_symbol]
        if (
            position is None
            or not position.market_value.is_finite()
            or position.market_value <= 0
        ):
            if pair.pair_index in comparisons_by_index:
                comparisons_by_index[pair.pair_index] = replace(
                    comparisons_by_index[pair.pair_index],
                    outcome="sizing_blocked",
                    reason="持仓市值缺失，无法计算替换仓位",
                )
            continue
        candidate_sell_symbols = selected_sell_symbols | {pair.sell_symbol}
        existing_risk, risk_reason = _post_sell_planned_risk(
            account=account,
            holdings=holdings,
            sell_symbols=candidate_sell_symbols,
            price_fx_to_account_currency=price_fx_to_account_currency,
            normal_cost_rate=normal_cost_rate,
        )
        if existing_risk is not None:
            existing_risk += replacement_risk
        potential_cash = remaining_cash + position.market_value * sale_factor
        actions, skips, _ = _plan_buy_actions(
            ranked=(candidate,),
            net_value=account.net_value,
            available_cash=potential_cash,
            current_position_count=POSITION_LIMIT - 1,
            position_weight=entry_weight,
            market=market,
            lot_sizes=lot_sizes,
            price_fx_to_account_currency=price_fx_to_account_currency,
            portfolio_planned_risk=existing_risk,
            normal_cost_rate=normal_cost_rate,
            cn_target_weights=cn_target_weights,
            critical_data_reason=critical_data_reason or risk_reason,
            kelly_state=kelly_state,
        )
        if not actions:
            if pair.pair_index in comparisons_by_index:
                skip_reason = next(
                    (
                        str(item.get("reason") or "")
                        for item in skips
                        if isinstance(item, Mapping)
                    ),
                    "买入规则阻止",
                )
                comparisons_by_index[pair.pair_index] = replace(
                    comparisons_by_index[pair.pair_index],
                    outcome="sizing_blocked",
                    reason=skip_reason,
                )
            continue
        action = actions[0]
        cash_required = (
            Decimal(action.estimated_shares)
            * action.close
            * price_fx_to_account_currency
            * (Decimal("1") + normal_cost_rate)
        )
        remaining_cash = max(Decimal("0"), potential_cash - cash_required)
        replacement_risk += action.planned_stop_risk
        selected_sell_symbols.add(pair.sell_symbol)
        selected.append(
            replace(
                pair,
                buy_futu_symbol=action.futu_symbol or pair.buy_futu_symbol,
                target_weight=action.target_weight,
                target_amount=action.target_amount,
                estimated_shares=action.estimated_shares,
                lot_size=action.lot_size,
                atr=action.atr,
            )
        )
    return tuple(selected), tuple(
        comparisons_by_index.get(item.pair_index, item)
        for item in comparisons
    )


def freeze_report_rotation_pairs(report: TrendReport, data_dir: Path) -> TrendReport:
    """Replace proposed pairs with the immutable reservations for this report date."""
    parameters = report.strategy_snapshot.get("parameters")
    allocation_sha256 = (
        str(parameters.get("allocation_snapshot_sha256") or "")
        if isinstance(parameters, Mapping)
        else ""
    )
    if len(allocation_sha256) != 64:
        if not report.simulate_rotation_pairs and not report.real_rotation_pairs:
            return report
        raise ValueError("rotation pairs require a frozen allocation snapshot")

    def frozen(
        account_key: str, pairs: Sequence[RotationPair], execution_mode: str,
    ) -> tuple[RotationPair, ...]:
        values = reserve_rotation_pairs(
            data_dir,
            market=str(report.metadata.get("market") or "CN"),
            account_key=account_key,
            execution_date=report.execution_date,
            pairs=[asdict(pair) for pair in pairs],
            allocation_sha256=allocation_sha256,
            reserved_at=report.generated_at,
        )
        decimal_fields = {
            "sell_global_strength",
            "sell_local_strength",
            "buy_global_strength",
            "buy_local_strength",
            "sell_compared_strength",
            "buy_compared_strength",
            "strength_gap",
            "target_weight",
            "target_amount",
            "atr",
            "threshold",
        }
        return tuple(
            RotationPair(
                **{
                    key: (
                        Decimal(str(value))
                        if key in decimal_fields and value is not None
                        else value
                    )
                    for key, value in pair.items()
                    if key not in {"execution_date", "execution_mode"}
                },
                execution_date=report.execution_date,
                execution_mode=execution_mode,
            )
            for pair in values
        )

    def frozen_comparisons(
        proposed: Sequence[RotationComparison],
        pairs: Sequence[RotationPair],
    ) -> tuple[RotationComparison, ...]:
        result: list[RotationComparison] = []
        for pair in pairs:
            if pair.strength_basis not in {"local", "global"}:
                continue
            result.append(
                RotationComparison(
                    pair_index=pair.pair_index,
                    sell_symbol=pair.sell_symbol,
                    sell_name=pair.sell_name,
                    sell_asset=pair.sell_asset,
                    sell_local_strength=pair.sell_local_strength,
                    sell_global_strength=pair.sell_global_strength,
                    buy_symbol=pair.buy_symbol,
                    buy_name=pair.buy_name,
                    buy_asset=pair.buy_asset,
                    buy_local_strength=pair.buy_local_strength,
                    buy_global_strength=pair.buy_global_strength,
                    strength_basis=pair.strength_basis,
                    sell_compared_strength=pair.sell_compared_strength,
                    buy_compared_strength=pair.buy_compared_strength,
                    strength_gap=pair.strength_gap,
                    threshold=pair.threshold,
                    outcome="planned",
                    reason="relative_rotation",
                )
            )
        used_indices = {item.pair_index for item in result}
        used_symbols = {
            symbol
            for item in result
            for symbol in (item.sell_symbol, item.buy_symbol)
        }
        for item in proposed:
            if len(result) >= 2 or item.pair_index in used_indices:
                continue
            if {item.sell_symbol, item.buy_symbol} & used_symbols:
                continue
            result.append(item)
            used_indices.add(item.pair_index)
            used_symbols.update((item.sell_symbol, item.buy_symbol))
        return tuple(result[:2])

    simulate_acc_id = report.metadata.get("simulate_acc_id")
    if (report.simulate_rotation_pairs or isinstance(simulate_acc_id, int)) and (
        not isinstance(simulate_acc_id, int) or simulate_acc_id <= 0
    ):
        raise ValueError("rotation pairs require a configured simulate account")
    real_broker = str(report.real_holdings_source.get("broker") or "")
    simulate_pairs = (
        frozen(
            f"simulate-{simulate_acc_id}", report.simulate_rotation_pairs,
            "automatic",
        )
        if isinstance(simulate_acc_id, int) and simulate_acc_id > 0
        else ()
    )
    real_pairs = (
        frozen(
            f"real-{real_broker}", report.real_rotation_pairs, "manual"
        )
        if real_broker
        else ()
    )
    return replace(
        report,
        simulate_rotation_pairs=simulate_pairs,
        real_rotation_pairs=real_pairs,
        simulate_rotation_comparisons=frozen_comparisons(
            report.simulate_rotation_comparisons, simulate_pairs
        ),
        real_rotation_comparisons=frozen_comparisons(
            report.real_rotation_comparisons, real_pairs
        ),
        metadata={
            **report.metadata,
            "rotation_allocation_sha256": allocation_sha256,
        },
    )


def collect_industry_contexts(
    *,
    api: object,
    candidates: Sequence[CandidateInput],
    candidate_rows: Sequence[Mapping[str, object]],
    held_symbols: set[str],
    holding_snapshots: Sequence[HoldingSnapshot | None] = (),
    expected_date: str,
    market: str,
    history_root: Path,
    strategy_version: str | None = None,
    cny_per_local_currency: Decimal | None = None,
) -> tuple[tuple[IndustryContext, ...], dict[str, object], dict[str, object]]:
    """Collect candidate context first, then append isolated holding context."""
    candidate_decision = build_candidate_list(
        candidates,
        held_symbols=held_symbols,
        expected_date=expected_date,
        market=market,
        strategy_version=strategy_version,
        cny_per_local_currency=cny_per_local_currency,
    )
    eligible = candidate_decision.eligible
    eligible_industry_ids = sorted(
        {
            item.industry_tm_id
            for item in eligible
            if item.industry_tm_id is not None
        }
    )
    industry_names = {
        item.industry_tm_id: item.industry
        for item in eligible
        if item.industry_tm_id is not None and item.industry
    }
    for row in candidate_rows:
        industry_id = _optional_int(row.get("industryTmId"))
        industry_name = row.get("industryName")
        if (
            industry_id is not None
            and industry_id in eligible_industry_ids
            and industry_id not in industry_names
            and isinstance(industry_name, str)
        ):
            industry_names[industry_id] = industry_name.strip()
    holding_industry_ids = sorted(
        {
            snapshot.industry_tm_id
            for snapshot in holding_snapshots
            if snapshot is not None and snapshot.industry_tm_id is not None
        }
    )
    for snapshot in holding_snapshots:
        if (
            snapshot is not None
            and snapshot.industry_tm_id is not None
            and snapshot.industry
            and snapshot.industry_tm_id not in industry_names
        ):
            industry_names[snapshot.industry_tm_id] = snapshot.industry
    holding_only_ids = [
        industry_id
        for industry_id in holding_industry_ids
        if industry_id not in eligible_industry_ids
    ]
    context_industry_ids = [*eligible_industry_ids, *holding_only_ids]
    component_ids_by_industry: dict[int, set[int]] = {}
    component_rows_by_industry: dict[int, list[Mapping[str, object]]] = {}
    component_rows_count = 0
    for industry_id in eligible_industry_ids:
        try:
            rows = api.get_components(  # type: ignore[attr-defined]
                tm_id=industry_id,
                expected_date=expected_date,
            )
        except TrendAnimalsNoCurrentRowsError:
            rows = []
        component_rows_count += len(rows)
        component_rows_by_industry[industry_id] = list(rows)
        component_ids_by_industry[industry_id] = {
            _row_tm_id(row) for row in rows
        }
    member_ids = sorted(
        {
            member_id
            for component_ids in component_ids_by_industry.values()
            for member_id in component_ids
        }
    )
    member_rows = (
        api.get_snapshots(  # type: ignore[attr-defined]
            tm_ids=member_ids,
            fields=INDUSTRY_MEMBER_FIELDS,
            expected_date=expected_date,
        )
        if member_ids
        else []
    )
    state_rows = (
        api.get_snapshots(  # type: ignore[attr-defined]
            tm_ids=eligible_industry_ids,
            fields=INDUSTRY_STATE_FIELDS,
            expected_date=expected_date,
        )
        if eligible_industry_ids
        else []
    )
    state_by_id: dict[int, Mapping[str, object]] = {}
    for row in state_rows:
        tm_id = _row_tm_id(row)
        if tm_id in state_by_id:
            raise TrendAnimalsError("industry state snapshot returned duplicate tmIds")
        state_by_id[tm_id] = row
    warm_to_hot_ids: defaultdict[int, set[int]] = defaultdict(set)
    for row in candidate_rows:
        tm_id = _optional_int(row.get("tmId"))
        industry_id = _optional_int(row.get("industryTmId"))
        if (
            tm_id is not None
            and industry_id is not None
            and row.get("trendTemperaturePrev") == "温"
            and row.get("trendTemperatureCurr") in HOT_TEMPERATURES
        ):
            warm_to_hot_ids[industry_id].add(tm_id)
    candidate_contexts = tuple(
        calculate_industry_context(
            industry_tm_id=industry_id,
            industry=industry_names.get(industry_id, ""),
            expected_date=expected_date,
            component_tm_ids=sorted(component_ids_by_industry[industry_id]),
            member_rows=member_rows,
            industry_row=state_by_id.get(industry_id),
            warm_to_hot_count=len(warm_to_hot_ids[industry_id]),
        )
        for industry_id in eligible_industry_ids
    )
    prior = load_latest_prior_context(
        history_root,
        market=market,
        before_date=expected_date,
    )
    candidate_contexts = attach_prior_context(candidate_contexts, prior)
    context_map = {
        context.industry_tm_id: context for context in candidate_contexts
    }
    ordering = build_candidate_list(
        candidates,
        held_symbols=held_symbols,
        expected_date=expected_date,
        market=market,
        industry_contexts=context_map,
        strategy_version=strategy_version,
        cny_per_local_currency=cny_per_local_currency,
    )

    holding_errors: dict[str, str] = {}
    holding_state_rows: list[Mapping[str, object]] = []
    if holding_only_ids:
        try:
            holding_state_rows = list(
                api.get_snapshots(  # type: ignore[attr-defined]
                    tm_ids=holding_only_ids,
                    fields=INDUSTRY_STATE_FIELDS,
                    expected_date=expected_date,
                )
            )
        except TrendAnimalsError as exc:
            holding_errors["states"] = str(exc)
    holding_state_by_id: dict[int, Mapping[str, object]] = {}
    for row in holding_state_rows:
        tm_id = _row_tm_id(row)
        if tm_id in holding_state_by_id:
            holding_errors[str(tm_id)] = "duplicate holding industry state"
            continue
        holding_state_by_id[tm_id] = row

    holding_contexts = tuple(
        calculate_industry_context(
            industry_tm_id=industry_id,
            industry=industry_names.get(industry_id, ""),
            expected_date=expected_date,
            component_tm_ids=(),
            member_rows=(),
            industry_row=holding_state_by_id.get(industry_id),
            warm_to_hot_count=len(warm_to_hot_ids[industry_id]),
            member_breadth_collected=False,
        )
        for industry_id in holding_only_ids
    )
    holding_contexts = attach_prior_context(holding_contexts, prior)
    contexts = tuple(
        sorted(
            (*candidate_contexts, *holding_contexts),
            key=lambda item: (
                item.strength is None or not item.strength.is_finite(),
                (
                    -item.strength
                    if item.strength is not None and item.strength.is_finite()
                    else Decimal("0")
                ),
                item.industry_tm_id,
            ),
        )
    )
    all_state_rows = [*state_rows, *holding_state_rows]
    facts = {
        "eligible_industry_ids": tuple(eligible_industry_ids),
        "holding_industry_ids": tuple(holding_industry_ids),
        "context_industry_ids": tuple(context_industry_ids),
        "holding_errors": holding_errors,
        "component_requests": len(eligible_industry_ids),
        "component_rows": component_rows_count,
        "component_rows_by_industry": component_rows_by_industry,
        "member_ids": tuple(member_ids),
        "member_rows": len(member_rows),
        "member_response": member_rows,
        "member_fields": INDUSTRY_MEMBER_FIELDS,
        "state_ids": tuple(context_industry_ids),
        "state_rows": len(all_state_rows),
        "state_response": all_state_rows,
        "state_fields": INDUSTRY_STATE_FIELDS,
    }
    return contexts, dict(ordering.industry_context_status), facts


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
            LEGACY_CN_TARGET_WEIGHTS.get(item.temperature_curr)
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
                futu_symbol=item.futu_symbol,
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
    cn_target_weights: Mapping[str, Decimal] = CN_TARGET_WEIGHTS,
    critical_data_reason: str = "",
    kelly_state: TrendKellyState | None = None,
) -> tuple[list[BuyAction], list[dict[str, object]], dict[str, object]]:
    def entry_weight(item: CandidateInput) -> Decimal | None:
        nominal = (
            cn_target_weights.get(item.temperature_curr)
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
            cn_target_weights.get(item.temperature_curr)
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
                futu_symbol=item.futu_symbol,
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
    overheat_trim_terminal: bool = False,
    current_exit_discipline: bool = False,
) -> tuple[str, str]:
    if symbol in triggered:
        return "SELL_ALL", "protection_line_already_triggered"
    if snapshot is not None and snapshot.danger is True:
        return "SELL_ALL", "danger_signal"
    if snapshot is not None and snapshot.right_side is False:
        return "SELL_ALL", "left_trend_right_side"
    temperature_exit = market == "CN" or current_exit_discipline
    if (
        temperature_exit
        and snapshot is not None
        and snapshot.temperature_prev in {"温", "热", "沸"}
        and snapshot.temperature_curr == "平"
    ):
        return "SELL_ALL", "temperature_changed_to_flat"
    if (
        not current_exit_discipline
        and snapshot is not None
        and (snapshot.boiling is True or snapshot.champagne is True)
        and not overheat_trim_terminal
    ):
        return "SELL_PARTIAL", "overheat_take_profit"
    if snapshot is None or any(
        signal is None
        for signal in (
            snapshot.right_side,
            snapshot.danger,
        )
    ) or (
        not current_exit_discipline
        and snapshot is not None
        and any(
            signal is None
            for signal in (snapshot.boiling, snapshot.champagne)
        )
    ) or (
        temperature_exit
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
    if snapshot.strength is None:
        hints.append("强度数据不可用")
    elif snapshot.strength < 95:
        hints.append(f"强度 {snapshot.strength}，低于入场线 95")
    if snapshot.industry_temperature is None:
        hints.append("行业温度数据不可用")
    elif snapshot.industry_temperature not in CN_ALLOWED_INDUSTRY_TEMPERATURES:
        hints.append(
            f"行业温度为{snapshot.industry_temperature}，未达到温、热或沸"
        )
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
            or holding.historical
            or holding.active_line is None
            or not holding.active_line.is_finite()
            or holding.active_line < 0
        ):
            return None, f"模拟持仓 {position.symbol} 活动保护线缺失，暂停新开仓"
        if (
            holding.close is None
            or not holding.close.is_finite()
            or holding.close <= 0
        ):
            return None, f"模拟持仓 {position.symbol} 价格缺失，暂停新开仓"
        planned_risk += position.quantity * (
            max(Decimal("0"), holding.close - holding.active_line)
            * price_fx_to_account_currency
            + holding.close * price_fx_to_account_currency * normal_cost_rate
        )
    return planned_risk, ""


def _evaluate_holding_positions(
    *,
    positions: Sequence[AccountPosition],
    holding_snapshots: Mapping[str, HoldingSnapshot | None],
    bars_by_symbol: Mapping[str, Sequence[DailyKlineBar] | None],
    prior_state: Mapping[str, object] | None,
    watch_events: Sequence[Mapping[str, object]],
    as_of_date: str,
    market: str,
    lot_sizes: Mapping[str, int] | None,
    current_exit_discipline: bool,
    read_only_real: bool,
    trend_excluded_symbols: Sequence[str] = (),
) -> HoldingEvaluation:
    old_positions = _state_positions(prior_state)
    decisions: list[HoldingDecision] = []
    new_positions: dict[str, object] = {}
    industries: Counter[str] = Counter()
    industry_values: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for position in positions:
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
        state_started_for = old_state.get("position_started_for")
        position_started_for = (
            state_started_for
            if isinstance(state_started_for, str) and state_started_for
            else as_of_date
        )
        overheat_trim_terminal = (
            state_started_for == position_started_for
            and old_state.get("overheat_trim_status") in {"complete", "below_lot"}
        )
        triggered = (
            {symbol}
            if _protection_was_triggered(symbol, old_state, watch_events)
            else set()
        )
        action, reason = _holding_action(
            symbol=symbol,
            snapshot=snapshot,
            triggered=triggered,
            market=market,
            overheat_trim_terminal=overheat_trim_terminal,
            current_exit_discipline=current_exit_discipline,
        )
        initial_line = _state_decimal(old_state, "initial_line")
        active_line = _state_decimal(old_state, "active_line")
        old_atr = _state_decimal(old_state, "atr14")
        tracking_active = old_state.get("tracking_active") is True
        if not current_exit_discipline and snapshot is not None and (
            snapshot.boiling is True or snapshot.champagne is True
        ):
            tracking_active = True
        if current_exit_discipline:
            tracking_active = False
        historical = not old_state
        daily_bars = tuple(bars_by_symbol.get(symbol) or ())
        current_atr, close, lows = _kline_metrics(
            daily_bars, before=as_of_date, expected_date=as_of_date
        )
        stale_kline = bool(daily_bars) and daily_bars[-1].date != as_of_date
        signal_complete = snapshot is not None and all(
            value is not None
            for value in (
                snapshot.right_side,
                snapshot.danger,
                snapshot.boiling,
                snapshot.champagne,
            )
        ) and (
            market != "CN"
            or (
                snapshot.temperature_prev in KNOWN_TEMPERATURES
                and snapshot.temperature_curr in KNOWN_TEMPERATURES
            )
        )
        can_build_line = (
            current_atr is not None
            and close is not None
            and not stale_kline
            and (not read_only_real or signal_complete)
        )
        if active_line is None and can_build_line:
            protection_anchor = (
                position.avg_cost_price
                if (
                    (read_only_real or current_exit_discipline)
                    and position.avg_cost_price is not None
                    and position.avg_cost_price.is_finite()
                    and position.avg_cost_price > 0
                )
                else close
            )
            initial_line = active_line = (
                protection_anchor - INITIAL_PROTECTION_ATR_MULTIPLE * current_atr
            )
        elif read_only_real and active_line is not None and can_build_line:
            protection_anchor = (
                position.avg_cost_price
                if position.avg_cost_price is not None
                and position.avg_cost_price.is_finite()
                and position.avg_cost_price > 0
                else close
            )
            recalculated_line = (
                protection_anchor - INITIAL_PROTECTION_ATR_MULTIPLE * current_atr
            )
            active_line = max(active_line, recalculated_line)
        if active_line is not None and tracking_active and action in {
            "HOLD", "SELL_PARTIAL"
        }:
            active_line = update_protection_line(
                old_line=active_line,
                boiling=True,
                champagne=False,
                prior_five_lows=lows,
            )
        if (active_line is None or stale_kline) and action == "HOLD":
            action, reason = "MANUAL_REVIEW", "holding_kline_unavailable"
        if read_only_real and not signal_complete and action == "HOLD":
            action, reason = "MANUAL_REVIEW", "holding_signal_unknown"
        if read_only_real and symbol in trend_excluded_symbols:
            action, reason = "MANUAL_REVIEW", "holding_trend_excluded"
        effective_atr = current_atr if current_atr is not None else old_atr
        target_fraction: Decimal | None = None
        estimated_shares: int | None = None
        lot_size: int | None = None
        overheat_signals: tuple[str, ...] = ()
        warnings: tuple[str, ...] = ()
        if action == "SELL_PARTIAL":
            lot_size = (
                100
                if market == "CN"
                else (lot_sizes or {}).get(symbol, 0)
                if market == "HK"
                else 1
            )
            if not isinstance(lot_size, int) or lot_size <= 0:
                action, reason = "MANUAL_REVIEW", "holding_lot_size_unavailable"
                lot_size = None
            else:
                target_fraction = OVERHEAT_TRIM_FRACTION
                estimated_shares = _floor_to_lot(
                    position.quantity * target_fraction, lot_size
                )
                overheat_signals = tuple(
                    signal
                    for signal in OVERHEAT_TRIM_SIGNALS
                    if getattr(snapshot, signal) is True
                )
                signal_unknown = snapshot is None or any(
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
                )
                kline_unavailable = (
                    not daily_bars
                    or stale_kline
                    or current_atr is None
                    or close is None
                )
                warnings = tuple(
                    warning
                    for warning, present in (
                        ("holding_signal_unknown", signal_unknown),
                        ("holding_kline_unavailable", kline_unavailable),
                    )
                    if present
                )
        industry = snapshot.industry if snapshot else ""
        if industry:
            industries[industry] += 1
            industry_values[industry] += position.market_value
        decisions.append(
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
                phase=snapshot.phase if snapshot else None,
                entry_hints=(
                    _holding_entry_hints(snapshot) if market == "CN" else ()
                ),
                historical=historical,
                position_started_for=(
                    position_started_for if action == "SELL_PARTIAL" else None
                ),
                target_fraction=target_fraction,
                estimated_shares=estimated_shares,
                lot_size=lot_size,
                overheat_signals=overheat_signals,
                warnings=warnings,
                futu_symbol=position.futu_symbol,
            )
        )
        new_state: dict[str, object] = {
            "atr14": str(effective_atr) if effective_atr is not None else "",
            "position_started_for": position_started_for,
            "tracking_active": tracking_active,
            "updated_for": as_of_date,
        }
        if initial_line is not None:
            new_state["initial_line"] = str(initial_line)
        if active_line is not None:
            new_state["active_line"] = str(active_line)
        for key in (
            "overheat_trim_status",
            "overheat_trim_target_qty",
            "overheat_trim_filled_qty",
            "overheat_trim_started_for",
        ):
            if isinstance(old_state.get(key), str):
                new_state[key] = old_state[key]
        new_positions[symbol] = new_state
    return HoldingEvaluation(
        decisions=tuple(decisions),
        protection_state={"schema_version": 1, "positions": new_positions},
        industry_counts=industries,
        industry_values=dict(industry_values),
    )


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
    industry_contexts: Sequence[IndustryContext] = (),
    industry_context_status: Mapping[str, object] | None = None,
    estimated_api_cost_complete: bool = True,
    real_holdings: RealHoldingInput | None = None,
    allocation_reference: Mapping[str, object] | None = None,
) -> TrendReport:
    symbol_mapping_required = (
        (metadata or {}).get("symbol_mapping_schema")
        == TREND_SYMBOL_MAPPING_SCHEMA
    )
    resolved_process_version = process_version or str(
        (metadata or {}).get("process_version")
        or (strategy_snapshot or {}).get("process_version")
        or ""
    )
    resolved_candidate_pool_ids: Sequence[int] = candidate_pool_ids
    if not resolved_candidate_pool_ids and strategy_snapshot is not None:
        supplied_parameters = strategy_snapshot.get("parameters")
        supplied_pool_ids = (
            supplied_parameters.get("candidate_pool_ids")
            if isinstance(supplied_parameters, Mapping)
            else None
        )
        if isinstance(supplied_pool_ids, list) and all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in supplied_pool_ids
        ):
            resolved_candidate_pool_ids = tuple(supplied_pool_ids)
    canonical_strategy_snapshot = _expected_report_strategy_snapshot(
        market,
        resolved_process_version,
        resolved_candidate_pool_ids,
        strategy_snapshot,
    )
    if strategy_snapshot is None:
        resolved_strategy_snapshot = canonical_strategy_snapshot
    else:
        normalized_strategy_snapshot = normalize_trend_strategy_snapshot(
            strategy_snapshot,
            market,
            expected_snapshot=canonical_strategy_snapshot,
        )
        # v1 reports must replay with their original nominal-sizing contract;
        # normalization is validation here, not an upgrade of historical facts.
        resolved_strategy_snapshot = (
            {**dict(strategy_snapshot), "process_version": resolved_process_version}
            if _preserve_v1_replay_snapshot(strategy_snapshot)
            else normalized_strategy_snapshot
        )
    snapshot_version = str(resolved_strategy_snapshot.get("strategy_version") or "")
    current_exit_discipline = (
        market.upper(), snapshot_version
    ) in CURRENT_EXIT_DISCIPLINES
    snapshot_parameters = resolved_strategy_snapshot.get("parameters")
    cny_per_local_currency = CNY_PER_LOCAL_CURRENCY.get(market, Decimal("1"))
    if isinstance(snapshot_parameters, Mapping):
        frozen_rate = snapshot_parameters.get("cny_per_local_currency")
        if frozen_rate is not None:
            cny_per_local_currency = _decimal(frozen_rate)
    raw_cn_weights = (
        snapshot_parameters.get("target_weight")
        if isinstance(snapshot_parameters, Mapping)
        else None
    )
    try:
        cn_target_weights = (
            {
                key: Decimal(str(raw_cn_weights[key]))
                for key in ("热", "沸")
            }
            if market == "CN" and isinstance(raw_cn_weights, Mapping)
            else {
                key: Decimal(str(raw_cn_weights))
                for key in ("热", "沸")
            }
            if market == "CN" and snapshot_version in {"v11", "v12"}
            else CN_TARGET_WEIGHTS
        )
    except (InvalidOperation, KeyError, ValueError):
        raise ValueError("strategy snapshot has invalid CN target weights") from None
    if any(
        not weight.is_finite() or weight <= 0 for weight in cn_target_weights.values()
    ):
        raise ValueError("strategy snapshot has invalid CN target weights")
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
        if snapshot_version in {"v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"} and kelly_data_reason
        else calculate_trend_kelly(
            kelly_rounds,
            market=market,
            strategy_id=str(resolved_strategy_snapshot.get("strategy_id") or ""),
            opening_strategy_version=snapshot_version,
        )
        if snapshot_version in {"v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"}
        else None
    )
    held_symbols = {position.symbol for position in account.positions}
    frozen_industry_contexts = tuple(industry_contexts)
    industry_context_map = {
        context.industry_tm_id: context for context in frozen_industry_contexts
    }
    candidate_decision = build_candidate_list(
        candidates,
        held_symbols=held_symbols,
        expected_date=as_of_date,
        market=market,
        industry_contexts=industry_context_map,
        strategy_version=snapshot_version,
        cny_per_local_currency=cny_per_local_currency,
    )
    resolved_industry_context_status = dict(
        candidate_decision.industry_context_status
    )
    supplied_status = dict(industry_context_status or {})
    if supplied_status.get("ordering_mode") == candidate_decision.ordering_mode:
        resolved_industry_context_status.update(
            {
                key: value
                for key, value in supplied_status.items()
                if key
                not in {
                    "ordering_mode",
                    "current_complete",
                    "history_complete",
                    "fallback_reason",
                }
            }
        )
    displayed_candidates = candidate_decision.eligible[:CANDIDATE_LIMIT]
    simulated_evaluation = _evaluate_holding_positions(
        positions=account.positions,
        holding_snapshots=holding_snapshots,
        bars_by_symbol=bars_by_symbol,
        prior_state=prior_state,
        watch_events=watch_events,
        as_of_date=as_of_date,
        market=market,
        lot_sizes=lot_sizes,
        current_exit_discipline=current_exit_discipline,
        read_only_real=False,
        trend_excluded_symbols=(),
    )
    holdings = list(simulated_evaluation.decisions)
    new_positions = simulated_evaluation.protection_state["positions"]
    assert isinstance(new_positions, dict)
    industries = simulated_evaluation.industry_counts
    industry_values = defaultdict(
        lambda: Decimal("0"), simulated_evaluation.industry_values
    )
    real_decisions: tuple[HoldingDecision, ...] = ()
    real_protection_state: dict[str, object] | None = None
    if real_holdings is not None and real_holdings.status == "available":
        real_evaluation = _evaluate_holding_positions(
            positions=real_holdings.positions,
            holding_snapshots=real_holdings.holding_snapshots,
            bars_by_symbol=real_holdings.bars_by_symbol,
            prior_state=real_holdings.prior_state,
            watch_events=(),
            as_of_date=as_of_date,
            market=market,
            lot_sizes=lot_sizes,
            current_exit_discipline=current_exit_discipline,
            read_only_real=True,
            trend_excluded_symbols=real_holdings.trend_excluded_symbols,
        )
        real_decisions = real_evaluation.decisions
        real_protection_state = real_evaluation.protection_state
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
            cn_target_weights=cn_target_weights,
            critical_data_reason=critical_data_reason,
            kelly_state=kelly_state,
        )
        if snapshot_version in {"v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"} and (
            not valid_drawdown_decision(
                drawdown_summary,
                expected_market=market,
                expected_strategy_id=str(
                    resolved_strategy_snapshot.get("strategy_id") or ""
                ),
                expected_strategy_version=snapshot_version,
                expected_equity=account.net_value,
                expected_entry_date=execution_date,
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
                        cn_target_weights.get(item.temperature_curr)
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
    if symbol_mapping_required:
        missing_mapping = {
            (item.tm_id, item.symbol)
            for item in candidate_decision.eligible
            if not item.futu_symbol
        }
        buy_actions = [
            action for action in buy_actions
            if action.futu_symbol
        ]
        risk_skips = [
            skip
            for skip in risk_skips
            if (skip.get("tm_id"), skip.get("symbol")) not in missing_mapping
        ]
        risk_skips.extend(
            _risk_skip(
                item,
                weight=(
                    cn_target_weights.get(item.temperature_curr)
                    if market == "CN"
                    else position_weight
                ),
                target_amount=None,
                reason="symbol_mapping_unavailable",
                decisive_constraint="趋势代码映射",
            )
            for item in candidate_decision.eligible
            if (item.tm_id, item.symbol) in missing_mapping
        )
        if snapshot_version != "v1":
            existing_risk = _nonnegative_risk_decimal(
                risk_summary.get("existing_planned_risk")
            )
            risk_summary = _risk_summary(
                net_value=account.net_value,
                existing_planned_risk=existing_risk,
                new_planned_risk=sum(
                    (item.planned_stop_risk for item in buy_actions),
                    Decimal("0"),
                ),
                normal_cost_rate=normal_cost_rate,
                pause_reason=str(risk_summary.get("pause_reason") or ""),
                kelly_state=kelly_state,
            )

    drawdown_pause_reason = ""
    if snapshot_version in {"v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"} and (
        not valid_drawdown_decision(
            drawdown_summary,
            expected_market=market,
            expected_strategy_id=str(resolved_strategy_snapshot.get("strategy_id") or ""),
            expected_strategy_version=snapshot_version,
            expected_equity=account.net_value,
            expected_entry_date=execution_date,
        )
        or drawdown_summary.get("entry_allowed") is not True
    ):
        drawdown_pause_reason = (
            str(drawdown_summary.get("pause_reason") or "")
            if isinstance(drawdown_summary, Mapping)
            else ""
        ) or "策略累计回撤状态无效，暂停新开仓"
    allocation_sha256 = (
        snapshot_parameters.get("allocation_snapshot_sha256")
        if isinstance(snapshot_parameters, Mapping)
        else None
    )
    rotation_enabled = (
        snapshot_version == ALLOCATION_PROJECTION_VERSIONS[market]
        and isinstance(allocation_sha256, str)
        and len(allocation_sha256) == 64
        and all(character in "0123456789abcdef" for character in allocation_sha256)
    )

    frozen_allocation = freeze_allocation_reference(allocation_reference)
    simulate_rotation_pairs: tuple[RotationPair, ...] = ()
    simulate_rotation_comparisons: tuple[RotationComparison, ...] = ()
    if (
        rotation_enabled
        and post_sell_position_count == POSITION_LIMIT
        and not buy_actions
        and not drawdown_pause_reason
    ):
        simulate_rotation_pairs, simulate_rotation_comparisons = _plan_account_rotation_pairs(
            account=account,
            holdings=holdings,
            holding_snapshots=holding_snapshots,
            candidates=candidate_decision.eligible,
            entry_weight=position_weight,
            forced_sell_symbols=sell_symbols,
            market=market,
            lot_sizes=lot_sizes,
            price_fx_to_account_currency=price_fx_to_account_currency,
            normal_cost_rate=normal_cost_rate,
            cn_target_weights=cn_target_weights,
            kelly_state=kelly_state,
            critical_data_reason=kelly_data_reason,
            require_mapping=symbol_mapping_required,
        )
        simulate_rotation_pairs = tuple(
            replace(
                pair,
                execution_date=execution_date,
                execution_mode="automatic",
            )
            for pair in simulate_rotation_pairs
        )
    real_rotation_pairs: tuple[RotationPair, ...] = ()
    real_rotation_comparisons: tuple[RotationComparison, ...] = ()
    if real_holdings is not None and real_holdings.status == "available":
        real_sell_symbols = {
            decision.symbol
            for decision in real_decisions
            if decision.action == "SELL_ALL"
        }
        real_post_sell_position_count = max(
            0,
            (real_holdings.position_count or len(real_holdings.positions))
            - len(real_sell_symbols),
        )
        if (
            rotation_enabled
            and real_post_sell_position_count == POSITION_LIMIT
            and not drawdown_pause_reason
        ):
            real_account = AccountSnapshot(
                source_date=as_of_date,
                fresh=True,
                net_value=real_holdings.net_value,
                available_cash=real_holdings.available_cash,
                positions=real_holdings.positions,
                exceptions=(),
                position_count=real_holdings.position_count,
            )
            real_rotation_pairs, real_rotation_comparisons = _plan_account_rotation_pairs(
                account=real_account,
                holdings=real_decisions,
                holding_snapshots=real_holdings.holding_snapshots,
                candidates=candidate_decision.eligible,
                entry_weight=position_weight,
                forced_sell_symbols=real_sell_symbols,
                market=market,
                lot_sizes=lot_sizes,
                price_fx_to_account_currency=price_fx_to_account_currency,
                normal_cost_rate=normal_cost_rate,
                cn_target_weights=cn_target_weights,
                kelly_state=kelly_state,
                critical_data_reason=kelly_data_reason,
                require_mapping=symbol_mapping_required,
            )
            real_rotation_pairs = tuple(
                replace(
                    pair,
                    execution_date=execution_date,
                    execution_mode="manual",
                )
                for pair in real_rotation_pairs
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
    real_holding_signals = {
        position.symbol: (
            _holding_signal(
                real_holdings.holding_snapshots[position.symbol],
                market=market,
            )
            if real_holdings.holding_snapshots.get(position.symbol) is not None
            else None
        )
        for position in real_holdings.positions
    } if real_holdings is not None and real_holdings.status == "available" else {}
    excluded_signals = {
        symbol: [
            _candidate_signal(
                item,
                market=market,
                strategy_version=snapshot_version,
                cny_per_local_currency=cny_per_local_currency,
            )
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
            **_candidate_signal(
                item,
                market=market,
                strategy_version=snapshot_version,
                cny_per_local_currency=cny_per_local_currency,
            ),
            "eligible": (item.tm_id, item.symbol) in ranks,
            "excluded_reasons": _candidate_reasons(
                item,
                held_symbols,
                as_of_date,
                market=market,
                strategy_version=snapshot_version,
                cny_per_local_currency=cny_per_local_currency,
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
            **(
                {"real_holdings": real_holding_signals}
                if real_holdings is not None and real_holdings.status == "available"
                else {}
            ),
        },
        metadata={
            **dict(metadata or {}),
            "position_weight": str(position_weight),
            "position_weight_source": position_weight_source,
        },
        strategy_snapshot=resolved_strategy_snapshot,
        industry_contexts=frozen_industry_contexts,
        industry_context_status=resolved_industry_context_status,
        estimated_api_cost_complete=estimated_api_cost_complete,
        drawdown_summary=(
            dict(drawdown_summary) if drawdown_summary is not None else None
        ),
        replay_evidence=None,
        real_holdings=real_decisions,
        real_holdings_status=(real_holdings.status if real_holdings is not None else None),
        real_holdings_reason=(real_holdings.reason if real_holdings is not None else ""),
        real_holdings_source=(dict(real_holdings.source) if real_holdings is not None else {}),
        real_protection_state=real_protection_state,
        allocation=frozen_allocation,
        simulate_rotation_pairs=simulate_rotation_pairs,
        real_rotation_pairs=real_rotation_pairs,
        simulate_rotation_comparisons=simulate_rotation_comparisons,
        real_rotation_comparisons=real_rotation_comparisons,
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
        "asset": item.asset,
        "industry": item.industry,
        "industry_tm_id": item.industry_tm_id,
        "industry_temperature": item.industry_temperature,
        "filter_price": item.filter_price,
        "market_cap": item.market_cap,
        "days": item.days,
        "strength": item.strength,
        "temperature_prev": item.temperature_prev,
        "temperature_curr": item.temperature_curr,
        "phase": item.phase,
        **_paid_expansion_signal(item),
    }
    if market.upper() in {"US", "HK"}:
        signal.update(name=item.name)
    return signal


def _candidate_signal(
    item: CandidateInput,
    *,
    market: str,
    strategy_version: str | None = None,
    cny_per_local_currency: Decimal | None = None,
) -> dict[str, object]:
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
    shared_discipline = _uses_shared_entry_discipline(market, strategy_version)
    if shared_discipline:
        rate = (
            cny_per_local_currency
            if cny_per_local_currency is not None
            else CNY_PER_LOCAL_CURRENCY.get(market.upper(), Decimal("1"))
        )
        signal.update(
            {
                "market_value_currency": MARKET_CURRENCY[market.upper()],
                "cny_per_local_currency": rate,
                "market_cap_cny_100m": (
                    item.market_cap * rate if item.market_cap is not None else None
                ),
                "amount_cny_100m": (
                    item.amount * rate if item.amount is not None else None
                ),
                "market_cap_cny_threshold_met": (
                    item.market_cap * rate >= CN_MIN_MARKET_CAP_100M
                    if item.market_cap is not None
                    else None
                ),
                "amount_cny_threshold_met": (
                    item.amount * rate >= CN_MIN_AMOUNT_100M
                    if item.amount is not None
                    else None
                ),
            }
        )
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
    "SELL_PARTIAL": "止盈减仓 30%",
    "HOLD": "继续持有",
    "MANUAL_REVIEW": "人工复核",
}

REASON_LABELS = {
    "protection_line_already_triggered": "活动保护线已触发",
    "danger_signal": "危险信号触发",
    "left_trend_right_side": "右侧趋势已结束",
    "holding_signal_unknown": "趋势信号不完整",
    "holding_trend_excluded": "已排除趋势查询",
    "holding_kline_unavailable": "持仓日线数据不可用",
    "holding_lot_size_unavailable": "持仓整手信息不可用",
    "stale_tiger_account": "老虎账户数据非实时，禁止新增买入；持仓需复核",
    "trend_intact": "趋势保持完好",
    "temperature_changed_to_flat": "趋势温度转平",
    "overheat_take_profit": "沸腾/开香槟过热止盈",
    "a_share_only": "仅限 A 股股票",
    "temperature_missing": "个股趋势温度缺失",
    "temperature_transition_not_entry": "不是温转热或温转沸",
    "filter_price_missing": "筛选价缺失",
    "filter_price_above_200": "筛选价高于 200 元",
    "strength_missing": "趋势强度缺失",
    "strength_below_95": "趋势强度低于 95",
    "industry_id_missing": "行业 ID 缺失",
    "industry_temperature_missing": "行业温度缺失",
    "industry_temperature_not_hot": "行业温度未达到要求",
    "phase_missing": "趋势节气缺失",
    "phase_after_summer_solstice": "趋势节气晚于夏至",
    "market_cap_missing": "市值缺失",
    "market_cap_below_100": "市值低于 100 亿元",
    "market_cap_below_100_cny": "市值折算人民币后低于 100 亿元",
    "amount_missing": "日成交额缺失",
    "amount_below_2": "日成交额不足 2 亿元",
    "amount_below_2_cny": "日成交额折算人民币后不足 2 亿元",
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


def _holding_reason_label(
    item: Mapping[str, object],
    *,
    current_exit_discipline: bool,
) -> str:
    reason = str(item.get("reason") or "")
    if reason != "protection_line_already_triggered" or not current_exit_discipline:
        return _reason_label(reason)
    initial = _optional_decimal(item.get("initial_line"))
    active = _optional_decimal(item.get("active_line"))
    return (
        "2×ATR14 硬止损"
        if initial is not None and active == initial
        else "既有活动保护线触发"
    )


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


def _feishu_reason(
    item: Mapping[str, object], *, current_exit_discipline: bool = False
) -> str:
    reason = str(item.get("reason") or "")
    if reason not in REASON_LABELS:
        return "未知动作或原因，需人工确认"
    return _holding_reason_label(
        item, current_exit_discipline=current_exit_discipline
    )


def _feishu_money(value: object) -> str:
    return _money(Decimal(str(value))).rstrip("0").rstrip(".")


def _append_feishu_action_sections(
    lines: list[str],
    sells: Sequence[Mapping[str, object]],
    buys: Sequence[Mapping[str, object]],
    reviews: Sequence[Mapping[str, object]],
    *,
    market: str,
    current_exit_discipline: bool = False,
) -> None:
    if sells:
        lines.extend(["", "卖出"])
        for index, item in enumerate(sells, 1):
            line = (
                f"{index}. {_feishu_identity(item)}｜"
                f"{_feishu_reason(item, current_exit_discipline=current_exit_discipline)}"
            )
            if item.get("action") == "SELL_PARTIAL":
                signals = {
                    "boiling": "沸腾",
                    "champagne": "开香槟",
                }
                line += f"｜{_action_label('SELL_PARTIAL')}"
                line += f"｜模拟预计数量 {item.get('estimated_shares', '-')} 股"
                if item.get("lot_size") not in {None, ""}:
                    line += f"｜每手 {item['lot_size']} 股"
                triggered = [
                    signals[value]
                    for value in item.get("overheat_signals", [])
                    if value in signals
                ]
                if triggered:
                    line += f"｜触发信号 {'、'.join(triggered)}"
                warnings = [
                    _reason_label(value)
                    for value in item.get("warnings", [])
                    if value in REASON_LABELS
                ]
                if warnings:
                    line += f"｜提示 {'、'.join(warnings)}"
            elif item.get("action") == "SELL_ALL":
                line += f"｜{_action_label('SELL_ALL')}"
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
            f"{index}. {_feishu_identity(item)}｜"
            f"{_feishu_reason(item, current_exit_discipline=current_exit_discipline)}"
            for index, item in enumerate(reviews, 1)
        )


def _serialized_api_cost_label(payload: Mapping[str, object]) -> str | None:
    api_cost = payload.get("api_cost")
    if isinstance(api_cost, Mapping) and isinstance(api_cost.get("label"), str):
        return api_cost["label"]
    if "actual_api_cost" not in payload and "estimated_api_cost" not in payload:
        return None
    actual = payload.get("actual_api_cost")
    estimated = payload.get("estimated_api_cost")
    try:
        actual_decimal = None if actual is None else Decimal(str(actual))
        estimated_decimal = None if estimated is None else Decimal(str(estimated))
    except (InvalidOperation, TypeError, ValueError):
        return None
    complete = (
        api_cost.get("estimate_complete")
        if isinstance(api_cost, Mapping) and "estimate_complete" in api_cost
        else payload.get("estimated_api_cost_complete", True)
    )
    return trend_api_cost_label(
        actual=actual_decimal,
        estimated=estimated_decimal,
        estimate_complete=complete is True,
    )


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
    strategy_snapshot = payload.get("strategy_snapshot")
    strategy_version = (
        str(strategy_snapshot.get("strategy_version") or "")
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    current_exit_discipline = (market, strategy_version) in CURRENT_EXIT_DISCIPLINES
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
        if item.get("action") in {"SELL_ALL", "SELL_PARTIAL"}
        and not _trend_action_needs_review(item)
    ]
    sells.extend(
        item
        for item in holdings
        if item.get("action") == "SELL_PARTIAL"
        and not _trend_action_needs_review(item)
        and item not in sells
    )
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
    title = render_daily_title(broker_label, market_label, execution_date)
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
    allocation = payload.get("allocation")
    if allocation is not None:
        if not isinstance(allocation, Mapping):
            raise ValueError("冻结配置无效")
        lines.extend(
            _allocation_markdown_lines(allocation, execution_date=execution_date)
        )
    if cost_label := _serialized_api_cost_label(payload):
        lines.append(cost_label)
    _append_feishu_action_sections(
        lines,
        sells,
        (),
        (),
        market=market,
        current_exit_discipline=current_exit_discipline,
    )
    if allocation is not None:
        currency = {"CN": "元", "HK": "港元", "US": "美元"}.get(market, "")
        simulate_comparisons = [
            item for item in judgments.get("simulate_rotation_comparisons", [])
            if isinstance(item, Mapping)
        ]
        real_comparisons = [
            item for item in judgments.get("real_rotation_comparisons", [])
            if isinstance(item, Mapping)
        ]
        if simulate_comparisons:
            lines.extend(
                _rotation_comparison_markdown_lines(
                    simulate_comparisons,
                    [
                        item for item in judgments.get("simulate_rotation_pairs", [])
                        if isinstance(item, Mapping)
                    ],
                    title="模拟盘自动轮换",
                    currency=currency,
                )
            )
        else:
            lines.extend(
                _rotation_markdown_lines(
                    [
                        item for item in judgments.get("simulate_rotation_pairs", [])
                        if isinstance(item, Mapping)
                    ],
                    title="模拟盘自动轮换",
                    currency=currency,
                )
            )
        if real_comparisons:
            lines.extend(
                _rotation_comparison_markdown_lines(
                    real_comparisons,
                    [
                        item for item in judgments.get("real_rotation_pairs", [])
                        if isinstance(item, Mapping)
                    ],
                    title="实盘手动轮换建议",
                    currency=currency,
                )
            )
        else:
            lines.extend(
                _rotation_markdown_lines(
                    [
                        item for item in judgments.get("real_rotation_pairs", [])
                        if isinstance(item, Mapping)
                    ],
                    title="实盘手动轮换建议",
                    currency=currency,
                )
            )
    _append_feishu_action_sections(
        lines,
        (),
        buys,
        reviews,
        market=market,
        current_exit_discipline=current_exit_discipline,
    )
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
    return render_attention(
        broker_label,
        f"{market_label}趋势报告生成失败",
        report_date,
        happened="趋势报告未生成",
        impact="不能依据旧报告交易",
        action=recovery_action,
        detail=reason,
    )


def _industry_ratio_percent(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{_industry_number(value * Decimal('100'))}%"


def _industry_number(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")).normalize(), "f")


def _industry_ratio_transition(
    current: Decimal | None,
    prior: Decimal | None,
) -> str:
    current_text = _industry_ratio_percent(current)
    if current_text is None:
        return "未提供"
    prior_text = _industry_ratio_percent(prior)
    return (
        f"{prior_text} → {current_text}"
        if prior_text is not None
        else f"{current_text} · 基准建立中"
    )


def _industry_structure_explanation(context: IndustryContext) -> str:
    count = context.aggregate_right_count_ratio
    market_cap = context.aggregate_right_market_cap_ratio
    if count is None or market_cap is None:
        return ""
    prior_market_cap = context.prior_aggregate_right_market_cap_ratio
    gap = (market_cap - count) * Decimal("100")
    relation = "高于" if gap > 0 else "低于" if gap < 0 else "等于"
    bias = (
        "右侧更偏大市值成分"
        if gap > 0
        else "右侧更偏小市值成分"
        if gap < 0
        else "两个占比相同"
    )
    text = (
        f"右侧市值占比{relation}右侧个数占比 {_industry_number(abs(gap))} 个百分点，"
        f"{bias}。"
    )
    if prior_market_cap is not None:
        market_cap_change = (market_cap - prior_market_cap) * Decimal("100")
        text = (
            "较前一有效交易日"
            f"{'上升' if market_cap_change > 0 else '下降' if market_cap_change < 0 else '持平'}"
            f" {_industry_number(abs(market_cap_change))} 个百分点。"
            + text
        )
    prior_count = context.prior_aggregate_right_count_ratio
    if prior_count is not None and prior_market_cap is not None:
        gap_change = gap - (prior_market_cap - prior_count) * Decimal("100")
        text += (
            f"结构差较前值{'扩大' if gap_change > 0 else '收窄' if gap_change < 0 else '持平'}"
            f" {_industry_number(abs(gap_change))} 个百分点。"
        )
    return text + "该指标不是账户仓位或上涨概率。"


def _allocation_markdown_lines(
    allocation: Mapping[str, object], *, execution_date: str
) -> list[str]:
    if not valid_frozen_allocation(allocation):
        raise ValueError("frozen allocation is invalid")
    roots = allocation["roots"]
    markets = allocation["markets"]
    assert isinstance(roots, Mapping) and isinstance(markets, Mapping)
    lines = ["", "## 市场资源排名", ""]
    if allocation["reused"]:
        lines.append(
            f"沿用旧排名 · {allocation['stale_a_trading_days']} 个 A 股交易日"
        )
    for market, label in (("CN", "A股"), ("HK", "港股"), ("US", "美股")):
        root = roots[market]
        values = markets[market]
        assert isinstance(root, Mapping) and isinstance(values, Mapping)
        stock = root["stock"]
        etf = root["etf"]
        assert isinstance(stock, Mapping) and isinstance(etf, Mapping)
        lines.append(
            f"- {label} 第 {values['rank']}｜{stock['asset']} 全局强度 {stock['global_strength']}"
            f"｜{etf['asset']} 全局强度 {etf['global_strength']}｜分数来源 {values['score_source']}"
            f"｜单仓基准 {_money(Decimal(str(values['entry_weight'])) * Decimal('100'))}%"
            f"｜10 席位名义仓位 {_money(Decimal(str(values['nominal_weight'])) * Decimal('100'))}%"
            f"｜来源 {stock['as_of_date']}/{etf['as_of_date']}"
        )
    lines.append(
        f"- 快照 {allocation['allocation_date']}｜生成 {allocation['generated_at']}"
        f"｜目标交易日 {execution_date}｜SHA {str(allocation['sha256'])[:12]}"
    )
    if allocation["failure_reason"]:
        lines.append(f"- 本次更新失败原因：{allocation['failure_reason']}")
    return lines


def _rotation_markdown_lines(
    pairs: Sequence[RotationPair] | Sequence[Mapping[str, object]], *,
    title: str,
    currency: str,
) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not pairs:
        return [*lines, "- 无。"]
    for pair in pairs:
        raw = asdict(pair) if isinstance(pair, RotationPair) else pair
        lines.append(
            f"- 全部卖出 {raw['sell_symbol']} {raw['sell_name']}（全局强度 {raw['sell_global_strength']}）"
            f"，再买入 {raw['buy_symbol']} {raw['buy_name']}（全局强度 {raw['buy_global_strength']}）"
            f"｜差值 {raw['strength_gap']}｜目标仓位 {_money(Decimal(str(raw['target_weight'])) * Decimal('100'))}%"
            f"｜金额 {_money(Decimal(str(raw['target_amount'])))} {currency}｜约 {raw['estimated_shares']} 股"
            f"｜MARKET 卖出全成后才买入｜目标交易日 {raw['execution_date']}｜不得跨日"
        )
    return lines


def _rotation_comparison_markdown_lines(
    comparisons: Sequence[RotationComparison] | Sequence[Mapping[str, object]],
    pairs: Sequence[RotationPair] | Sequence[Mapping[str, object]],
    *,
    title: str,
    currency: str,
) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not comparisons:
        return [*lines, "- 无。"]
    pair_by_index = {
        int(raw.get("pair_index")): raw
        for item in pairs
        for raw in (asdict(item) if isinstance(item, RotationPair) else item,)
        if isinstance(raw, Mapping) and isinstance(raw.get("pair_index"), int)
    }
    for item in comparisons:
        raw = asdict(item) if isinstance(item, RotationComparison) else item
        basis = {
            "local": "大类内强度",
            "global": "全局强度",
        }.get(raw.get("strength_basis"), "数据不可用")
        outcome = str(raw.get("outcome") or "data_unavailable")
        gap = raw.get("strength_gap")
        gap_text = "数据未提供" if gap is None else str(gap)
        if outcome == "planned":
            pair = pair_by_index.get(raw.get("pair_index"))
            if pair is None:
                lines.append(
                    f"- 已进入轮换｜比较口径 {basis}｜买入 {raw.get('buy_symbol')} "
                    f"{raw.get('buy_name')}｜卖出 {raw.get('sell_symbol')} {raw.get('sell_name')}"
                    f"｜差值 {gap_text}｜门槛 20"
                )
                continue
            lines.append(
                f"- 已进入轮换｜比较口径 {basis}｜全部卖出 {pair['sell_symbol']} "
                f"{pair['sell_name']}，再买入 {pair['buy_symbol']} {pair['buy_name']}"
                f"｜差值 {gap_text}｜门槛 20"
                f"｜目标仓位 {_money(Decimal(str(pair['target_weight'])) * Decimal('100'))}%"
                f"｜金额 {_money(Decimal(str(pair['target_amount'])))} {currency}"
                f"｜约 {pair['estimated_shares']} 股｜MARKET 卖出全成后才买入"
                f"｜目标交易日 {pair.get('execution_date') or '待定'}｜不得跨日"
            )
            continue
        status = {
            "gap_below_threshold": "未触发",
            "sizing_blocked": "仓位规则阻止",
            "data_unavailable": "数据不可用",
        }.get(outcome, outcome)
        reason = str(raw.get("reason") or "数据未提供")
        threshold = raw.get("threshold") or Decimal("20")
        remaining = ""
        if outcome == "gap_below_threshold" and gap is not None:
            try:
                remaining = f"｜还差 {_money(Decimal(str(threshold)) - Decimal(str(gap)))}"
            except (InvalidOperation, ValueError):
                remaining = ""
        lines.append(
            f"- {status}｜比较口径 {basis}｜卖出 {raw.get('sell_symbol')} "
            f"{raw.get('sell_name')}｜买入 {raw.get('buy_symbol')} {raw.get('buy_name')}"
            f"｜实际差值 {gap_text}｜门槛 {threshold}{remaining}｜原因 {reason}"
        )
    return lines


def render_markdown(report: TrendReport) -> str:
    market = str(report.metadata.get("market") or "CN").upper()
    strategy_version = str(report.strategy_snapshot.get("strategy_version") or "")
    current_exit_discipline = (market, strategy_version) in CURRENT_EXIT_DISCIPLINES
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
    sells = [
        item
        for item in report.holdings
        if item.action in {"SELL_ALL", "SELL_PARTIAL"}
    ]
    full_sells = [item for item in sells if item.action == "SELL_ALL"]
    partial_sells = [item for item in sells if item.action == "SELL_PARTIAL"]
    holds = [item for item in report.holdings if item.action == "HOLD"]
    reviews = [item for item in report.holdings if item.action == "MANUAL_REVIEW"]
    others = [item for item in report.holdings if item.action not in ACTION_LABELS]
    industry_facts = {
        industry: (count, weight)
        for industry, count, weight in report.industry_concentration
    }
    summary_counts = [f"全部卖出 {len(full_sells)}"]
    if not current_exit_discipline:
        summary_counts.append(f"止盈减仓 30% {len(partial_sells)}")
    summary_counts.extend(
        [
            f"允许买入 {len(report.buy_actions)}",
            f"继续持有 {len(holds)}",
            f"人工复核 {len(reviews)}",
            f"其他动作 {len(others)}",
        ]
    )
    lines = [
        f"# {market_label}趋势操作计划 · {report.execution_date}",
        "",
        "## 操作摘要",
        "",
        f"数据日期：{report.as_of_date}｜生成时间：{report.generated_at}｜账户：{freshness}",
        "｜".join(summary_counts),
    ]
    if report.allocation is not None:
        lines.extend(
            _allocation_markdown_lines(
                report.allocation, execution_date=report.execution_date
            )
        )
    if report.strategy_snapshot.get("strategy_version") in {
        "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12",
    }:
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
            reason = _holding_reason_label(
                asdict(item), current_exit_discipline=current_exit_discipline
            )
            line = (
                f"- {item.symbol} {item.name}｜{_action_label(item.action)}｜"
                f"{reason}"
            )
            if item.action == "SELL_PARTIAL":
                signals = {
                    "boiling": "沸腾",
                    "champagne": "开香槟",
                }
                line += f"｜模拟预计数量 {item.estimated_shares} 股"
                if item.lot_size is not None:
                    line += f"｜每手 {item.lot_size} 股"
                triggered = [
                    signals[value]
                    for value in item.overheat_signals
                    if value in signals
                ]
                if triggered:
                    line += f"｜触发信号 {'、'.join(triggered)}"
                warnings = [
                    _reason_label(value)
                    for value in item.warnings
                    if value in REASON_LABELS
                ]
                if warnings:
                    line += f"｜提示 {'、'.join(warnings)}"
            if item.active_line is not None:
                line += f"｜活动保护线 {_money(item.active_line)}"
            lines.append(line)
    else:
        lines.append("- 无需卖出。")

    if report.allocation is not None:
        if report.simulate_rotation_comparisons:
            lines.extend(
                _rotation_comparison_markdown_lines(
                    report.simulate_rotation_comparisons,
                    report.simulate_rotation_pairs,
                    title="模拟盘自动轮换",
                    currency=currency,
                )
            )
        else:
            lines.extend(
                _rotation_markdown_lines(
                    report.simulate_rotation_pairs,
                    title="模拟盘自动轮换",
                    currency=currency,
                )
            )
        if report.real_rotation_comparisons:
            lines.extend(
                _rotation_comparison_markdown_lines(
                    report.real_rotation_comparisons,
                    report.real_rotation_pairs,
                    title="实盘手动轮换建议",
                    currency=currency,
                )
            )
        else:
            lines.extend(
                _rotation_markdown_lines(
                    report.real_rotation_pairs,
                    title="实盘手动轮换建议",
                    currency=currency,
                )
            )

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
        reason = _holding_reason_label(
            asdict(item), current_exit_discipline=current_exit_discipline
        )
        line = (
            f"- {item.symbol} {item.name}｜{_action_label(item.action)}｜"
            f"{reason}"
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

    lines.extend(["", "### 行业上下文", ""])
    if report.industry_contexts:
        for context in report.industry_contexts:
            explanation = _industry_structure_explanation(context)
            line = (
                f"- {context.industry or '未知行业'}｜"
                "右侧个数占比 "
                f"{_industry_ratio_transition(context.aggregate_right_count_ratio, context.prior_aggregate_right_count_ratio)}｜"
                "右侧市值占比 "
                f"{_industry_ratio_transition(context.aggregate_right_market_cap_ratio, context.prior_aggregate_right_market_cap_ratio)}"
            )
            if explanation:
                line += f"｜{explanation}"
            lines.append(line)
    else:
        lines.append("- 无可用行业上下文。")

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
        "- "
        + trend_api_cost_label(
            actual=report.actual_api_cost,
            estimated=report.estimated_api_cost,
            estimate_complete=report.estimated_api_cost_complete,
        )
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


def _candidate_ordering_context(
    item: CandidateInput,
    *,
    contexts: Mapping[int, IndustryContext],
    status: Mapping[str, object],
) -> dict[str, object]:
    mode = str(status.get("ordering_mode") or "legacy_invalid_current")
    context = contexts.get(item.industry_tm_id) if item.industry_tm_id is not None else None
    if mode not in {"context_with_history", "context_current_only"} or context is None:
        return {
            "applied": False,
            "industry_tm_id": item.industry_tm_id,
            "ordering_mode": mode,
            "fallback_reason": status.get("fallback_reason"),
        }
    values: dict[str, object] = {
        "applied": True,
        "industry_tm_id": context.industry_tm_id,
        "ordering_mode": mode,
        "temperature": context.temperature,
        "industry_strength": context.strength,
        "warm_to_hot_count": context.warm_to_hot_count,
        "right_share": context.right_share,
    }
    if mode == "context_with_history":
        values.update(
            {
                "temperature_direction": context.temperature_direction,
                "right_share_change_pp": context.right_share_change_pp,
            }
        )
    return values


def validate_report_strategy_snapshot(report: TrendReport) -> None:
    snapshot = report.strategy_snapshot
    parameters = snapshot.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("strategy snapshot does not match report actions")
    market = str(report.metadata.get("market") or "CN").upper()
    pool_ids = parameters.get("candidate_pool_ids")
    if not isinstance(pool_ids, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in pool_ids
    ):
        raise ValueError("strategy snapshot does not match report actions")
    version = snapshot.get("strategy_version")
    if version not in {
        "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12",
    }:
        raise ValueError("strategy snapshot does not match report actions")
    expected_snapshot = _expected_report_strategy_snapshot(
        market,
        str(snapshot.get("process_version") or ""),
        pool_ids,
        snapshot,
    )
    try:
        normalize_trend_strategy_snapshot(
            snapshot,
            market,
            expected_snapshot=expected_snapshot,
        )
    except ValueError:
        raise ValueError("strategy snapshot does not match report actions") from None
    expected_window = "美股常规交易时段" if market == "US" else "09:30-10:00"
    if parameters.get("buy_window") != expected_window:
        raise ValueError("strategy snapshot does not match report actions")
    if (
        "overheat_trim_fraction" in parameters
        or any(item.action == "SELL_PARTIAL" for item in report.holdings)
    ) and (
        parameters.get("overheat_trim_fraction") != str(OVERHEAT_TRIM_FRACTION)
        or parameters.get("overheat_trim_once_per_position") is not True
        or parameters.get("overheat_trim_signals") != list(OVERHEAT_TRIM_SIGNALS)
        or parameters.get("overheat_trim_rounding") != "floor_to_market_lot"
        or parameters.get("overheat_trim_below_lot") != "no_order_terminal"
        or parameters.get("full_exit_precedes_partial_exit") is not True
    ):
        raise ValueError("strategy snapshot does not match report actions")
    if version in {"v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"}:
        valid_contract = {
            "v2": valid_v2_risk_contract,
            "v3": valid_v3_risk_contract,
            "v4": valid_v4_risk_contract,
            "v5": valid_v4_risk_contract,
            "v6": valid_v4_risk_contract,
            "v7": valid_v4_risk_contract,
            "v8": valid_v4_risk_contract,
            "v9": valid_v4_risk_contract,
            "v10": valid_v4_risk_contract,
            "v11": valid_v4_risk_contract,
            "v12": valid_v4_risk_contract,
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
    if version in {"v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"}:
        if (
            not valid_drawdown_decision(
                report.drawdown_summary,
                expected_market=market,
                expected_strategy_id=str(snapshot.get("strategy_id") or ""),
                expected_strategy_version=version,
                expected_equity=report.account.net_value,
                expected_entry_date=report.execution_date,
            )
            or report.drawdown_summary.get("entry_allowed") is not True
            and report.buy_actions
        ):
            raise ValueError("strategy snapshot does not match report actions")
    if (
        "use_available_cash" in parameters
        and (
            parameters.get("use_available_cash") is not True
            or parameters.get("trailing_activation_signals")
            != ["boiling", "champagne"]
        )
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
        if version in {
            "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12",
        } and report.risk_summary.get("kelly_phase") != "cold_start":
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
    industry_context_map = {
        context.industry_tm_id: context for context in report.industry_contexts
    }
    for item in report.candidates:
        candidate = asdict(item)
        if market == "CN":
            candidate.pop("boiling")
            candidate.pop("champagne")
        candidate["ordering_context"] = _candidate_ordering_context(
            item,
            contexts=industry_context_map,
            status=report.industry_context_status,
        )
        top10_candidates.append(_json_value(candidate))
    formal_actions = [
        _json_value(asdict(item))
        for item in report.holdings
        if item.action in {"SELL_ALL", "SELL_PARTIAL"}
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
    if (
        report.allocation is not None
        or report.simulate_rotation_pairs
        or report.real_rotation_pairs
    ):
        strategy_judgments.update(
            simulate_rotation_pairs=[
                _json_value(asdict(pair)) for pair in report.simulate_rotation_pairs
            ],
            real_rotation_pairs=[
                _json_value(asdict(pair)) for pair in report.real_rotation_pairs
            ],
            simulate_rotation_comparisons=[
                _json_value(asdict(item))
                for item in report.simulate_rotation_comparisons
            ],
            real_rotation_comparisons=[
                _json_value(asdict(item))
                for item in report.real_rotation_comparisons
            ],
        )
    if not legacy_v1 or (
        report.metadata.get("symbol_mapping_schema")
        == TREND_SYMBOL_MAPPING_SCHEMA
    ):
        strategy_judgments["risk_skips"] = _json_value(report.risk_skips)
    if report.real_holdings_status == "available":
        strategy_judgments.update(
            {
                "real_holding_decisions": [
                    _json_value(asdict(item)) for item in report.real_holdings
                ],
                "real_holding_decisions_status": "available",
                "real_holding_decisions_source": _json_value(
                    report.real_holdings_source
                ),
            }
        )
    elif report.real_holdings_status == "unavailable":
        strategy_judgments.update(
            {
                "real_holding_decisions_status": "unavailable",
                "real_holding_decisions_reason": report.real_holdings_reason,
                "real_holding_decisions_source": _json_value(
                    report.real_holdings_source
                ),
            }
        )
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
        "api_cost": {
            "actual": _json_value(report.actual_api_cost),
            "estimated": _json_value(report.estimated_api_cost),
            "estimate_complete": report.estimated_api_cost_complete,
            "unit": TREND_API_COST_UNIT,
            "label": trend_api_cost_label(
                actual=report.actual_api_cost,
                estimated=report.estimated_api_cost,
                estimate_complete=report.estimated_api_cost_complete,
            ),
        },
        "industry_contexts": [
            _json_value(asdict(context)) for context in report.industry_contexts
        ],
        "industry_context_status": _json_value(report.industry_context_status),
        "protection_state": report.protection_state,
        "signal_snapshots": _json_value(report.signal_snapshots),
        "metadata": _json_value(report.metadata),
        "strategy_snapshot": _json_value(report.strategy_snapshot),
        "disclaimer": DISCLAIMER_TEXT,
    }
    if report.allocation is not None:
        payload["allocation"] = dict(report.allocation)
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
    if not valid_frozen_report_contract(payload):
        raise ValueError("frozen report contract is invalid")
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
        initial_line = state.get("initial_line")
        active_line = state.get("active_line")
        if initial_line is not None or active_line is not None:
            if (
                _optional_decimal(initial_line) is None
                or _optional_decimal(active_line) is None
            ):
                raise ValueError(f"protection state for {symbol} has no active line")
        _optional_decimal(state.get("atr14"))
        tracking_active = state.get("tracking_active")
        if tracking_active is not None and not isinstance(tracking_active, bool):
            raise ValueError(f"protection state for {symbol} has invalid tracking state")
        position_started_for = state.get("position_started_for")
        if position_started_for is not None and not isinstance(position_started_for, str):
            raise ValueError(f"protection state for {symbol} has invalid start date")
        trim_status = state.get("overheat_trim_status")
        if trim_status is not None and trim_status not in {
            "pending", "complete", "below_lot"
        }:
            raise ValueError(f"protection state for {symbol} has invalid trim status")
        for key in ("overheat_trim_target_qty", "overheat_trim_filled_qty"):
            value = state.get(key)
            if value is not None and (
                _optional_decimal(value) is None or _decimal(value) < 0
            ):
                raise ValueError(f"protection state for {symbol} has invalid trim quantity")
        trim_started_for = state.get("overheat_trim_started_for")
        if trim_started_for is not None and not isinstance(trim_started_for, str):
            raise ValueError(f"protection state for {symbol} has invalid trim start date")
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
    real_protection_state: Mapping[str, object] | None = None,
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
    if real_protection_state is not None:
        real_state_bytes = json.dumps(
            real_protection_state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload["real_protection_state_sha256"] = hashlib.sha256(
            real_state_bytes
        ).hexdigest()
        content += b"\0" + real_state_bytes
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
    real_protection_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    frozen_state = json.loads(
        json.dumps(protection_state, ensure_ascii=False, sort_keys=True)
    )
    frozen_real_state = (
        json.loads(
            json.dumps(real_protection_state, ensure_ascii=False, sort_keys=True)
        )
        if real_protection_state is not None
        else None
    )
    payload = {
        "status": status,
        "generated_at": generated_at,
        "artifact_stem": artifact_stem,
        "markdown": markdown,
        "report_json": report_json,
        "protection_state": frozen_state,
        **(
            {"real_protection_state": frozen_real_state}
            if frozen_real_state is not None
            else {}
        ),
        **_payload_hashes(
            markdown,
            report_json,
            frozen_state,
            frozen_real_state,
        ),
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


def read_delivery_receipt(
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
    real_protection_state = payload.get("real_protection_state")
    if real_protection_state is not None:
        if not isinstance(real_protection_state, dict):
            raise ValueError("delivery receipt real protection state is invalid")
        _validate_protection_state(real_protection_state)
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
    if protection_state != report_payload.get("protection_state"):
        raise ValueError("delivery receipt protection state mismatch")
    _validate_protection_state(protection_state)
    hashes = _payload_hashes(
        markdown,
        report_json,
        protection_state,
        real_protection_state,
    )
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
        real_protection_state=receipt.get("real_protection_state")  # type: ignore[arg-type]
        if isinstance(receipt.get("real_protection_state"), Mapping)
        else None,
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
        receipt = read_delivery_receipt(receipt_path, artifact_stem=stem)
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


def _write_frozen_industry_context_history(
    *,
    receipt: Mapping[str, object],
    history_root: Path,
    market: str,
) -> Path | None:
    payload = json.loads(str(receipt["report_json"]))
    if not isinstance(payload, Mapping):
        raise ValueError("frozen report payload must be an object")
    rows = payload.get("industry_contexts")
    if not isinstance(rows, list):
        return None
    contexts: list[IndustryContext] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("frozen industry context row is invalid")
        context = _context_from_mapping(row)
        if context is None:
            raise ValueError("frozen industry context row is invalid")
        contexts.append(context)
    if not contexts:
        return None
    generated_at = payload.get("generated_at")
    strategy_snapshot = payload.get("strategy_snapshot")
    strategy_version = (
        strategy_snapshot.get("strategy_version")
        if isinstance(strategy_snapshot, Mapping)
        else None
    )
    if not isinstance(generated_at, str) or not isinstance(strategy_version, str):
        raise ValueError("frozen report is missing industry history metadata")
    artifact_stem = receipt.get("artifact_stem")
    revision_match = (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}-r([1-9]\d*)", artifact_stem)
        if isinstance(artifact_stem, str)
        else None
    )
    return write_industry_context_history(
        history_root,
        market=market,
        generated_at=generated_at,
        strategy_version=strategy_version,
        contexts=contexts,
        revision=int(revision_match.group(1)) if revision_match else 0,
    )


def _recover_receipt_report(
    *,
    config: DailyPremarketConfig,
    run_date: str,
    artifact_stem: str,
    notifier: Notifier,
) -> AShareTrendRunResult | None:
    receipt_path = _receipt_path(config.data_dir, artifact_stem)
    receipt = read_delivery_receipt(
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
            if isinstance(receipt.get("real_protection_state"), Mapping):
                write_protection_state(
                    config.data_dir / "trend_a_share/real_protection_state.json",
                    receipt["real_protection_state"],  # type: ignore[arg-type]
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
    _write_frozen_industry_context_history(
        receipt=receipt,
        history_root=config.data_dir / "trend_industry_context",
        market="CN",
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


def favorite_candidate_ids(rows: object, *, market: str) -> set[int]:
    """Return only tradable favorite securities for the requested market."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TrendAnimalsError("Trend Animals favorites are invalid")
    allowed_assets = {
        "CN": {"A股", "ETF基金"},
        "HK": {"港股", "香港ETF"},
        "US": {"美股", "美国ETF"},
    }[market.upper()]
    return {
        _row_tm_id(row)
        for row in rows
        if isinstance(row, Mapping)
        and row.get("asset") in allowed_assets
        and isinstance(row.get("tickerSymbol"), str)
        and row["tickerSymbol"].strip()
    }


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
                value = _decimal(row[key])
            except ValueError:
                break
            if value < 0:
                raise TrendAnimalsError(
                    "getAccountBalance returned no valid balance"
                )
            return value
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
        asset=str(row.get("asset") or "").strip(),
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


def load_industry_temperatures(
    api: object,
    *,
    tm_ids: Sequence[int],
    expected_date: str,
) -> tuple[list[Mapping[str, object]], dict[int, str | None]]:
    requested_ids = sorted(set(tm_ids))
    rows = (
        api.get_snapshots(
            tm_ids=requested_ids,
            fields=A_SHARE_INDUSTRY_FIELDS,
            expected_date=expected_date,
        )
        if requested_ids
        else []
    )
    returned_ids = [_row_tm_id(row) for row in rows]
    if len(returned_ids) != len(set(returned_ids)) or any(
        tm_id not in requested_ids for tm_id in returned_ids
    ):
        raise TrendAnimalsError("industry snapshot returned mismatched tmIds")
    if any(row.get("asOfDate") != expected_date for row in rows):
        raise TrendAnimalsError("industry snapshot returned a stale data date")
    return rows, {
        _row_tm_id(row): (
            str(row["trendTemperatureCurr"])
            if row.get("trendTemperatureCurr") in INDUSTRY_KNOWN_TEMPERATURES
            else None
        )
        for row in rows
    }


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
    allocation_reference: Mapping[str, object] | None = None,
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
        candidate_pool_ids = (
            config.trend_animals_a_share_tm_id,
            config.trend_animals_etf_tm_id,
        )
        allocation_market = _allocation_market_for(allocation_reference, "CN")
        strategy_snapshot = live_trend_strategy_snapshot(
            "CN",
            process_version,
            candidate_pool_ids,
            execution_date=execution_date,
            allocation=allocation_reference,
        )
        strategy_version = str(strategy_snapshot["strategy_version"])
        component_rows = []
        component_pools: defaultdict[int, set[str]] = defaultdict(set)
        for tm_id in candidate_pool_ids:
            rows = api.get_components(tm_id=tm_id, expected_date=run_date)
            component_rows.extend(rows)
            for row in rows:
                component_pools[_row_tm_id(row)].add(str(tm_id))
        component_ids = {_row_tm_id(row) for row in component_rows}
        get_favorites = getattr(api, "get_favorites_tickers", None)
        favorite_rows = get_favorites() if callable(get_favorites) else []
        favorite_ids = favorite_candidate_ids(favorite_rows, market="CN")
        candidate_ids = component_ids | favorite_ids

        simulate_acc_id = require_trend_review_config(config, "CN")
        account = load_futu_simulate_trend_account(
            host=config.futu_host,
            port=config.futu_port,
            simulate_acc_id=simulate_acc_id,
            market="CN",
            expected_date=run_date,
            account_factory=account_factory,
        )
        real_holdings = load_real_holding_input(
            config.data_dir,
            "CN",
            state_path=config.data_dir
            / "trend_a_share/real_protection_state.json",
        )
        holding_ids: dict[str, int] = {}
        for position in account.positions:
            try:
                holding_ids[position.symbol] = api.search_exact_symbol(
                    position.symbol,
                    market="CN",
                    expected_date=run_date,
                )
            except TrendAnimalsError:
                continue

        requested_ids = sorted(candidate_ids | set(holding_ids.values()))
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
        industry_rows, industry_temperatures = load_industry_temperatures(
            api,
            tm_ids=industry_ids,
            expected_date=run_date,
        )
        candidates: list[CandidateInput] = []
        holding_snapshots: dict[str, HoldingSnapshot | None] = {
            position.symbol: None for position in account.positions
        }
        rows_by_tm_id = {_row_tm_id(row): row for row in snapshot_rows}
        kline_start = (run_day - timedelta(days=90)).isoformat()
        bars_by_symbol: dict[str, Sequence[DailyKlineBar] | None] = {}
        for tm_id in sorted(candidate_ids):
            row = rows_by_tm_id.get(tm_id)
            if row is None:
                continue
            mapping_verified = False
            futu_symbol: str | None = None
            try:
                symbol, exchange = _symbol_parts(row.get("tickerSymbol"))
                futu_symbol = f"{exchange}.{symbol}"
                daily_bars = quote.get_daily_kline(
                    futu_symbol, start=kline_start, end=run_date
                )
                try:
                    mapping_verified = _remember_verified_symbol_row(
                        api,
                        market="CN",
                        expected_futu_symbol=futu_symbol,
                        expected_tm_id=tm_id,
                        row=row,
                    )
                except TrendAnimalsError:
                    mapping_verified = False
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
                    futu_symbol=futu_symbol if mapping_verified else None,
                )
            )
        for position in account.positions:
            try:
                bars_by_symbol[position.symbol] = quote.get_daily_kline(
                    to_futu_symbol("CN", position.symbol),
                    start=kline_start,
                    end=run_date,
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                bars_by_symbol[position.symbol] = None
            except ValueError:
                bars_by_symbol[position.symbol] = None
        for symbol, tm_id in holding_ids.items():
            row = rows_by_tm_id.get(tm_id)
            daily_bars = bars_by_symbol[symbol]
            if row is not None:
                try:
                    if from_trend_animals_symbol(
                        "CN", str(row.get("tickerSymbol") or "")
                    ) != to_futu_symbol("CN", symbol):
                        continue
                    _remember_verified_symbol_row(
                        api,
                        market="CN",
                        expected_futu_symbol=symbol,
                        expected_tm_id=tm_id,
                        row=row,
                        require_unmapped=True,
                    )
                    holding_snapshots[symbol] = _holding_snapshot(
                        row,
                        industry_temperature=industry_temperatures.get(
                            _optional_int(row.get("industryTmId"))
                        ),
                        bars=tuple(daily_bars or ()),
                    )
                except ValueError:
                    holding_snapshots[symbol] = None

        real_holdings, real_snapshot_rows, real_bars_by_symbol, real_only_count = (
            enrich_real_holding_input(
                real_holdings,
                api=api,
                quote=quote,
                market="CN",
                as_of_date=run_date,
                kline_start=kline_start,
                existing_holding_ids=holding_ids,
                existing_rows_by_tm_id=rows_by_tm_id,
                existing_holding_snapshots=holding_snapshots,
                existing_bars_by_symbol=bars_by_symbol,
            )
        )

        candidate_pool_rows = [
            rows_by_tm_id[tm_id] for tm_id in sorted(candidate_ids)
            if tm_id in rows_by_tm_id
        ]
        industry_contexts, industry_context_status, industry_facts = (
            collect_industry_contexts(
                api=api,
                candidates=candidates,
                candidate_rows=candidate_pool_rows,
                held_symbols={position.symbol for position in account.positions},
                holding_snapshots=(
                    *holding_snapshots.values(),
                    *(
                        real_holdings.holding_snapshots.values()
                        if real_holdings.status == "available"
                        else ()
                    ),
                ),
                expected_date=run_date,
                market="CN",
                history_root=config.data_dir / "trend_industry_context",
                strategy_version=strategy_version,
            )
        )
        balance_after = _balance(api.get_account_balance())

        estimated_cost = (
            unified_unit_cost * (len(requested_ids) + real_only_count)
            + sum(
                (_billing_price(billing[field]) for field in A_SHARE_INDUSTRY_FIELDS),
                Decimal("0"),
            )
            * len(industry_ids)
            + sum(
                (_billing_price(billing[field]) for field in INDUSTRY_MEMBER_FIELDS if field in billing),
                Decimal("0"),
            )
            * len(industry_facts["member_ids"])
            + sum(
                (_billing_price(billing[field]) for field in INDUSTRY_STATE_FIELDS if field in billing),
                Decimal("0"),
            )
            * len(industry_facts["state_ids"])
        )
        balance_delta = balance_before - balance_after
        actual_cost = balance_delta if balance_delta >= 0 else None
        cache_events = tuple(getattr(api, "paid_cache_events", ()))
        cache_metadata = {
            "hits": sum(event.get("cache") == "hit" for event in cache_events),
            "misses": sum(event.get("cache") == "miss" for event in cache_events),
            "events": [dict(event) for event in cache_events],
        }
        expected_component_requests = len(candidate_pool_ids) + int(
            industry_facts["component_requests"]
        )
        component_events = [
            event for event in cache_events
            if event.get("endpoint") == "getComponentTicker"
        ]
        industry_field_prices_complete = all(
            field in billing
            for field in (*INDUSTRY_MEMBER_FIELDS, *INDUSTRY_STATE_FIELDS)
        )
        estimate_complete = (
            len(component_events) == expected_component_requests
            and all(event.get("cache") == "hit" for event in component_events)
            and industry_field_prices_complete
        )
        prior_state = rebuild_overheat_trim_projection(
            config.data_dir,
            market="CN",
            state_path=config.data_dir / "trend_a_share/protection_state.json",
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
        drawdown_summary = observe_strategy_equity(
            config.data_dir,
            market="CN",
            strategy_id=str(strategy_snapshot["strategy_id"]),
            strategy_version=str(strategy_snapshot["strategy_version"]),
            current_equity=account.net_value,
            observed_at=generated_at,
            entry_date=execution_date,
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
                f"getFavoritesTicker securities={len(favorite_ids)}",
                *_component_api_facts(api, len(component_rows)),
                f"getTickerSnapshot fields={','.join(fields)} rows={len(snapshot_rows)} cache=client-managed",
                f"getTickerSnapshot industries fields={','.join(A_SHARE_INDUSTRY_FIELDS)} rows={len(industry_rows)} cache=client-managed",
                f"getComponentTicker eligible_industries={industry_facts['component_requests']} rows={industry_facts['component_rows']} cache=client-managed",
                f"getTickerSnapshot fields={','.join(INDUSTRY_MEMBER_FIELDS)} ids={len(industry_facts['member_ids'])} rows={industry_facts['member_rows']} cache=client-managed",
                f"getTickerSnapshot fields={','.join(INDUSTRY_STATE_FIELDS)} ids={len(industry_facts['state_ids'])} rows={industry_facts['state_rows']} cache=client-managed",
            ),
            data_sources=(
                "Trend Animals",
                "Futu CN calendar/QFQ daily K-line",
                "Futu CN SIMULATE account",
                "Eastmoney frozen real account snapshot (read-only)",
            ),
            estimated_api_cost=estimated_cost,
            actual_api_cost=actual_cost,
            generated_at=generated_at,
            position_weight=Decimal(
                str(allocation_market["entry_weight"])
                if allocation_market is not None
                else "0.04"
            ),
            position_weight_source=(
                "trend_allocation_rank"
                if allocation_market is not None
                else "fallback_4pct"
            ),
            process_version=process_version,
            candidate_pool_ids=candidate_pool_ids,
            strategy_snapshot=strategy_snapshot,
            drawdown_summary=drawdown_summary,
            industry_contexts=industry_contexts,
            industry_context_status=industry_context_status,
            estimated_api_cost_complete=estimate_complete,
            metadata={
                "market": "CN",
                "broker": "eastmoney",
                "simulate_acc_id": simulate_acc_id,
                "run_date": run_date,
                "paid_response_cache": cache_metadata,
                **(
                    {"symbol_mapping_schema": TREND_SYMBOL_MAPPING_SCHEMA}
                    if _supports_symbol_mapping_contract(api)
                    else {}
                ),
            },
            kelly_rounds=kelly_rounds,
            kelly_data_reason=kelly_data_reason,
            real_holdings=real_holdings,
            allocation_reference=allocation_reference,
        )
        report = freeze_report_rotation_pairs(report, config.data_dir)
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
                "component_pool_ids": list(candidate_pool_ids),
                "favorite_ids": sorted(favorite_ids),
                "snapshot_fields": list(fields),
                "industry_fields": list(A_SHARE_INDUSTRY_FIELDS),
                "industry_member_fields": list(INDUSTRY_MEMBER_FIELDS),
                "industry_state_fields": list(INDUSTRY_STATE_FIELDS),
            },
            responses={
                "update_status": update_rows,
                "components": component_rows,
                "favorites": favorite_rows,
                "snapshots": snapshot_rows,
                "real_snapshots": list(real_snapshot_rows.values()),
                "industries": industry_rows,
                "industry_components": [
                    row
                    for rows in industry_facts["component_rows_by_industry"].values()
                    for row in rows
                ],
                "industry_members": industry_facts["member_response"],
                "industry_states": industry_facts["state_response"],
            },
            candidate_pool_ids=candidate_pool_ids,
            lot_sizes={},
            price_fx_to_account_currency=Decimal("1"),
            previous_attention_rows=(),
            option_attention_broker_label=None,
            kelly_rounds=kelly_rounds,
            kelly_data_reason=kelly_data_reason,
            real_holdings_input=real_holdings,
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
            real_protection_state=report.real_protection_state,
        )
        write_protection_state(
            config.data_dir / "trend_a_share/protection_state.json",
            report.protection_state,
        )
        if report.real_protection_state is not None:
            write_protection_state(
                config.data_dir / "trend_a_share/real_protection_state.json",
                report.real_protection_state,
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
        _write_frozen_industry_context_history(
            receipt=receipt,
            history_root=config.data_dir / "trend_industry_context",
            market="CN",
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
    allocation_reference: Mapping[str, object] | None = None,
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
        receipt = read_delivery_receipt(
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
                _write_frozen_industry_context_history(
                    receipt=receipt,
                    history_root=config.data_dir / "trend_industry_context",
                    market="CN",
                )
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
        deadline = datetime.combine(run_day, time(19, 0), tzinfo=SHANGHAI)
        notified_waiting = False
        last_error = "Trend Animals update status is not ready"
        _write_run_log(
            log_path,
            {"event": "start", "process_version": version, "run_date": run_date},
            append=False,
        )
        while True:
            try:
                attempt_kwargs: dict[str, object] = {
                    "config": config,
                    "run_date": run_date,
                    "artifact_stem": artifact_stem,
                    "process_version": version,
                    "api_factory": api_factory,
                    "quote_factory": quote_factory,
                    "account_factory": (
                        account_factory or FutuSimulateOrderExecutionClient
                    ),
                    "notifier": notifier,
                }
                if allocation_reference is not None:
                    attempt_kwargs["allocation_reference"] = allocation_reference
                attempt = _attempt_report(
                    **attempt_kwargs,
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
