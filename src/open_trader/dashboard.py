from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


MONTH_DIR_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class DashboardConfig:
    portfolio_path: Path
    data_dir: Path
    reports_dir: Path
    poll_seconds: int
    futu_host: str
    futu_port: int


@dataclass(frozen=True)
class DashboardState:
    config: DashboardConfig
    broker_detail_month: str
    detail_available: bool
    summary: dict[str, Any]
    holdings: list[dict[str, Any]]
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

    positions_by_holding = _group_by_market_symbol(broker_positions)
    actions_by_holding = _latest_by_market_symbol(trade_actions)
    holdings = [
        _merge_holding(row, positions_by_holding, actions_by_holding)
        for row in portfolio_rows
    ]

    return DashboardState(
        config=config,
        broker_detail_month=detail_month,
        detail_available=bool(detail_month),
        summary=_build_summary(portfolio_rows),
        holdings=holdings,
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
        and MONTH_DIR_PATTERN.fullmatch(path.name)
        and (path / "extracted_positions.csv").is_file()
    ]
    return max(months) if months else ""


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

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


def _merge_holding(
    row: dict[str, str],
    positions_by_holding: dict[tuple[str, str], list[dict[str, str]]],
    actions_by_holding: dict[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
    holding: dict[str, Any] = dict(row)
    key = _market_symbol_key(row)
    broker_details = positions_by_holding.get(key, []) if key is not None else []
    holding["broker_detail_count"] = len(broker_details)
    holding["trade_action"] = actions_by_holding.get(key, {}) if key is not None else {}
    return holding


def _build_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    total = Decimal("0")
    for row in rows:
        market_value = _optional_decimal(row.get("market_value_hkd", ""))
        if market_value is not None:
            total += market_value

    return {
        "holding_count": len(rows),
        "portfolio_value_hkd": _money_text(total),
        "broker_count": _broker_count(rows),
    }


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
