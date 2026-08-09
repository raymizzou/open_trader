from __future__ import annotations

import csv
import copy
import json
import re
import socket
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .a_share_trend import (
    ACTION_LABELS,
    CNY_PER_LOCAL_CURRENCY,
    NON_REALTIME_ACCOUNT_WARNING,
    PORTFOLIO_RISK_LIMIT,
    REASON_LABELS,
    SINGLE_ENTRY_RISK_LIMIT,
    TREND_API_COST_UNIT,
    live_trend_strategy_snapshot,
    trend_api_cost_label,
    valid_serialized_account,
    valid_frozen_report_contract,
    valid_v2_risk_contract,
    valid_v3_risk_contract,
    valid_v4_risk_contract,
)
from .backtest_prices import normalize_backtest_symbol
from .account_snapshot import build_instrument_id
from .futu_symbols import to_futu_symbol

from .decision_facts import (
    KLINE_FIELDS,
    NEWS_SENTIMENT_FIELDS,
    build_missing_fields,
    extract_decision_sources,
    index_decision_facts_by_market_symbol,
    load_decision_facts_cache,
)
from .decision_source_availability import (
    decision_module_available,
    futu_module_available,
    futu_module_unsupported,
    technical_facts_available,
    tradingagents_available,
)
from .decision_plan import load_decision_plans
from .futu_skill_facts import (
    futu_skill_facts_latest_path,
    futu_skill_facts_run_path,
    index_futu_skill_facts_by_market_symbol,
    load_futu_skill_facts_cache,
)
from .kelly_lab import (
    index_kelly_experiments_by_market_symbol,
    load_kelly_lab_state,
)
from .models import AssetClass
from .parsers.base import detect_asset_class
from .plan_events import load_plan_events, replay_plan_status
from .research_chat import load_research_view_for_holding
from .t_signal_store import (
    index_t_signals_by_market_symbol,
    load_t_signals_cache,
    t_signals_latest_path,
)
from .technical_facts import (
    extract_market_report,
    index_technical_facts_by_market_symbol,
    load_technical_facts_cache,
    source_hash,
    technical_facts_has_missing_timeframe,
    technical_facts_latest_path,
)
from .trend_review import (
    BENCHMARK_IDENTITIES,
    _report_hash,
    _rotation_pair_key,
    _validate_execution_batch,
    _validate_rotation_event,
)
from .trend_market_controller import _valid_status
from .strategy_drawdown import valid_drawdown_decision
from .trend_api_stats import (
    load_trend_api_stats,
    read_trend_api_stats_snapshot,
    trend_statistics_disposition,
)
from .tradingagents_summary import (
    index_tradingagents_summary_by_market_symbol,
    load_tradingagents_summary_cache,
    normalize_current_action,
    normalize_ta_view,
    tradingagents_summary_latest_path,
)
from .trading_plan import backtest_plan_side, load_trading_plan_rows


BROKER_LABELS = {
    "futu": "富途",
    "tiger": "老虎",
    "phillips": "辉立",
    "eastmoney": "东方财富",
}
TREND_REPORT_SOURCES = {
    "tiger": ("US", "美股", "老虎", "trend_us_tiger", "美股常规交易时段"),
    "phillips": ("HK", "港股", "辉立", "trend_hk_phillips", "09:30–10:00"),
    "eastmoney": ("CN", "A股", "东方财富", "trend_a_share", "09:30–10:00"),
}
CURRENT_FINAL_PLAN_TREND_VERSIONS = frozenset({
    ("CN", "v13"),
    ("HK", "v11"),
    ("US", "v11"),
})
TREND_ACTUAL_BROKERS = {
    market: broker for broker, (market, *_rest) in TREND_REPORT_SOURCES.items()
}
OPTION_ATTENTION_KEYS = {
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
}
OPTION_ATTENTION_TRANSITIONS = {
    "right_side",
    "temperature",
    "phase",
    "strength_change",
    "danger",
    "boiling",
    "champagne",
}
TREND_REVIEW_SOURCES = {
    "tiger": ("US", "美股", "老虎"),
    "phillips": ("HK", "港股", "辉立"),
    "eastmoney": ("CN", "A股", "东方财富"),
}
TREND_REVIEW_METRICS = {
    "period_net_return",
    "market_excess_return",
    "max_drawdown",
    "calmar",
    "sharpe",
}
TREND_REVIEW_SERIES = {
    "discipline",
    "actual",
    "same_period_benchmark",
    "market_1y",
    "market_5y",
}
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHANGHAI = ZoneInfo("Asia/Shanghai")
TREND_MARKET_TIMEZONES = {
    "CN": SHANGHAI,
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}


@dataclass(frozen=True)
class DashboardConfig:
    portfolio_path: Path | None
    data_dir: Path
    reports_dir: Path
    poll_seconds: float
    futu_host: str
    futu_port: int
    trend_review_cn_simulate_acc_id: int = 0
    trend_review_us_simulate_acc_id: int = 0
    trend_review_hk_simulate_acc_id: int = 0
    trend_executor_host: str = ""
    trend_cn_candidate_pool_ids: tuple[int, ...] = ()
    trend_us_candidate_pool_ids: tuple[int, ...] = ()
    trend_hk_candidate_pool_ids: tuple[int, ...] = ()
    prediction_config_path: Path | None = None

    def trend_candidate_pool_ids(self, market: str) -> tuple[int, ...]:
        return {
            "CN": self.trend_cn_candidate_pool_ids,
            "US": self.trend_us_candidate_pool_ids,
            "HK": self.trend_hk_candidate_pool_ids,
        }.get(market.upper(), ())


@dataclass(frozen=True)
class DashboardState:
    config: DashboardConfig
    trade_actions: list[dict[str, str]]
    holding_enrichment: list[dict[str, Any]]
    kelly_lab: dict[str, Any]
    backtest_universe: dict[str, list[dict[str, str]]]
    trend_reports: dict[str, dict[str, Any]]
    trend_reviews: dict[str, dict[str, Any]]
    trend_controllers: dict[str, dict[str, object]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.config.data_dir),
            "reports_dir": str(self.config.reports_dir),
            "poll_seconds": self.config.poll_seconds,
            "futu_host": self.config.futu_host,
            "futu_port": self.config.futu_port,
            "trade_actions": self.trade_actions,
            "holding_enrichment": self.holding_enrichment,
            "kelly_lab": self.kelly_lab,
            "backtest_universe": self.backtest_universe,
            "trend_reports": self.trend_reports,
            "trend_reviews": self.trend_reviews,
            "trend_controllers": self.trend_controllers,
        }


def load_dashboard_state(config: DashboardConfig) -> DashboardState:
    holding_markets = {"CN", "HK", "US"}
    trade_actions, _ = _latest_rows_for_markets(
        data_dir=config.data_dir,
        filename="trade_actions.csv",
        markets=holding_markets,
    )
    trading_plan, _ = _latest_rows_for_markets(
        data_dir=config.data_dir,
        filename="trading_plan.csv",
        markets=holding_markets,
    )
    premarket_actions, _ = _latest_rows_for_markets(
        data_dir=config.data_dir,
        filename="premarket_actions.csv",
        markets=holding_markets,
    )
    trading_advice, scoped_advice_markets = _latest_rows_for_markets(
        data_dir=config.data_dir,
        filename="trading_advice.csv",
        markets=holding_markets,
    )
    technical_facts_by_holding, technical_facts_file_exists_by_market = (
        _latest_technical_facts_for_markets(
            data_dir=config.data_dir,
            markets=holding_markets,
            scoped_advice_markets=scoped_advice_markets,
        )
    )
    decision_facts_by_holding, decision_facts_file_exists_by_market = (
        _latest_decision_facts_for_markets(
            data_dir=config.data_dir,
            markets=holding_markets,
        )
    )
    futu_skill_facts_by_holding = _latest_futu_skill_facts_for_markets(
        data_dir=config.data_dir,
        markets=holding_markets,
    )
    tradingagents_summary_by_holding = _latest_tradingagents_summary_for_markets(
        data_dir=config.data_dir,
        markets=holding_markets,
    )
    t_signals_by_holding = _latest_t_signals_for_markets(
        data_dir=config.data_dir,
        markets=holding_markets,
    )
    kelly_lab, kelly_experiments_by_holding = _load_dashboard_kelly_lab(
        config.data_dir
    )
    agent_reports_by_holding = _latest_by_market_symbol(trading_advice)
    strategies_by_holding = _latest_by_market_symbol(trading_plan)
    premarket_actions_by_holding = _latest_by_market_symbol(premarket_actions)
    actions_by_holding = _latest_by_market_symbol(trade_actions)
    decision_plans_by_holding, decision_plan_errors_by_market = (
        _latest_decision_plans_for_markets(config.data_dir, holding_markets)
    )
    holding_rows = _module_holding_rows(
        trading_advice,
        trading_plan,
        premarket_actions,
        trade_actions,
        _read_csv_rows(config.data_dir / "latest" / "watchlist.csv"),
    )
    holding_enrichment = [
        _merge_holding(
            row,
            config.data_dir,
            agent_reports_by_holding,
            strategies_by_holding,
            premarket_actions_by_holding,
            actions_by_holding,
            technical_facts_by_holding,
            technical_facts_file_exists_by_market,
            decision_facts_by_holding,
            decision_facts_file_exists_by_market,
            futu_skill_facts_by_holding,
            tradingagents_summary_by_holding,
            t_signals_by_holding,
            kelly_experiments_by_holding,
            decision_plans_by_holding,
            decision_plan_errors_by_market,
        )
        for row in holding_rows
    ]
    backtest_universe = _build_backtest_universe(
        [],
        _read_csv_rows(config.data_dir / "latest" / "watchlist.csv"),
    )

    return DashboardState(
        config=config,
        trade_actions=trade_actions,
        holding_enrichment=holding_enrichment,
        kelly_lab=kelly_lab,
        backtest_universe=backtest_universe,
        trend_reports=_load_trend_reports(
            config.data_dir,
            config.reports_dir,
            current_candidate_pool_ids={
                market: config.trend_candidate_pool_ids(market)
                for market in ("CN", "US", "HK")
            },
        ),
        trend_reviews=_load_trend_reviews(config.data_dir),
        trend_controllers=_load_trend_controllers(
            config.data_dir,
            executor_host=config.trend_executor_host,
        ),
    )




def _load_trend_controllers(
    data_dir: Path,
    *,
    executor_host: str,
    now: datetime | None = None,
    hostname_fn: Callable[[], str] = socket.gethostname,
) -> dict[str, dict[str, object]]:
    local_host = hostname_fn().strip()
    executor_host = executor_host.strip()
    current = now or datetime.now(SHANGHAI)
    effective_mode = (
        "execute" if executor_host and executor_host == local_host else "readonly"
    )

    def base(
        market: str, health: str, blocking: bool, reason: str
    ) -> dict[str, object]:
        return {
            "market": market,
            "effective_mode": effective_mode,
            "executor_host": executor_host,
            "local_host": local_host,
            "health": health,
            "blocking": blocking,
            "reason": reason,
            "pid": None,
            "working_directory": "",
            "git_sha": "",
            "phase": "readonly" if health == "readonly" else "unavailable",
            "heartbeat_at": "",
            "last_success": None,
            "blocker": reason or None,
            "next_check_at": "",
        }

    def load(market: str) -> dict[str, object]:
        if not executor_host or executor_host != local_host:
            reason = (
                "OPEN_TRADER_TREND_EXECUTOR_HOST is not configured"
                if not executor_host
                else "local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST"
            )
            return base(market, "readonly", False, reason)
        path = data_dir / "trend_controller" / market / "status.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return base(
                market, "unavailable", True, "controller status file is missing"
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return base(
                market, "unavailable", True, "controller status is malformed"
            )
        if not isinstance(payload, dict) or not _valid_status(payload):
            return base(
                market, "unavailable", True, "controller status is malformed"
            )
        if (
            payload["effective_mode"] != "execute"
            or payload["executor_host"] != executor_host
            or payload["local_host"] != local_host
        ):
            return base(
                market, "unavailable", True, "controller hostname does not match"
            )
        heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"]))
        if abs(current - heartbeat) > timedelta(minutes=2):
            return {
                **base(market, "unavailable", True, "controller heartbeat is stale"),
                **payload,
                "health": "unavailable",
                "blocking": True,
                "reason": "controller heartbeat is stale",
                "blocker": "controller heartbeat is stale",
            }
        unhealthy_phase = payload["phase"] in {
            "starting",
            "blocked",
            "uncertain",
            "conflict",
            "missed",
        }
        if payload["blocker"] not in (None, "") or unhealthy_phase:
            reason = str(
                payload["blocker"] or f"controller phase is {payload['phase']}"
            )
            return {
                **payload,
                "market": market,
                "health": "unavailable",
                "blocking": True,
                "reason": reason,
            }
        return {
            **payload,
            "market": market,
            "health": "healthy",
            "blocking": False,
            "reason": "",
        }

    return {
        broker: load(market)
        for broker, (market, *_rest) in TREND_REPORT_SOURCES.items()
    }


def _trend_review_unavailable(
    broker: str, market: str, market_label: str, broker_label: str, status: str
) -> dict[str, Any]:
    return {
        "available": False,
        "broker": broker,
        "broker_label": broker_label,
        "market": market,
        "market_label": market_label,
        "status_text": status,
    }


def _valid_trend_review_metric_cell(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"value", "reason"}:
        return False
    metric_value = value["value"]
    reason = value["reason"]
    if metric_value is None:
        return isinstance(reason, str) and bool(reason.strip())
    try:
        finite = Decimal(str(metric_value)).is_finite()
    except (InvalidOperation, TypeError, ValueError):
        return False
    return finite and reason is None


def _valid_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_aware_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_trend_review_sample_detail(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "available",
        "eligible_sample_count",
        "discovered_candidate_count",
        "excluded_candidate_count",
        "incomplete_open_candidate_count",
        "exclusion_reasons",
        "statistics_cutoff_at",
        "reason",
    }:
        return False
    counts = [
        value[key]
        for key in (
            "eligible_sample_count",
            "discovered_candidate_count",
            "excluded_candidate_count",
            "incomplete_open_candidate_count",
        )
    ]
    reasons = value["exclusion_reasons"]
    if (
        not isinstance(value["available"], bool)
        or any(type(count) is not int or count < 0 for count in counts)
        or counts[1] != counts[0] + counts[2] + counts[3]
        or not isinstance(reasons, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"reason", "count"}
            or not isinstance(item["reason"], str)
            or not item["reason"].strip()
            or type(item["count"]) is not int
            or item["count"] < 1
            for item in reasons
        )
        or not isinstance(value["statistics_cutoff_at"], str)
        or not isinstance(value["reason"], str)
    ):
        return False
    if value["available"]:
        return _valid_aware_datetime(value["statistics_cutoff_at"]) and not value["reason"]
    return not value["statistics_cutoff_at"] and bool(value["reason"].strip())


def _valid_trend_review_benchmark_metadata(
    context: object, refresh: object, market: str
) -> bool:
    if not isinstance(context, dict) or set(context) != {
        "name", "source_id", "futu_symbol", "same_period_dates", "windows"
    } or {
        key: context[key] for key in ("name", "source_id", "futu_symbol")
    } != BENCHMARK_IDENTITIES[market]:
        return False
    dates = context["same_period_dates"]
    windows = context["windows"]
    if (
        not isinstance(dates, list)
        or any(not _valid_iso_date(item) for item in dates)
        or dates != sorted(set(dates))
        or not isinstance(windows, dict)
        or set(windows) != {"1Y", "5Y"}
        or not isinstance(refresh, dict)
        or not isinstance(refresh.get("status"), str)
    ):
        return False
    if refresh["status"] == "unavailable":
        return (
            set(refresh) == {"status", "reason"}
            and isinstance(refresh["reason"], str)
            and bool(refresh["reason"].strip())
            and not dates
            and windows == {"1Y": None, "5Y": None}
        )
    if refresh["status"] not in {"available", "failed"}:
        return False
    expected_keys = {
        "status", "month", "completed_at", "process_git_sha", "cutoff", "refresh"
    }
    if refresh["status"] == "failed":
        expected_keys |= {
            "reason", "attempted_at", "attempt_process_git_sha", "attempt_refresh"
        }
    if set(refresh) != expected_keys:
        return False

    def valid_refresh_controls(value: object) -> bool:
        if not isinstance(value, dict) or set(value) != {"force", "actor", "reason"}:
            return False
        if value["force"] is True:
            return all(
                isinstance(value[key], str) and value[key].strip()
                for key in ("actor", "reason")
            )
        return value["force"] is False and value["actor"] is None and value["reason"] is None

    try:
        month = datetime.strptime(str(refresh["month"]), "%Y-%m")
    except ValueError:
        return False
    if (
        month.strftime("%Y-%m") != refresh["month"]
        or not _valid_aware_datetime(refresh["completed_at"])
        or not isinstance(refresh["process_git_sha"], str)
        or not refresh["process_git_sha"].strip()
        or not _valid_iso_date(refresh["cutoff"])
        or not valid_refresh_controls(refresh["refresh"])
    ):
        return False
    if refresh["status"] == "failed" and (
        not isinstance(refresh["reason"], str)
        or not refresh["reason"].strip()
        or not _valid_aware_datetime(refresh["attempted_at"])
        or not isinstance(refresh["attempt_process_git_sha"], str)
        or not refresh["attempt_process_git_sha"].strip()
        or not valid_refresh_controls(refresh["attempt_refresh"])
    ):
        return False
    for label, basis in (("1Y", "period_return"), ("5Y", "CAGR")):
        window = windows[label]
        if (
            not isinstance(window, dict)
            or set(window) != {"start", "cutoff", "observation_count", "return_basis"}
            or not _valid_iso_date(window["start"])
            or not _valid_iso_date(window["cutoff"])
            or window["start"] > window["cutoff"]
            or window["cutoff"] != refresh["cutoff"]
            or type(window["observation_count"]) is not int
            or window["observation_count"] < 2
            or window["return_basis"] != basis
        ):
            return False
    return True


def _valid_trend_review_projection(
    payload: object, *, broker: str, market: str
) -> bool:
    if not isinstance(payload, dict):
        return False
    snapshot = payload.get("strategy_snapshot")
    sample_counts = payload.get("sample_counts")
    common_cutoff = payload.get("common_cutoff")
    interval = payload.get("interval")
    metrics = payload.get("metrics")
    schema_version = payload.get("schema_version")
    sample_details = payload.get("sample_details")
    sample_cutoffs = payload.get("sample_cutoffs")
    metric_cutoffs = payload.get("metric_cutoffs")
    benchmark_context = payload.get("benchmark_context")
    benchmark_refresh = payload.get("benchmark_refresh")
    if (
        set(payload) != {
            "schema_version", "available", "market", "market_label", "broker",
            "strategy_snapshot", "sample_counts", "sample_details", "sample_cutoffs",
            "metric_cutoffs", "common_cutoff", "interval", "metrics",
            "benchmark_context", "benchmark_refresh",
        }
        or schema_version != "open_trader.trend_review.projection.v4"
        or payload.get("available") is not True
        or payload.get("broker") != broker
        or payload.get("market") != market
        or not isinstance(snapshot, dict)
        or not isinstance(sample_counts, dict)
        or set(sample_counts) != {"discipline", "actual", "required"}
        or any(
            sample_counts[key] is not None
            and (type(sample_counts[key]) is not int or sample_counts[key] < 0)
            for key in ("discipline", "actual")
        )
        or type(sample_counts["required"]) is not int
        or sample_counts["required"] != 30
        or not isinstance(interval, dict)
        or set(interval) != {"start", "end"}
        or not _valid_iso_date(interval["start"])
        or interval["end"] != common_cutoff
        or (
            common_cutoff is not None
            and (
                not _valid_iso_date(common_cutoff)
                or common_cutoff < interval["start"]
            )
        )
        or not isinstance(metrics, dict)
        or set(metrics) != TREND_REVIEW_METRICS
        or not isinstance(sample_details, dict)
        or set(sample_details) != {"discipline", "actual"}
        or not all(
            _valid_trend_review_sample_detail(sample_details[key])
            for key in ("discipline", "actual")
        )
        or not isinstance(sample_cutoffs, dict)
        or set(sample_cutoffs) != {"discipline", "actual"}
        or not isinstance(metric_cutoffs, dict)
        or set(metric_cutoffs) != {"discipline", "actual"}
        or not _valid_trend_review_benchmark_metadata(
            benchmark_context, benchmark_refresh, market
        )
    ):
        return False
    for key in ("discipline", "actual"):
        detail = sample_details[key]
        expected_count = (
            detail["eligible_sample_count"]
            if isinstance(detail, dict) and detail["available"] is True
            else None
        )
        expected_cutoff = (
            detail["statistics_cutoff_at"]
            if isinstance(detail, dict) and detail["available"] is True
            else None
        )
        if (
            sample_counts[key] != expected_count
            or sample_cutoffs[key] != expected_cutoff
            or (
                metric_cutoffs[key] is not None
                and not _valid_iso_date(metric_cutoffs[key])
            )
        ):
            return False
    expected_common_cutoff = (
        min(metric_cutoffs.values())
        if all(metric_cutoffs.values())
        else None
    )
    if common_cutoff != expected_common_cutoff:
        return False
    for key in (
        "strategy_id",
        "strategy_name",
        "strategy_version",
        "process_version",
    ):
        if not isinstance(snapshot.get(key), str) or not snapshot[key].strip():
            return False
    if not _valid_iso_date(snapshot.get("effective_from")):
        return False
    if not isinstance(snapshot.get("parameters"), dict):
        return False
    rows = snapshot.get("parameter_rows")
    if (
        not isinstance(rows, list)
        or not rows
        or any(
            not isinstance(row, dict)
            or set(row) != {"group", "name", "value"}
            or any(
                not isinstance(row[key], str) or not row[key].strip()
                for key in row
            )
            for row in rows
        )
    ):
        return False
    return all(
        isinstance(metrics[key], dict)
        and set(metrics[key]) == TREND_REVIEW_SERIES
        and all(
            _valid_trend_review_metric_cell(metrics[key][series])
            for series in TREND_REVIEW_SERIES
        )
        for key in TREND_REVIEW_METRICS
    )


def _latest_trend_statistics_cycle(data_dir: Path, market: str) -> dict[str, Any]:
    root = data_dir / "trend_api_stats" / "daily" / market
    paths = sorted(root.glob("*.json"), reverse=True)
    if not paths:
        return {}
    path = paths[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version")
        != "open_trader.trend_api_stats.cycle.v1"
        or payload.get("market") != market
        or payload.get("as_of_date") != path.stem
        or payload.get("status") not in {"completed", "failed"}
        or not _valid_iso_date(payload.get("as_of_date"))
        or not isinstance(payload.get("process_git_sha"), str)
        or not payload["process_git_sha"].strip()
    ):
        return {}
    if payload["status"] == "failed":
        if (
            not _valid_aware_datetime(payload.get("attempted_at"))
            or not isinstance(payload.get("reason"), str)
            or not payload["reason"].strip()
        ):
            return {}
    elif (
        not _valid_aware_datetime(payload.get("completed_at"))
        or not _valid_aware_datetime(payload.get("statistics_cutoff_at"))
        or not isinstance(payload.get("artifact_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["artifact_sha256"]) is None
    ):
        return {}
    if payload["status"] == "completed" and payload.get("last_forced_failure_status") == "failed":
        forced_at = payload.get("last_forced_failure_at")
        forced_sha = payload.get("last_forced_failure_process_git_sha")
        forced_reason = payload.get("last_forced_failure_error") or payload.get(
            "last_forced_failure_reason"
        )
        if (
            not _valid_aware_datetime(forced_at)
            or not isinstance(forced_sha, str)
            or not forced_sha.strip()
            or not isinstance(forced_reason, str)
            or not forced_reason.strip()
        ):
            return {}
        return {
            **payload,
            "status": "failed",
            "attempted_at": forced_at,
            "process_git_sha": forced_sha,
            "reason": forced_reason,
        }
    return payload


def _overlay_trend_statistics(
    data_dir: Path, payload: Mapping[str, Any], market: str
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    details = dict(payload["sample_details"])
    artifact_available = False
    try:
        statistics, _digest = read_trend_api_stats_snapshot(data_dir)
    except ValueError:
        pass
    else:
        artifact_available = True
        snapshot = payload["strategy_snapshot"]
        for key, source in (("discipline", "simulation"), ("actual", "actual")):
            details[key] = trend_statistics_disposition(
                statistics,
                market=market,
                strategy_id=snapshot["strategy_id"],
                opening_strategy_version=snapshot["strategy_version"],
                source=source,
            )
    return details, _latest_trend_statistics_cycle(data_dir, market), artifact_available


def _load_trend_reviews(data_dir: Path) -> dict[str, dict[str, Any]]:
    reviews: dict[str, dict[str, Any]] = {}
    for broker, (market, market_label, broker_label) in TREND_REVIEW_SOURCES.items():
        unavailable = _trend_review_unavailable(
            broker, market, market_label, broker_label, "暂无复盘数据"
        )
        path = data_dir / "latest" / f"trend_review_{market.lower()}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            reviews[broker] = unavailable
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            reviews[broker] = {**unavailable, "status_text": "复盘数据无效"}
            continue
        if not _valid_trend_review_projection(payload, broker=broker, market=market):
            reviews[broker] = {**unavailable, "status_text": "复盘数据无效"}
            continue
        sample_details, cycle, artifact_available = _overlay_trend_statistics(
            data_dir, payload, market
        )
        sample_counts = {
            key: (
                detail["eligible_sample_count"]
                if isinstance(detail := sample_details[key], dict)
                and detail["available"] is True
                else None
            )
            for key in ("discipline", "actual")
        }
        sample_counts["required"] = payload["sample_counts"]["required"]
        reviews[broker] = {
            "available": True,
            "broker": broker,
            "broker_label": broker_label,
            "market": market,
            "market_label": market_label,
            "strategy_snapshot": payload["strategy_snapshot"],
            "sample_counts": sample_counts,
            "sample_details": sample_details,
            "sample_cutoffs": {
                key: (
                    detail["statistics_cutoff_at"]
                    if isinstance(detail := sample_details[key], dict)
                    and detail["available"] is True
                    else None
                )
                for key in ("discipline", "actual")
            },
            "metric_cutoffs": payload["metric_cutoffs"],
            "common_cutoff": payload["common_cutoff"],
            "interval": payload["interval"],
            "metrics": payload["metrics"],
            "benchmark_context": payload["benchmark_context"],
            "benchmark_refresh": payload["benchmark_refresh"],
            "statistics_status": (
                "stale"
                if cycle.get("status") == "completed" and not artifact_available
                else cycle.get("status", "unavailable")
            ),
            "statistics_reason": cycle.get("reason", ""),
            "statistics_as_of_date": cycle.get("as_of_date"),
        }
    return reviews


def _load_trend_reports(
    data_dir: Path,
    reports_dir: Path,
    *,
    today: date | None = None,
    now: datetime | None = None,
    current_candidate_pool_ids: Mapping[str, tuple[int, ...]] | None = None,
) -> dict[str, dict[str, Any]]:
    reports = {
        broker: _load_broker_trend_report(
            data_dir=data_dir,
            reports_dir=reports_dir / directory,
            broker=broker,
            market=market,
            market_label=market_label,
            broker_label=broker_label,
            buy_window=buy_window,
            report_date=(
                today or _trend_market_date(market, now=now)
            ).isoformat(),
            current_candidate_pool_ids=(current_candidate_pool_ids or {}).get(market, ()),
        )
        for broker, (market, market_label, broker_label, directory, buy_window)
        in TREND_REPORT_SOURCES.items()
    }
    return reports


def _validated_trend_report_artifact(
    reports_dir: Path, *, artifact: str, market: str, broker: str
) -> tuple[Path, dict[str, Any], date, date, date, datetime, str] | None:
    artifact_path = Path(artifact)
    if artifact_path.name != artifact or artifact_path.suffix != ".json":
        raise ValueError("unsafe trend report artifact")
    reports_dir = reports_dir.resolve()
    path = (reports_dir / artifact).resolve()
    if path.parent != reports_dir:
        raise ValueError("unsafe trend report artifact")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    chronology = _valid_trend_report_payload(
        payload, market=market, broker=broker
    )
    snapshot = payload.get("strategy_snapshot")
    strategy_version = (
        str(snapshot.get("strategy_version") or "").strip()
        if isinstance(snapshot, dict)
        else ""
    )
    if chronology is None or not strategy_version:
        return None
    return path, payload, *chronology, strategy_version


def load_trend_report_history(
    reports_dir: Path, *, broker: str
) -> list[dict[str, Any]]:
    """Return strict, newest-first summaries for one trend broker."""
    try:
        market, _, _, directory, _ = TREND_REPORT_SOURCES[broker]
    except KeyError:
        raise ValueError(f"unsupported trend report broker: {broker}") from None
    rows: list[tuple[date, datetime, int, str, dict[str, Any]]] = []
    invalid: list[dict[str, Any]] = []

    def mark_unreadable(path: Path) -> None:
        invalid.append({
            "available": False,
            "artifact": path.name,
            "status_text": "报告不可读取",
        })

    broker_dir = reports_dir / directory
    for path in broker_dir.glob("*.json"):
        try:
            selected = _validated_trend_report_artifact(
                broker_dir,
                artifact=path.name,
                market=market,
                broker=broker,
            )
        except (FileNotFoundError, ValueError):
            selected = None
        if selected is None:
            mark_unreadable(path)
            continue
        (
            _,
            payload,
            execution_date,
            as_of_date,
            _,
            generated_at,
            strategy_version,
        ) = selected
        sell_actions, buy_actions, hold_actions, review_actions = (
            _project_trend_actions(payload, executions={})
        )
        revision_match = re.search(r"-r(\d+)\.json\Z", path.name)
        revision = int(revision_match.group(1)) if revision_match else 0
        summary = {
            "available": True,
            "artifact": path.name,
            "execution_date": execution_date.isoformat(),
            "data_date": as_of_date.isoformat(),
            "generated_at": generated_at.isoformat(),
            "strategy_version": strategy_version,
            "revision": revision,
            "execution_counts": {
                "sell": len(sell_actions),
                "buy": len(buy_actions),
                "hold": len(hold_actions),
                "review": len(review_actions),
            },
        }
        rows.append((execution_date, generated_at, revision, path.name, summary))
    rows.sort(key=lambda row: row[:4], reverse=True)
    invalid.sort(key=lambda row: row["artifact"], reverse=True)
    return [row[-1] for row in rows] + invalid


def load_historical_trend_report(
    config: DashboardConfig, *, broker: str, artifact: str
) -> dict[str, Any]:
    """Return the same report projection used by the current-report UI."""
    try:
        market, market_label, broker_label, directory, buy_window = (
            TREND_REPORT_SOURCES[broker]
        )
    except KeyError:
        raise ValueError(f"unsupported trend report broker: {broker}") from None
    broker_dir = config.reports_dir / directory
    selected = _validated_trend_report_artifact(
        broker_dir,
        artifact=artifact,
        market=market,
        broker=broker,
    )
    if selected is None:
        raise ValueError("trend report artifact is unreadable")
    (
        path,
        payload,
        execution_date,
        as_of_date,
        freshness_date,
        generated_at,
        _,
    ) = selected
    return _project_broker_trend_report(
        selected=(
            path,
            payload,
            execution_date,
            as_of_date,
            freshness_date,
            generated_at,
        ),
        data_dir=config.data_dir,
        reports_dir=broker_dir.resolve(),
        broker=broker,
        market=market,
        market_label=market_label,
        broker_label=broker_label,
        buy_window=buy_window,
        report_date=_shanghai_date().isoformat(),
        current_candidate_pool_ids=config.trend_candidate_pool_ids(market),
        historical=True,
    )


def _shanghai_date(now: datetime | None = None) -> date:
    return (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date()


def _trend_market_date(market: str, *, now: datetime | None = None) -> date:
    reference_now = now or datetime.now(SHANGHAI)
    return reference_now.astimezone(TREND_MARKET_TIMEZONES[market]).date()


def _latest_valid_report_payload(
    reports_dir: Path, *, market: str, broker: str
) -> tuple[Path, dict[str, Any], date, date, date, datetime] | None:
    matches: list[
        tuple[date, datetime, date, str, Path, dict[str, Any], date]
    ] = []
    for path in reports_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        chronology = _valid_trend_report_payload(
            payload, market=market, broker=broker
        )
        if chronology is None:
            continue
        execution_date, as_of_date, freshness_date, generated_at = chronology
        matches.append(
            (
                freshness_date,
                generated_at,
                execution_date,
                path.name,
                path,
                payload,
                as_of_date,
            )
        )
    if not matches:
        return None
    freshness_date, generated_at, execution_date, _, path, payload, as_of_date = max(
        matches, key=lambda item: item[:4]
    )
    return path, payload, execution_date, as_of_date, freshness_date, generated_at


def _valid_partial_trend_action(item: dict[str, Any]) -> bool:
    try:
        target_fraction = Decimal(str(item.get("target_fraction")))
        lot = Decimal(str(item.get("lot_size")))
        if isinstance(item.get("lot_size"), bool):
            return False
        lot_size = int(item.get("lot_size"))
        estimate = Decimal(str(item.get("estimated_shares")))
        position_started_for = item.get("position_started_for")
        started = date.fromisoformat(str(position_started_for)).isoformat()
        signals = item.get("overheat_signals")
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not all(value.is_finite() for value in (target_fraction, lot, estimate)):
        return False
    valid_signals = (
        isinstance(signals, list)
        and bool(signals)
        and all(
            isinstance(signal, str) and signal in {"boiling", "champagne"}
            for signal in signals
        )
        and len(signals) == len(set(signals))
    )
    return (
        item.get("reason") == "overheat_take_profit"
        and target_fraction == Decimal("0.30")
        and lot > 0
        and lot == lot.to_integral_value()
        and lot == Decimal(lot_size)
        and estimate >= 0
        and estimate == estimate.to_integral_value()
        and estimate % lot == 0
        and isinstance(position_started_for, str)
        and position_started_for == started
        and valid_signals
    )


def _trend_action_needs_review(item: dict[str, Any]) -> bool:
    action = item.get("action")
    reason = item.get("reason")
    known_reason = isinstance(reason, str) and reason in REASON_LABELS
    if action == "BUY":
        return reason not in (None, "") and not known_reason
    if action == "SELL_PARTIAL":
        return not _valid_partial_trend_action(item)
    return (
        action == "MANUAL_REVIEW"
        or action not in ACTION_LABELS
        or action in {"SELL_ALL", "HOLD"} and not known_reason
    )


def _canonical_trend_symbol(item: dict[str, Any], market: str) -> str:
    try:
        return to_futu_symbol(market, str(item.get("symbol") or ""))
    except ValueError:
        return ""


def _project_trend_membership_state(
    item: dict[str, Any],
    *,
    market: str,
    included_symbols: set[str],
) -> str:
    if item.get("reason") == "holding_trend_excluded":
        return "blacklisted"
    symbol = _canonical_trend_symbol(item, market)
    return "included" if symbol and symbol in included_symbols else "excluded"


def _project_trend_money_fields(
    item: dict[str, Any], *, payload: dict[str, Any], market: str
) -> dict[str, Any]:
    projected = dict(item)
    snapshot = payload.get("strategy_snapshot")
    parameters = snapshot.get("parameters") if isinstance(snapshot, dict) else None
    rate_value = projected.get("cny_per_local_currency")
    if rate_value in (None, "") and isinstance(parameters, dict):
        rate_value = parameters.get("cny_per_local_currency")
    try:
        rate = Decimal(str(rate_value)) if rate_value not in (None, "") else CNY_PER_LOCAL_CURRENCY[market]
    except (KeyError, InvalidOperation, TypeError, ValueError):
        rate = CNY_PER_LOCAL_CURRENCY.get(market, Decimal("1"))
    if not rate.is_finite() or rate <= 0:
        rate = CNY_PER_LOCAL_CURRENCY.get(market, Decimal("1"))
    for raw_key, normalized_key in (
        ("market_cap", "market_cap_cny_100m"),
        ("amount", "amount_cny_100m"),
    ):
        if projected.get(normalized_key) not in (None, ""):
            continue
        raw = projected.get(raw_key)
        if raw in (None, "") or isinstance(raw, bool):
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if value.is_finite():
            projected[normalized_key] = _decimal_text(value * rate)
    return projected


def _project_trend_money_items(
    items: object, *, payload: dict[str, Any], market: str
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        _project_trend_money_fields(item, payload=payload, market=market)
        for item in items
        if isinstance(item, dict)
    ]


def _project_trend_strength_fields(
    items: list[dict[str, Any]], snapshots: object,
) -> list[dict[str, Any]]:
    by_symbol = snapshots if isinstance(snapshots, dict) else {
        str(item.get("symbol") or ""): item
        for item in snapshots
        if isinstance(item, dict)
    } if isinstance(snapshots, list) else {}
    projected: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        snapshot = by_symbol.get(str(row.get("symbol") or ""))
        if isinstance(snapshot, dict):
            for key in ("strength", "global_strength"):
                if row.get(key) in (None, "") and snapshot.get(key) not in (None, ""):
                    row[key] = snapshot[key]
        projected.append(row)
    return projected


def _project_trend_order_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _project_trend_order_int(value: object) -> int | None:
    parsed = _project_trend_order_decimal(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _project_trend_holding_individual_key(item: dict[str, Any]) -> tuple[object, ...]:
    strength = _project_trend_order_decimal(item.get("strength"))
    days = _project_trend_order_int(item.get("days"))
    amount = _project_trend_order_decimal(
        item.get("amount")
        if item.get("amount") not in (None, "")
        else item.get("amount_cny_100m")
    )
    return (
        strength is None,
        -(strength or Decimal("0")),
        days is None,
        days or 0,
        amount is None,
        -(amount or Decimal("0")),
        str(item.get("symbol") or ""),
    )


def _project_trend_actions(
    payload: dict[str, Any],
    executions: dict[tuple[str, str], dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    judgments = payload["strategy_judgments"]
    metadata = payload.get("metadata")
    market = (
        str(metadata.get("market") or "CN").upper()
        if isinstance(metadata, dict)
        else "CN"
    )
    formal = [
        {
            **(
                projected_item := _project_trend_money_fields(
                    item, payload=payload, market=market
                )
            ),
            **(
                {"execution": executions[key]}
                if (key := (
                    str(projected_item.get("symbol") or "").strip(),
                    {"BUY": "buy", "SELL_ALL": "sell", "SELL_PARTIAL": "sell"}.get(
                        projected_item.get("action"), ""
                    ),
                )) in executions
                else {}
            ),
        }
        for item in judgments["formal_actions"]
    ]
    frozen_holding_snapshots = payload.get("signal_snapshots", {})
    frozen_holding_snapshots = (
        frozen_holding_snapshots.get("holdings", {})
        if isinstance(frozen_holding_snapshots, dict)
        else {}
    )
    holdings = []
    for item in judgments["holding_decisions"]:
        if not isinstance(item, dict):
            continue
        projected = _project_trend_money_fields(
            item, payload=payload, market=market
        )
        snapshot = frozen_holding_snapshots.get(str(projected.get("symbol") or ""))
        if isinstance(snapshot, dict):
            for key in ("industry", "industry_tm_id", "days"):
                if projected.get(key) in (None, "") and snapshot.get(key) not in (None, ""):
                    projected[key] = snapshot[key]
        if projected.get("phase") in (None, ""):
            if isinstance(snapshot, dict) and snapshot.get("phase") not in (None, ""):
                projected["phase"] = snapshot["phase"]
        holdings.append(projected)
    full_exit_symbols = {
        symbol
        for item in formal
        if item.get("action") == "SELL_ALL"
        and not _trend_action_needs_review(item)
        if (symbol := _canonical_trend_symbol(item, market))
    }
    sell_actions = [
        item
        for item in formal
        if item.get("action") in {"SELL_ALL", "SELL_PARTIAL"}
        and not _trend_action_needs_review(item)
        and not (
            item.get("action") == "SELL_PARTIAL"
            and _canonical_trend_symbol(item, market) in full_exit_symbols
        )
    ]
    buy_actions = [
        item
        for item in formal
        if item.get("action") == "BUY"
        and not _trend_action_needs_review(item)
    ]
    hold_actions = sorted(
        [
            item
            for item in holdings
            if item.get("action") == "HOLD"
            and not _trend_action_needs_review(item)
        ],
        key=_project_trend_holding_individual_key,
    )
    review_actions: list[dict[str, Any]] = []
    for item in formal + holdings:
        if _trend_action_needs_review(item) and item not in review_actions:
            review_actions.append(item)
    return sell_actions, buy_actions, hold_actions, review_actions


def _project_trend_real_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project the frozen read-only real-account decisions for the dashboard."""
    judgments = payload.get("strategy_judgments")
    if not isinstance(judgments, dict):
        return []
    raw_items = judgments.get("real_holding_decisions")
    if not isinstance(raw_items, list):
        return []
    metadata = payload.get("metadata")
    market = (
        str(metadata.get("market") or "CN").upper()
        if isinstance(metadata, dict)
        else "CN"
    )
    snapshots = payload.get("signal_snapshots")
    frozen = (
        snapshots.get("real_holdings", {})
        if isinstance(snapshots, dict)
        else {}
    )
    projected_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        projected = _project_trend_money_fields(
            item, payload=payload, market=market
        )
        snapshot = frozen.get(str(projected.get("symbol") or "")) if isinstance(frozen, dict) else None
        if isinstance(snapshot, dict):
            for key in ("industry", "industry_tm_id", "days", "phase"):
                if projected.get(key) in (None, "") and snapshot.get(key) not in (None, ""):
                    projected[key] = snapshot[key]
        projected_items.append(projected)
    return sorted(
        projected_items,
        key=lambda item: (
            item.get("reason") == "holding_trend_excluded",
            *_project_trend_holding_individual_key(item),
        ),
    )


def _project_rotation_execution_actions(
    payload: dict[str, Any],
    executions: Mapping[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Surface executed rotation legs as historical sell/buy actions."""
    judgments = payload.get("strategy_judgments")
    pairs = (
        judgments.get("simulate_rotation_pairs")
        if isinstance(judgments, dict)
        else None
    )
    sell_actions: list[dict[str, Any]] = []
    buy_actions: list[dict[str, Any]] = []
    for pair in pairs or []:
        if not isinstance(pair, Mapping) or pair.get("execution_mode") != "automatic":
            continue
        for side, symbol_key, name_key, futu_key, action, output in (
            ("sell", "sell_symbol", "sell_name", "sell_futu_symbol", "全部卖出", sell_actions),
            ("buy", "buy_symbol", "buy_name", "buy_futu_symbol", "正式买入", buy_actions),
        ):
            symbol = str(pair.get(symbol_key) or "").strip()
            execution = executions.get((symbol, side))
            if not symbol or execution is None:
                continue
            output.append({
                "symbol": symbol,
                "name": str(pair.get(name_key) or symbol).strip(),
                "futu_symbol": str(pair.get(futu_key) or "").strip(),
                "action": action,
                "reason": "relative_rotation",
                "target_weight": str(pair.get("target_weight") or ""),
                "target_amount": str(pair.get("target_amount") or ""),
                "estimated_shares": pair.get("estimated_shares"),
                "execution": dict(execution),
            })
    return sell_actions, buy_actions


def _valid_trend_collections(
    payload: dict[str, Any], judgments: dict[str, Any]
) -> bool:
    if any(
        not all(isinstance(item, dict) for item in judgments[key])
        for key in ("formal_actions", "holding_decisions", "top10_candidates")
    ):
        return False
    real_status = judgments.get("real_holding_decisions_status")
    if real_status is not None:
        if real_status == "available":
            if not isinstance(judgments.get("real_holding_decisions"), list):
                return False
            if not all(
                isinstance(item, dict)
                for item in judgments["real_holding_decisions"]
            ):
                return False
            if "real_holding_decisions_reason" in judgments:
                return False
        elif real_status == "unavailable":
            reason = judgments.get("real_holding_decisions_reason")
            if not isinstance(reason, str) or not reason.strip():
                return False
            if "real_holding_decisions" in judgments:
                return False
        else:
            return False
        source = judgments.get("real_holding_decisions_source", {})
        if not isinstance(source, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in source.items()
        ):
            return False
    elif any(
        key in judgments
        for key in (
            "real_holding_decisions",
            "real_holding_decisions_reason",
            "real_holding_decisions_source",
        )
    ):
        return False
    risk_skips = judgments.get("risk_skips", [])
    if not isinstance(risk_skips, list) or not all(
        isinstance(item, dict) for item in risk_skips
    ):
        return False
    snapshots = payload.get("signal_snapshots")
    if _is_current_final_plan_payload(payload):
        candidates = snapshots.get("candidates") if isinstance(snapshots, dict) else None
        if not isinstance(candidates, list) or not all(
            _valid_current_candidate_signal(item) for item in candidates
        ):
            return False
    elif snapshots is not None and (
        not isinstance(snapshots, dict)
        or "candidates" in snapshots
        and (
            not isinstance(snapshots["candidates"], list)
            or not all(isinstance(item, dict) for item in snapshots["candidates"])
        )
    ):
        return False
    excluded = payload.get("excluded", {})
    if not isinstance(excluded, dict) or any(
        not isinstance(symbol, str)
        or not isinstance(reasons, list)
        or not all(isinstance(reason, str) for reason in reasons)
        for symbol, reasons in excluded.items()
    ):
        return False
    industries = payload.get("industry_concentration", [])
    if not isinstance(industries, list) or any(
        not isinstance(row, list)
        or len(row) != 3
        or any(isinstance(value, (dict, list)) for value in row)
        for row in industries
    ):
        return False
    return all(
        isinstance(values, list)
        and all(isinstance(value, str) for value in values)
        for values in (
            payload.get("data_sources", []),
            payload.get("api_facts", []),
        )
    )


def _is_current_final_plan_payload(payload: Mapping[str, object]) -> bool:
    snapshot = payload.get("strategy_snapshot")
    metadata = payload.get("metadata")
    market = str(
        (snapshot.get("market") if isinstance(snapshot, Mapping) else None)
        or (metadata.get("market") if isinstance(metadata, Mapping) else "")
    ).upper()
    version = str(
        snapshot.get("strategy_version") if isinstance(snapshot, Mapping) else ""
    )
    return (market, version) in CURRENT_FINAL_PLAN_TREND_VERSIONS


def _valid_current_candidate_signal(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    eligible = value.get("eligible")
    excluded_reasons = value.get("excluded_reasons")
    name = value.get("name")
    if (
        not isinstance(value.get("symbol"), str)
        or not value["symbol"].strip()
        or type(eligible) is not bool
        or not isinstance(excluded_reasons, list)
        or not all(isinstance(reason, str) and reason.strip() for reason in excluded_reasons)
    ):
        return False
    rank = value.get("rank")
    name_missing = name is None or isinstance(name, str) and not name.strip()
    if name_missing:
        if not (
            eligible is False
            and rank is None
            and "name_missing" in excluded_reasons
        ):
            return False
    elif not isinstance(name, str):
        return False
    if eligible:
        return (
            isinstance(rank, int)
            and not isinstance(rank, bool)
            and rank >= 1
            and not excluded_reasons
        )
    return rank is None and bool(excluded_reasons)


def _valid_frozen_trend_facts(payload: dict[str, Any]) -> bool:
    fact_keys = {"api_cost", "industry_context_status", "industry_contexts"}
    if not fact_keys.intersection(payload):
        status = payload.get("industry_context_status")
        mode = status.get("ordering_mode") if isinstance(status, dict) else None
        if _is_current_final_plan_payload(payload) or mode == "individual_global":
            return False
        return True
    api_cost = payload.get("api_cost")
    status = payload.get("industry_context_status")
    contexts = payload.get("industry_contexts")
    if not isinstance(api_cost, dict):
        return False
    api_cost_keys = set(api_cost)
    legacy_api_cost = api_cost_keys == {
        "actual",
        "estimated",
        "estimate_complete",
        "unit",
    }
    current_api_cost = legacy_api_cost or api_cost_keys == {
        "actual",
        "estimated",
        "estimate_complete",
        "unit",
        "label",
    }
    if not current_api_cost:
        return False

    def valid_cost(value: object) -> bool:
        if value is None or isinstance(value, bool):
            return value is None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return parsed.is_finite() and parsed >= 0

    if (
        not valid_cost(api_cost["actual"])
        or not valid_cost(api_cost["estimated"])
        or type(api_cost["estimate_complete"]) is not bool
        or api_cost["unit"] != TREND_API_COST_UNIT
        or not legacy_api_cost
        and (
            not isinstance(api_cost["label"], str)
            or not api_cost["label"].strip()
        )
    ):
        return False
    try:
        actual = (
            None
            if api_cost["actual"] is None
            else Decimal(str(api_cost["actual"]))
        )
        estimated = (
            None
            if api_cost["estimated"] is None
            else Decimal(str(api_cost["estimated"]))
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not legacy_api_cost and api_cost["label"] != trend_api_cost_label(
        actual=actual,
        estimated=estimated,
        estimate_complete=api_cost["estimate_complete"],
    ):
        return False
    if legacy_api_cost and not {
        "industry_context_status",
        "industry_contexts",
    }.intersection(payload):
        return not _is_current_final_plan_payload(payload)
    snapshot = payload.get("strategy_snapshot")
    if not isinstance(snapshot, dict):
        return False
    rows = snapshot.get("parameter_rows")
    if (
        not isinstance(rows, list)
        or not rows
        or any(
            not isinstance(row, dict)
            or set(row) != {"group", "name", "value"}
            or any(
                not isinstance(row[key], str) or not row[key].strip() for key in row
            )
            for row in rows
        )
    ):
        return False

    if not isinstance(status, dict):
        return False
    mode = status.get("ordering_mode")
    if mode not in {
        "context_with_history",
        "context_current_only",
        "individual_global",
        "legacy_invalid_current",
        "legacy_no_eligible_candidates",
    }:
        return False
    if type(status.get("current_complete")) is not bool or type(
        status.get("history_complete")
    ) is not bool:
        return False
    fallback_reason = status.get("fallback_reason")
    if fallback_reason is not None and (
        not isinstance(fallback_reason, str) or not fallback_reason.strip()
    ):
        return False
    current_final_plan = _is_current_final_plan_payload(payload)
    if (mode == "individual_global") != current_final_plan:
        return False
    if mode == "context_with_history" and (
        not status["current_complete"] or not status["history_complete"]
    ):
        return False
    if mode == "context_current_only" and (
        not status["current_complete"] or status["history_complete"]
    ):
        return False
    if mode == "individual_global" and (
        not status["current_complete"]
        or status["history_complete"]
        or fallback_reason is not None
    ):
        return False
    if mode == "legacy_invalid_current" and status["current_complete"]:
        return False
    affected = status.get("affected_industry_ids")
    if affected is not None and (
        not isinstance(affected, list)
        or not all(
            value == "unknown"
            or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            )
            for value in affected
        )
    ):
        return False
    validation_reasons = status.get("validation_reasons")
    if validation_reasons is not None and (
        not isinstance(validation_reasons, dict)
        or any(
            not isinstance(key, str)
            or not isinstance(reasons, list)
            or not all(isinstance(reason, str) and reason.strip() for reason in reasons)
            for key, reasons in validation_reasons.items()
        )
    ):
        return False

    if not isinstance(contexts, list):
        return False
    context_keys = {
        "industry_tm_id",
        "industry",
        "as_of_date",
        "component_count",
        "snapshot_count",
        "tradable_count",
        "valid_count",
        "right_count",
        "snapshot_coverage",
        "right_state_coverage",
        "right_share",
        "warm_to_hot_count",
        "temperature",
        "strength",
        "valid",
        "invalid_reasons",
        "prior_as_of_date",
        "prior_temperature",
        "prior_right_share",
        "temperature_direction",
        "right_share_change_pp",
    }
    aggregate_ratio_keys = {
        "aggregate_right_count_ratio",
        "aggregate_right_market_cap_ratio",
        "prior_aggregate_right_count_ratio",
        "prior_aggregate_right_market_cap_ratio",
    }
    optional_context_keys = aggregate_ratio_keys | {"member_breadth_collected"}

    def valid_decimal(value: object, *, minimum: Decimal, maximum: Decimal) -> bool:
        if isinstance(value, bool):
            return False
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return parsed.is_finite() and minimum <= parsed <= maximum

    seen_ids: set[int] = set()
    for context in contexts:
        if (
            not isinstance(context, dict)
            or not context_keys <= set(context)
            or set(context) - context_keys - optional_context_keys
        ):
            return False
        if (
            "member_breadth_collected" in context
            and type(context["member_breadth_collected"]) is not bool
        ):
            return False
        industry_id = context["industry_tm_id"]
        if (
            isinstance(industry_id, bool)
            or not isinstance(industry_id, int)
            or industry_id <= 0
            or industry_id in seen_ids
            or not isinstance(context["industry"], str)
            or not context["industry"].strip()
            or not _valid_iso_date(context["as_of_date"])
            or type(context["valid"]) is not bool
            or not isinstance(context["invalid_reasons"], list)
            or not all(
                isinstance(reason, str) and reason.strip()
                for reason in context["invalid_reasons"]
            )
        ):
            return False
        seen_ids.add(industry_id)
        for key in (
            "component_count",
            "snapshot_count",
            "tradable_count",
            "valid_count",
            "right_count",
            "warm_to_hot_count",
        ):
            if (
                isinstance(context[key], bool)
                or not isinstance(context[key], int)
                or context[key] < 0
            ):
                return False
        if not valid_decimal(
            context["snapshot_coverage"], minimum=Decimal("0"), maximum=Decimal("1")
        ) or not valid_decimal(
            context["right_state_coverage"], minimum=Decimal("0"), maximum=Decimal("1")
        ):
            return False
        for key in ("right_share", "prior_right_share"):
            if context[key] is not None and not valid_decimal(
                context[key], minimum=Decimal("0"), maximum=Decimal("1")
            ):
                return False
        for key in aggregate_ratio_keys:
            if context.get(key) is not None and not valid_decimal(
                context[key], minimum=Decimal("0"), maximum=Decimal("1")
            ):
                return False
        if context["strength"] is not None and not valid_decimal(
            context["strength"], minimum=Decimal("0"), maximum=Decimal("100")
        ):
            return False
        if context["prior_as_of_date"] is not None and not _valid_iso_date(
            context["prior_as_of_date"]
        ):
            return False
        if context["prior_temperature"] is not None and not isinstance(
            context["prior_temperature"], str
        ):
            return False
        if context["temperature"] is not None and not isinstance(
            context["temperature"], str
        ):
            return False
        if context["temperature_direction"] is not None and context[
            "temperature_direction"
        ] not in {"rising", "unchanged", "falling"}:
            return False
        if context["right_share_change_pp"] is not None and not valid_decimal(
            context["right_share_change_pp"],
            minimum=Decimal("-100"),
            maximum=Decimal("100"),
        ):
            return False
    return True


def _valid_trend_risk_summary(payload: dict[str, Any]) -> bool:
    snapshot = payload.get("strategy_snapshot")
    strategy_version = (
        str(snapshot.get("strategy_version") or "")
        if isinstance(snapshot, dict)
        else ""
    )
    summary = payload.get("risk_summary")
    if summary is None:
        if _is_current_final_plan_payload(payload):
            return False
        return strategy_version not in {
            "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10"
        }
    if not isinstance(summary, dict) or any(
        isinstance(value, (dict, list)) for value in summary.values()
    ):
        return False
    if _is_current_final_plan_payload(payload):
        judgments = payload.get("strategy_judgments")
        parameters = snapshot.get("parameters") if isinstance(snapshot, dict) else None
        account = payload.get("account")
        expected_nav = account.get("net_value") if isinstance(account, dict) else None
        return _valid_current_trend_risk_contract(
            payload,
            judgments,
            parameters=parameters,
            expected_nav=expected_nav,
        )
    if strategy_version not in {
        "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10"
    }:
        return summary.get("status") in {"active", "paused"}
    judgments = payload.get("strategy_judgments")
    parameters = snapshot.get("parameters") if isinstance(snapshot, dict) else None
    account = payload.get("account")
    expected_nav = account.get("net_value") if isinstance(account, dict) else None
    risk_valid = (
        isinstance(judgments, dict)
        and "risk_skips" in judgments
        and {
            "v2": valid_v2_risk_contract,
            "v3": valid_v3_risk_contract,
            "v4": valid_v4_risk_contract,
            "v5": valid_v4_risk_contract,
            "v6": valid_v4_risk_contract,
            "v7": valid_v4_risk_contract,
            "v8": valid_v4_risk_contract,
            "v9": valid_v4_risk_contract,
            "v10": valid_v4_risk_contract,
        }[strategy_version](
            parameters, summary, expected_nav=expected_nav
        )
        and _valid_v2_risk_items(
            payload, judgments, summary, strategy_version=strategy_version
        )
    )
    if strategy_version in {"v2", "v3"}:
        return risk_valid
    metadata = payload.get("metadata")
    market = metadata.get("market") if isinstance(metadata, dict) else ""
    strategy_id = snapshot.get("strategy_id") if isinstance(snapshot, dict) else ""
    formal_actions = judgments.get("formal_actions") if isinstance(judgments, dict) else None
    drawdown = payload.get("drawdown_summary")
    return (
        risk_valid
        and valid_drawdown_decision(
            drawdown,
            expected_market=str(market),
            expected_strategy_id=str(strategy_id),
            expected_strategy_version=strategy_version,
            expected_equity=expected_nav,
            expected_entry_date=str(payload.get("execution_date") or ""),
        )
        and (
            drawdown.get("entry_allowed") is True
            or isinstance(formal_actions, list)
            and not any(
                isinstance(action, dict) and action.get("action") == "BUY"
                for action in formal_actions
            )
        )
    )


def _valid_current_trend_risk_contract(
    payload: dict[str, Any],
    judgments: object,
    *,
    parameters: object,
    expected_nav: object,
) -> bool:
    if not isinstance(judgments, dict):
        return False
    if any(
        not isinstance(judgments.get(key), list)
        or not all(isinstance(item, dict) for item in judgments[key])
        for key in ("formal_actions", "holding_decisions")
    ):
        return False
    risk_skips = judgments.get("risk_skips")
    if not isinstance(risk_skips, list):
        return False
    summary = payload.get("risk_summary")
    if not isinstance(summary, dict) or not valid_v4_risk_contract(
        parameters, summary, expected_nav=expected_nav
    ):
        return False
    if not isinstance(parameters, Mapping):
        return False
    raw_target_weight = parameters.get("target_weight")
    if isinstance(raw_target_weight, Mapping):
        target_weights = [
            parsed
            for value in raw_target_weight.values()
            if (parsed := _dashboard_risk_decimal(value)) is not None
        ]
        target_weight_cap = max(target_weights, default=None)
    else:
        target_weight_cap = _dashboard_risk_decimal(raw_target_weight)
    if (
        target_weight_cap is None
        or target_weight_cap <= 0
        or target_weight_cap > 1
    ):
        return False
    for item in risk_skips:
        if not isinstance(item, dict):
            return False
        symbol = item.get("symbol")
        reason = item.get("reason")
        constraint = item.get("decisive_constraint")
        shares = item.get("estimated_shares")
        if (
            not isinstance(symbol, str) or not symbol.strip()
            or not isinstance(reason, str) or not reason.strip()
            or not isinstance(constraint, str) or not constraint.strip()
            or isinstance(shares, bool) or not isinstance(shares, int) or shares != 0
        ):
            return False
        target_weight = item.get("target_weight")
        target_amount = item.get("target_amount")
        if target_weight is None:
            if target_amount is not None or shares != 0:
                return False
            continue
        parsed_weight = _dashboard_risk_decimal(target_weight)
        parsed_amount = _dashboard_risk_decimal(target_amount)
        zero_kelly_skip = (
            parsed_weight == 0
            and parsed_amount == 0
            and summary.get("status") == "paused"
            and summary.get("kelly_cap") in {"0", "0.000000", 0}
            and summary.get("pause_reason") == "Kelly 上限为 0，仅暂停未来新开仓"
            and item.get("reason") == summary.get("pause_reason")
            and constraint == "Kelly 上限"
        )
        if (
            parsed_weight is None
            or parsed_weight < 0
            or parsed_weight > 1
            or parsed_weight > target_weight_cap
            or parsed_weight == 0 and not zero_kelly_skip
        ):
            return False
        if target_amount is not None and parsed_amount is None:
            return False
    risk_judgments = {**judgments, "risk_skips": []}
    if not _valid_v2_risk_items(
        payload,
        risk_judgments,
        summary,
        strategy_version="v10",
    ):
        return False
    snapshot = payload.get("strategy_snapshot")
    metadata = payload.get("metadata")
    drawdown = payload.get("drawdown_summary")
    formal_actions = judgments.get("formal_actions")
    market = metadata.get("market") if isinstance(metadata, dict) else ""
    strategy_id = snapshot.get("strategy_id") if isinstance(snapshot, dict) else ""
    return valid_drawdown_decision(
        drawdown,
        expected_market=str(market),
        expected_strategy_id=str(strategy_id),
        expected_strategy_version=str(snapshot.get("strategy_version") or "") if isinstance(snapshot, dict) else "",
        expected_equity=expected_nav,
        expected_entry_date=str(payload.get("execution_date") or ""),
    ) and (
        drawdown.get("entry_allowed") is True
        or isinstance(formal_actions, list)
        and not any(
            isinstance(action, dict) and action.get("action") == "BUY"
            for action in formal_actions
        )
    )


def _dashboard_risk_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _valid_v2_risk_items(
    payload: dict[str, Any],
    judgments: dict[str, Any],
    summary: dict[str, Any],
    *,
    strategy_version: str = "v2",
) -> bool:
    snapshot = payload.get("strategy_snapshot")
    parameters = snapshot.get("parameters") if isinstance(snapshot, dict) else None
    target_weight_limit = PORTFOLIO_RISK_LIMIT
    configured_target = parameters.get("target_weight") if isinstance(parameters, dict) else None
    if configured_target is not None and not isinstance(configured_target, (dict, list)):
        configured_limit = _dashboard_risk_decimal(configured_target)
        if configured_limit is None or configured_limit <= 0 or configured_limit > 1:
            return False
        target_weight_limit = configured_limit
    portfolio_limit = _dashboard_risk_decimal(summary.get("portfolio_risk_limit"))
    nav = (
        portfolio_limit / PORTFOLIO_RISK_LIMIT
        if portfolio_limit is not None and portfolio_limit > 0
        else None
    )
    buys = [
        item
        for item in judgments["formal_actions"]
        if item.get("action") == "BUY"
    ]
    if (nav is None or summary.get("status") == "paused") and buys:
        return False
    new_planned_risk = Decimal("0")
    allowed_buy_constraints = {
        "名义仓位上限", "单笔风险上限", "组合剩余风险", "现金"
    }
    if strategy_version in {"v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10"}:
        allowed_buy_constraints.add("Kelly 上限")
    for item in buys:
        shares = item.get("estimated_shares")
        lot_size = item.get("lot_size")
        planned_risk = _dashboard_risk_decimal(item.get("planned_stop_risk"))
        planned_pct = _dashboard_risk_decimal(item.get("planned_stop_risk_pct"))
        normal_cost = _dashboard_risk_decimal(item.get("normal_cost"))
        target_weight = _dashboard_risk_decimal(item.get("target_weight"))
        target_amount = _dashboard_risk_decimal(item.get("target_amount"))
        close = _dashboard_risk_decimal(item.get("close"))
        if (
            not isinstance(item.get("symbol"), str)
            or not item["symbol"].strip()
            or isinstance(shares, bool)
            or not isinstance(shares, int)
            or shares <= 0
            or isinstance(lot_size, bool)
            or not isinstance(lot_size, int)
            or lot_size <= 0
            or shares % lot_size != 0
            or planned_risk is None
            or planned_risk <= 0
            or planned_pct is None
            or planned_pct <= 0
            or normal_cost is None
            or normal_cost <= 0
            or target_weight is None
            or target_weight <= 0
            or target_weight > target_weight_limit
            or strategy_version in {
                "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10",
            }
            and summary.get("kelly_phase") != "cold_start"
            and target_weight
            > (_dashboard_risk_decimal(summary.get("kelly_cap")) or Decimal("0"))
            or target_amount is None
            or close is None
            or close <= 0
            or normal_cost > planned_risk
            or nav is None
            or planned_pct != planned_risk / nav
            or planned_pct > SINGLE_ENTRY_RISK_LIMIT
            or item.get("decisive_constraint") not in allowed_buy_constraints
        ):
            return False
        new_planned_risk += planned_risk

    summary_new_risk = _dashboard_risk_decimal(summary.get("new_planned_risk"))
    if summary_new_risk != new_planned_risk:
        return False
    allowed_constraints = {
        "名义仓位上限",
        "单笔风险上限",
        "组合剩余风险",
        "现金",
        "持仓席位",
        "交易单位",
        "关键风险数据",
    }
    if strategy_version in {"v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10"}:
        allowed_constraints.add("Kelly 上限")
    if strategy_version in {"v4", "v5", "v6", "v7", "v8", "v9", "v10"}:
        allowed_constraints.add("策略累计回撤")
    for item in judgments["risk_skips"]:
        shares = item.get("estimated_shares")
        target_weight = _dashboard_risk_decimal(item.get("target_weight"))
        target_amount_raw = item.get("target_amount")
        target_amount = _dashboard_risk_decimal(target_amount_raw)
        zero_kelly_skip = (
            strategy_version in {
                "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10",
            }
            and summary.get("status") == "paused"
            and summary.get("kelly_cap") in {"0", "0.000000", 0}
            and summary.get("pause_reason") == "Kelly 上限为 0，仅暂停未来新开仓"
            and item.get("reason") == summary.get("pause_reason")
            and item.get("decisive_constraint") == "Kelly 上限"
            and target_weight == 0
            and target_amount == 0
        )
        if (
            not isinstance(item.get("symbol"), str)
            or not item["symbol"].strip()
            or isinstance(shares, bool)
            or not isinstance(shares, int)
            or shares != 0
            or target_weight is None
            or target_weight <= 0
            and not zero_kelly_skip
            or target_weight > target_weight_limit
            or target_amount_raw is not None
            and target_amount is None
            or not isinstance(item.get("reason"), str)
            or not item["reason"].strip()
            or item.get("decisive_constraint") not in allowed_constraints
        ):
            return False
    return True


def _valid_option_attention(payload: dict[str, Any], *, market: str) -> bool:
    if "option_attention" not in payload:
        return market == "CN"
    attention = payload["option_attention"]
    if not isinstance(attention, list):
        return False
    if market == "CN":
        return not attention

    def scalar(value: object) -> bool:
        return (
            value is None
            or isinstance(value, (str, bool, int))
            or isinstance(value, float) and Decimal(str(value)).is_finite()
        )

    for item in attention:
        if not isinstance(item, dict) or set(item) != OPTION_ATTENTION_KEYS:
            return False
        if any(
            not scalar(item[key])
            for key in OPTION_ATTENTION_KEYS - OPTION_ATTENTION_TRANSITIONS
        ):
            return False
        if (
            item["market"] != market
            or not isinstance(item["symbol"], str)
            or not item["symbol"].strip()
            or not isinstance(item["category"], str)
            or item["category"] not in {"risk", "strengthened", "watch"}
            or not isinstance(item["source_broker"], str)
            or not item["source_broker"].strip()
            or not isinstance(item["source_action"], str)
            or not item["source_action"].strip()
        ):
            return False
        for key in OPTION_ATTENTION_TRANSITIONS:
            transition = item[key]
            if (
                not isinstance(transition, dict)
                or set(transition) != {"previous", "current", "changed"}
                or not isinstance(transition["changed"], bool)
                or not scalar(transition["previous"])
                or not scalar(transition["current"])
            ):
                return False
    return True


def _valid_trend_report_payload(
    payload: dict[str, Any], *, market: str, broker: str
) -> tuple[date, date, date, datetime] | None:
    try:
        execution_date = date.fromisoformat(payload["execution_date"])
        as_of_date = date.fromisoformat(payload["as_of_date"])
        generated_at = datetime.fromisoformat(payload["generated_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        execution_date.isoformat() != payload["execution_date"]
        or as_of_date.isoformat() != payload["as_of_date"]
        or generated_at.isoformat() != payload["generated_at"]
        or generated_at.tzinfo is None
        or generated_at.utcoffset() is None
    ):
        return None
    judgments = payload.get("strategy_judgments")
    account = payload.get("account")
    metadata = payload.get("metadata")
    source_run_date = metadata.get("run_date") if isinstance(metadata, dict) else None
    if source_run_date is None:
        freshness_date = generated_at.astimezone(SHANGHAI).date()
    else:
        try:
            freshness_date = date.fromisoformat(source_run_date)
        except (TypeError, ValueError):
            return None
        if freshness_date.isoformat() != source_run_date:
            return None
    if not (
        isinstance(judgments, dict)
        and all(
            isinstance(judgments.get(key), list)
            for key in ("formal_actions", "holding_decisions", "top10_candidates")
        )
        and valid_serialized_account(account)
        and isinstance(metadata, dict)
        and str(metadata.get("market") or "").upper() == market
        and str(metadata.get("broker") or "").lower() == broker
        and _valid_trend_collections(payload, judgments)
        and _valid_frozen_trend_facts(payload)
        and _valid_trend_risk_summary(payload)
        and _valid_option_attention(payload, market=market)
        and valid_frozen_report_contract(payload)
        and as_of_date <= freshness_date <= execution_date
    ):
        return None
    return execution_date, as_of_date, freshness_date, generated_at


def _load_broker_trend_report(
    *,
    data_dir: Path,
    reports_dir: Path,
    broker: str,
    market: str,
    market_label: str,
    broker_label: str,
    buy_window: str,
    report_date: str,
    current_candidate_pool_ids: tuple[int, ...] = (),
) -> dict[str, Any]:
    unavailable = {
        "available": False,
        "data_status": "unavailable",
        "broker": broker,
        "broker_label": broker_label,
        "market": market,
        "market_label": market_label,
        "status_text": "暂时不可用",
    }
    selected = _latest_valid_report_payload(
        reports_dir, market=market, broker=broker
    )
    if selected is None:
        return unavailable
    return _project_broker_trend_report(
        selected=selected,
        data_dir=data_dir,
        reports_dir=reports_dir,
        broker=broker,
        market=market,
        market_label=market_label,
        broker_label=broker_label,
        buy_window=buy_window,
        report_date=report_date,
        current_candidate_pool_ids=current_candidate_pool_ids,
        use_execution_batch=True,
    )


def _project_broker_trend_report(
    *,
    selected: tuple[Path, dict[str, Any], date, date, date, datetime],
    data_dir: Path,
    reports_dir: Path,
    broker: str,
    market: str,
    market_label: str,
    broker_label: str,
    buy_window: str,
    report_date: str,
    current_candidate_pool_ids: tuple[int, ...] = (),
    use_execution_batch: bool = False,
    historical: bool = False,
) -> dict[str, Any]:
    _, latest_payload, *_ = selected
    latest_report_sha256 = _report_hash(latest_payload)
    execution_batch: dict[str, object] | None = None
    execution_batch_error = ""
    revision_anomaly = False
    if use_execution_batch:
        batch_path = (
            data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "batches"
            / f"{selected[2].isoformat()}.json"
        )
        try:
            batch_text = batch_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            execution_batch_error = "执行批次无效，已阻止操作投影"
        else:
            try:
                batch = json.loads(batch_text)
                batch = _validate_execution_batch(
                    batch,
                    market=market,
                    execution_date=selected[2].isoformat(),
                )
                locked_path = Path(str(batch["report_path"])).resolve()
                if locked_path.parent != reports_dir.resolve():
                    raise ValueError
                locked = _validated_trend_report_artifact(
                    reports_dir,
                    artifact=locked_path.name,
                    market=market,
                    broker=broker,
                )
                if (
                    locked is None
                    or locked[0].resolve() != locked_path
                    or locked[2] != selected[2]
                    or _report_hash(locked[1]) != batch.get("report_sha256")
                ):
                    raise ValueError
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                ValueError,
            ):
                execution_batch_error = "执行批次无效，已阻止操作投影"
            else:
                (
                    path,
                    payload,
                    execution_date,
                    as_of_date,
                    freshness_date,
                    generated_at,
                    _,
                ) = locked
                locked_selected = (
                    path,
                    payload,
                    execution_date,
                    as_of_date,
                    freshness_date,
                    generated_at,
                )
                if (
                    latest_payload["strategy_judgments"]["formal_actions"]
                    != payload["strategy_judgments"]["formal_actions"]
                ):
                    selected = locked_selected
                execution_batch = batch
                revision_anomaly = batch["report_sha256"] != latest_report_sha256
    if execution_batch_error:
        return {
            "available": False,
            "data_status": "unavailable",
            "broker": broker,
            "broker_label": broker_label,
            "market": market,
            "market_label": market_label,
            "status_text": execution_batch_error,
            "execution_batch": None,
            "execution_batch_blocking": True,
            "execution_batch_error": execution_batch_error,
            "artifact": "",
            "report_sha256": "",
            "latest_report_sha256": "",
            "revision_anomaly": False,
            "strategy_version": "",
            "current_strategy_version": "",
            "current_strategy_parameter_rows": [],
            "report_date": "",
            "data_date": "",
            "generated_at": "",
            "option_attention": [],
            "account_source_date": "",
            "account_fresh": False,
            "account_status": "",
            "buy_window": buy_window,
            "run_status": "",
            "sell_actions": [],
            "buy_actions": [],
            "hold_actions": [],
            "review_actions": [],
            "real_position_actions": [],
            "simulate_rotation_pairs": [],
            "simulate_rotation_comparisons": [],
            "real_rotation_pairs": [],
            "real_rotation_comparisons": [],
            "real_position_status": "unavailable",
            "real_position_reason": execution_batch_error,
            "real_position_source": {},
            "risk_skips": [],
            "risk_summary": {},
            "drawdown_summary": {},
            "actual_overlay": {},
            "counts": {"sell": 0, "buy": 0, "hold": 0, "review": 0},
            "recent_protection_alert": None,
            "audit": {},
        }
    path, payload, execution_date, as_of_date, freshness_date, generated_at = selected
    account = payload["account"]
    metadata = payload["metadata"]
    report_sha256 = _report_hash(payload)
    execution_report_sha256 = (
        str(execution_batch["report_sha256"])
        if execution_batch is not None
        else report_sha256
    )
    executions = _trend_action_executions(
        data_dir,
        market=market,
        execution_date=execution_date.isoformat(),
        report_sha256=execution_report_sha256,
    )
    sell_actions, buy_actions, hold_actions, review_actions = (
        _project_trend_actions(payload, executions)
    )
    rotation_sell_actions, rotation_buy_actions = _project_rotation_execution_actions(
        payload, executions
    )
    sell_actions = [*sell_actions, *rotation_sell_actions]
    buy_actions = [*buy_actions, *rotation_buy_actions]
    real_position_actions = _project_trend_real_actions(payload)
    frozen_signals = payload.get("signal_snapshots")
    frozen_signals = frozen_signals if isinstance(frozen_signals, dict) else {}
    buy_actions = _project_trend_strength_fields(
        buy_actions, frozen_signals.get("candidates")
    )
    sell_actions = _project_trend_strength_fields(
        sell_actions, frozen_signals.get("holdings")
    )
    hold_actions = _project_trend_strength_fields(
        hold_actions, frozen_signals.get("holdings")
    )
    real_position_actions = _project_trend_strength_fields(
        real_position_actions, frozen_signals.get("real_holdings")
    )
    included_symbols = {
        symbol
        for item in [*buy_actions, *hold_actions]
        if (symbol := _canonical_trend_symbol(item, market))
    }
    for item in [*hold_actions, *real_position_actions]:
        item["trend_report_state"] = _project_trend_membership_state(
            item,
            market=market,
            included_symbols=included_symbols,
        )
    if market in {"US", "HK"}:
        option_anomalies = _trend_option_anomalies(
            data_dir,
            market=market,
            report_date=execution_date.isoformat(),
            historical=historical,
        )

        def attach_option_anomaly(item: dict[str, Any]) -> None:
            symbol = str(item.get("symbol") or "").strip().upper()
            try:
                normalized_symbol = normalize_backtest_symbol(market, symbol)
            except ValueError:
                normalized_symbol = symbol
            option_anomaly = option_anomalies.get((market, normalized_symbol))
            if option_anomaly is None:
                option_anomaly = {
                    **_missing_futu_skill_signal(),
                    "run_date": "",
                    "reason": "富途未返回该标的期权异动",
                }
            item["option_anomaly"] = option_anomaly

        for action in [*buy_actions, *hold_actions, *real_position_actions]:
            attach_option_anomaly(action)
    risk_skips = _project_trend_money_items(
        payload["strategy_judgments"].get("risk_skips", []),
        payload=payload,
        market=market,
    )
    account_fresh = account.get("fresh") is True
    directory = reports_dir.name
    signal_snapshots = payload.get("signal_snapshots", {})
    audit_candidates = payload["strategy_judgments"]["top10_candidates"]
    if _is_current_final_plan_payload(payload):
        audit_candidates = (
            signal_snapshots.get("candidates", [])
            if isinstance(signal_snapshots, dict)
            else []
        )
    elif isinstance(signal_snapshots, dict):
        audit_candidates = signal_snapshots.get("candidates", audit_candidates)
    updated_today = freshness_date.isoformat() == report_date
    execution_today = execution_date.isoformat() == report_date
    current = updated_today or execution_today
    data_date = as_of_date.isoformat()
    risk_summary = dict(payload.get("risk_summary", {}))
    risk_summary["trade_stats"] = _project_trend_trade_stats(
        data_dir,
        market=market,
        strategy_snapshot=payload.get("strategy_snapshot"),
    )
    strategy_snapshot = payload.get("strategy_snapshot")
    raw_strategy_parameters = (
        strategy_snapshot.get("parameters")
        if isinstance(strategy_snapshot, dict)
        else None
    )
    strategy_parameters = (
        dict(raw_strategy_parameters)
        if isinstance(raw_strategy_parameters, dict)
        else {}
    )
    frozen_api_cost = payload.get("api_cost")
    if not isinstance(frozen_api_cost, dict):
        frozen_api_cost = None
    frozen_industry_context_status = payload.get("industry_context_status")
    if not isinstance(frozen_industry_context_status, dict):
        frozen_industry_context_status = {}
    frozen_industry_contexts = payload.get("industry_contexts")
    if not isinstance(frozen_industry_contexts, list):
        frozen_industry_contexts = []
    frozen_parameter_rows = (
        strategy_snapshot.get("parameter_rows")
        if isinstance(strategy_snapshot, dict)
        and {"api_cost", "industry_context_status", "industry_contexts"}.intersection(
            payload
        )
        else None
    )
    if not isinstance(frozen_parameter_rows, list):
        frozen_parameter_rows = []
    allocation = payload.get("allocation")
    current_allocation_reference = (
        {
            "daily_path": allocation["daily_path"],
            "sha256": allocation["sha256"],
            "snapshot": {"markets": allocation["markets"]},
        }
        if isinstance(allocation, dict)
        else None
    )
    current_strategy_snapshot = (
        live_trend_strategy_snapshot(
            market,
            str(strategy_snapshot.get("process_version") or ""),
            current_candidate_pool_ids,
            allocation=current_allocation_reference,
        )
        if isinstance(strategy_snapshot, dict)
        and current_candidate_pool_ids
        else {}
    )
    current_parameter_rows = (
        current_strategy_snapshot.get("parameter_rows")
        if current_strategy_snapshot
        else None
    )
    real_status = payload["strategy_judgments"].get(
        "real_holding_decisions_status"
    )
    if real_status not in {"available", "unavailable"}:
        real_status = "legacy"
    real_reason = payload["strategy_judgments"].get(
        "real_holding_decisions_reason", ""
    )
    if real_status == "legacy":
        real_reason = "当前报告未包含真实持仓判断"
    return {
        "available": True,
        "artifact": path.name,
        "report_sha256": report_sha256,
        "execution_batch": execution_batch,
        "execution_batch_blocking": bool(execution_batch_error),
        "execution_batch_error": execution_batch_error,
        "latest_report_sha256": latest_report_sha256,
        "revision_anomaly": revision_anomaly,
        "strategy_version": str(
            (payload.get("strategy_snapshot") or {}).get("strategy_version") or ""
        ),
        "current_strategy_version": str(
            current_strategy_snapshot.get("strategy_version") or ""
        ),
        "current_strategy_parameter_rows": current_parameter_rows,
        "data_status": "current" if current else "stale",
        "broker": broker,
        "broker_label": broker_label,
        "market": market,
        "market_label": market_label,
        "report_date": execution_date.isoformat(),
        "data_date": data_date,
        "generated_at": generated_at.isoformat(),
        "status_text": (
            "今日已更新"
            if updated_today
            else f"今日执行（数据截至 {data_date}）"
            if execution_today
            else f"数据截至 {data_date}；今日未更新"
        ),
        "option_attention": payload.get("option_attention", []),
        "real_position_actions": real_position_actions,
        "real_position_status": real_status,
        "real_position_reason": real_reason,
        "real_position_source": payload["strategy_judgments"].get(
            "real_holding_decisions_source", {}
        ),
        "account_source_date": str(account.get("source_date") or ""),
        "account_fresh": account_fresh,
        "account_status": "已更新" if account_fresh else NON_REALTIME_ACCOUNT_WARNING,
        "buy_window": buy_window,
        "run_status": _latest_trend_run_status(
            data_dir / directory / "run.log",
            str(payload.get("delivery_status") or metadata.get("delivery_status") or "generated"),
        ),
        "sell_actions": sell_actions,
        "buy_actions": buy_actions,
        "risk_skips": risk_skips,
        "risk_summary": risk_summary,
        "drawdown_summary": payload.get("drawdown_summary", {}),
        "api_cost": frozen_api_cost,
        "allocation": payload.get("allocation"),
        "simulate_rotation_pairs": _project_simulated_rotation_pairs(
            payload["strategy_judgments"].get("simulate_rotation_pairs", []),
            data_dir=data_dir,
            market=market,
            execution_date=execution_date.isoformat(),
            report_sha256=report_sha256,
            account_id=metadata.get("simulate_acc_id"),
        ) if payload.get("allocation") is not None else [],
        "real_rotation_pairs": (
            payload["strategy_judgments"].get("real_rotation_pairs", [])
            if payload.get("allocation") is not None
            else []
        ),
        "simulate_rotation_comparisons": (
            copy.deepcopy(
                payload["strategy_judgments"].get(
                    "simulate_rotation_comparisons", []
                )
            )
            if payload.get("allocation") is not None
            else []
        ),
        "real_rotation_comparisons": (
            copy.deepcopy(
                payload["strategy_judgments"].get(
                    "real_rotation_comparisons", []
                )
            )
            if payload.get("allocation") is not None
            else []
        ),
        "industry_context_status": frozen_industry_context_status,
        "industry_contexts": frozen_industry_contexts,
        "strategy_parameter_rows": frozen_parameter_rows,
        "actual_overlay": {},
        "hold_actions": hold_actions,
        "review_actions": review_actions,
        "counts": {
            "sell": len(sell_actions),
            "buy": len(buy_actions),
            "hold": len(hold_actions),
            "review": len(review_actions),
        },
        "recent_protection_alert": _recent_trend_protection_alert(
            data_dir / directory / "watch_events.jsonl"
        ),
        "audit": {
            "candidates": audit_candidates,
            "strategy_parameters": strategy_parameters,
            "excluded": payload.get("excluded", {}),
            "account_exceptions": account.get("exceptions", []),
            "industry_concentration": payload.get("industry_concentration", []),
            "data_sources": payload.get("data_sources", []),
            "estimated_api_cost": payload.get("estimated_api_cost"),
            "actual_api_cost": payload.get("actual_api_cost"),
            "artifact": path.name,
        },
    }


def _trend_option_anomalies(
    data_dir: Path,
    *,
    market: str,
    report_date: str,
    historical: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    path = (
        futu_skill_facts_run_path(data_dir, report_date, market)
        if historical
        else futu_skill_facts_latest_path(data_dir, market)
    )
    records = index_futu_skill_facts_by_market_symbol(
        load_futu_skill_facts_cache(path)
    )
    projected: dict[tuple[str, str], dict[str, Any]] = {}
    for (record_market, symbol), record in records.items():
        try:
            normalized_symbol = normalize_backtest_symbol(record_market, symbol)
        except ValueError:
            normalized_symbol = symbol
        run_date = str(record.get("run_date") or "")
        detail = _futu_skill_signal_detail(
            record.get("derivatives_anomaly"),
            run_date,
            {"run_date": report_date},
        )
        if detail["available"]:
            reason = ""
        elif detail["status"] == "stale_run_date":
            reason = "富途期权异动日期与趋势报告不一致"
        elif detail.get("unsupported"):
            reason = "富途不支持该标的期权异动"
        else:
            reason = str(detail.get("error") or "富途未返回该标的期权异动")
        projected[(record_market, normalized_symbol)] = {
            **detail,
            "run_date": run_date,
            "reason": reason,
        }
    return projected


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _project_trend_trade_stats(
    data_dir: Path,
    *,
    market: str,
    strategy_snapshot: object,
) -> dict[str, Any]:
    unavailable = {
        "available": False,
        "status_text": "交易统计暂不可用",
    }
    if not isinstance(strategy_snapshot, dict):
        return unavailable
    strategy_id = str(strategy_snapshot.get("strategy_id") or "").strip()
    version = str(strategy_snapshot.get("strategy_version") or "").strip()
    if not strategy_id or not version:
        return unavailable
    try:
        payload = load_trend_api_stats(data_dir)
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return unavailable
    matching = [
        stat for stat in payload["stats"]
        if stat["market"] == market
        and stat["strategy_id"] == strategy_id
        and stat["opening_strategy_version"] == version
    ]
    by_source = {str(stat["source"]): stat for stat in matching}
    if len(matching) != 2 or set(by_source) != {"simulation", "actual"}:
        return unavailable
    actual_sources = [
        source
        for source in payload["sources"]
        if source["source"] == "actual" and source["market"] == market
    ]
    actual_broker = (
        str(actual_sources[0]["broker"])
        if len(actual_sources) == 1
        else {"CN": "eastmoney", "HK": "phillips", "US": "tiger"}[market]
    )
    def compact(source: str) -> dict[str, Any]:
        stat = by_source[source]
        return {
            "win_rate": stat["win_rate"],
            "payoff_ratio": stat["payoff_ratio"],
            "payoff_ratio_status": stat["payoff_ratio_status"],
            "eligible_sample_count": stat["eligible_sample_count"],
        }

    return {
        "available": True,
        "strategy_id": strategy_id,
        "opening_strategy_version": version,
        "statistics_cutoff_at": (
            actual_sources[0]["statistics_cutoff_at"]
            if len(actual_sources) == 1
            else payload["statistics_cutoff_at"]
        ),
        "actual_broker": actual_broker,
        "actual_broker_label": BROKER_LABELS[actual_broker],
        "simulation": compact("simulation"),
        "actual": compact("actual"),
    }


def _trend_action_executions(
    data_dir: Path, *, market: str, execution_date: str, report_sha256: str
) -> dict[tuple[str, str], dict[str, Any]]:
    root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
    )
    revision_key = (-1, -1)
    try:
        candidates = (root, *root.iterdir())
    except OSError:
        candidates = ()
    for candidate in candidates:
        try:
            revision = candidate.stat()
        except OSError:
            continue
        revision_key = max(
            revision_key, (revision.st_mtime_ns, revision.st_ctime_ns)
        )
    cached = _trend_action_executions_cached(
        str(data_dir.resolve()),
        market,
        execution_date,
        report_sha256,
        *revision_key,
    )
    return copy.deepcopy(cached)


def _project_simulated_rotation_pairs(
    pairs: list[dict[str, Any]],
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    report_sha256: str,
    account_id: object,
) -> list[dict[str, Any]]:
    projected = [{**pair, "execution_status": "待执行"} for pair in pairs]
    if (
        isinstance(account_id, bool)
        or not isinstance(account_id, int)
        or account_id <= 0
    ):
        return projected
    terminal_labels = {
        "complete": "完成",
        "failed": "失败",
        "partial": "部分完成",
        "incomplete": "未完成",
        "skipped": "跳过",
        "missed": "错过执行日",
    }
    for pair, output in zip(pairs, projected):
        pair_index = pair.get("pair_index")
        if isinstance(pair_index, bool) or not isinstance(pair_index, int):
            continue
        pair_key = _rotation_pair_key(
            market, account_id, execution_date, report_sha256, pair_index
        )
        root = (
            data_dir / "trend_review" / "ledgers" / market / "rotations"
            / execution_date / pair_key
        )
        events: list[dict[str, object]] = []
        for path in sorted(root.glob("*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(event, dict):
                    continue
                _validate_rotation_event(event, path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            events.append(event)
        terminal = next(
            (
                terminal_labels.get(str(event.get("status") or ""))
                for event in events
                if event.get("kind") == "terminal"
            ),
            None,
        )
        if terminal:
            output["execution_status"] = terminal
        elif any(event.get("kind") == "buy_fill" for event in events):
            output["execution_status"] = "买入已成交"
        elif any(event.get("kind") in {"buy_intent", "buy_result"} for event in events):
            output["execution_status"] = "买入中"
        elif any(event.get("kind") in {"sell_fill", "sell_observation"} for event in events):
            output["execution_status"] = "卖出已成交"
        elif any(event.get("kind") in {"sell_intent", "sell_result"} for event in events):
            output["execution_status"] = "卖出中"
        elif any(event.get("kind") == "pending" for event in events):
            output["execution_status"] = "执行中"
    return projected


@lru_cache(maxsize=256)
def _trend_action_executions_cached(
    data_dir: str,
    market: str,
    execution_date: str,
    report_sha256: str,
    root_mtime_ns: int,
    root_ctime_ns: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    del root_mtime_ns, root_ctime_ns
    return _trend_action_executions_uncached(
        Path(data_dir),
        market=market,
        execution_date=execution_date,
        report_sha256=report_sha256,
    )


def _trend_action_executions_uncached(
    data_dir: Path, *, market: str, execution_date: str, report_sha256: str
) -> dict[tuple[str, str], dict[str, Any]]:
    executions: dict[tuple[str, str], dict[str, Any]] = {}
    root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
    )
    ordered_events: list[tuple[int, float, str, dict[str, Any]]] = []
    for path in root.glob("*/*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        try:
            recorded_at = datetime.fromisoformat(str(event.get("recorded_at") or ""))
        except ValueError:
            recorded_at = None
        if (
            recorded_at is None
            or recorded_at.tzinfo is None
            or recorded_at.utcoffset() is None
        ):
            ordered_events.append((0, 0.0, str(path), event))
        else:
            ordered_events.append((1, recorded_at.timestamp(), str(path), event))
    for _, _, _, event in sorted(ordered_events):
        if event.get("report_sha256") != report_sha256:
            continue
        symbol = str(event.get("symbol") or "").strip()
        side = str(event.get("side") or "").strip().lower()
        status = str(event.get("status") or "").strip()
        if not symbol or side not in {"buy", "sell"} or not status:
            continue
        executions[(symbol, side)] = {
            "status": status,
            "filled_qty": str(event.get("filled_qty") or ""),
            "target_qty": str(event.get("target_qty") or ""),
            "avg_fill_price": str(event.get("avg_fill_price") or ""),
            "order_ids": event.get("order_ids")
            if isinstance(event.get("order_ids"), list)
            else [],
            "updated_at": str(event.get("recorded_at") or ""),
            "reason": str(event.get("reason") or ""),
        }
        if event.get("sell_goal") == "partial_30":
            execution = executions[(symbol, side)]
            execution["sell_goal"] = "partial_30"
            execution["lifecycle_target_qty"] = str(
                event.get("lifecycle_target_qty") or ""
            )
            try:
                lifecycle_target = Decimal(execution["lifecycle_target_qty"])
                filled = Decimal(execution["filled_qty"])
            except (InvalidOperation, ValueError):
                pass
            else:
                if lifecycle_target.is_finite() and filled.is_finite():
                    execution["remaining_qty"] = _decimal_text(
                        max(Decimal("0"), lifecycle_target - filled)
                    )
    return executions


def _recent_trend_protection_alert(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return "无"
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event_type") != "protection_triggered":
            continue
        symbol = str(event.get("symbol") or "-")
        occurred_at = str(event.get("occurred_at") or "-")
        line_value = str(event.get("active_line") or "-")
        return f"{symbol} · {occurred_at} · 保护线 {line_value}"
    return "无"


def _latest_trend_run_status(path: Path, fallback: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return fallback
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        status = str(event.get("event") or "")
        if status in {"start", "retry", "failed", "generated", "existing", "holiday"}:
            return status
    return fallback


def _build_backtest_universe(
    holding_rows: list[dict[str, str]],
    watchlist_rows: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    holdings: list[dict[str, str]] = []
    watchlist: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def append_valid(target: list[dict[str, str]], row: dict[str, str]) -> None:
        market = str(row.get("market") or "").strip().upper()
        symbol = str(row.get("symbol") or "").strip().upper()
        if market not in {"HK", "US", "CN"}:
            return
        asset_class = str(row.get("asset_class") or "").strip().lower()
        if asset_class in {"", "unknown"}:
            asset_class = detect_asset_class(
                symbol, str(row.get("name") or "")
            ).value
        if asset_class not in {AssetClass.STOCK.value, AssetClass.ETF.value}:
            return
        try:
            normalized_symbol = normalize_backtest_symbol(market, symbol)
        except ValueError:
            return
        key = (market, normalized_symbol)
        if key in seen:
            return
        seen.add(key)
        target.append(
            {
                "market": market,
                "symbol": symbol,
                "futu_symbol": f"{market}.{normalized_symbol}",
            }
        )

    for row in holding_rows:
        append_valid(holdings, row)
    for row in watchlist_rows:
        append_valid(watchlist, row)
    return {"holdings": holdings, "watchlist": watchlist}


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [_json_safe_row(row) for row in csv.DictReader(handle)]


def _latest_rows_for_markets(
    *,
    data_dir: Path,
    filename: str,
    markets: set[str],
) -> tuple[list[dict[str, str]], set[str]]:
    unscoped_rows = _read_csv_rows(data_dir / "latest" / filename)
    rows_by_key = _latest_by_market_symbol(unscoped_rows)
    scoped_markets: set[str] = set()
    for market in markets:
        scoped_path = data_dir / "latest" / market / filename
        if not scoped_path.exists():
            continue
        scoped_markets.add(market)
        rows_by_key = {
            key: row
            for key, row in rows_by_key.items()
            if key[0] != market
        }
        rows_by_key.update(_latest_by_market_symbol(_read_csv_rows(scoped_path)))
    return list(rows_by_key.values()), scoped_markets


def _latest_technical_facts_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
    scoped_advice_markets: set[str],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, bool]]:
    unscoped_path = technical_facts_latest_path(data_dir)
    unscoped_exists = unscoped_path.exists()
    unscoped_records = index_technical_facts_by_market_symbol(
        load_technical_facts_cache(unscoped_path)
    )
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    file_exists_by_market: dict[str, bool] = {}
    for market in markets:
        scoped_path = data_dir / "latest" / market / "technical_facts.json"
        if scoped_path.exists():
            file_exists_by_market[market] = True
            records_by_key.update(
                index_technical_facts_by_market_symbol(
                    load_technical_facts_cache(scoped_path)
                )
            )
            continue
        if market in scoped_advice_markets:
            file_exists_by_market[market] = False
            continue
        file_exists_by_market[market] = unscoped_exists
        records_by_key.update(
            {
                key: record
                for key, record in unscoped_records.items()
                if key[0] == market
            }
        )
    return records_by_key, file_exists_by_market


def _latest_decision_facts_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, bool]]:
    unscoped_path = data_dir / "latest" / "decision_facts.json"
    unscoped_exists = unscoped_path.exists()
    unscoped_records = index_decision_facts_by_market_symbol(
        load_decision_facts_cache(unscoped_path)
    )
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    file_exists_by_market: dict[str, bool] = {}
    for market in markets:
        scoped_path = data_dir / "latest" / market / "decision_facts.json"
        if scoped_path.exists():
            file_exists_by_market[market] = True
            records_by_key.update(
                index_decision_facts_by_market_symbol(
                    load_decision_facts_cache(scoped_path)
                )
            )
            continue
        market_unscoped_records = {
            key: record
            for key, record in unscoped_records.items()
            if key[0] == market
        }
        file_exists_by_market[market] = unscoped_exists and bool(market_unscoped_records)
        records_by_key.update(market_unscoped_records)
    return records_by_key, file_exists_by_market


def _latest_futu_skill_facts_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    unscoped_records = index_futu_skill_facts_by_market_symbol(
        load_futu_skill_facts_cache(data_dir / "latest" / "futu_skill_facts.json")
    )
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for market in markets:
        scoped_path = data_dir / "latest" / market / "futu_skill_facts.json"
        if scoped_path.exists():
            records_by_key.update(
                index_futu_skill_facts_by_market_symbol(
                    load_futu_skill_facts_cache(scoped_path)
                )
            )
            continue
        records_by_key.update(
            {
                key: record
                for key, record in unscoped_records.items()
                if key[0] == market
            }
        )
    return records_by_key


def _latest_tradingagents_summary_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    unscoped_records = index_tradingagents_summary_by_market_symbol(
        load_tradingagents_summary_cache(
            tradingagents_summary_latest_path(data_dir)
        )
    )
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for market in markets:
        path = tradingagents_summary_latest_path(data_dir, market)
        if path.exists():
            records_by_key.update(
                index_tradingagents_summary_by_market_symbol(
                    load_tradingagents_summary_cache(path)
                )
            )
            continue
        records_by_key.update(
            {
                key: record
                for key, record in unscoped_records.items()
                if key[0] == market
            }
        )
    return records_by_key


def _latest_t_signals_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for market in markets:
        path = t_signals_latest_path(data_dir, market)
        if path.exists():
            records_by_key.update(
                index_t_signals_by_market_symbol(load_t_signals_cache(path))
            )
    return records_by_key


def _load_dashboard_kelly_lab(
    data_dir: Path,
) -> tuple[dict[str, Any], dict[tuple[str, str], list[dict[str, Any]]]]:
    try:
        kelly_lab_state = load_kelly_lab_state(data_dir)
    except ValueError as exc:
        return _unavailable_kelly_lab(str(exc)), {}

    return (
        kelly_lab_state.to_dict(),
        index_kelly_experiments_by_market_symbol(kelly_lab_state.experiments),
    )


def _unavailable_kelly_lab(error: str) -> dict[str, Any]:
    return {
        "available": False,
        "template_count": 0,
        "experiment_count": 0,
        "templates": [],
        "experiments": [],
        "error": f"Kelly Lab unavailable: {error}",
    }


def _latest_backtests_by_holding(
    *,
    data_dir: Path,
    reports_dir: Path,
    markets: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    backtests_dir = data_dir / "backtests"
    if not backtests_dir.exists():
        return {}

    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for metrics_path in backtests_dir.glob("*/metrics.json"):
        detail = _backtest_detail(metrics_path, reports_dir)
        if not detail:
            continue
        key = (detail["market"], detail["symbol"])
        if key[0] not in markets:
            continue
        current = records_by_key.get(key)
        if current is None or _backtest_sort_key(detail) > _backtest_sort_key(current):
            records_by_key[key] = detail
    return records_by_key


def _backtest_detail(metrics_path: Path, reports_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    market = str(payload.get("market", "")).strip().upper()
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not market or not symbol:
        return None
    run_id = str(payload.get("run_id", "") or metrics_path.parent.name).strip()
    report_path = reports_dir / "backtests" / f"{run_id}.md"
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else payload
    metric_keys = (
        "total_return_pct",
        "win_rate_pct",
        "max_drawdown_pct",
        "trade_count",
        "round_trips",
        "initial_cash",
        "final_equity",
    )
    return {
        "available": True,
        "run_id": run_id,
        "run_date": str(payload.get("run_date", "")).strip(),
        "market": market,
        "symbol": symbol,
        "strategy": str(payload.get("strategy", "trading_plan")).strip() or "trading_plan",
        "adapter": str(payload.get("adapter", "legacy")).strip() or "legacy",
        "metrics": {
            key: str(metrics.get(key, ""))
            for key in metric_keys
            if isinstance(metrics, dict) and metrics.get(key, "") != ""
        },
        "metrics_path": str(metrics_path),
        "trades_path": str(metrics_path.parent / "trades.csv"),
        "equity_curve_path": str(metrics_path.parent / "equity_curve.csv"),
        "trades": _backtest_csv_rows(metrics_path.parent / "trades.csv"),
        "equity_curve": _backtest_csv_rows(metrics_path.parent / "equity_curve.csv"),
        "report_path": str(report_path),
        "status": "ok",
        "error": "",
    }


def _backtest_csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [
                {str(key): str(value or "") for key, value in row.items()}
                for row in csv.DictReader(handle)
                if row
            ]
    except OSError:
        return []


def _backtest_sort_key(detail: dict[str, Any]) -> tuple[str, str]:
    return (str(detail.get("run_date", "")), str(detail.get("run_id", "")))


def _backtest_readiness_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    readiness: dict[tuple[str, str], dict[str, Any]] = {}
    for market in markets:
        plan_path = _backtest_plan_path(data_dir, market)
        if not plan_path.exists():
            continue
        try:
            plans = load_trading_plan_rows(plan_path)
        except (OSError, ValueError):
            continue
        for plan in plans:
            if plan.status != "active" or plan.market.upper() != market:
                continue
            symbol = plan.symbol.upper()
            detail = _backtest_readiness_detail(
                data_dir=data_dir,
                plan_path=plan_path,
                market=market,
                symbol=symbol,
                run_date=plan.run_date,
                rating=plan.rating,
                fields={
                    "entry_zone_low": plan.entry_zone_low,
                    "entry_zone_high": plan.entry_zone_high,
                    "max_weight": plan.max_weight,
                    "stop_loss": plan.stop_loss,
                    "target_1": plan.target_1,
                },
            )
            key = (market, symbol)
            current = readiness.get(key)
            if current is None or _backtest_sort_key(detail) > _backtest_sort_key(current):
                readiness[key] = detail
    return readiness


def _backtest_plan_path(data_dir: Path, market: str) -> Path:
    scoped_path = data_dir / "latest" / market / "trading_plan.csv"
    if scoped_path.exists():
        return scoped_path
    return data_dir / "latest" / "trading_plan.csv"


def _backtest_readiness_detail(
    *,
    data_dir: Path,
    plan_path: Path,
    market: str,
    symbol: str,
    run_date: str,
    rating: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    prices_path = data_dir / "prices" / market / f"{symbol}.csv"
    prices_missing = not prices_path.exists()
    side = backtest_plan_side(rating)
    if side is None:
        return {
            "available": False,
            "status": "unsupported_strategy",
            "run_date": run_date,
            "plan_path": str(plan_path),
            "prices_path": str(prices_path),
            "prices_missing": prices_missing,
            "missing_fields": [],
            "error": "unsupported backtest strategy rating",
        }
    required_fields = (
        ("entry_zone_high", "max_weight")
        if side == "buy"
        else ("stop_loss_or_target_1",)
    )
    missing_fields = [
        field
        for field in required_fields
        if (
            field == "stop_loss_or_target_1"
            and fields.get("stop_loss") is None
            and fields.get("target_1") is None
        )
        or (
            field != "stop_loss_or_target_1"
            and (fields.get(field) is None or str(fields.get(field)).strip() == "")
        )
    ]
    if missing_fields:
        status = "missing_fields"
        error = f"missing backtest field(s): {', '.join(missing_fields)}"
    elif prices_missing:
        status = "missing_prices"
        error = f"missing price CSV: {prices_path}"
    else:
        status = "ready"
        error = ""
    return {
        "available": status == "ready",
        "status": status,
        "run_date": run_date,
        "plan_path": str(plan_path),
        "prices_path": str(prices_path),
        "prices_missing": prices_missing,
        "missing_fields": missing_fields,
        "error": error,
    }


def _json_safe_row(row: dict[str | None, str | None]) -> dict[str, str]:
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if key is not None
    }


def _latest_by_market_symbol(
    rows: list[dict[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    keyed: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = _market_symbol_key(row)
        if key is not None:
            keyed[key] = row
    return keyed


def _module_holding_rows(*artifact_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for artifact in artifact_rows:
        for row in artifact:
            key = _market_symbol_key(row)
            if key is None or key[0] not in {"CN", "HK", "US"}:
                continue
            merged = dict(rows.get(key, {}))
            merged.update({key: value for key, value in row.items() if value})
            asset_class = str(merged.get("asset_class") or "").strip().lower()
            if asset_class in {"", "unknown"}:
                asset_class = detect_asset_class(
                    key[1], str(merged.get("name") or "")
                ).value
            if asset_class not in {AssetClass.STOCK.value, AssetClass.ETF.value}:
                continue
            merged["market"], merged["symbol"], merged["asset_class"] = (*key, asset_class)
            rows[key] = merged
    return [rows[key] for key in sorted(rows)]


def _market_symbol_key(row: dict[str, str]) -> tuple[str, str] | None:
    market = row.get("market", "").strip().upper()
    symbol = row.get("symbol", "").strip().upper()
    if not market or not symbol:
        return None
    return (market, symbol)






def _latest_decision_plans_for_markets(
    data_dir: Path,
    markets: set[str],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, str],
]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for market in markets:
        path = data_dir / "latest" / market / "decision_plans.json"
        if not path.exists():
            errors[market] = "decision_plans.json 不存在"
            continue
        try:
            plans = _load_decision_plans_file(path)
        except ValueError:
            errors[market] = "decision_plans.json 无效"
            continue
        for plan in plans:
            key = (str(plan["market"]), str(plan["symbol"]))
            try:
                indexed[key] = _project_decision_plan(data_dir, plan)
            except ValueError:
                errors[market] = "plan_events.jsonl 无效"
    return indexed, errors


def _load_decision_plans_file(path: Path) -> tuple[dict[str, Any], ...]:
    stat = path.stat()
    return _load_decision_plans_cached(
        str(path.resolve()), stat.st_mtime_ns, stat.st_size
    )


@lru_cache(maxsize=8)
def _load_decision_plans_cached(
    path: str, mtime_ns: int, size: int,
) -> tuple[dict[str, Any], ...]:
    del mtime_ns, size
    return tuple(load_decision_plans(Path(path)))


def _project_decision_plan(
    data_dir: Path,
    plan: dict[str, object],
) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(value)
        for key, value in plan.items()
        if key != "backtests"
    }
    projected["backtests"] = [
        _project_decision_plan_backtest(item)
        for item in plan.get("backtests", [])
        if isinstance(item, Mapping)
    ]
    run_date = str(plan["run_date"])
    market = str(plan["market"])
    plan_id = str(plan["plan_id"])
    events = load_plan_events(
        data_dir / "runs" / run_date / market / "plan_events.jsonl"
    )
    trigger_counts: dict[str, int] = {}
    for event in events:
        if event.plan_id == plan_id and event.event_type == "condition_triggered":
            trigger_counts[event.condition_id] = trigger_counts.get(event.condition_id, 0) + 1
    projected["available"] = True
    projected["error"] = ""
    projected["status"] = replay_plan_status(events, plan_id)
    projected["conditions"] = [
        {**condition, "trigger_count": trigger_counts.get(str(condition["condition_id"]), 0)}
        for condition in projected["conditions"]
    ]
    projected["trigger_count"] = sum(trigger_counts.values())
    projected["previous_review"] = _previous_decision_plan_review(data_dir, plan)
    return projected


def _project_decision_plan_backtest(item: Mapping[str, object]) -> dict[str, object]:
    strategy = item.get("strategy")
    benchmark = item.get("market_benchmark")
    gate = item.get("gate")
    strategy = strategy if isinstance(strategy, Mapping) else {}
    benchmark = benchmark if isinstance(benchmark, Mapping) else {}
    gate = gate if isinstance(gate, Mapping) else {}
    return {
        "strategy_id": item.get("strategy_id"),
        "range": item.get("range"),
        "gate": {"passed": gate.get("passed")},
        "strategy": {
            key: strategy.get(key)
            for key in (
                "total_return_pct",
                "max_drawdown_pct",
                "sharpe_ratio",
                "calmar_ratio",
            )
        },
        "market_benchmark": {
            key: benchmark.get(key) for key in ("symbol", "total_return_pct")
        },
        "market_excess_return_pct": item.get("market_excess_return_pct"),
    }


def _previous_decision_plan_review(
    data_dir: Path,
    current: dict[str, object],
) -> dict[str, Any] | None:
    current_date = str(current["run_date"])
    market = str(current["market"])
    symbol = str(current["symbol"])
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return None
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        if not run_dir.is_dir() or run_dir.name >= current_date:
            continue
        path = run_dir / market / "decision_plans.json"
        if not path.exists():
            continue
        try:
            previous = next(
                (
                    item
                    for item in _load_decision_plans_file(path)
                    if item["market"] == market and item["symbol"] == symbol
                ),
                None,
            )
        except ValueError:
            continue
        if previous is None:
            continue
        events = load_plan_events(
            data_dir / "runs" / str(previous["run_date"]) / market / "plan_events.jsonl"
        )
        plan_id = str(previous["plan_id"])
        triggered: dict[str, int] = {}
        for event in events:
            if event.plan_id == plan_id and event.event_type == "condition_triggered":
                triggered[event.condition_id] = triggered.get(event.condition_id, 0) + 1
        return {
            "run_date": previous["run_date"],
            "plan_id": plan_id,
            "mode": previous["mode"],
            "status": replay_plan_status(events, plan_id),
            "action_summary": previous["action_summary"],
            "starting_quantity": previous["current_quantity"],
            "closing_quantity": current["current_quantity"],
            "trigger_count": sum(triggered.values()),
            "triggered_conditions": [
                {"condition_id": condition_id, "trigger_count": count}
                for condition_id, count in triggered.items()
            ],
        }
    return None


def _decision_plan_detail(
    plan: dict[str, Any] | None,
    error: str,
) -> dict[str, Any]:
    if plan is not None:
        return plan
    return {
        "available": False,
        "error": error or "当前标的没有交易计划",
    }


def _merge_holding(
    row: dict[str, str],
    data_dir: Path,
    agent_reports_by_holding: dict[tuple[str, str], dict[str, str]],
    strategies_by_holding: dict[tuple[str, str], dict[str, str]],
    premarket_actions_by_holding: dict[tuple[str, str], dict[str, str]],
    actions_by_holding: dict[tuple[str, str], dict[str, str]],
    technical_facts_by_holding: dict[tuple[str, str], dict[str, Any]],
    technical_facts_file_exists_by_market: dict[str, bool],
    decision_facts_by_holding: dict[tuple[str, str], dict[str, Any]],
    decision_facts_file_exists_by_market: dict[str, bool],
    futu_skill_facts_by_holding: dict[tuple[str, str], dict[str, Any]],
    tradingagents_summary_by_holding: dict[tuple[str, str], dict[str, Any]],
    t_signals_by_holding: dict[tuple[str, str], dict[str, Any]],
    kelly_experiments_by_holding: dict[tuple[str, str], list[dict[str, Any]]],
    decision_plans_by_holding: dict[tuple[str, str], dict[str, Any]],
    decision_plan_errors_by_market: dict[str, str],
) -> dict[str, Any]:
    holding: dict[str, Any] = dict(row)
    holding["instrument_id"] = build_instrument_id(
        row.get("market", ""), row.get("asset_class", ""), row.get("symbol", "")
    )
    key = _market_symbol_key(row)
    agent_report = agent_reports_by_holding.get(key) if key is not None else None
    strategy = strategies_by_holding.get(key) if key is not None else None
    premarket_action = premarket_actions_by_holding.get(key) if key is not None else None
    trade_action = actions_by_holding.get(key) if key is not None else None
    holding["agent_report"] = _agent_report_detail(agent_report)
    holding["tradingagents_summary"] = _tradingagents_summary_detail(
        tradingagents_summary_by_holding.get(key) if key is not None else None,
        agent_report,
        trade_action or premarket_action,
    )
    holding["strategy"] = _strategy_detail(strategy)
    holding["premarket_action"] = _row_detail(premarket_action)
    holding["trade_action"] = _row_detail(trade_action)
    holding["technical_facts"] = _technical_facts_detail(
        technical_facts_by_holding.get(key) if key is not None else None,
        agent_report,
        cache_file_exists=(
            technical_facts_file_exists_by_market.get(key[0], False)
            if key is not None
            else False
        ),
    )
    holding["decision_facts"] = _decision_facts_detail(
        decision_facts_by_holding.get(key) if key is not None else None,
        agent_report,
        cache_file_exists=(
            decision_facts_file_exists_by_market.get(key[0], False)
            if key is not None
            else False
        ),
    )
    holding["futu_skill_facts"] = _futu_skill_facts_detail(
        futu_skill_facts_by_holding.get(key) if key is not None else None,
        agent_report,
    )
    holding["t_signal"] = _t_signal_detail(
        t_signals_by_holding.get(key) if key is not None else None,
    )
    holding["kelly"] = _kelly_detail(
        kelly_experiments_by_holding.get(key, []) if key is not None else [],
    )
    holding["decision_plan"] = _decision_plan_detail(
        decision_plans_by_holding.get(key) if key is not None else None,
        decision_plan_errors_by_market.get(key[0], "") if key is not None else "",
    )
    holding["research_view"] = (
        load_research_view_for_holding(
            data_dir=data_dir,
            market=key[0],
            symbol=key[1],
        )
        if key is not None
        else load_research_view_for_holding(
            data_dir=data_dir,
            market=row.get("market", ""),
            symbol=row.get("symbol", ""),
        )
    )
    return holding


def _backtest_holding_detail(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return _unavailable_detail()
    return record


def _backtest_readiness_holding_detail(
    record: dict[str, Any] | None,
    *,
    data_dir: Path,
    key: tuple[str, str] | None,
) -> dict[str, Any]:
    if record is not None:
        return record
    market = key[0] if key is not None else ""
    symbol = key[1] if key is not None else ""
    return {
        "available": False,
        "status": "missing_plan",
        "run_date": "",
        "plan_path": str(_backtest_plan_path(data_dir, market)) if market else "",
        "prices_path": str(data_dir / "prices" / market / f"{symbol}.csv") if market and symbol else "",
        "prices_missing": (
            not (data_dir / "prices" / market / f"{symbol}.csv").exists()
            if market and symbol
            else False
        ),
        "missing_fields": [],
        "error": "no active trading plan found",
    }


def _unavailable_detail() -> dict[str, Any]:
    return {"available": False, "error": ""}


def _row_detail(row: dict[str, str] | None) -> dict[str, Any]:
    if row is None:
        return _unavailable_detail()
    return {"available": True, **row}


def _t_signal_detail(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return _unavailable_detail()
    return {"available": True, **record}


def _kelly_detail(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    if experiments:
        return {
            "available": True,
            "experiment_count": len(experiments),
            "experiments": experiments,
            "status": "available",
            "message": "该标的已关联 Kelly 策略实验。",
        }
    return {
        "available": False,
        "experiment_count": 0,
        "experiments": [],
        "status": "missing_experiment",
        "message": "该标的未参与任何已锁定的 Kelly 策略实验。",
    }


def _agent_report_detail(row: dict[str, str] | None) -> dict[str, Any]:
    if row is None:
        return _unavailable_detail()
    return {
        "available": True,
        "run_date": row.get("run_date", ""),
        "market": row.get("market", ""),
        "symbol": row.get("symbol", ""),
        "rating": row.get("advice_action", ""),
        "summary": row.get("advice_summary", ""),
        "summary_zh": row.get("advice_summary_zh", ""),
        "raw_decision": row.get("raw_decision", ""),
        "source_status": row.get("source_status", ""),
        "fallback_reason": row.get("fallback_reason", ""),
        "fallback_from_date": row.get("fallback_from_date", ""),
        "status": row.get("status", ""),
        "error": row.get("error", ""),
    }


def _tradingagents_summary_detail(
    record: dict[str, Any] | None,
    agent_report: dict[str, str] | None,
    action: dict[str, str] | None,
) -> dict[str, Any]:
    if _is_current_tradingagents_summary(record, agent_report):
        return {
            "available": True,
            "status": "available",
            "error": "",
            "ta_view": _display_or_missing(record.get("ta_view")),
            "current_action": _display_or_missing(record.get("current_action")),
            "core_reason": _display_or_missing(record.get("core_reason")),
            "ta_report_date": _display_or_missing(record.get("ta_report_date")),
            "latest_run_date": _display_or_missing(record.get("latest_run_date")),
        }

    return {
        "available": False,
        "status": "missing_current_summary",
        "error": "TradingAgents summary is unavailable for current advice",
        "ta_view": _fallback_ta_view(agent_report),
        "current_action": _fallback_current_action(action),
        "core_reason": "缺失",
        "ta_report_date": _fallback_ta_report_date(agent_report),
        "latest_run_date": _fallback_latest_run_date(agent_report, action),
    }


def _is_current_tradingagents_summary(
    record: dict[str, Any] | None,
    agent_report: dict[str, str] | None,
) -> bool:
    return bool(
        agent_report
        and tradingagents_available(record, agent_report.get("run_date", "").strip())
    )


def _display_or_missing(value: object) -> str:
    text = str(value or "").strip()
    return text or "缺失"


def _fallback_ta_view(agent_report: dict[str, str] | None) -> str:
    if agent_report is None:
        return "缺失"
    return normalize_ta_view(agent_report.get("advice_action", ""))


def _fallback_current_action(action: dict[str, str] | None) -> str:
    if action is None:
        return "缺失"
    return normalize_current_action(
        action.get("action", "") or action.get("suggested_action", "")
    )


def _fallback_ta_report_date(agent_report: dict[str, str] | None) -> str:
    if agent_report is None:
        return "缺失"
    return (
        agent_report.get("fallback_from_date", "").strip()
        or agent_report.get("run_date", "").strip()
        or "缺失"
    )


def _fallback_latest_run_date(
    agent_report: dict[str, str] | None,
    action: dict[str, str] | None,
) -> str:
    for row in (action, agent_report):
        if row is None:
            continue
        run_date = row.get("run_date", "").strip()
        if run_date:
            return run_date
    return "缺失"


def _strategy_detail(row: dict[str, str] | None) -> dict[str, Any]:
    return _row_detail(row)


def _technical_facts_detail(
    record: dict[str, Any] | None,
    advice_row: dict[str, str] | None,
    *,
    cache_file_exists: bool,
) -> dict[str, Any]:
    if not cache_file_exists:
        return _technical_facts_unavailable(
            "missing_file",
            error="technical_facts.json not found",
            current_source_hash=_current_advice_source_hash(advice_row),
        )
    if record is None:
        return _technical_facts_unavailable(
            "missing_record",
            error="technical facts record not found",
            current_source_hash=_current_advice_source_hash(advice_row),
        )

    facts = record.get("facts")
    facts_payload: dict[str, object] = facts if isinstance(facts, dict) else {}
    freshness = record.get("freshness")
    freshness_payload: dict[str, Any] = freshness if isinstance(freshness, dict) else {}
    run_date = str(record.get("run_date") or "")
    data_date = str(facts_payload.get("market_data_as_of") or "")
    record_source_hash = str(
        record.get("source_hash") or record.get("source_advice_hash") or ""
    ).strip()
    current_source_hash = _current_advice_source_hash(advice_row)
    source_type = str(record.get("source_type") or "").strip()
    requires_advice_hash = source_type not in {"futu_kline"}

    common = {
        "run_date": run_date,
        "data_date": data_date,
        "source_hash": record_source_hash,
        "current_source_hash": current_source_hash,
        "freshness": freshness_payload,
    }
    advice_run_date = str((advice_row or {}).get("run_date") or "")
    if run_date != advice_run_date:
        return _technical_facts_unavailable(
            "stale_run_date",
            error="technical facts run date does not match latest advice",
            **common,
        )
    if requires_advice_hash and not current_source_hash:
        return _technical_facts_unavailable(
            "missing_source_hash",
            error="latest advice market report source hash unavailable",
            **common,
        )
    if requires_advice_hash and record_source_hash != current_source_hash:
        return _technical_facts_unavailable(
            "stale_source_hash",
            error="technical facts source hash does not match latest advice",
            **common,
        )

    extraction_status = str(record.get("extraction_status") or "").strip()
    if extraction_status != "ok":
        status = (
            "missing_source"
            if extraction_status == "missing_source"
            else "extraction_error"
        )
        return _technical_facts_unavailable(
            status,
            error=str(record.get("error") or extraction_status or "extraction failed"),
            **common,
        )
    if not technical_facts_available(record, advice_row):
        return _technical_facts_unavailable(
            "missing_timeframe",
            error=(
                "technical facts timeframe missing"
                if freshness_payload.get("status") == "missing_timeframe"
                or technical_facts_has_missing_timeframe(facts_payload)
                else "technical facts unavailable"
            ),
            **common,
        )

    return {
        "available": True,
        "status": "usable",
        "error": "",
        **common,
        "source_type": source_type,
        "facts": facts_payload,
    }


def _technical_facts_unavailable(
    status: str,
    *,
    run_date: str = "",
    data_date: str = "",
    source_hash: str = "",
    current_source_hash: str = "",
    error: str = "",
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "available": False,
        "status": status,
        "run_date": run_date,
        "data_date": data_date,
        "source_hash": source_hash,
        "current_source_hash": current_source_hash,
        "error": error,
        "freshness": freshness or {},
        "facts": {},
    }


def _decision_facts_detail(
    record: dict[str, Any] | None,
    advice_row: dict[str, str] | None,
    *,
    cache_file_exists: bool,
) -> dict[str, Any]:
    decision_sources = extract_decision_sources(
        advice_row.get("raw_decision", "") if advice_row is not None else ""
    )
    return {
        "kline": _decision_module_detail(
            record.get("kline") if record is not None else None,
            fields=KLINE_FIELDS,
            current_source_hash=decision_sources.kline_hash,
            cache_file_exists=cache_file_exists,
        ),
        "news_sentiment": _decision_module_detail(
            record.get("news_sentiment") if record is not None else None,
            fields=NEWS_SENTIMENT_FIELDS,
            current_source_hash=decision_sources.news_sentiment_hash,
            cache_file_exists=cache_file_exists,
        ),
    }


def _decision_module_detail(
    module: object,
    *,
    fields: tuple[str, ...],
    current_source_hash: str,
    cache_file_exists: bool,
) -> dict[str, Any]:
    if not cache_file_exists or not isinstance(module, dict):
        return _decision_module_missing(
            fields,
            current_source_hash=current_source_hash,
        )

    source_hash_value = str(module.get("source_hash") or "").strip()
    raw_fields = module.get("fields")
    if not decision_module_available(
        module,
        fields=fields,
        current_source_hash=current_source_hash,
    ):
        return _decision_module_missing(
            fields,
            source_hash_value=source_hash_value,
            current_source_hash=current_source_hash,
        )

    return {
        "available": True,
        "status": "usable",
        "source_hash": source_hash_value,
        "current_source_hash": current_source_hash,
        "fields": {field: str(raw_fields[field]) for field in fields},
    }


def _futu_skill_facts_detail(
    record: dict[str, Any] | None,
    advice_row: dict[str, str] | None,
) -> dict[str, Any]:
    run_date = str((record or {}).get("run_date") or "")
    return {
        "news_sentiment": _futu_skill_news_sentiment_detail(
            record.get("news_sentiment") if isinstance(record, dict) else None,
            run_date,
            advice_row,
        ),
        "technical_anomaly": _futu_skill_signal_detail(
            record.get("technical_anomaly") if isinstance(record, dict) else None,
            run_date,
            advice_row,
        ),
        "capital_anomaly": _futu_skill_signal_detail(
            record.get("capital_anomaly") if isinstance(record, dict) else None,
            run_date,
            advice_row,
        ),
        "derivatives_anomaly": _futu_skill_signal_detail(
            record.get("derivatives_anomaly") if isinstance(record, dict) else None,
            run_date,
            advice_row,
        ),
    }


def _futu_skill_signal_detail(
    module: object,
    run_date: str,
    advice_row: dict[str, str] | None,
) -> dict[str, Any]:
    if not isinstance(module, dict):
        return _missing_futu_skill_signal()
    status = str(module.get("status") or "").strip()
    signal = str(module.get("signal") or "").strip()
    confidence = str(module.get("confidence") or "").strip()
    advice_run_date = str((advice_row or {}).get("run_date") or "")
    available = futu_module_available(
        module,
        run_date,
        advice_run_date,
    )
    unsupported = futu_module_unsupported(module)
    stale_run_date = (
        futu_module_available(module) and not available
        or unsupported and run_date != advice_run_date
    )
    return {
        "available": available,
        "unsupported": unsupported and not stale_run_date,
        "status": (
            "stale_run_date"
            if stale_run_date
            else "not_applicable" if unsupported else status or "missing"
        ),
        "error": "Futu facts run date does not match latest advice" if stale_run_date else "",
        "signal": signal,
        "confidence": confidence,
        "suggested_constraint": str(module.get("suggested_constraint") or ""),
        "window_days": _safe_int(module.get("window_days")),
        "summary": str(module.get("summary") or ""),
        "categories": _futu_skill_signal_categories(module.get("categories")),
    }


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0


def _futu_skill_signal_categories(categories: object) -> list[dict[str, str]]:
    if not isinstance(categories, list):
        return []
    normalized: list[dict[str, str]] = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        normalized.append(
            {
                "name": str(category.get("name") or ""),
                "state": str(category.get("state") or ""),
                "direction": str(category.get("direction") or ""),
                "detail": str(category.get("detail") or ""),
                "evidence_date": str(category.get("evidence_date") or ""),
            }
        )
    return normalized


def _futu_skill_news_sentiment_detail(
    module: object,
    run_date: str,
    advice_row: dict[str, str] | None,
) -> dict[str, Any]:
    if not isinstance(module, dict):
        return _missing_futu_skill_news_sentiment()
    status = str(module.get("status") or "").strip()
    signal = str(module.get("signal") or "").strip()
    confidence = str(module.get("confidence") or "").strip()
    if not status or status in {"missing", "error"}:
        return {
            **_missing_futu_skill_news_sentiment(),
            "status": status or "missing",
            "signal": signal,
            "confidence": confidence,
        }
    evidence = module.get("evidence")
    available = futu_module_available(
        module,
        run_date,
        str((advice_row or {}).get("run_date") or ""),
    )
    stale_run_date = futu_module_available(module) and not available
    return {
        "available": available,
        "status": "stale_run_date" if stale_run_date else status,
        "error": "Futu facts run date does not match latest advice" if stale_run_date else "",
        "signal": signal,
        "confidence": confidence,
        "freshness": module.get("freshness") if isinstance(module.get("freshness"), dict) else {},
        "evidence": evidence if isinstance(evidence, list) else [],
        "domestic_discussion": (
            module.get("domestic_discussion")
            if isinstance(module.get("domestic_discussion"), dict)
            else _missing_futu_domestic_discussion()
        ),
        "blocking_reason": str(module.get("blocking_reason") or ""),
        "suggested_constraint": str(module.get("suggested_constraint") or ""),
    }


def _missing_futu_skill_news_sentiment() -> dict[str, Any]:
    return {
        "available": False,
        "status": "missing",
        "signal": "",
        "confidence": "",
        "freshness": {},
        "evidence": [],
        "domestic_discussion": _missing_futu_domestic_discussion(),
        "blocking_reason": "",
        "suggested_constraint": "",
    }


def _missing_futu_skill_signal() -> dict[str, Any]:
    return {
        "available": False,
        "status": "missing",
        "unsupported": False,
        "signal": "",
        "confidence": "",
        "suggested_constraint": "",
        "window_days": 0,
        "summary": "",
        "categories": [],
    }


def _missing_futu_domestic_discussion() -> dict[str, Any]:
    return {
        "status": "missing",
        "keyword_counts": [],
        "summary": "富途社区未找到足够相关讨论。",
        "focus": "缺失",
        "divergence_risk": "缺失",
        "credibility": "缺失",
        "trading_constraint": "富途社区未找到足够相关讨论，不作为交易依据。",
        "post_count": 0,
        "relevant_post_count": 0,
    }


def _decision_module_missing(
    fields: tuple[str, ...],
    *,
    source_hash_value: str = "",
    current_source_hash: str = "",
) -> dict[str, Any]:
    return {
        "available": False,
        "status": "missing",
        "source_hash": source_hash_value,
        "current_source_hash": current_source_hash,
        "fields": build_missing_fields(fields),
    }


def _current_advice_source_hash(row: dict[str, str] | None) -> str:
    if row is None:
        return ""
    market_report = extract_market_report(row.get("raw_decision", ""))
    if not market_report:
        return ""
    return source_hash(market_report)


def _is_dashboard_holding(row: Mapping[str, str]) -> bool:
    """Keep cash rows out of module holding counts without Account projection."""
    market = row.get("market", "").strip().upper()
    asset_class = row.get("asset_class", "").strip().lower()
    if market == "CASH" or asset_class in {"cash", "money_market_fund"}:
        return False
    try:
        quantity = Decimal(row.get("total_quantity", "").replace(",", ""))
    except (InvalidOperation, ValueError):
        return True
    return not quantity.is_finite() or quantity != Decimal("0")
