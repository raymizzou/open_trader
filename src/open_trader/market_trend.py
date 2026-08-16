from __future__ import annotations

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

from .account_http import fetch_account_snapshot
from .a_share_trend import (
    A_SHARE_INDUSTRY_FIELDS,
    AShareTrendRunResult,
    INDUSTRY_KNOWN_TEMPERATURES,
    INDUSTRY_MEMBER_FIELDS,
    INDUSTRY_STATE_FIELDS,
    UNIFIED_TREND_FIELDS,
    _balance,
    _account_input,
    _allocation_market_for,
    _billing_field,
    _billing_price,
    _component_api_facts,
    _final_pair_matches,
    _finalize_market_report,
    _holding_snapshot,
    _is_systemic_futu_error,
    _optional_int,
    _process_version,
    _remember_verified_symbol_row,
    _supports_symbol_mapping_contract,
    _uses_shared_entry_discipline,
    read_delivery_receipt,
    _redact_api_key,
    _report_payload,
    _row_tm_id,
    _transition_delivery_receipt,
    _unified_trend_unit_cost,
    _uses_individual_global_ranking,
    _write_delivery_receipt,
    _freeze_receipt_report,
    _write_frozen_industry_context_history,
    build_report,
    collect_industry_contexts,
    enrich_real_holding_input,
    evaluate_candidate,
    fetch_staged_candidates,
    favorite_candidate_ids,
    freeze_report_rotation_pairs,
    load_futu_simulate_trend_account,
    load_industry_temperatures,
    load_real_holding_input,
    live_trend_strategy_snapshot,
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
from .trend_kelly import load_trend_kelly_evidence
from .notifications import Notifier, NullNotifier
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import from_trend_animals_symbol, to_futu_symbol
from .trend_animals import (
    TREND_SYMBOL_MAPPING_SCHEMA,
    TrendAnimalsClient,
    TrendAnimalsError,
    TrendAnimalsNoCurrentRowsError,
)
from .trend_delivery import deliver_daily_trend_text, retry_daily_trend_text
from .trend_review import freeze_report_evidence, rebuild_overheat_trim_projection
from .strategy_drawdown import observe_strategy_equity


SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_SETTINGS = {
    "US": {"broker": "tiger", "currency": "HKD", "asset": "美股", "deadline": time(19)},
    "HK": {"broker": "phillips", "currency": "HKD", "asset": "港股", "deadline": time(19)},
}
MARKET_UPDATE_ASSETS = {
    "US": ("美股", "美国ETF"),
    "HK": ("港股", "香港ETF"),
}
HK_ETF_ROOT_TM_ID = 707617
HK_ETF_WARM_TO_HOT_NAME = "温转热(香港ETF)"
MARKET_NOTIFICATION_LABELS = {
    "US": ("老虎", "美股", "确认 Trend Animals 与老虎账户状态后手动重跑老虎报告"),
    "HK": ("辉立", "港股", "确认 Trend Animals 与辉立日结单状态后手动重跑辉立报告"),
}
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
    real_state: Path
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
        real_state=root / "real_protection_state.json",
        events=root / "watch_events.jsonl",
        log=root / "run.log",
        report_lock=data_dir / "runs" / f".trend_{suffix}_report.lock",
        watch_lock=data_dir / "runs" / f".trend_{suffix}_watch.lock",
    )


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


def _normalized_symbol(market: str, value: str) -> str:
    normalized = value.strip().upper()
    suffix = f".{market}"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    return to_futu_symbol(market, normalized).split(".", 1)[1]


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
    return update_status_gap(rows, market=market, as_of_date=as_of_date) is None


def update_status_gap(
    rows: Sequence[Mapping[str, object]], *, market: str, as_of_date: str
) -> str | None:
    required = MARKET_UPDATE_ASSETS[_market(market)]
    invalid = any(not isinstance(row, Mapping) for row in rows)
    gaps: list[str] = []
    for asset in required:
        dates = [
            _status_date(row)
            for row in rows
            if isinstance(row, Mapping) and row.get("asset") == asset
        ]
        if invalid or dates != [as_of_date]:
            current = max((day for day in dates if day), default="")
            gaps.append(
                f"{asset} {current} → {as_of_date}"
                if current
                else f"{asset} 数据缺失 → {as_of_date}"
            )
    return "，".join(gaps) if gaps else None


def _candidate_pool_components(
    api: object,
    *,
    market: str,
    pool_id: int,
    expected_date: str,
) -> tuple[list[Mapping[str, object]], int | None]:
    try:
        rows = api.get_components(  # type: ignore[attr-defined]
            tm_id=pool_id,
            expected_date=expected_date,
        )
    except TrendAnimalsNoCurrentRowsError:
        if market == "HK" and pool_id == HK_ETF_ROOT_TM_ID:
            return [], None
        raise
    if market != "HK" or pool_id != HK_ETF_ROOT_TM_ID:
        return list(rows), pool_id
    matches: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TrendAnimalsError("Trend Animals returned an invalid HK ETF root row")
        ticker_name = row.get("tickerName")
        if not isinstance(ticker_name, str) or not ticker_name.strip():
            raise TrendAnimalsError("Trend Animals returned an invalid HK ETF root row")
        try:
            _row_tm_id(row)
        except TrendAnimalsError as exc:
            raise TrendAnimalsError("Trend Animals returned an invalid HK ETF root row") from exc
        if ticker_name == HK_ETF_WARM_TO_HOT_NAME:
            matches.append(row)
    if not matches:
        return [], None
    if len(matches) != 1:
        raise TrendAnimalsError("HK ETF warm-to-hot pool is not unique")
    resolved_id = _row_tm_id(matches[0])
    try:
        resolved_rows = api.get_components(  # type: ignore[attr-defined]
            tm_id=resolved_id,
            expected_date=expected_date,
        )
    except TrendAnimalsNoCurrentRowsError:
        resolved_rows = []
    return list(resolved_rows), resolved_id


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
        receipt = read_delivery_receipt(
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
    receipt = read_delivery_receipt(receipt_path, artifact_stem=artifact_stem)
    if receipt is None:
        return None
    markdown_path = paths.reports / f"{artifact_stem}.md"
    json_path = paths.reports / f"{artifact_stem}.json"
    if receipt["status"] == "sent" and _final_pair_matches(
        receipt, markdown_path, json_path
    ):
        _write_frozen_industry_context_history(
            receipt=receipt,
            history_root=paths.root.parent / "trend_industry_context",
            market=market,
        )
        return AShareTrendRunResult("existing", markdown_path, json_path)
    if receipt["status"] in {"prepared", "pending", "delivery_failed"}:
        if receipt["status"] == "prepared":
            write_protection_state(
                paths.state, receipt["protection_state"]  # type: ignore[arg-type]
            )
            if isinstance(receipt.get("real_protection_state"), Mapping):
                write_protection_state(
                    paths.real_state,
                    receipt["real_protection_state"],  # type: ignore[arg-type]
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
    _write_frozen_industry_context_history(
        receipt=receipt,
        history_root=paths.root.parent / "trend_industry_context",
        market=market,
    )
    return AShareTrendRunResult("generated", markdown_path, json_path)


def _attempt_market_report(
    *,
    config: DailyPremarketConfig,
    market: str,
    run_date: str,
    revision: bool,
    notifier: Notifier,
    account_snapshot: Mapping[str, object],
    api_factory: Callable[..., object] = TrendAnimalsClient,
    quote_factory: Callable[..., object] = FutuQuoteClient,
    account_factory: Callable[..., object] | None = None,
    allocation_reference: Mapping[str, object] | None = None,
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
            return AShareTrendRunResult(
                "waiting", None, None,
                waiting_reason=update_status_gap(
                    update_rows, market=market, as_of_date=as_of_date
                ),
            )

        prior_state = rebuild_overheat_trim_projection(
            config.data_dir, market=market, state_path=paths.state
        )
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
        real_holdings = load_real_holding_input(
            account_snapshot,
            market,
            state_path=paths.real_state,
        )

        balance_before = _balance(api.get_account_balance())
        pool_ids = (
            config.trend_animals_us_tm_ids
            if market == "US"
            else config.trend_animals_hk_tm_ids
        )
        process_version = _process_version(config.repo)
        allocation_market = _allocation_market_for(allocation_reference, market)
        strategy_snapshot = live_trend_strategy_snapshot(
            market,
            process_version,
            pool_ids,
            execution_date=execution_date,
            allocation=allocation_reference,
        )
        strategy_version = str(strategy_snapshot["strategy_version"])
        individual_global_ranking = _uses_individual_global_ranking(
            market, strategy_version
        )
        shared_entry_discipline = _uses_shared_entry_discipline(
            market,
            strategy_version,
        )
        strategy_parameters = strategy_snapshot["parameters"]
        assert isinstance(strategy_parameters, Mapping)
        cny_per_local_currency = _decimal(
            strategy_parameters.get("cny_per_local_currency", "1")
        )
        component_rows: list[Mapping[str, object]] = []
        component_pools: defaultdict[int, set[str]] = defaultdict(set)
        pool_resolution_facts: list[str] = []
        extra_component_requests = 0
        for pool_id in pool_ids:
            rows, resolved_pool_id = _candidate_pool_components(
                api,
                market=market,
                pool_id=pool_id,
                expected_date=as_of_date,
            )
            if market == "HK" and pool_id == HK_ETF_ROOT_TM_ID:
                extra_component_requests += int(resolved_pool_id is not None)
                pool_resolution_facts.append(
                    "getComponentTicker configured_pool=707617 "
                    f"resolved_pool={resolved_pool_id or 'none'}"
                )
            component_rows.extend(rows)
            for row in rows:
                component_pools[_row_tm_id(row)].add(str(pool_id))
        component_ids = {_row_tm_id(row) for row in component_rows}
        get_favorites = getattr(api, "get_favorites_tickers", None)
        favorite_rows = get_favorites() if callable(get_favorites) else []
        favorite_ids = favorite_candidate_ids(favorite_rows, market=market)
        candidate_ids = component_ids | favorite_ids

        holding_ids: dict[str, int] = {}
        for position in account.positions:
            try:
                holding_ids[position.symbol] = api.search_exact_symbol(
                    position.symbol,
                    market=market,
                    expected_date=as_of_date,
                )
            except TrendAnimalsError:
                continue
        holding_snapshot_ids = sorted(set(holding_ids.values()))
        requested_ids = (
            holding_snapshot_ids
            if individual_global_ranking
            else sorted(candidate_ids | set(holding_ids.values()))
        )
        billing = {
            _billing_field(row): row for row in api.get_snapshot_billing()
        }
        requested_fields = tuple(
            dict.fromkeys(
                UNIFIED_TREND_FIELDS
                + (
                    A_SHARE_INDUSTRY_FIELDS
                    if shared_entry_discipline
                    else ()
                )
            )
        )
        missing = [field for field in requested_fields if field not in billing]
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
        if (
            sorted(returned_ids) != requested_ids
            or len(returned_ids) != len(set(returned_ids))
        ):
            raise ValueError("getTickerSnapshot returned mismatched tmIds")
        if any(row.get("asOfDate") != as_of_date for row in snapshot_rows):
            raise ValueError("getTickerSnapshot returned a stale data date")
        rows_by_id = {_row_tm_id(row): row for row in snapshot_rows}
        start = (date.fromisoformat(as_of_date) - timedelta(days=90)).isoformat()
        bars_by_symbol: dict[str, object] = {}
        industry_rows: list[Mapping[str, object]] = []
        industry_temperatures: dict[int, str | None] = {}
        candidates: Sequence[object] = ()
        industry_data_reason = ""
        if not individual_global_ranking and shared_entry_discipline:
            try:
                industry_ids = sorted(
                    {
                        industry_id
                        for row in snapshot_rows
                        if (industry_id := _optional_int(row.get("industryTmId")))
                        is not None and industry_id > 0
                    }
                )
                industry_rows, industry_temperatures = load_industry_temperatures(
                    api,
                    tm_ids=industry_ids,
                    expected_date=as_of_date,
                )
            except TrendAnimalsError as exc:
                industry_data_reason = f"行业温度数据不可用，暂停新开仓：{exc}"
        else:
            industry_ids = []
        if not individual_global_ranking:
            legacy_candidates = []
            for tm_id in sorted(candidate_ids):
                row = rows_by_id.get(tm_id)
                if row is None:
                    continue
                mapping_verified = False
                futu_symbol: str | None = None
                try:
                    futu_symbol = from_trend_animals_symbol(
                        market, str(row.get("tickerSymbol", ""))
                    )
                    bars = quote.get_daily_kline(
                        futu_symbol, start=start, end=as_of_date
                    )
                    try:
                        mapping_verified = _remember_verified_symbol_row(
                            api,
                            market=market,
                            expected_futu_symbol=futu_symbol,
                            expected_tm_id=tm_id,
                            row=row,
                        )
                    except TrendAnimalsError:
                        mapping_verified = False
                except FutuQuoteError as exc:
                    if _is_systemic_futu_error(exc):
                        raise
                    bars = None
                except ValueError:
                    bars = None
                legacy_candidates.append(
                    evaluate_candidate(
                        row,
                        bars,
                        pools=component_pools[tm_id],
                        market=market,
                        industry_temperature=industry_temperatures.get(
                            _optional_int(row.get("industryTmId"))
                        ),
                        futu_symbol=futu_symbol if mapping_verified else None,
                    )
                )
            candidates = tuple(legacy_candidates)
        holding_snapshots = {position.symbol: None for position in account.positions}
        for position in account.positions:
            try:
                bars_by_symbol[position.symbol] = quote.get_daily_kline(
                    to_futu_symbol(market, position.symbol),
                    start=start,
                    end=as_of_date,
                )
            except FutuQuoteError as exc:
                if _is_systemic_futu_error(exc):
                    raise
                bars_by_symbol[position.symbol] = None
            except ValueError:
                bars_by_symbol[position.symbol] = None
        for symbol, tm_id in holding_ids.items():
            row = rows_by_id.get(tm_id)
            bars = bars_by_symbol[symbol]
            if row is not None:
                try:
                    if from_trend_animals_symbol(
                        market, str(row.get("tickerSymbol") or "")
                    ) != to_futu_symbol(market, symbol):
                        continue
                    _remember_verified_symbol_row(
                        api,
                        market=market,
                        expected_futu_symbol=symbol,
                        expected_tm_id=tm_id,
                        row=row,
                        require_unmapped=True,
                    )
                    holding_snapshots[symbol] = _holding_snapshot(
                        row,
                        market=market,
                        industry_temperature=(
                            industry_temperatures.get(_optional_int(row.get("industryTmId")))
                            if not individual_global_ranking
                            else None
                        ),
                        bars=tuple(bars or ()),
                    )
                except ValueError:
                    pass

        real_holdings, real_snapshot_rows, real_bars_by_symbol, real_only_count = (
            enrich_real_holding_input(
                real_holdings,
                api=api,
                quote=quote,
                market=market,
                as_of_date=as_of_date,
                kline_start=start,
                existing_holding_ids=holding_ids,
                existing_rows_by_tm_id=rows_by_id,
                existing_holding_snapshots=holding_snapshots,
                existing_bars_by_symbol=bars_by_symbol,
            )
        )
        real_only_holding_ids = set(real_snapshot_rows)
        staged_candidate_ids = (
            candidate_ids
            - set(holding_ids.values())
            - real_only_holding_ids
        )
        staged_held_symbols = {position.symbol for position in account.positions}
        if real_holdings.status == "available":
            staged_held_symbols.update(
                position.symbol for position in real_holdings.positions
            )

        staged = None
        candidate_pool_rows: Sequence[Mapping[str, object]] = ()
        if individual_global_ranking:
            verified_candidate_symbols: dict[int, str] = {}

            def resolve_candidate_bars(
                row: Mapping[str, object],
            ) -> Sequence[DailyKlineBar] | None:
                tm_id = _row_tm_id(row)
                try:
                    futu_symbol = from_trend_animals_symbol(
                        market, str(row.get("tickerSymbol", ""))
                    )
                    bars = quote.get_daily_kline(
                        futu_symbol, start=start, end=as_of_date
                    )
                    try:
                        if _remember_verified_symbol_row(
                            api,
                            market=market,
                            expected_futu_symbol=futu_symbol,
                            expected_tm_id=tm_id,
                            row=row,
                        ):
                            verified_candidate_symbols[tm_id] = futu_symbol
                    except TrendAnimalsError:
                        pass
                    return bars
                except FutuQuoteError as exc:
                    if _is_systemic_futu_error(exc):
                        raise
                except ValueError:
                    pass
                return None

            staged = fetch_staged_candidates(
                api,
                candidate_ids=staged_candidate_ids,
                component_pools=component_pools,
                held_symbols=staged_held_symbols,
                holding_snapshots=(
                    *holding_snapshots.values(),
                    *(
                        real_holdings.holding_snapshots.values()
                        if real_holdings.status == "available"
                        else ()
                    ),
                ),
                expected_date=as_of_date,
                market=market,
                strategy_version=strategy_version,
                cny_per_local_currency=cny_per_local_currency,
                billing=billing,
                resolve_bars=resolve_candidate_bars,
            )
            industry_rows = list(staged.industry_rows)
            industry_temperatures = {
                _row_tm_id(row): (
                    str(row["trendTemperatureCurr"])
                    if row.get("trendTemperatureCurr") in INDUSTRY_KNOWN_TEMPERATURES
                    else None
                )
                for row in industry_rows
            }
            holding_snapshots = {
                symbol: (
                    replace(
                        snapshot,
                        industry_temperature=industry_temperatures.get(
                            snapshot.industry_tm_id
                        ),
                    )
                    if snapshot is not None
                    else None
                )
                for symbol, snapshot in holding_snapshots.items()
            }
            if real_holdings.status == "available":
                real_holdings = replace(
                    real_holdings,
                    holding_snapshots={
                        symbol: (
                            replace(
                                snapshot,
                                industry_temperature=industry_temperatures.get(
                                    snapshot.industry_tm_id
                                ),
                            )
                            if snapshot is not None
                            else None
                        )
                        for symbol, snapshot in real_holdings.holding_snapshots.items()
                    },
                )
            candidates = tuple(
                replace(
                    item,
                    futu_symbol=verified_candidate_symbols.get(item.tm_id),
                )
                for item in staged.candidates
            )
        else:
            candidate_pool_rows = tuple(
                rows_by_id[tm_id]
                for tm_id in sorted(candidate_ids)
                if tm_id in rows_by_id
            )
        industry_contexts, industry_context_status, industry_facts = (
            collect_industry_contexts(
                api=api,
                candidates=candidates,
                candidate_rows=candidate_pool_rows,
                held_symbols=(
                    staged_held_symbols
                    if individual_global_ranking
                    else {position.symbol for position in account.positions}
                ),
                holding_snapshots=(
                    *holding_snapshots.values(),
                    *(
                        real_holdings.holding_snapshots.values()
                        if real_holdings.status == "available"
                        else ()
                    ),
                ),
                expected_date=as_of_date,
                market=market,
                history_root=paths.root.parent / "trend_industry_context",
                strategy_version=strategy_version,
                cny_per_local_currency=cny_per_local_currency,
                industry_rows=(industry_rows if individual_global_ranking else None),
            )
        )
        balance_after = _balance(api.get_account_balance())

        lot_sizes: dict[str, int] = {}
        if market == "HK":
            symbols = sorted({
                *(to_futu_symbol("HK", item.symbol) for item in candidates),
                *(to_futu_symbol("HK", item.symbol) for item in account.positions),
            })
            wire_lots = quote.get_lot_sizes(symbols) if symbols else {}
            lot_sizes = {
                wire.split(".", 1)[1]: size for wire, size in wire_lots.items()
            }
        if individual_global_ranking:
            assert staged is not None
            estimated_cost = (
                unified_unit_cost * (len(holding_snapshot_ids) + real_only_count)
                + staged.estimated_cost
            )
        else:
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
        actual_cost = balance_before - balance_after
        cache_events = tuple(getattr(api, "paid_cache_events", ()))
        cache_metadata = {
            "hits": sum(event.get("cache") == "hit" for event in cache_events),
            "misses": sum(event.get("cache") == "miss" for event in cache_events),
            "events": [dict(event) for event in cache_events],
        }
        expected_component_requests = (
            len(pool_ids)
            + extra_component_requests
            + int(industry_facts["component_requests"])
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
            (staged.estimate_complete if staged is not None else industry_field_prices_complete)
            and len(component_events) == expected_component_requests
            and all(event.get("cache") == "hit" for event in component_events)
        )
        staged_api_facts = tuple(
            "getTickerSnapshot staged "
            f"fields={','.join(str(field) for field in trace['fields'])} "
            f"ids={','.join(str(tm_id) for tm_id in trace['tm_ids'])} "
            "cache=client-managed"
            for trace in (staged.request_trace if staged is not None else ())
        )
        legacy_industry_api_facts = (
            (
                f"getComponentTicker eligible_industries={industry_facts['component_requests']} "
                f"rows={industry_facts['component_rows']} cache=client-managed",
                f"getTickerSnapshot fields={','.join(INDUSTRY_MEMBER_FIELDS)} "
                f"ids={len(industry_facts['member_ids'])} rows={industry_facts['member_rows']} cache=client-managed",
                f"getTickerSnapshot fields={','.join(INDUSTRY_STATE_FIELDS)} "
                f"ids={len(industry_facts['state_ids'])} rows={industry_facts['state_rows']} cache=client-managed",
            )
            if not individual_global_ranking
            else ()
        )
        snapshot_api_facts = (
            (
                f"getTickerSnapshot holdings fields={','.join(UNIFIED_TREND_FIELDS)} rows={len(snapshot_rows)} cache=client-managed",
                *staged_api_facts,
            )
            if individual_global_ranking
            else (
                f"getTickerSnapshot fields={','.join(UNIFIED_TREND_FIELDS)} rows={len(snapshot_rows)} cache=client-managed",
                *(
                    (
                        f"getTickerSnapshot industries fields={','.join(A_SHARE_INDUSTRY_FIELDS)} "
                        f"rows={len(industry_rows)} cache=client-managed",
                    )
                    if shared_entry_discipline
                    else ()
                ),
                *((f"industry_data_reason={industry_data_reason}",) if industry_data_reason else ()),
            )
        )
        watch_events = load_watch_events(paths.events)
        kelly_evidence = load_trend_kelly_evidence(config.data_dir)
        kelly_rounds = kelly_evidence.rounds
        kelly_data_reason = (
            ""
            if kelly_evidence.status == "available"
            else f"Kelly 模拟闭环统计不可用，使用固定风险仓位：{kelly_evidence.reason}"
        )
        generated_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        drawdown_summary = observe_strategy_equity(
            config.data_dir,
            market=market,
            strategy_id=str(strategy_snapshot["strategy_id"]),
            strategy_version=str(strategy_snapshot["strategy_version"]),
            current_equity=account.net_value,
            observed_at=generated_at,
            entry_date=execution_date,
        )
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
                f"getFavoritesTicker securities={len(favorite_ids)}",
                *_component_api_facts(api, len(component_rows)),
                *pool_resolution_facts,
                *snapshot_api_facts,
                *legacy_industry_api_facts,
            ),
            data_sources=(
                "Trend Animals",
                f"Futu {market} calendar/QFQ daily K-line",
                f"Futu {market} SIMULATE account",
                f"{settings['broker']} frozen real account snapshot (read-only)",
            ),
            estimated_api_cost=estimated_cost,
            actual_api_cost=actual_cost if actual_cost >= 0 else None,
            generated_at=generated_at,
            market=market,
            lot_sizes=lot_sizes,
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
            price_fx_to_account_currency=Decimal("1"),
            process_version=process_version,
            candidate_pool_ids=pool_ids,
            strategy_snapshot=strategy_snapshot,
            drawdown_summary=drawdown_summary,
            industry_contexts=industry_contexts,
            industry_context_status=industry_context_status,
            estimated_api_cost_complete=estimate_complete,
            metadata={
                "market": market,
                "broker": settings["broker"],
                "simulate_acc_id": simulate_acc_id,
                "run_date": run_date,
                "process_version": process_version,
                "paid_response_cache": cache_metadata,
                "trend_statistics": {
                    "status": kelly_evidence.status,
                    "artifact_sha256": kelly_evidence.artifact_sha256,
                    "statistics_cutoff_at": kelly_evidence.statistics_cutoff_at,
                },
                **(
                    {"symbol_mapping_schema": TREND_SYMBOL_MAPPING_SCHEMA}
                    if _supports_symbol_mapping_contract(api)
                    else {}
                ),
                **(
                    {"industry_data_reason": industry_data_reason}
                    if shared_entry_discipline and not individual_global_ranking
                    else {}
                ),
                **(
                    {
                        "account_currency": "USD",
                        "price_fx_to_account_currency": "1",
                    }
                    if market == "US"
                    else {}
                ),
            },
            kelly_rounds=kelly_rounds,
            kelly_data_reason=kelly_data_reason,
            critical_data_reason=(
                industry_data_reason if not individual_global_ranking else ""
            ),
            real_holdings=real_holdings,
            allocation_reference=allocation_reference,
            account_input=_account_input(account_snapshot),
        )
        report = _finalize_market_report(report, managed_symbols=sorted(managed))
        report = freeze_report_rotation_pairs(report, config.data_dir)
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
                "favorite_ids": sorted(favorite_ids),
                **(
                    {
                        "holding_snapshot_fields": list(UNIFIED_TREND_FIELDS),
                        "staged_snapshot_requests": [
                            dict(trace) for trace in staged.request_trace
                        ],
                    }
                    if staged is not None
                    else {
                        "snapshot_fields": list(UNIFIED_TREND_FIELDS),
                        **(
                            {"industry_fields": list(A_SHARE_INDUSTRY_FIELDS)}
                            if shared_entry_discipline
                            else {}
                        ),
                    }
                ),
                **(
                    {
                        "industry_member_fields": list(INDUSTRY_MEMBER_FIELDS),
                        "industry_state_fields": list(INDUSTRY_STATE_FIELDS),
                    }
                    if not individual_global_ranking
                    else {}
                ),
            },
            responses={
                "update_status": update_rows,
                "components": component_rows,
                "favorites": favorite_rows,
                "snapshots": snapshot_rows,
                "real_snapshots": list(real_snapshot_rows.values()),
                **(
                    {"staged_candidates": candidates, "industries": industry_rows}
                    if staged is not None
                    else (
                        {"industries": industry_rows}
                        if shared_entry_discipline
                        else {}
                    )
                ),
                **(
                    {
                        "industry_components": [
                            row
                            for rows in industry_facts["component_rows_by_industry"].values()
                            for row in rows
                        ],
                        "industry_members": industry_facts["member_response"],
                        "industry_states": industry_facts["state_response"],
                    }
                    if not individual_global_ranking
                    else {}
                ),
            },
            candidate_pool_ids=pool_ids,
            lot_sizes=lot_sizes,
            price_fx_to_account_currency=Decimal("1"),
            previous_attention_rows=previous_attention_rows,
            option_attention_broker_label=option_attention_broker_label,
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
            real_protection_state=report.real_protection_state,
        )
        write_protection_state(paths.state, report.protection_state)
        if report.real_protection_state is not None:
            write_protection_state(paths.real_state, report.real_protection_state)
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
        _write_frozen_industry_context_history(
            receipt=receipt,
            history_root=paths.root.parent / "trend_industry_context",
            market=market,
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
    allocation_reference: Mapping[str, object] | None = None,
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
        report_dependencies = dict(attempt_dependencies)
        report_dependencies["account_snapshot"] = fetch_account_snapshot()
        if allocation_reference is not None:
            report_dependencies["allocation_reference"] = allocation_reference
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
            attempt_dependencies=report_dependencies,
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
    waiting_gap: str | None = None
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
            if result.status == "waiting" and result.waiting_reason:
                waiting_gap = result.waiting_reason
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
            return AShareTrendRunResult(
                "failed", None, None,
                waiting_reason=waiting_gap or last_error,
            )
        sleep_fn(min(600.0, max(1.0, (deadline - now).total_seconds())))
