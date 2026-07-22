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
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .a_share_trend import (
    ACTION_LABELS,
    NON_REALTIME_ACCOUNT_WARNING,
    PORTFOLIO_RISK_LIMIT,
    REASON_LABELS,
    SINGLE_ENTRY_RISK_LIMIT,
    valid_serialized_account,
    valid_v2_risk_contract,
    valid_v3_risk_contract,
    valid_v4_risk_contract,
)
from .backtest_prices import normalize_backtest_symbol

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
    index_futu_skill_facts_by_market_symbol,
    load_futu_skill_facts_cache,
)
from .kelly_lab import (
    index_kelly_experiments_by_market_symbol,
    load_kelly_lab_state,
)
from .market_scope import MarketScope
from .models import AssetClass
from .parsers.base import detect_asset_class
from .portfolio import PortfolioBuildError, recalculate_portfolio_weights
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
from .trend_review import _report_hash, _validate_execution_batch
from .trend_market_controller import _valid_status
from .strategy_drawdown import valid_drawdown_decision
from .trend_api_stats import load_trend_api_stats
from .tradingagents_summary import (
    index_tradingagents_summary_by_market_symbol,
    load_tradingagents_summary_cache,
    normalize_current_action,
    normalize_ta_view,
    tradingagents_summary_latest_path,
)
from .trading_plan import backtest_plan_side, load_trading_plan_rows


DETAIL_DIR_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-([0-2]\d|3[01]))?$")
BROKERS = ("futu", "tiger", "phillips", "eastmoney")
BROKER_LABELS = {
    "futu": "富途",
    "tiger": "老虎",
    "phillips": "辉立",
    "eastmoney": "东方财富",
}
BROKER_SOURCE_KINDS = {
    "futu": "live_account",
    "tiger": "live_account",
    "phillips": "statement",
    "eastmoney": "statement",
}
DETAIL_FX_TO_HKD = {
    "HKD": Decimal("1"),
    "USD": Decimal("7.8"),
    "CNY": Decimal("1.08"),
}
TREND_MARKET_CURRENCIES = {"CN": "CNY", "HK": "HKD", "US": "USD"}
TREND_REPORT_SOURCES = {
    "tiger": ("US", "美股", "老虎", "trend_us_tiger", "美股常规交易时段"),
    "phillips": ("HK", "港股", "辉立", "trend_hk_phillips", "09:30–10:00"),
    "eastmoney": ("CN", "A股", "东方财富", "trend_a_share", "09:30–10:00"),
}
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
TREND_REVIEW_SERIES = {"discipline", "actual", "benchmark"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
TREND_MARKET_TIMEZONES = {
    "CN": SHANGHAI,
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}


@dataclass(frozen=True)
class DashboardConfig:
    portfolio_path: Path
    data_dir: Path
    reports_dir: Path
    poll_seconds: float
    futu_host: str
    futu_port: int
    trend_review_cn_simulate_acc_id: int = 0
    trend_review_us_simulate_acc_id: int = 0
    trend_review_hk_simulate_acc_id: int = 0
    trend_executor_host: str = ""


@dataclass(frozen=True)
class DashboardState:
    config: DashboardConfig
    broker_detail_month: str
    detail_available: bool
    summary: dict[str, Any]
    holdings: list[dict[str, Any]]
    broker_summaries: list[dict[str, Any]]
    source_statuses: list[dict[str, str]]
    cash_rows: list[dict[str, str]]
    broker_positions: list[dict[str, str]]
    cash_details: list[dict[str, str]]
    trade_actions: list[dict[str, str]]
    kelly_lab: dict[str, Any]
    backtest_universe: dict[str, list[dict[str, str]]]
    trend_reports: dict[str, dict[str, Any]]
    trend_reviews: dict[str, dict[str, Any]]
    trend_controllers: dict[str, dict[str, object]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_path": str(self.config.portfolio_path),
            "data_dir": str(self.config.data_dir),
            "reports_dir": str(self.config.reports_dir),
            "poll_seconds": self.config.poll_seconds,
            "futu_host": self.config.futu_host,
            "futu_port": self.config.futu_port,
            "broker_detail_month": self.broker_detail_month,
            "detail_available": self.detail_available,
            "summary": self.summary,
            "holdings": self.holdings,
            "broker_summaries": self.broker_summaries,
            "source_statuses": self.source_statuses,
            "cash_rows": self.cash_rows,
            "broker_positions": self.broker_positions,
            "cash_details": self.cash_details,
            "trade_actions": self.trade_actions,
            "kelly_lab": self.kelly_lab,
            "backtest_universe": self.backtest_universe,
            "trend_reports": self.trend_reports,
            "trend_reviews": self.trend_reviews,
            "trend_controllers": self.trend_controllers,
        }


def load_dashboard_state(config: DashboardConfig) -> DashboardState:
    portfolio_rows = _read_csv_rows(config.portfolio_path)
    detail_month = latest_broker_detail_month(config.data_dir)
    broker_positions, raw_cash_details = _latest_broker_details(config.data_dir)
    cash_details = [_cash_detail_row(row) for row in raw_cash_details]
    holding_rows = [row for row in portfolio_rows if _is_dashboard_holding(row)]
    holding_markets = _markets_from_rows(holding_rows)
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
    positions_by_holding = _group_by_market_symbol(broker_positions)
    agent_reports_by_holding = _latest_by_market_symbol(trading_advice)
    strategies_by_holding = _latest_by_market_symbol(trading_plan)
    premarket_actions_by_holding = _latest_by_market_symbol(premarket_actions)
    actions_by_holding = _latest_by_market_symbol(trade_actions)
    decision_plans_by_holding, decision_plan_errors_by_market = (
        _latest_decision_plans_for_markets(config.data_dir, holding_markets)
    )
    cash_rows = [row for row in portfolio_rows if _is_cash_like_row(row)]
    holdings = [
        _merge_holding(
            row,
            config.data_dir,
            positions_by_holding,
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
        holding_rows,
        _read_csv_rows(config.data_dir / "latest" / "watchlist.csv"),
    )

    return DashboardState(
        config=config,
        broker_detail_month=detail_month,
        detail_available=bool(detail_month),
        summary=_build_summary(portfolio_rows, holding_rows),
        holdings=holdings,
        broker_summaries=_build_broker_summaries(
            portfolio_rows,
            broker_positions,
            cash_details,
            _latest_tiger_account_metrics(config.data_dir),
        ),
        source_statuses=_build_source_statuses(
            broker_positions,
            cash_details,
            detail_month,
        ),
        cash_rows=cash_rows,
        broker_positions=broker_positions,
        cash_details=cash_details,
        trade_actions=trade_actions,
        kelly_lab=kelly_lab,
        backtest_universe=backtest_universe,
        trend_reports=_load_trend_reports(
            config.data_dir,
            config.reports_dir,
            broker_positions=broker_positions,
            cash_details=raw_cash_details,
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


def _valid_trend_review_projection(
    payload: object, *, broker: str, market: str
) -> bool:
    if not isinstance(payload, dict):
        return False
    snapshot = payload.get("strategy_snapshot")
    metrics = payload.get("metrics")
    schema_version = payload.get("schema_version")
    if (
        schema_version not in {
            "open_trader.trend_review.projection.v1",
            "open_trader.trend_review.projection.v2",
        }
        or payload.get("available") is not True
        or payload.get("broker") != broker
        or payload.get("market") != market
        or not isinstance(snapshot, dict)
        or not isinstance(metrics, dict)
        or set(metrics) != TREND_REVIEW_METRICS
    ):
        return False
    if schema_version == "open_trader.trend_review.projection.v2":
        sample_counts = payload.get("sample_counts")
        common_cutoff = payload.get("common_cutoff")
        interval = payload.get("interval")
        if (
            not isinstance(sample_counts, dict)
            or set(sample_counts) != {"discipline", "actual", "required"}
            or any(
                type(sample_counts[key]) is not int or sample_counts[key] < 0
                for key in ("discipline", "actual")
            )
            or type(sample_counts["required"]) is not int
            or sample_counts["required"] != 30
            or not isinstance(interval, dict)
            or set(interval) != {"start", "end"}
            or not _valid_iso_date(interval["start"])
            or snapshot.get("effective_from") != interval["start"]
            or interval["end"] != common_cutoff
            or (
                common_cutoff is not None
                and (
                    not _valid_iso_date(common_cutoff)
                    or common_cutoff < interval["start"]
                )
            )
        ):
            return False
    for key in (
        "strategy_id",
        "strategy_name",
        "strategy_version",
        "process_version",
    ):
        if not isinstance(snapshot.get(key), str) or not snapshot[key].strip():
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
            or any(not isinstance(row[key], str) or not row[key].strip() for key in row)
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
        reviews[broker] = {
            "available": True,
            "broker": broker,
            "broker_label": broker_label,
            "market": market,
            "market_label": market_label,
            "strategy_snapshot": payload["strategy_snapshot"],
            "metrics": payload["metrics"],
        }
    return reviews


def _load_trend_reports(
    data_dir: Path,
    reports_dir: Path,
    *,
    today: date | None = None,
    now: datetime | None = None,
    broker_positions: list[dict[str, str]] | None = None,
    cash_details: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    if broker_positions is None or cash_details is None:
        broker_positions, cash_details = _latest_broker_details(data_dir)
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
            broker_positions=broker_positions,
            cash_details=cash_details,
        )
        for broker, (market, market_label, broker_label, directory, buy_window)
        in TREND_REPORT_SOURCES.items()
    }
    reports["futu"] = _project_futu_attention(
        reports["tiger"], reports["phillips"]
    )
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
    data_dir: Path, reports_dir: Path, *, broker: str, artifact: str
) -> dict[str, Any]:
    """Return the same report projection used by the current-report UI."""
    try:
        market, market_label, broker_label, directory, buy_window = (
            TREND_REPORT_SOURCES[broker]
        )
    except KeyError:
        raise ValueError(f"unsupported trend report broker: {broker}") from None
    broker_dir = reports_dir / directory
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
    broker_positions, cash_details = _latest_broker_details(data_dir)
    return _project_broker_trend_report(
        selected=(
            path,
            payload,
            execution_date,
            as_of_date,
            freshness_date,
            generated_at,
        ),
        data_dir=data_dir,
        reports_dir=broker_dir.resolve(),
        broker=broker,
        market=market,
        market_label=market_label,
        broker_label=broker_label,
        buy_window=buy_window,
        report_date=_shanghai_date().isoformat(),
        broker_positions=broker_positions,
        cash_details=cash_details,
    )


def _project_futu_attention(
    tiger: dict[str, Any], phillips: dict[str, Any]
) -> dict[str, Any]:
    def project(source: dict[str, Any]) -> dict[str, Any]:
        return {
            "market": source["market"],
            "market_label": source["market_label"],
            "data_status": source["data_status"],
            "data_date": source.get("data_date", ""),
            "status_text": source["status_text"],
            "items": source.get("option_attention", [])
            if source["available"]
            else [],
        }

    return {
        "available": tiger["available"] or phillips["available"],
        "broker": "futu",
        "broker_label": "富途",
        "market": "US_HK",
        "market_label": "美股 / 港股",
        "status_text": "期权关注",
        "attention_markets": [project(tiger), project(phillips)],
    }


def _shanghai_date(now: datetime | None = None) -> date:
    return (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date()


def _trend_market_date(market: str, *, now: datetime | None = None) -> date:
    reference_now = now or datetime.now(SHANGHAI)
    return reference_now.astimezone(TREND_MARKET_TIMEZONES[market]).date()


def _latest_valid_report_payload(
    reports_dir: Path, report_date: str, *, market: str, broker: str
) -> tuple[Path, dict[str, Any], date, date, date, datetime] | None:
    matches: list[
        tuple[date, datetime, date, str, Path, dict[str, Any], date]
    ] = []
    today = date.fromisoformat(report_date)
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
        if (
            freshness_date > today
            or generated_at.astimezone(SHANGHAI).date() > today
        ):
            continue
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


def _trend_action_needs_review(item: dict[str, Any]) -> bool:
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
    formal = [
        {
            **item,
            **(
                {"execution": executions[key]}
                if (key := (
                    str(item.get("symbol") or "").strip(),
                    {"BUY": "buy", "SELL_ALL": "sell"}.get(
                        item.get("action"), ""
                    ),
                )) in executions
                else {}
            ),
        }
        for item in judgments["formal_actions"]
    ]
    holdings = judgments["holding_decisions"]
    sell_actions = [
        item
        for item in formal
        if item.get("action") == "SELL_ALL"
        and not _trend_action_needs_review(item)
    ]
    buy_actions = [
        item
        for item in formal
        if item.get("action") == "BUY"
        and not _trend_action_needs_review(item)
    ]
    hold_actions = [
        item
        for item in holdings
        if item.get("action") == "HOLD"
        and not _trend_action_needs_review(item)
    ]
    review_actions: list[dict[str, Any]] = []
    for item in formal + holdings:
        if _trend_action_needs_review(item) and item not in review_actions:
            review_actions.append(item)
    return sell_actions, buy_actions, hold_actions, review_actions


def _valid_trend_collections(
    payload: dict[str, Any], judgments: dict[str, Any]
) -> bool:
    if any(
        not all(isinstance(item, dict) for item in judgments[key])
        for key in ("formal_actions", "holding_decisions", "top10_candidates")
    ):
        return False
    risk_skips = judgments.get("risk_skips", [])
    if not isinstance(risk_skips, list) or not all(
        isinstance(item, dict) for item in risk_skips
    ):
        return False
    snapshots = payload.get("signal_snapshots")
    if snapshots is not None and (
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


def _valid_trend_risk_summary(payload: dict[str, Any]) -> bool:
    snapshot = payload.get("strategy_snapshot")
    strategy_version = (
        str(snapshot.get("strategy_version") or "")
        if isinstance(snapshot, dict)
        else ""
    )
    summary = payload.get("risk_summary")
    if summary is None:
        return strategy_version not in {"v2", "v3", "v4"}
    if not isinstance(summary, dict) or any(
        isinstance(value, (dict, list)) for value in summary.values()
    ):
        return False
    if strategy_version not in {"v2", "v3", "v4"}:
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
            expected_strategy_version="v4",
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
    if strategy_version in {"v3", "v4"}:
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
            or target_weight > PORTFOLIO_RISK_LIMIT
            or strategy_version in {"v3", "v4"}
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
    if strategy_version in {"v3", "v4"}:
        allowed_constraints.add("Kelly 上限")
    if strategy_version == "v4":
        allowed_constraints.add("策略累计回撤")
    for item in judgments["risk_skips"]:
        shares = item.get("estimated_shares")
        target_weight = _dashboard_risk_decimal(item.get("target_weight"))
        target_amount_raw = item.get("target_amount")
        target_amount = _dashboard_risk_decimal(target_amount_raw)
        zero_kelly_skip = (
            strategy_version in {"v3", "v4"}
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
            or target_weight > PORTFOLIO_RISK_LIMIT
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
        and _valid_trend_risk_summary(payload)
        and _valid_option_attention(payload, market=market)
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
    broker_positions: list[dict[str, str]],
    cash_details: list[dict[str, str]],
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
        reports_dir, report_date, market=market, broker=broker
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
        broker_positions=broker_positions,
        cash_details=cash_details,
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
    broker_positions: list[dict[str, str]] | None = None,
    cash_details: list[dict[str, str]] | None = None,
    use_execution_batch: bool = False,
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
                selected = (
                    path,
                    payload,
                    execution_date,
                    as_of_date,
                    freshness_date,
                    generated_at,
                )
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
    executions = _trend_action_executions(
        data_dir,
        market=market,
        execution_date=execution_date.isoformat(),
        report_sha256=report_sha256,
    )
    sell_actions, buy_actions, hold_actions, review_actions = (
        _project_trend_actions(payload, executions)
    )
    account_fresh = account.get("fresh") is True
    directory = reports_dir.name
    signal_snapshots = payload.get("signal_snapshots", {})
    audit_candidates = payload["strategy_judgments"]["top10_candidates"]
    if market == "CN" and isinstance(signal_snapshots, dict):
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
    actual_overlay = _project_trend_actual_overlay(
        broker=broker,
        market=market,
        sell_actions=sell_actions,
        buy_actions=buy_actions,
        hold_actions=hold_actions,
        review_actions=review_actions,
        risk_skips=payload["strategy_judgments"].get("risk_skips", []),
        broker_positions=broker_positions or [],
        cash_details=cash_details or [],
    )
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
        "risk_skips": payload["strategy_judgments"].get("risk_skips", []),
        "risk_summary": risk_summary,
        "drawdown_summary": payload.get("drawdown_summary", {}),
        "actual_overlay": actual_overlay,
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
            "excluded": payload.get("excluded", {}),
            "account_exceptions": account.get("exceptions", []),
            "industry_concentration": payload.get("industry_concentration", []),
            "data_sources": payload.get("data_sources", []),
            "estimated_api_cost": payload.get("estimated_api_cost"),
            "actual_api_cost": payload.get("actual_api_cost"),
            "artifact": path.name,
        },
    }


def _project_trend_actual_overlay(
    *,
    broker: str,
    market: str,
    sell_actions: list[dict[str, Any]],
    buy_actions: list[dict[str, Any]],
    hold_actions: list[dict[str, Any]],
    review_actions: list[dict[str, Any]],
    risk_skips: list[dict[str, Any]],
    broker_positions: list[dict[str, str]],
    cash_details: list[dict[str, str]],
) -> dict[str, Any]:
    positions = [
        row
        for row in broker_positions
        if _broker_key(row.get("broker", "")) == broker
        and str(row.get("market") or "").strip().upper() == market
        and not _is_cash_like_row(row)
    ]
    broker_cash = [
        row
        for row in cash_details
        if _broker_key(row.get("broker", "")) == broker
    ]
    broker_rows = [
        row
        for row in broker_positions
        if _broker_key(row.get("broker", "")) == broker
    ]
    if not broker_rows and not broker_cash:
        return {
            "available": False,
            "broker": broker,
            "broker_label": BROKER_LABELS[broker],
            "market": market,
            "status_text": "实盘账户数据暂不可用",
            "items": [],
            "outside_positions": [],
        }

    summary = _build_broker_summary(
        broker, [], broker_positions, cash_details, {}
    )
    nav_hkd = _optional_decimal(summary.get("portfolio_value_hkd", ""))
    actual_rows = [*broker_rows, *broker_cash]
    price_fx_to_hkd, price_fx_note = _trend_actual_price_fx_to_hkd(
        actual_rows,
        broker=broker,
        market=market,
    )
    position_by_symbol = _aggregate_trend_actual_positions(positions)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in [
        *sell_actions,
        *buy_actions,
        *hold_actions,
        *review_actions,
        *risk_skips,
    ]:
        symbol = str(action.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            _project_trend_actual_item(
                action,
                position_by_symbol.get(symbol),
                nav_hkd=nav_hkd,
                market=market,
                price_fx_to_hkd=price_fx_to_hkd,
                price_fx_note=price_fx_note,
                risk_skip=action in risk_skips,
            )
        )

    outside = [
        {
            "symbol": position["symbol"],
            "name": position["name"],
            "actual_quantity": _decimal_text(position["quantity"]),
            "actual_market_value": (
                _decimal_text(position["market_value"])
                if position["market_value"] is not None
                else ""
            ),
            "currency": position["currency"],
            "deviation": "outside_report_addition",
            "deviation_label": "报告外加仓",
            "attribution_status": "unconfirmed",
            "risk_note": "风险未纳入估算",
        }
        for symbol, position in sorted(position_by_symbol.items())
        if symbol not in seen
    ]
    live = _has_live_statement_row(actual_rows, broker)
    return {
        "available": True,
        "broker": broker,
        "broker_label": BROKER_LABELS[broker],
        "market": market,
        "account_nav_hkd": _money_text(nav_hkd) if nav_hkd is not None else "",
        "account_nav_basis": "完整账户净值（持仓+现金）",
        "status_text": "账户实时同步" if live else "结单数据，非实时",
        "notice": (
            "只读执行辅助；实盘变化不会改写模拟建议、Kelly、模拟统计或报告哈希；"
            "系统不会自动交易真实账户。"
        ),
        "items": rows,
        "outside_positions": outside,
    }


def _trend_actual_price_fx_to_hkd(
    rows: list[dict[str, str]],
    *,
    broker: str,
    market: str,
) -> tuple[Decimal | None, str]:
    currency = TREND_MARKET_CURRENCIES.get(market, "")
    matching_rows = [
        row
        for row in rows
        if _broker_key(row.get("broker", "")) == broker
        and str(row.get("currency") or "").strip().upper() == currency
    ]
    if _has_live_statement_row(rows, broker):
        live_suffix = f"-{broker}-live"
        live_rows = [
            row
            for row in matching_rows
            if row.get("statement_id", "").strip().lower().endswith(live_suffix)
        ]
        rates: list[Decimal] = []
        for row in live_rows:
            rate = _optional_decimal(row.get("fx_to_hkd", ""))
            if rate is None or rate <= 0:
                return None, "实盘汇率缺失，暂无法换算"
            rates.append(rate)
        if not rates:
            return None, "实盘汇率缺失，暂无法换算"
        if any(rate != rates[0] for rate in rates[1:]):
            return None, "实盘汇率冲突，暂无法换算"
        return rates[0], ""

    return (
        next(
            (
                rate
                for row in matching_rows
                if (rate := _detail_fx_to_hkd(row)) is not None
            ),
            DETAIL_FX_TO_HKD.get(currency),
        ),
        "",
    )


def _aggregate_trend_actual_positions(
    positions: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for row in positions:
        symbol = str(row.get("symbol") or "").strip().upper()
        quantity = _optional_decimal(row.get("quantity", ""))
        market_value = _optional_decimal(row.get("market_value", ""))
        if not symbol or quantity is None or quantity <= 0:
            continue
        current = aggregated.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": str(row.get("name") or symbol).strip() or symbol,
                "currency": str(row.get("currency") or "").strip().upper(),
                "quantity": Decimal("0"),
                "market_value": Decimal("0"),
            },
        )
        current["quantity"] += quantity
        if market_value is None:
            current["market_value"] = None
        elif current["market_value"] is not None:
            current["market_value"] += market_value
    return aggregated


def _project_trend_actual_item(
    action: dict[str, Any],
    position: dict[str, Any] | None,
    *,
    nav_hkd: Decimal | None,
    market: str,
    price_fx_to_hkd: Decimal | None,
    price_fx_note: str,
    risk_skip: bool,
) -> dict[str, Any]:
    symbol = str(action.get("symbol") or "").strip().upper()
    frozen_action = "SKIP" if risk_skip else str(action.get("action") or "")
    actual_quantity = position["quantity"] if position else Decimal("0")
    currency = (
        str(position.get("currency") or "").strip().upper()
        if position
        else TREND_MARKET_CURRENCIES.get(market, "")
    )
    reference_quantity = (
        _trend_actual_reference_quantity(
            action,
            nav_hkd=nav_hkd,
            price_fx_to_hkd=price_fx_to_hkd,
        )
        if frozen_action == "BUY"
        else Decimal("0")
        if frozen_action in {"SELL_ALL", "SKIP"}
        else None
    )
    deviation, deviation_label = _trend_actual_deviation(
        frozen_action,
        actual_quantity=actual_quantity,
        reference_quantity=reference_quantity,
    )
    line = _optional_decimal(
        str(action.get("active_line") or action.get("estimated_initial_line") or "")
    )
    close = _optional_decimal(str(action.get("close") or ""))
    estimated_loss = (
        max(Decimal("0"), close - line) * actual_quantity
        if line is not None and close is not None
        else None
    )
    item = {
        "symbol": symbol,
        "name": str(action.get("name") or symbol),
        "frozen_action": frozen_action,
        "frozen_action_label": {
            "BUY": "正式买入",
            "SELL_ALL": "全部卖出",
            "HOLD": "继续持有",
            "SKIP": "跳过",
            "MANUAL_REVIEW": "人工复核",
        }.get(frozen_action, "人工复核"),
        "target_weight": str(action.get("target_weight") or ""),
        "simulation_quantity": str(action.get("estimated_shares") or ""),
        "actual_reference_quantity": (
            _decimal_text(reference_quantity)
            if reference_quantity is not None
            else ""
        ),
        "actual_quantity": _decimal_text(actual_quantity),
        "actual_market_value": (
            _decimal_text(position["market_value"])
            if position and position["market_value"] is not None
            else "0"
            if position is None
            else ""
        ),
        "currency": currency,
        "deviation": deviation,
        "deviation_label": deviation_label,
        "frozen_reference_price": _decimal_text(close) if close is not None else "",
        "protection_line": _decimal_text(line) if line is not None else "",
        "protection_line_label": (
            "活动保护线"
            if action.get("active_line") not in {None, ""}
            else "预计保护线"
            if action.get("estimated_initial_line") not in {None, ""}
            else ""
        ),
        "estimated_exit_loss": (
            _money_text(estimated_loss) if estimated_loss is not None else ""
        ),
        "risk_note": (
            f"若按策略保护线退出，预计损失 {currency} {_money_text(estimated_loss)}"
            "（按冻结参考价估算，不代表实时风险上限）"
            if estimated_loss is not None
            else "暂无策略保护线，风险未纳入估算"
        ),
    }
    if frozen_action == "BUY" and price_fx_note:
        item["reference_note"] = price_fx_note
    return item


def _trend_actual_reference_quantity(
    action: dict[str, Any],
    *,
    nav_hkd: Decimal | None,
    price_fx_to_hkd: Decimal | None,
) -> Decimal | None:
    weight = _optional_decimal(str(action.get("target_weight") or ""))
    price = _optional_decimal(str(action.get("close") or ""))
    lot = _optional_decimal(str(action.get("lot_size") or ""))
    if (
        nav_hkd is None
        or weight is None
        or weight < 0
        or price is None
        or price <= 0
        or lot is None
        or lot <= 0
        or price_fx_to_hkd is None
    ):
        return None
    return Decimal(
        int(nav_hkd * weight / price / price_fx_to_hkd / lot)
    ) * lot


def _trend_actual_deviation(
    frozen_action: str,
    *,
    actual_quantity: Decimal,
    reference_quantity: Decimal | None,
) -> tuple[str, str]:
    if frozen_action == "BUY":
        if reference_quantity is None:
            return "reference_unavailable", "暂无法换算"
        if actual_quantity == 0:
            return "skipped", "跳过"
        if actual_quantity < reference_quantity:
            return "underbought", "少买"
        if actual_quantity > reference_quantity:
            return "overbought", "超买"
        return "followed", "已跟随"
    if frozen_action == "SELL_ALL":
        return (
            ("missed_sell", "漏卖")
            if actual_quantity > 0
            else ("followed", "已跟随")
        )
    if frozen_action == "SKIP":
        return (
            ("chased", "追买")
            if actual_quantity > 0
            else ("followed", "跳过")
        )
    if frozen_action == "HOLD":
        return (
            ("followed", "已跟随")
            if actual_quantity > 0
            else ("not_held", "未持有")
        )
    return "review", "待人工复核"


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


def latest_broker_detail_month(data_dir: Path) -> str:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return ""

    months = [
        path.name
        for path in runs_dir.iterdir()
        if path.is_dir()
        and DETAIL_DIR_PATTERN.fullmatch(path.name)
        and _has_broker_detail_files(path)
    ]
    return max(months) if months else ""


def _latest_broker_details(
    data_dir: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return [], []
    positions: list[dict[str, str]] = []
    cash: list[dict[str, str]] = []
    found: set[str] = set()
    statement_candidates: dict[
        str,
        tuple[tuple[str, str], list[dict[str, str]], list[dict[str, str]]],
    ] = {}
    run_dirs = sorted(
        (
            path
            for path in runs_dir.iterdir()
            if path.is_dir() and DETAIL_DIR_PATTERN.fullmatch(path.name)
        ),
        reverse=True,
    )
    for run_dir in run_dirs:
        run_positions = _read_csv_rows(run_dir / "extracted_positions.csv")
        run_cash = _read_csv_rows(run_dir / "extracted_cash.csv")
        for broker in BROKERS:
            if broker in found:
                continue
            broker_positions = [
                row
                for row in run_positions
                if _broker_key(row.get("broker", "")) == broker
                and _optional_decimal(row.get("quantity", "")) != Decimal("0")
            ]
            broker_cash = [
                row for row in run_cash if _broker_key(row.get("broker", "")) == broker
            ]
            if broker_positions or broker_cash:
                if broker in {"phillips", "eastmoney"}:
                    period = _latest_statement_period(
                        [*broker_positions, *broker_cash], broker
                    )
                    key = (period, run_dir.name)
                    current = statement_candidates.get(broker)
                    if current is None or key > current[0]:
                        statement_candidates[broker] = (
                            key,
                            broker_positions,
                            broker_cash,
                        )
                    continue
                positions.extend(broker_positions)
                cash.extend(broker_cash)
                found.add(broker)
    for broker in ("phillips", "eastmoney"):
        candidate = statement_candidates.get(broker)
        if candidate is None:
            continue
        positions.extend(candidate[1])
        cash.extend(candidate[2])
    return positions, cash


def _has_broker_detail_files(path: Path) -> bool:
    return (
        (path / "extracted_positions.csv").is_file()
        or (path / "extracted_cash.csv").is_file()
    )


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


def _markets_from_rows(rows: list[dict[str, str]]) -> set[str]:
    scoped_markets = {market.value for market in MarketScope}
    markets: set[str] = set()
    for row in rows:
        market = row.get("market", "").strip().upper()
        if market in scoped_markets:
            markets.add(market)
    return markets


def _json_safe_row(row: dict[str | None, str | None]) -> dict[str, str]:
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if key is not None
    }


def _group_by_market_symbol(
    rows: list[dict[str, str]],
) -> dict[tuple[str, str], list[dict[str, str]]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        key = _market_symbol_key(row)
        if key is None:
            continue
        grouped.setdefault(key, []).append(row)
    return grouped


def _latest_by_market_symbol(
    rows: list[dict[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    keyed: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = _market_symbol_key(row)
        if key is not None:
            keyed[key] = row
    return keyed


def _market_symbol_key(row: dict[str, str]) -> tuple[str, str] | None:
    market = row.get("market", "").strip().upper()
    symbol = row.get("symbol", "").strip().upper()
    if not market or not symbol:
        return None
    return (market, symbol)


def _is_dashboard_holding(row: dict[str, str]) -> bool:
    quantity = _optional_decimal(row.get("total_quantity", ""))
    return not _is_cash_like_row(row) and quantity != Decimal("0")


def _is_cash_like_row(row: dict[str, str]) -> bool:
    market = row.get("market", "").strip().upper()
    asset_class = row.get("asset_class", "").strip().lower()
    if market == "CASH":
        return True
    if asset_class in {"cash", "money_market_fund"}:
        return True
    return False


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
            plans = load_decision_plans(path)
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


def _project_decision_plan(
    data_dir: Path,
    plan: dict[str, object],
) -> dict[str, Any]:
    projected = copy.deepcopy(plan)
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
                    for item in load_decision_plans(path)
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
    positions_by_holding: dict[tuple[str, str], list[dict[str, str]]],
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
    key = _market_symbol_key(row)
    broker_details = (
        [_broker_detail_row(row) for row in positions_by_holding.get(key, [])]
        if key is not None
        else []
    )
    holding["broker_detail_count"] = len(broker_details)
    holding["broker_details"] = broker_details
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


def _broker_detail_row(row: dict[str, str]) -> dict[str, str]:
    detail = dict(row)
    value = _detail_value_hkd(row, "market_value")
    detail["market_value_hkd"] = _money_text(value) if value is not None else ""
    return detail


def _cash_detail_row(row: dict[str, str]) -> dict[str, str]:
    detail = dict(row)
    value = _detail_value_hkd(row, "cash_balance")
    currency = row.get("currency", "").strip().upper()
    detail["market"] = "CASH"
    detail["asset_class"] = "cash"
    detail["symbol"] = f"{currency}_CASH" if currency else "CASH"
    detail["name"] = f"{currency} Cash" if currency else "Cash"
    detail["brokers"] = row.get("broker", "")
    detail["market_value_hkd"] = _money_text(value) if value is not None else ""
    return detail


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


def _build_summary(
    rows: list[dict[str, str]],
    holding_rows: list[dict[str, str]],
) -> dict[str, Any]:
    total = Decimal("0")
    for row in rows:
        market_value = _optional_decimal(row.get("market_value_hkd", ""))
        if market_value is not None:
            total += market_value

    holding_total = Decimal("0")
    for row in holding_rows:
        market_value = _optional_decimal(row.get("market_value_hkd", ""))
        if market_value is not None:
            holding_total += market_value
    cash_like_total = total - holding_total

    return {
        "holding_count": len(holding_rows),
        "portfolio_value_hkd": _money_text(total),
        "holding_value_hkd": _money_text(holding_total),
        "cash_like_value_hkd": _money_text(cash_like_total),
        "holding_weight_hkd": _pct_text(_ratio(holding_total, total)),
        "cash_like_weight_hkd": _pct_text(_ratio(cash_like_total, total)),
        "broker_count": _broker_count(rows),
    }


def _build_broker_summaries(
    portfolio_rows: list[dict[str, str]],
    broker_positions: list[dict[str, str]],
    cash_details: list[dict[str, str]],
    tiger_account_metrics: dict[str, str],
) -> list[dict[str, Any]]:
    return [
        _build_broker_summary(
            broker,
            portfolio_rows,
            broker_positions,
            cash_details,
            tiger_account_metrics if broker == "tiger" else {},
        )
        for broker in BROKERS
    ]


def _build_broker_summary(
    broker: str,
    portfolio_rows: list[dict[str, str]],
    broker_positions: list[dict[str, str]],
    cash_details: list[dict[str, str]],
    account_metrics: dict[str, str],
) -> dict[str, Any]:
    broker_detail_positions = [
        row for row in broker_positions if _broker_key(row.get("broker", "")) == broker
    ]
    detail_positions = [
        row for row in broker_detail_positions if not _is_cash_like_row(row)
    ]
    position_cash_rows = [
        row for row in broker_detail_positions if _is_cash_like_row(row)
    ]
    detail_cash_rows = [
        row for row in cash_details if _broker_key(row.get("broker", "")) == broker
    ]
    detail_available = bool(broker_detail_positions or detail_cash_rows)
    if detail_available:
        holding_value = _sum_detail_hkd(detail_positions, "market_value")
        cash_balance = _sum_detail_hkd(detail_cash_rows, "cash_balance")
        position_cash = _sum_detail_hkd(position_cash_rows, "market_value")
        cash_like_value = (
            cash_balance + position_cash
            if cash_balance is not None and position_cash is not None
            else None
        )
        portfolio_value = (
            holding_value + cash_like_value
            if holding_value is not None and cash_like_value is not None
            else None
        )
        money = {
            "holding_value_hkd": _money_text(holding_value)
            if holding_value is not None
            else "",
            "cash_like_value_hkd": _money_text(cash_like_value)
            if cash_like_value is not None
            else "",
            "portfolio_value_hkd": _money_text(portfolio_value)
            if portfolio_value is not None
            else "",
            "holding_count": len(detail_positions),
        }
    else:
        money = _build_portfolio_fallback_summary(portfolio_rows, broker)
    cash_components = (
        [
            {
                "label": f"{row.get('currency', '').strip().upper()} 现金".strip(),
                "value_hkd": _detail_money_text(row, "cash_balance"),
            }
            for row in detail_cash_rows
        ]
        + [
            {
                "label": row.get("name", "").strip()
                or row.get("symbol", "").strip(),
                "value_hkd": _detail_money_text(row, "market_value"),
            }
            for row in position_cash_rows
        ]
        if broker == "tiger" and detail_available
        else []
    )

    return {
        "broker": broker,
        "label": BROKER_LABELS[broker],
        "source_kind": BROKER_SOURCE_KINDS[broker],
        "detail_available": detail_available,
        **account_metrics,
        **({"cash_components": cash_components} if cash_components else {}),
        **money,
    }


def _latest_tiger_account_metrics(data_dir: Path) -> dict[str, str]:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return {}
    snapshot_paths = sorted(
        (
            path / "tiger_account_snapshot.json"
            for path in runs_dir.iterdir()
            if path.is_dir() and DETAIL_DIR_PATTERN.fullmatch(path.name)
        ),
        reverse=True,
    )
    for path in snapshot_paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = payload.get("cash_records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or record.get("record_type") != "account_total":
                continue
            available = _optional_decimal(record.get("cash_available_for_trade", ""))
            fx_to_hkd = _optional_decimal(record.get("fx_to_hkd", ""))
            if available is None or fx_to_hkd is None or fx_to_hkd <= 0:
                continue
            return {"available_to_trade_hkd": _money_text(available * fx_to_hkd)}
    return {}


def _sum_detail_hkd(
    rows: list[dict[str, str]], value_field: str
) -> Decimal | None:
    total = Decimal("0")
    for row in rows:
        value, complete = _detail_value_hkd_for_summary(row, value_field)
        if not complete:
            return None
        if value is not None:
            total += value
    return total


def _detail_money_text(row: dict[str, str], value_field: str) -> str:
    value = _detail_value_hkd(row, value_field)
    return _money_text(value) if value is not None else ""


def _detail_value_hkd_for_summary(
    row: dict[str, str], value_field: str
) -> tuple[Decimal | None, bool]:
    raw_value = row.get(value_field, "").strip()
    if not raw_value:
        return None, True
    value = _optional_decimal(raw_value)
    fx_rate = _detail_fx_to_hkd(row)
    if value is None or fx_rate is None:
        return None, False
    return value * fx_rate, True


def _detail_value_hkd(row: dict[str, str], value_field: str) -> Decimal | None:
    value = _optional_decimal(row.get(value_field, ""))
    fx_rate = _detail_fx_to_hkd(row)
    if value is None or fx_rate is None:
        return None
    return value * fx_rate


def _detail_fx_to_hkd(row: dict[str, str]) -> Decimal | None:
    raw_rate = row.get("fx_to_hkd", "").strip()
    if raw_rate:
        rate = _optional_decimal(raw_rate)
        return rate if rate is not None and rate > 0 else None
    if (
        _broker_key(row.get("broker", "")) == "tiger"
        and row.get("statement_id", "").strip().endswith("-tiger-live")
    ):
        return None
    currency = row.get("currency", "").strip().upper()
    return DETAIL_FX_TO_HKD.get(currency)


def _build_portfolio_fallback_summary(
    portfolio_rows: list[dict[str, str]],
    broker: str,
) -> dict[str, Any]:
    broker_rows: list[dict[str, str]] = []
    for row in portfolio_rows:
        brokers = _broker_list(row.get("brokers", ""))
        if broker not in brokers:
            continue
        if len(brokers) != 1 or brokers[0] != broker:
            return _empty_broker_money()
        broker_rows.append(row)

    if not broker_rows:
        return _empty_broker_money()

    holding_value = Decimal("0")
    cash_like_value = Decimal("0")
    holding_count = 0
    for row in broker_rows:
        market_value = _optional_decimal(row.get("market_value_hkd", ""))
        if market_value is None:
            return _empty_broker_money()
        if _is_cash_like_row(row):
            cash_like_value += market_value
        elif _is_dashboard_holding(row):
            holding_value += market_value
            holding_count += 1

    return {
        "holding_value_hkd": _money_text(holding_value),
        "cash_like_value_hkd": _money_text(cash_like_value),
        "portfolio_value_hkd": _money_text(holding_value + cash_like_value),
        "holding_count": holding_count,
    }


def _empty_broker_money() -> dict[str, Any]:
    return {
        "holding_value_hkd": "",
        "cash_like_value_hkd": "",
        "portfolio_value_hkd": "",
        "holding_count": 0,
    }


def _build_source_statuses(
    broker_positions: list[dict[str, str]],
    cash_details: list[dict[str, str]],
    detail_month: str,
) -> list[dict[str, str]]:
    detail_rows = [*broker_positions, *cash_details]
    detail_brokers: set[str] = set()
    for row in detail_rows:
        broker = _broker_key(row.get("broker", ""))
        if broker:
            detail_brokers.add(broker)

    statuses: list[dict[str, str]] = []
    for broker in BROKERS:
        detail_available = broker in detail_brokers
        if broker == "futu":
            live_available = _has_live_statement_row(detail_rows, broker)
            status = "ok" if live_available else "non_realtime" if detail_available else "missing"
            display_text = (
                "账户实时同步"
                if live_available
                else "仅月结单明细"
                if detail_available
                else "未检测到账户同步"
            )
            statuses.append(
                {
                    "broker": broker,
                    "label": BROKER_LABELS[broker],
                    "capability": "quote_and_live_account",
                    "status": status,
                    "display_text": display_text,
                }
            )
            continue
        if broker == "tiger":
            live_available = _has_live_statement_row(detail_rows, broker)
            status = "ok" if live_available else "non_realtime" if detail_available else "missing"
            display_text = (
                "账户实时同步，行情走富途"
                if live_available
                else "仅月结单明细"
                if detail_available
                else "未检测到账户同步"
            )
            statuses.append(
                {
                    "broker": broker,
                    "label": BROKER_LABELS[broker],
                    "capability": "live_account",
                    "status": status,
                    "display_text": display_text,
                }
            )
            continue
        statement_period = _latest_statement_period(detail_rows, broker) or detail_month
        display_text = (
            f"{statement_period} 月结单导入"
            if detail_available and statement_period
            else "暂无月结单明细"
        )
        statuses.append(
            {
                "broker": broker,
                "label": BROKER_LABELS[broker],
                "capability": "statement",
                "status": "non_realtime",
                "display_text": display_text,
            }
        )
    return statuses


def _has_live_statement_row(rows: list[dict[str, str]], broker: str) -> bool:
    suffix = f"-{broker}-live"
    for row in rows:
        if _broker_key(row.get("broker", "")) != broker:
            continue
        statement_id = row.get("statement_id", "").strip().lower()
        if statement_id.endswith(suffix):
            return True
    return False


def _latest_statement_period(rows: list[dict[str, str]], broker: str) -> str:
    periods: list[str] = []
    for row in rows:
        if _broker_key(row.get("broker", "")) != broker:
            continue
        statement_id = row.get("statement_id", "")
        match = re.search(r"\d{4}-(?:0[1-9]|1[0-2])(?:-(?:[0-2]\d|3[01]))?", statement_id)
        if match:
            periods.append(match.group(0))
    return max(periods) if periods else ""


def _broker_list(value: str) -> list[str]:
    return [
        broker
        for broker in (_broker_key(part) for part in value.split(";"))
        if broker
    ]


def _broker_key(value: str) -> str:
    return value.strip().lower()


def _broker_count(rows: list[dict[str, str]]) -> int:
    brokers: set[str] = set()
    for row in rows:
        brokers.update(
            broker.strip()
            for broker in row.get("brokers", "").split(";")
            if broker.strip()
        )
    return len(brokers)


def _optional_decimal(value: str) -> Decimal | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _money_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _pct_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return (
        f"{(value * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
    )


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == Decimal("0"):
        return None
    return numerator / denominator
