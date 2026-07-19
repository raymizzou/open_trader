from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import sleep
from typing import Any
from zoneinfo import ZoneInfo

from .a_share_trend import (
    AShareTrendRunResult,
    AccountPosition,
    AccountSnapshot,
    UNIFIED_TREND_FIELDS,
    _balance,
    _billing_field,
    _component_api_facts,
    _final_pair_matches,
    _finalize_market_report,
    _holding_snapshot,
    _is_systemic_futu_error,
    _process_version,
    _read_delivery_receipt,
    _redact_api_key,
    _report_payload,
    _row_tm_id,
    _transition_delivery_receipt,
    _unified_trend_unit_cost,
    _write_delivery_receipt,
    _freeze_receipt_report,
    build_report,
    evaluate_candidate,
    load_futu_simulate_trend_account,
    load_protection_state,
    load_watch_events,
    render_trend_failure_text,
    render_trend_feishu_text,
    render_markdown,
    write_protection_state,
)
from .daily_premarket import (
    DailyPremarketConfig,
    RunLock,
    require_trend_review_config,
    send_notification_with_results,
)
from .kelly_order_execution import FutuSimulateOrderExecutionClient
from .notifications import Notifier, NullNotifier
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import to_futu_symbol
from .parsers.base import detect_asset_class
from .trend_animals import TrendAnimalsClient, TrendAnimalsLookupError
from .trend_delivery import deliver_daily_trend_text, retry_daily_trend_text
from .trend_review import freeze_report_evidence


SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_SETTINGS = {
    "US": {"broker": "tiger", "currency": "HKD", "asset": "美股", "deadline": time(12)},
    "HK": {"broker": "phillips", "currency": "HKD", "asset": "港股", "deadline": time(19)},
}
MARKET_NOTIFICATION_LABELS = {
    "US": ("老虎", "美股", "确认 Trend Animals 与老虎账户状态后手动重跑老虎报告"),
    "HK": ("辉立", "港股", "确认 Trend Animals 与辉立日结单状态后手动重跑辉立报告"),
}
USD_TO_HKD = Decimal("7.85")
SOURCE_DATE = re.compile(r"^(\d{4}-\d{2}(?:-\d{2})?)")
ATTENTION_CHANGE_FIELDS = (
    "right_side",
    "temperature_curr",
    "phase_curr",
    "danger",
    "boiling",
    "champagne",
    "strength_change",
)
ATTENTION_RISK_FIELDS = ("danger", "boiling", "champagne")
ATTENTION_TEMPERATURES = ("凉", "平", "温", "热", "沸")
REPORT_REVISION = re.compile(r"-r(\d+)\.json$")


class MarketHoliday(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketTrendPaths:
    root: Path
    reports: Path
    state: Path
    events: Path
    log: Path
    report_lock: Path
    watch_lock: Path


def _market(value: str) -> str:
    market = value.strip().upper()
    if market not in MARKET_SETTINGS:
        raise ValueError("market must be US or HK")
    return market


def market_paths(data_dir: Path, reports_dir: Path, market: str) -> MarketTrendPaths:
    market = _market(market)
    suffix = "us_tiger" if market == "US" else "hk_phillips"
    root = data_dir / f"trend_{suffix}"
    return MarketTrendPaths(
        root=root,
        reports=reports_dir / f"trend_{suffix}",
        state=root / "protection_state.json",
        events=root / "watch_events.jsonl",
        log=root / "run.log",
        report_lock=data_dir / "runs" / f".trend_{suffix}_report.lock",
        watch_lock=data_dir / "runs" / f".trend_{suffix}_watch.lock",
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _latest_broker_rows(
    data_dir: Path, broker: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    runs = data_dir / "runs"
    if not runs.exists():
        return [], []
    for run_dir in sorted((item for item in runs.iterdir() if item.is_dir()), reverse=True):
        positions = [
            row for row in _read_rows(run_dir / "extracted_positions.csv")
            if row.get("broker", "").strip().lower() == broker
        ]
        cash = [
            row for row in _read_rows(run_dir / "extracted_cash.csv")
            if row.get("broker", "").strip().lower() == broker
        ]
        if positions or cash:
            return positions, cash
    return [], []


def _decimal(value: object, *, default: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        if default is not None:
            return default
        raise ValueError(f"invalid decimal value: {value!r}") from None
    if not parsed.is_finite():
        if default is not None:
            return default
        raise ValueError(f"invalid decimal value: {value!r}")
    return parsed


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    return _decimal(value)


def _source_date(rows: Sequence[Mapping[str, str]]) -> str:
    dates = []
    for row in rows:
        match = SOURCE_DATE.match(row.get("statement_id", "").strip())
        if match:
            dates.append(match.group(1))
    return max(dates) if dates else "unknown"


def _normalized_symbol(market: str, value: str) -> str:
    normalized = value.strip().upper()
    suffix = f".{market}"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    return to_futu_symbol(market, normalized).split(".", 1)[1]


def load_market_account(
    *,
    data_dir: Path,
    broker: str,
    market: str,
    expected_date: str,
    managed_symbols: set[str],
) -> AccountSnapshot:
    market = _market(market)
    settings = MARKET_SETTINGS[market]
    if broker != settings["broker"]:
        raise ValueError(f"{market} trend account must use {settings['broker']}")
    currency = str(settings["currency"])
    position_rows, cash_rows = _latest_broker_rows(data_dir, broker)
    if not position_rows and not cash_rows:
        raise FileNotFoundError(f"no {broker} account details found")
    source_date = _source_date([*position_rows, *cash_rows])
    normalized_managed = {
        _normalized_symbol(market, symbol) for symbol in managed_symbols
    }
    market_rows = [
        row for row in position_rows
        if row.get("market", "").strip().upper() == market
        and row.get("currency", "").strip().upper() == currency
    ]
    native_cash_rows = [
        row for row in cash_rows
        if row.get("currency", "").strip().upper() == currency
    ]
    exceptions: list[str] = []
    positions: list[AccountPosition] = []
    for row in market_rows:
        try:
            symbol = _normalized_symbol(market, row.get("symbol", ""))
        except ValueError:
            continue
        quantity = _decimal(row.get("quantity", "0"), default=Decimal("0"))
        if market != "US" and symbol not in normalized_managed:
            continue
        asset_class = row.get("asset_class", "").strip().lower()
        if asset_class not in {"stock", "etf"}:
            exceptions.append(
                f"趋势判断不支持当前持仓：{symbol}（{asset_class or 'unknown'}）"
            )
            continue
        if quantity <= 0:
            exceptions.append(f"趋势判断不支持非多头持仓：{symbol}")
            continue
        positions.append(
            AccountPosition(
                symbol=symbol,
                name=row.get("name", "").strip() or symbol,
                asset_class=asset_class,
                quantity=quantity,
                avg_cost_price=_optional_decimal(row.get("cost_price")),
                market_value=_decimal(row.get("market_value")),
            )
        )
    if market == "US":
        exceptions.extend(
            f"现金类资产不参与趋势判断：{row.get('symbol', '').strip()}（{row.get('asset_class', '').strip().lower() or 'unknown'}）"
            for row in position_rows
            if row.get("market", "").strip().upper() == "CASH"
            or row.get("asset_class", "").strip().lower()
            in {"cash", "money_market_fund"}
        )
    position_value = sum(
        (_decimal(row.get("market_value"), default=Decimal("0")) for row in market_rows),
        Decimal("0"),
    )
    cash_balance = sum(
        (_decimal(row.get("cash_balance"), default=Decimal("0")) for row in native_cash_rows),
        Decimal("0"),
    )
    available_cash = sum(
        (
            _decimal(
                row.get("available_balance"),
                default=_decimal(row.get("cash_balance"), default=Decimal("0")),
            )
            if broker == "futu"
            else min(
                _decimal(row.get("cash_balance"), default=Decimal("0")),
                _decimal(
                    row.get("available_balance"),
                    default=_decimal(row.get("cash_balance"), default=Decimal("0")),
                ),
            )
            for row in native_cash_rows
        ),
        Decimal("0"),
    )
    net_value = position_value + cash_balance
    if broker == "futu" and market == "US":
        fx = StaticMonthEndFxProvider(expected_date[:7], DEFAULT_RATES_TO_HKD)
        target_rate = fx.get_rate_to_hkd(currency).rate
        net_value = sum(
            (
                _decimal(row.get("market_value"), default=Decimal("0"))
                * fx.get_rate_to_hkd(row.get("currency", currency)).rate
                / target_rate
                for row in position_rows
            ),
            Decimal("0"),
        ) + sum(
            (
                _decimal(row.get("cash_balance"), default=Decimal("0"))
                * fx.get_rate_to_hkd(row.get("currency", currency)).rate
                / target_rate
                for row in cash_rows
            ),
            Decimal("0"),
        )
    return AccountSnapshot(
        source_date=source_date,
        fresh=source_date == expected_date,
        net_value=net_value,
        available_cash=max(Decimal("0"), available_cash),
        positions=tuple(sorted(positions, key=lambda item: item.symbol)),
        exceptions=tuple(exceptions),
        position_count=len(positions),
    )


def _tiger_fx_to_hkd(row: Mapping[str, object]) -> Decimal:
    currency = str(row.get("currency") or "").strip().upper()
    if currency == "USD":
        return USD_TO_HKD
    if currency == "HKD":
        return Decimal("1")
    rate = _decimal(row.get("fx_to_hkd"))
    if not currency or rate <= 0:
        raise ValueError("Tiger account snapshot has invalid currency FX")
    return rate


def _load_tiger_snapshot(
    path: Path, *, source_date: str, expected_date: str, managed_symbols: set[str]
) -> AccountSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Tiger account snapshot must be an object")
    cash_rows = payload.get("cash_records")
    position_rows = payload.get("position_records")
    if not isinstance(cash_rows, list) or not all(isinstance(row, Mapping) for row in cash_rows):
        raise ValueError("Tiger account cash records are invalid")
    if not isinstance(position_rows, list) or not all(
        isinstance(row, Mapping) for row in position_rows
    ):
        raise ValueError("Tiger account position records are invalid")

    totals = [
        row for row in cash_rows
        if row.get("record_type") == "account_total"
        and str(row.get("currency") or "").strip().upper() == "USD"
    ]
    if len(totals) != 1:
        raise ValueError("Tiger account snapshot requires exactly one USD account_total")
    net_value = _decimal(totals[0].get("account_total")) * USD_TO_HKD
    available_cash = sum(
        (
            min(
                _decimal(row.get("cash_balance")),
                _decimal(row.get("available_balance")),
            ) * _tiger_fx_to_hkd(row)
            for row in cash_rows
            if row.get("record_type") != "account_total"
        ),
        Decimal("0"),
    )

    normalized_managed = {_normalized_symbol("US", symbol) for symbol in managed_symbols}
    exceptions: list[str] = []
    position_count = 0
    positions: list[AccountPosition] = []
    for row in position_rows:
        if str(row.get("market") or "").strip().upper() != "US":
            continue
        if str(row.get("sec_type") or "").strip().upper() != "STK":
            continue
        quantity = _decimal(row.get("position_qty"))
        if quantity <= 0:
            continue
        symbol = _normalized_symbol("US", str(row.get("symbol") or ""))
        name = str(row.get("name") or "").strip() or symbol
        asset_class = str(row.get("asset_class") or "").strip().lower()
        if not asset_class:
            asset_class = detect_asset_class(symbol, name).value
        if asset_class not in {"stock", "etf"}:
            if symbol in normalized_managed:
                exceptions.append(
                    f"unsupported managed asset: {symbol} ({asset_class or 'unknown'})"
                )
            continue
        position_count += 1
        if symbol not in normalized_managed:
            continue
        fx = _tiger_fx_to_hkd(row)
        avg_cost = _optional_decimal(row.get("average_cost"))
        positions.append(AccountPosition(
            symbol=symbol,
            name=name,
            asset_class=asset_class,
            quantity=quantity,
            avg_cost_price=avg_cost * fx if avg_cost is not None else None,
            market_value=_decimal(row.get("market_value")) * fx,
        ))
    return AccountSnapshot(
        source_date=source_date,
        fresh=source_date == expected_date,
        net_value=net_value,
        available_cash=max(Decimal("0"), available_cash),
        positions=tuple(sorted(positions, key=lambda item: item.symbol)),
        exceptions=tuple(exceptions),
        position_count=position_count,
    )


def load_tiger_trend_account(
    *,
    data_dir: Path,
    expected_date: str,
    managed_symbols: set[str],
    snapshot_before: str | None = None,
) -> AccountSnapshot:
    runs = data_dir / "runs"
    if not runs.exists():
        raise FileNotFoundError("no Tiger account snapshot found")
    last_error: Exception | None = None
    for run_dir in sorted((item for item in runs.iterdir() if item.is_dir()), reverse=True):
        try:
            date.fromisoformat(run_dir.name)
        except ValueError:
            continue
        if snapshot_before is not None and run_dir.name >= snapshot_before:
            continue
        path = run_dir / "tiger_account_snapshot.json"
        if not path.exists():
            continue
        try:
            return _load_tiger_snapshot(
                path,
                source_date=run_dir.name,
                expected_date=expected_date,
                managed_symbols=managed_symbols,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        raise ValueError("no valid Tiger account snapshot found") from last_error
    raise FileNotFoundError("no Tiger account snapshot found")


def load_trend_account(
    *,
    data_dir: Path,
    market: str,
    expected_date: str,
    managed_symbols: set[str],
    snapshot_before: str | None = None,
) -> AccountSnapshot:
    if _market(market) == "US":
        return load_tiger_trend_account(
            data_dir=data_dir,
            expected_date=expected_date,
            managed_symbols=managed_symbols,
            snapshot_before=snapshot_before,
        )
    return load_market_account(
        data_dir=data_dir,
        broker="phillips",
        market="HK",
        expected_date=expected_date,
        managed_symbols=managed_symbols,
    )


def resolve_market_dates(quote: object, *, market: str, run_date: str) -> tuple[str, str]:
    market = _market(market)
    run_day = date.fromisoformat(run_date)
    calendar = quote.get_trading_days(
        market=market,
        start=(run_day - timedelta(days=10)).isoformat(),
        end=(run_day + timedelta(days=14)).isoformat(),
    )
    as_of_date = run_date if market == "HK" else (run_day - timedelta(days=1)).isoformat()
    if as_of_date not in calendar:
        raise MarketHoliday(f"{market} signal date {as_of_date} is not a trading day")
    later = sorted(day for day in calendar if day > as_of_date)
    if not later:
        raise ValueError(f"Futu {market} calendar has no execution trading day")
    return as_of_date, later[0]


def _status_date(row: Mapping[str, object]) -> str:
    for key in ("asOfDate", "updateDate", "latestDate", "date"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def updates_ready(
    rows: Sequence[Mapping[str, object]], *, market: str, as_of_date: str
) -> bool:
    asset = str(MARKET_SETTINGS[_market(market)]["asset"])
    return any(row.get("asset") == asset and _status_date(row) == as_of_date for row in rows)


def _write_log(path: Path, event: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")


def _managed_symbols(
    state: Mapping[str, object], configured: Sequence[str], market: str
) -> set[str]:
    values = list(configured)
    stored = state.get("managed_symbols")
    if isinstance(stored, list):
        values.extend(str(item) for item in stored)
    positions = state.get("positions")
    if isinstance(positions, Mapping):
        values.extend(str(item) for item in positions)
    return {_normalized_symbol(market, value) for value in values if value.strip()}


def build_option_attention(
    current_rows: Sequence[Mapping[str, object]],
    previous_rows: Sequence[Mapping[str, object]],
    actions: Mapping[str, str],
    market: str,
    broker_label: str,
) -> list[dict[str, object]]:
    market = _market(market)

    def merged(rows: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
        result: dict[str, Mapping[str, object]] = {}
        for row in rows:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                continue
            try:
                result[_normalized_symbol(market, symbol)] = row
            except ValueError:
                continue
        return result

    current_by_symbol = merged(current_rows)
    previous_by_symbol = merged(previous_rows)
    attention: list[dict[str, object]] = []
    for normalized in sorted(current_by_symbol):
        current = current_by_symbol[normalized]
        previous = previous_by_symbol.get(normalized)
        if previous is None:
            if current.get("right_side") is not True or current.get("danger") is not False:
                continue
        elif not any(
            previous.get(field) != current.get(field)
            for field in ATTENTION_CHANGE_FIELDS
        ):
            continue

        def transition(field: str) -> dict[str, object]:
            previous_value = previous.get(field) if previous is not None else None
            current_value = current.get(field)
            return {
                "previous": previous_value,
                "current": current_value,
                "changed": previous_value != current_value,
            }

        risk = any(
            current.get(field) is True
            and (previous is None or previous.get(field) is not True)
            for field in ATTENTION_RISK_FIELDS
        )
        old_temperature = previous.get("temperature_curr") if previous else None
        new_temperature = current.get("temperature_curr")
        temperature_rose = (
            old_temperature in ATTENTION_TEMPERATURES
            and new_temperature in ATTENTION_TEMPERATURES
            and ATTENTION_TEMPERATURES.index(new_temperature)
            > ATTENTION_TEMPERATURES.index(old_temperature)
        )
        strengthened = (
            current.get("right_side") is True
            and (previous is None or previous.get("right_side") is not True)
        ) or temperature_rose
        symbol = str(current["symbol"])
        attention.append(
            {
                "market": market,
                "symbol": symbol,
                "name": current.get("name"),
                "category": "risk" if risk else "strengthened" if strengthened else "watch",
                "right_side": transition("right_side"),
                "temperature": transition("temperature_curr"),
                "phase": transition("phase_curr"),
                "local_strength": current.get("strength"),
                "global_strength": current.get("global_strength"),
                "strength_prev_week": current.get("strength_prev_week"),
                "strength_prev_month": current.get("strength_prev_month"),
                "strength_change": transition("strength_change"),
                "days": current.get("days"),
                "gain_since_entry": current.get("gain_since_entry"),
                "danger": transition("danger"),
                "boiling": transition("boiling"),
                "champagne": transition("champagne"),
                "source_broker": broker_label,
                "source_action": actions.get(symbol, "WATCH"),
            }
        )
    return attention


def _attention_rows(signal_snapshots: object) -> list[Mapping[str, object]] | None:
    if not isinstance(signal_snapshots, Mapping):
        return None
    candidates = signal_snapshots.get("candidates", [])
    holdings = signal_snapshots.get("holdings", {})
    if not isinstance(candidates, list) or not all(
        isinstance(row, Mapping) for row in candidates
    ):
        return None
    if not isinstance(holdings, Mapping) or not all(
        row is None or isinstance(row, Mapping) for row in holdings.values()
    ):
        return None
    return [*candidates, *(row for row in holdings.values() if row is not None)]


def _attention_report_rows(
    path: Path, *, market: str
) -> tuple[date, list[Mapping[str, object]]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return None
        as_of_date = date.fromisoformat(str(payload.get("as_of_date") or ""))
        rows = _attention_rows(payload.get("signal_snapshots"))
        if rows is None:
            return None
        for row in rows:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                return None
            _normalized_symbol(market, symbol)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return (as_of_date, rows) if rows is not None else None


def _previous_attention_rows(
    paths: MarketTrendPaths, *, current_as_of_date: str, market: str
) -> list[Mapping[str, object]]:
    current_date = date.fromisoformat(current_as_of_date)
    valid_reports: list[tuple[date, int, str, list[Mapping[str, object]]]] = []
    report_files = list(paths.reports.glob("*.json")) if paths.reports.exists() else []
    for path in report_files:
        loaded = _attention_report_rows(path, market=market)
        if loaded is not None:
            match = REPORT_REVISION.search(path.name)
            revision = int(match.group(1)) if match else 0
            valid_reports.append((loaded[0], revision, path.name, loaded[1]))
    predecessors = [item for item in valid_reports if item[0] < current_date]
    if predecessors:
        return max(predecessors, key=lambda item: (item[0], item[1], item[2]))[3]
    if _market(market) == "US" and not report_files:
        baseline = _attention_report_rows(
            paths.root / "attention_baseline.json", market=market
        )
        if baseline is not None and baseline[0] < current_date:
            return baseline[1]
    return []


def _attention_actions(payload: Mapping[str, object]) -> dict[str, str]:
    judgments = payload.get("strategy_judgments")
    if not isinstance(judgments, Mapping):
        return {}
    actions: dict[str, str] = {}
    for key in ("holding_decisions", "formal_actions"):
        rows = judgments.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            symbol = row.get("symbol")
            action = row.get("action")
            if isinstance(symbol, str) and isinstance(action, str):
                actions[symbol] = action
    return actions


def _market_receipt_path(paths: MarketTrendPaths, artifact_stem: str) -> Path:
    return paths.root / "delivery" / f"{artifact_stem}.json"


def _market_artifact_stem(
    paths: MarketTrendPaths, *, as_of_date: str, revision: bool
) -> str:
    if not revision:
        return as_of_date
    number = 1
    while True:
        stem = f"{as_of_date}-r{number}"
        markdown_path = paths.reports / f"{stem}.md"
        json_path = paths.reports / f"{stem}.json"
        receipt = _read_delivery_receipt(
            _market_receipt_path(paths, stem), artifact_stem=stem
        )
        if receipt is not None and (
            receipt["status"] != "sent"
            or not _final_pair_matches(receipt, markdown_path, json_path)
        ):
            return stem
        if receipt is None and not markdown_path.exists() and not json_path.exists():
            return stem
        number += 1


def _deliver_market_daily_text(
    *,
    paths: MarketTrendPaths,
    market: str,
    run_date: str,
    notifier: Notifier,
    payload: Mapping[str, object],
) -> str:
    broker_label, market_label, _ = MARKET_NOTIFICATION_LABELS[market]
    title, message = render_trend_feishu_text(
        payload, broker_label=broker_label, market_label=market_label
    )
    return deliver_daily_trend_text(
        notifier,
        ledger_path=paths.root / "daily_delivery" / f"{run_date}.json",
        title=title,
        message=message,
    )


def _recover_market_receipt(
    *,
    paths: MarketTrendPaths,
    market: str,
    run_date: str,
    artifact_stem: str,
    notifier: Notifier,
) -> AShareTrendRunResult | None:
    receipt_path = _market_receipt_path(paths, artifact_stem)
    receipt = _read_delivery_receipt(receipt_path, artifact_stem=artifact_stem)
    if receipt is None:
        return None
    markdown_path = paths.reports / f"{artifact_stem}.md"
    json_path = paths.reports / f"{artifact_stem}.json"
    if receipt["status"] == "sent" and _final_pair_matches(
        receipt, markdown_path, json_path
    ):
        return AShareTrendRunResult("existing", markdown_path, json_path)
    if receipt["status"] in {"prepared", "pending", "delivery_failed"}:
        if receipt["status"] == "prepared":
            write_protection_state(
                paths.state, receipt["protection_state"]  # type: ignore[arg-type]
            )
        receipt = _transition_delivery_receipt(
            receipt_path, receipt, status="pending", delivery_status="pending"
        )
        payload = json.loads(str(receipt["report_json"]))
        if not isinstance(payload, dict):
            raise ValueError("delivery receipt report JSON must be an object")
        delivery_status = _deliver_market_daily_text(
            paths=paths,
            market=market,
            run_date=run_date,
            notifier=notifier,
            payload=payload,
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status=(
                "sent"
                if delivery_status in {"sent", "sent_prior_message"}
                else delivery_status
            ),
            delivery_status=delivery_status,
        )
    markdown_path, json_path = _freeze_receipt_report(
        receipt=receipt,
        reports_dir=paths.reports,
        artifact_stem=artifact_stem,
    )
    return AShareTrendRunResult("generated", markdown_path, json_path)


def _attempt_market_report(
    *,
    config: DailyPremarketConfig,
    market: str,
    run_date: str,
    revision: bool,
    notifier: Notifier,
    api_factory: Callable[..., object] = TrendAnimalsClient,
    quote_factory: Callable[..., object] = FutuQuoteClient,
    account_factory: Callable[..., object] | None = None,
) -> AShareTrendRunResult:
    market = _market(market)
    settings = MARKET_SETTINGS[market]
    paths = market_paths(config.data_dir, config.reports_dir, market)
    quote = quote_factory(host=config.futu_host, port=config.futu_port)
    try:
        try:
            as_of_date, execution_date = resolve_market_dates(
                quote, market=market, run_date=run_date
            )
        except MarketHoliday:
            return AShareTrendRunResult("holiday", None, None)
        base_markdown = paths.reports / f"{as_of_date}.md"
        base_json = paths.reports / f"{as_of_date}.json"
        artifact_stem = _market_artifact_stem(
            paths, as_of_date=as_of_date, revision=revision
        )
        recovered = _recover_market_receipt(
            paths=paths,
            market=market,
            run_date=run_date,
            artifact_stem=artifact_stem,
            notifier=notifier,
        )
        if recovered is not None:
            return recovered
        if not revision and base_markdown.exists() and base_json.exists():
            base_markdown.read_text(encoding="utf-8")
            json.loads(base_json.read_text(encoding="utf-8"))
            retry_daily_trend_text(
                notifier,
                ledger_path=paths.root / "daily_delivery" / f"{run_date}.json",
            )
            return AShareTrendRunResult("existing", base_markdown, base_json)

        api = api_factory(
            api_key=config.trend_animals_api_key,
            cache_dir=config.data_dir / "trend_animals/cache",
        )
        update_rows = api.get_update_status()
        if not updates_ready(update_rows, market=market, as_of_date=as_of_date):
            return AShareTrendRunResult("waiting", None, None)

        prior_state = load_protection_state(paths.state)
        configured = (
            config.trend_us_symbols if market == "US" else config.trend_hk_symbols
        )
        managed = _managed_symbols(prior_state, configured, market)
        simulate_acc_id = require_trend_review_config(config, market)
        account = load_futu_simulate_trend_account(
            host=config.futu_host,
            port=config.futu_port,
            simulate_acc_id=simulate_acc_id,
            market=market,
            expected_date=as_of_date,
            account_factory=account_factory or FutuSimulateOrderExecutionClient,
        )

        balance_before = _balance(api.get_account_balance())
        pool_ids = (
            config.trend_animals_us_tm_ids
            if market == "US"
            else config.trend_animals_hk_tm_ids
        )
        component_rows: list[Mapping[str, object]] = []
        component_pools: defaultdict[int, set[str]] = defaultdict(set)
        for pool_id in pool_ids:
            rows = api.get_components(tm_id=pool_id, expected_date=as_of_date)
            component_rows.extend(rows)
            for row in rows:
                component_pools[_row_tm_id(row)].add(str(pool_id))
        component_ids = {_row_tm_id(row) for row in component_rows}

        holding_ids: dict[str, int] = {}
        for position in account.positions:
            try:
                holding_ids[position.symbol] = api.search_exact_symbol(position.symbol)
            except TrendAnimalsError:
                continue
        requested_ids = sorted(component_ids | set(holding_ids.values()))
        billing = {
            _billing_field(row): row for row in api.get_snapshot_billing()
        }
        missing = [field for field in UNIFIED_TREND_FIELDS if field not in billing]
        if missing:
            raise ValueError(
                "getSnapshotColumnBilling missing requested field(s): "
                + ", ".join(missing)
            )
        unified_unit_cost = _unified_trend_unit_cost(billing)
        snapshot_rows = (
            api.get_snapshots(
                tm_ids=requested_ids,
                fields=UNIFIED_TREND_FIELDS,
                expected_date=as_of_date,
            )
            if requested_ids
            else []
        )
        returned_ids = [_row_tm_id(row) for row in snapshot_rows]
        if sorted(returned_ids) != requested_ids or len(returned_ids) != len(set(returned_ids)):
            raise ValueError("getTickerSnapshot returned mismatched tmIds")
        if any(row.get("asOfDate") != as_of_date for row in snapshot_rows):
            raise ValueError("getTickerSnapshot returned a stale data date")
        balance_after = _balance(api.get_account_balance())

        rows_by_id = {_row_tm_id(row): row for row in snapshot_rows}
        start = (date.fromisoformat(as_of_date) - timedelta(days=90)).isoformat()
        candidates = []
        bars_by_symbol: dict[str, object] = {}
        for tm_id in sorted(component_ids):
            row = rows_by_id.get(tm_id)
            if row is None:
                continue
            try:
                symbol = _normalized_symbol(market, str(row.get("tickerSymbol", "")))
                bars = quote.get_daily_kline(
                    to_futu_symbol(market, symbol), start=start, end=as_of_date
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                bars = None
            except ValueError:
                bars = None
            candidates.append(
                evaluate_candidate(
                    row, bars, pools=component_pools[tm_id], market=market
                )
            )
        holding_snapshots = {position.symbol: None for position in account.positions}
        for symbol, tm_id in holding_ids.items():
            row = rows_by_id.get(tm_id)
            bars = None
            try:
                bars = quote.get_daily_kline(
                    to_futu_symbol(market, symbol), start=start, end=as_of_date
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                bars = None
            bars_by_symbol[symbol] = bars
            if row is not None:
                try:
                    holding_snapshots[symbol] = _holding_snapshot(
                        row, market=market, bars=tuple(bars or ())
                    )
                except ValueError:
                    pass

        lot_sizes: dict[str, int] = {}
        if market == "HK":
            symbols = [to_futu_symbol("HK", item.symbol) for item in candidates]
            wire_lots = quote.get_lot_sizes(symbols) if symbols else {}
            lot_sizes = {
                wire.split(".", 1)[1]: size for wire, size in wire_lots.items()
            }
        estimated_cost = unified_unit_cost * len(requested_ids)
        actual_cost = balance_before - balance_after
        watch_events = load_watch_events(paths.events)
        report = build_report(
            as_of_date=as_of_date,
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
                f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows={len(snapshot_rows)} cache=client-managed",
            ),
            data_sources=(
                "Trend Animals",
                f"Futu {market} calendar/QFQ daily K-line",
                f"Futu {market} SIMULATE account",
            ),
            estimated_api_cost=estimated_cost,
            actual_api_cost=actual_cost if actual_cost >= 0 else None,
            market=market,
            lot_sizes=lot_sizes,
            position_weight=Decimal("0.04"),
            position_weight_source="fallback_4pct",
            price_fx_to_account_currency=Decimal("1"),
            process_version=_process_version(config.repo),
            candidate_pool_ids=pool_ids,
            metadata={
                "market": market,
                "broker": settings["broker"],
                "simulate_acc_id": simulate_acc_id,
                "run_date": run_date,
                "process_version": _process_version(config.repo),
                **(
                    {
                        "account_currency": "USD",
                        "price_fx_to_account_currency": "1",
                    }
                    if market == "US"
                    else {}
                ),
            },
        )
        report = _finalize_market_report(report, managed_symbols=sorted(managed))
        previous_attention_rows = _previous_attention_rows(
            paths, current_as_of_date=as_of_date, market=market
        )
        option_attention_broker_label = MARKET_NOTIFICATION_LABELS[market][0]
        evidence = freeze_report_evidence(
            data_dir=config.data_dir,
            report=report,
            candidates=candidates,
            holding_snapshots=holding_snapshots,
            bars_by_symbol=bars_by_symbol,
            prior_state=prior_state,
            watch_events=watch_events,
            query={
                "component_pool_ids": list(pool_ids),
                "snapshot_fields": list(UNIFIED_TREND_FIELDS),
            },
            responses={
                "update_status": update_rows,
                "components": component_rows,
                "snapshots": snapshot_rows,
            },
            candidate_pool_ids=pool_ids,
            lot_sizes=lot_sizes,
            price_fx_to_account_currency=Decimal("1"),
            previous_attention_rows=previous_attention_rows,
            option_attention_broker_label=option_attention_broker_label,
        )
        report = replace(
            report,
            replay_evidence={
                "path": str(Path(evidence["path"]).relative_to(config.data_dir)),
                "sha256": evidence["sha256"],
            },
        )
        payload = _report_payload(report)
        current_attention_rows = _attention_rows(payload.get("signal_snapshots")) or []
        payload["option_attention"] = build_option_attention(
            current_attention_rows,
            previous_attention_rows,
            _attention_actions(payload),
            market,
            option_attention_broker_label,
        )
        receipt_path = _market_receipt_path(paths, artifact_stem)
        receipt = _write_delivery_receipt(
            receipt_path,
            status="prepared",
            generated_at=report.generated_at,
            artifact_stem=artifact_stem,
            markdown=render_markdown(report),
            report_json=json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n",
            protection_state=report.protection_state,
        )
        write_protection_state(paths.state, report.protection_state)
        receipt = _transition_delivery_receipt(
            receipt_path, receipt, status="pending", delivery_status="pending"
        )
        delivery_status = _deliver_market_daily_text(
            paths=paths,
            market=market,
            run_date=run_date,
            notifier=notifier,
            payload=payload,
        )
        receipt = _transition_delivery_receipt(
            receipt_path,
            receipt,
            status=(
                "sent"
                if delivery_status in {"sent", "sent_prior_message"}
                else delivery_status
            ),
            delivery_status=delivery_status,
        )
        markdown_path, json_path = _freeze_receipt_report(
            receipt=receipt,
            reports_dir=paths.reports,
            artifact_stem=artifact_stem,
        )
        send_notification_with_results(
            notifier,
            f"{market} 趋势计划已生成",
            f"数据日 {as_of_date}；报告 {markdown_path}",
            channels={"macos"},
        )
        return AShareTrendRunResult("generated", markdown_path, json_path)
    finally:
        close = getattr(quote, "close", None)
        if callable(close):
            close()


def run_market_trend_report(
    *,
    config: DailyPremarketConfig,
    market: str,
    run_date: str,
    revision: bool = False,
    notifier: Notifier | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
    sleep_fn: Callable[[float], None] = sleep,
    attempt_fn: Callable[..., AShareTrendRunResult] = _attempt_market_report,
    **attempt_dependencies: object,
) -> AShareTrendRunResult:
    market = _market(market)
    date.fromisoformat(run_date)
    notifier = notifier or NullNotifier()
    paths = market_paths(config.data_dir, config.reports_dir, market)
    configured_ids = (
        config.trend_animals_us_tm_ids if market == "US" else config.trend_animals_hk_tm_ids
    )
    if not configured_ids:
        raise ValueError(f"Trend Animals {market} tmId list is required")
    with RunLock(paths.report_lock):
        return _run_market_trend_retry(
            config=config,
            market=market,
            run_date=run_date,
            revision=revision,
            notifier=notifier,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
            attempt_fn=attempt_fn,
            paths=paths,
            attempt_dependencies=attempt_dependencies,
        )


def _run_market_trend_retry(
    *,
    config: DailyPremarketConfig,
    market: str,
    run_date: str,
    revision: bool,
    notifier: Notifier,
    now_fn: Callable[[], datetime],
    sleep_fn: Callable[[float], None],
    attempt_fn: Callable[..., AShareTrendRunResult],
    paths: MarketTrendPaths,
    attempt_dependencies: Mapping[str, object],
) -> AShareTrendRunResult:
    deadline = datetime.combine(
        date.fromisoformat(run_date),
        MARKET_SETTINGS[market]["deadline"],
        tzinfo=SHANGHAI,
    )
    last_error = "Trend Animals update status is not ready"
    _write_log(paths.log, {"event": "start", "market": market, "run_date": run_date})
    while True:
        try:
            result = attempt_fn(
                config=config,
                market=market,
                run_date=run_date,
                revision=revision,
                notifier=notifier,
                **dict(attempt_dependencies),
            )
            if result.status in {"generated", "existing", "holiday"}:
                _write_log(paths.log, {
                    "event": result.status,
                    "market": market,
                    "run_date": run_date,
                })
                return result
        except Exception as exc:
            last_error = _redact_api_key(exc, config.trend_animals_api_key)
        now = now_fn().astimezone(SHANGHAI)
        _write_log(paths.log, {
            "event": "retry", "market": market, "run_date": run_date,
            "error": last_error, "at": now.isoformat(timespec="seconds"),
        })
        if now >= deadline:
            _write_log(paths.log, {
                "event": "failed", "market": market, "run_date": run_date,
                "error": last_error, "at": now.isoformat(timespec="seconds"),
            })
            broker_label, market_label, recovery_action = (
                MARKET_NOTIFICATION_LABELS[market]
            )
            title, message = render_trend_failure_text(
                broker_label=broker_label,
                market_label=market_label,
                report_date=run_date,
                reason=(
                    "趋势数据在截止时间前仍未更新"
                    if "not ready" in last_error.lower()
                    else "趋势报告生成失败，需检查运行日志"
                ),
                recovery_action=recovery_action,
            )
            deliver_daily_trend_text(
                notifier,
                ledger_path=paths.root / "daily_delivery" / f"{run_date}.json",
                title=title,
                message=message,
            )
            send_notification_with_results(
                notifier,
                f"{market} 趋势计划失败",
                f"{last_error}；本轮重试窗口已结束。",
                channels={"macos"},
            )
            return AShareTrendRunResult("failed", None, None)
        sleep_fn(min(600.0, max(1.0, (deadline - now).total_seconds())))
