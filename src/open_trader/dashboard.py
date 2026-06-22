from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .decision_facts import (
    KLINE_FIELDS,
    NEWS_SENTIMENT_FIELDS,
    build_missing_fields,
    extract_decision_sources,
    index_decision_facts_by_market_symbol,
    load_decision_facts_cache,
)
from .research_chat import load_research_view_for_holding
from .technical_facts import (
    extract_market_report,
    index_technical_facts_by_market_symbol,
    load_technical_facts_cache,
    source_hash,
    technical_facts_has_missing_timeframe,
    technical_facts_latest_path,
)


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
    raw_cash_details = (
        _read_csv_rows(detail_dir / "extracted_cash.csv")
        if detail_dir is not None
        else []
    )
    cash_details = [_cash_detail_row(row) for row in raw_cash_details]
    trade_actions = _read_csv_rows(config.data_dir / "latest" / "trade_actions.csv")
    trading_plan = _read_csv_rows(config.data_dir / "latest" / "trading_plan.csv")
    premarket_actions = _read_csv_rows(
        config.data_dir / "latest" / "premarket_actions.csv"
    )
    holding_rows = [row for row in portfolio_rows if _is_dashboard_holding(row)]
    holding_markets = _markets_from_rows(holding_rows)
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
    positions_by_holding = _group_by_market_symbol(broker_positions)
    agent_reports_by_holding = _latest_by_market_symbol(trading_advice)
    strategies_by_holding = _latest_by_market_symbol(trading_plan)
    premarket_actions_by_holding = _latest_by_market_symbol(premarket_actions)
    actions_by_holding = _latest_by_market_symbol(trade_actions)
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


def _markets_from_rows(rows: list[dict[str, str]]) -> set[str]:
    markets: set[str] = set()
    for row in rows:
        market = row.get("market", "").strip().upper()
        if market:
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

    common = {
        "run_date": run_date,
        "data_date": data_date,
        "source_hash": record_source_hash,
        "current_source_hash": current_source_hash,
        "freshness": freshness_payload,
    }
    if not current_source_hash:
        return _technical_facts_unavailable(
            "missing_source_hash",
            error="latest advice market report source hash unavailable",
            **common,
        )
    if record_source_hash != current_source_hash:
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
    if (
        freshness_payload.get("status") == "missing_timeframe"
        or technical_facts_has_missing_timeframe(facts_payload)
    ):
        return _technical_facts_unavailable(
            "missing_timeframe",
            error="technical facts timeframe missing",
            **common,
        )

    return {
        "available": True,
        "status": "usable",
        "error": "",
        **common,
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
    module_status = str(module.get("status") or "").strip()
    if (
        not current_source_hash
        or source_hash_value != current_source_hash
        or module_status != "ok"
        or not isinstance(raw_fields, dict)
        or set(raw_fields) != set(fields)
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

    return {
        "broker": broker,
        "label": BROKER_LABELS[broker],
        "source_kind": BROKER_SOURCE_KINDS[broker],
        "detail_available": detail_available,
        **money,
    }


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


def _detail_value_hkd_for_summary(
    row: dict[str, str], value_field: str
) -> tuple[Decimal | None, bool]:
    raw_value = row.get(value_field, "").strip()
    if not raw_value:
        return None, True
    value = _optional_decimal(raw_value)
    currency = row.get("currency", "").strip().upper()
    fx_rate = DETAIL_FX_TO_HKD.get(currency)
    if value is None or fx_rate is None:
        return None, False
    return value * fx_rate, True


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
