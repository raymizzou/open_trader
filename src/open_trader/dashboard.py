from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


DETAIL_DIR_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-([0-2]\d|3[01]))?$")
BROKERS = ("futu", "tiger", "phillips")
BROKER_LABELS = {
    "futu": "富途",
    "tiger": "老虎",
    "phillips": "辉立",
}
BROKER_SOURCE_KINDS = {
    "futu": "live_account",
    "tiger": "live_account",
    "phillips": "statement",
}
DETAIL_FX_TO_HKD = {
    "HKD": Decimal("1"),
    "USD": Decimal("7.8"),
    "CNY": Decimal("1.08"),
}


@dataclass(frozen=True)
class DashboardConfig:
    portfolio_path: Path
    data_dir: Path
    reports_dir: Path
    poll_seconds: float
    futu_host: str
    futu_port: int


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
        }


def load_dashboard_state(config: DashboardConfig) -> DashboardState:
    portfolio_rows = _read_csv_rows(config.portfolio_path)
    detail_month = latest_broker_detail_month(config.data_dir)
    detail_dir = config.data_dir / "runs" / detail_month if detail_month else None
    broker_positions = (
        _read_csv_rows(detail_dir / "extracted_positions.csv")
        if detail_dir is not None
        else []
    )
    cash_details = (
        _read_csv_rows(detail_dir / "extracted_cash.csv")
        if detail_dir is not None
        else []
    )
    trade_actions = _read_csv_rows(config.data_dir / "latest" / "trade_actions.csv")
    trading_advice = _read_csv_rows(config.data_dir / "latest" / "trading_advice.csv")
    trading_plan = _read_csv_rows(config.data_dir / "latest" / "trading_plan.csv")
    premarket_actions = _read_csv_rows(
        config.data_dir / "latest" / "premarket_actions.csv"
    )

    positions_by_holding = _group_by_market_symbol(broker_positions)
    agent_reports_by_holding = _latest_by_market_symbol(trading_advice)
    strategies_by_holding = _latest_by_market_symbol(trading_plan)
    premarket_actions_by_holding = _latest_by_market_symbol(premarket_actions)
    actions_by_holding = _latest_by_market_symbol(trade_actions)
    holding_rows = [row for row in portfolio_rows if _is_dashboard_holding(row)]
    cash_rows = [row for row in portfolio_rows if _is_cash_like_row(row)]
    holdings = [
        _merge_holding(
            row,
            positions_by_holding,
            agent_reports_by_holding,
            strategies_by_holding,
            premarket_actions_by_holding,
            actions_by_holding,
        )
        for row in holding_rows
    ]

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
    )


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
    return not _is_cash_like_row(row)


def _is_cash_like_row(row: dict[str, str]) -> bool:
    market = row.get("market", "").strip().upper()
    asset_class = row.get("asset_class", "").strip().lower()
    if market == "CASH":
        return True
    if asset_class in {"cash", "money_market_fund"}:
        return True
    return False


def _merge_holding(
    row: dict[str, str],
    positions_by_holding: dict[tuple[str, str], list[dict[str, str]]],
    agent_reports_by_holding: dict[tuple[str, str], dict[str, str]],
    strategies_by_holding: dict[tuple[str, str], dict[str, str]],
    premarket_actions_by_holding: dict[tuple[str, str], dict[str, str]],
    actions_by_holding: dict[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
    holding: dict[str, Any] = dict(row)
    key = _market_symbol_key(row)
    broker_details = positions_by_holding.get(key, []) if key is not None else []
    holding["broker_detail_count"] = len(broker_details)
    holding["broker_details"] = broker_details
    agent_report = agent_reports_by_holding.get(key) if key is not None else None
    strategy = strategies_by_holding.get(key) if key is not None else None
    premarket_action = premarket_actions_by_holding.get(key) if key is not None else None
    trade_action = actions_by_holding.get(key) if key is not None else None
    holding["agent_report"] = _agent_report_detail(agent_report)
    holding["strategy"] = _strategy_detail(strategy)
    holding["premarket_action"] = _row_detail(premarket_action)
    holding["trade_action"] = _row_detail(trade_action)
    return holding


def _unavailable_detail() -> dict[str, Any]:
    return {"available": False, "error": ""}


def _row_detail(row: dict[str, str] | None) -> dict[str, Any]:
    if row is None:
        return _unavailable_detail()
    return {"available": True, **row}


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


def _strategy_detail(row: dict[str, str] | None) -> dict[str, Any]:
    return _row_detail(row)


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
) -> list[dict[str, Any]]:
    return [
        _build_broker_summary(
            broker,
            portfolio_rows,
            broker_positions,
            cash_details,
        )
        for broker in BROKERS
    ]


def _build_broker_summary(
    broker: str,
    portfolio_rows: list[dict[str, str]],
    broker_positions: list[dict[str, str]],
    cash_details: list[dict[str, str]],
) -> dict[str, Any]:
    detail_positions = [
        row for row in broker_positions if _broker_key(row.get("broker", "")) == broker
    ]
    detail_cash_rows = [
        row for row in cash_details if _broker_key(row.get("broker", "")) == broker
    ]
    detail_available = bool(detail_positions or detail_cash_rows)
    if detail_available:
        holding_value = _sum_detail_hkd(detail_positions, "market_value")
        cash_like_value = _sum_detail_hkd(detail_cash_rows, "cash_balance")
        portfolio_value = holding_value + cash_like_value
        money = {
            "holding_value_hkd": _money_text(holding_value),
            "cash_like_value_hkd": _money_text(cash_like_value),
            "portfolio_value_hkd": _money_text(portfolio_value),
            "holding_count": len(detail_positions),
        }
    else:
        money = _build_portfolio_fallback_summary(portfolio_rows, broker)

    return {
        "broker": broker,
        "label": BROKER_LABELS[broker],
        "source_kind": BROKER_SOURCE_KINDS[broker],
        "detail_available": detail_available,
        **money,
    }


def _sum_detail_hkd(rows: list[dict[str, str]], value_field: str) -> Decimal:
    total = Decimal("0")
    for row in rows:
        value = _detail_value_hkd(row, value_field)
        if value is not None:
            total += value
    return total


def _detail_value_hkd(row: dict[str, str], value_field: str) -> Decimal | None:
    value = _optional_decimal(row.get(value_field, ""))
    currency = row.get("currency", "").strip().upper()
    fx_rate = DETAIL_FX_TO_HKD.get(currency)
    if value is None or fx_rate is None:
        return None
    return value * fx_rate


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
        else:
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
    detail_brokers: set[str] = set()
    for row in [*broker_positions, *cash_details]:
        broker = _broker_key(row.get("broker", ""))
        if broker:
            detail_brokers.add(broker)

    statuses: list[dict[str, str]] = []
    for broker in BROKERS:
        detail_available = broker in detail_brokers
        if broker == "futu":
            statuses.append(
                {
                    "broker": broker,
                    "label": BROKER_LABELS[broker],
                    "capability": "quote_and_live_account",
                    "status": "ok" if detail_available else "missing",
                    "display_text": "账户实时同步"
                    if detail_available
                    else "未检测到账户同步",
                }
            )
            continue
        if broker == "tiger":
            statuses.append(
                {
                    "broker": broker,
                    "label": BROKER_LABELS[broker],
                    "capability": "live_account",
                    "status": "ok" if detail_available else "missing",
                    "display_text": "账户实时同步，行情走富途"
                    if detail_available
                    else "未检测到账户同步",
                }
            )
            continue
        display_text = (
            f"{detail_month} 月结单导入"
            if detail_available and detail_month
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
