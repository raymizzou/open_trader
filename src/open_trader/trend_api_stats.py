from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .kelly_order_execution import (
    FutuSimulateOrderExecutionClient,
)
from .kelly_trade_samples import _write_json_atomic
from .tiger_account import TigerAccountClient, TigerAccountError
from .trend_review import _report_hash
from .trend_simulate_positions import _action_events


TREND_API_STATS_SCHEMA_VERSION = "open_trader.trend_api_stats.v1"
_CALCULATION_CONTEXT = Context(prec=28)
_MARKET_TIMEZONES = {
    "CN": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}
_REPORT_DIRECTORIES = {
    "CN": "trend_a_share",
    "HK": "trend_hk_phillips",
    "US": "trend_us_tiger",
}
_SOURCE_MARKETS = {
    ("simulation", "futu"): {"CN", "HK", "US"},
    ("actual", "tiger"): {"US"},
    ("actual", "eastmoney"): {"CN"},
    ("actual", "phillips"): {"HK"},
}
_STATEMENT_ACCOUNTS = {
    "eastmoney": ("eastmoney_main", "CN"),
    "phillips": ("phillips_main", "HK"),
}


class FutuSimulateFillClient(FutuSimulateOrderExecutionClient):
    def fetch_fills(
        self,
        *,
        start: str,
        end: str,
        attributions_by_order: Mapping[str, Mapping[str, object]],
    ) -> dict[str, object]:
        orders = _deduplicate_records(
            self.list_orders(start=start, end=end)["orders"], "order_id"
        )
        fills = []
        for order in orders:
            dealt_quantity = _required_decimal(
                order.get("dealt_qty"), "Futu order dealt quantity"
            )
            if dealt_quantity < 0:
                raise ValueError("Futu order dealt quantity must be non-negative")
            if dealt_quantity == 0:
                continue
            dealt_price = _required_decimal(
                order.get("dealt_avg_price"), "Futu order dealt average price"
            )
            if dealt_price <= 0:
                raise ValueError("Futu dealt average price must be positive")
            order_id = str(order["order_id"]).strip()
            fills.append(_normalized_futu_fill(
                {
                    "deal_id": f"futu-sim-order:{order_id}:aggregate",
                    "broker_fill_id": None,
                    "execution_granularity": "order_aggregate",
                    "order_id": order_id,
                    "code": order.get("code"),
                    "trd_side": order.get("trd_side"),
                    "qty": order.get("dealt_qty"),
                    "price": order.get("dealt_avg_price"),
                    "create_time": order.get("updated_time") or order.get("create_time"),
                },
                order=order,
                account_id=str(self.account["acc_id"]),
                market=self.trd_market,
                attribution=attributions_by_order.get(order_id),
            ))
        account_id = str(self.account["acc_id"])
        return {
            "source_id": f"simulation:futu:{account_id}",
            "account_id": account_id,
            "market": self.trd_market,
            "orders_seen": len(orders),
            "fills": fills,
        }


class TigerActualFillClient(TigerAccountClient):
    def fetch_fills(
        self,
        *,
        start: str,
        end: str,
        attributions_by_order: Mapping[str, Mapping[str, object]],
    ) -> dict[str, object]:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        if end_date < start_date:
            raise ValueError("Tiger fill end date must not precede start date")
        orders: list[object] = []
        token = ""
        seen_tokens: set[str] = set()
        while token not in seen_tokens:
            seen_tokens.add(token)
            page = _tiger_api_call(
                "orders",
                lambda: self.trade_client.get_orders(
                    account=self.config.account,
                    sec_type="STK",
                    market="US",
                    start_time=start,
                    end_time=(end_date + timedelta(days=1)).isoformat(),
                    limit=100,
                    page_token=token,
                ),
            )
            if page is None:
                break
            orders.extend(list(getattr(page, "result", [])))
            next_token = str(getattr(page, "next_page_token", None) or "")
            if not next_token:
                break
            token = next_token

        order_ids = sorted({
            int(getattr(order, "id"))
            for order in orders
            if _required_decimal(
                getattr(order, "filled", None), "Tiger order filled quantity"
            ) > 0
        })
        transactions: list[object] = []
        for order_id in order_ids:
            token = ""
            seen_tokens = set()
            while token not in seen_tokens:
                seen_tokens.add(token)
                page = _tiger_api_call(
                    "order transactions",
                    lambda: self.trade_client.get_transactions(
                        account=self.config.account,
                        order_id=order_id,
                        limit=100,
                        page_token=token,
                    ),
                )
                if page is None:
                    break
                transactions.extend(list(getattr(page, "result", [])))
                next_token = str(getattr(page, "next_page_token", None) or "")
                if not next_token:
                    break
                token = next_token

        raw_fills = _deduplicate_records(
            [_tiger_transaction_record(item) for item in transactions], "fill_id"
        )
        by_order: dict[str, list[dict[str, object]]] = defaultdict(list)
        for fill in raw_fills:
            by_order[str(fill["order_id"])].append(fill)
        normalized: list[dict[str, object]] = []
        for order_id, fills in sorted(by_order.items()):
            order = _tiger_api_call(
                "order costs",
                lambda: self.trade_client.get_order(
                    account=self.config.account,
                    id=int(order_id),
                    show_charges=True,
                ),
            )
            fee = _tiger_order_fee(order)
            allocated = _allocate_order_fee(fills, fee)
            for fill, fill_fee in zip(fills, allocated, strict=True):
                attribution = dict(attributions_by_order.get(order_id) or {})
                attributed = bool(
                    attribution.get("strategy_id")
                    and attribution.get("strategy_version")
                )
                normalized.append({
                    **fill,
                    "source": "actual",
                    "source_id": f"actual:tiger:{self.config.account}",
                    "broker": "tiger",
                    "account_id": self.config.account,
                    "fee": _decimal_text(fill_fee) if fill_fee is not None else "0",
                    "costs_complete": fill_fee is not None,
                    "cost_source": "broker_actual" if fill_fee is not None else "unavailable",
                    "strategy_id": str(attribution.get("strategy_id") or ""),
                    "strategy_version": str(attribution.get("strategy_version") or ""),
                    "report_sha256": str(attribution.get("report_sha256") or ""),
                    "attribution_status": "attributed" if attributed else "outside_strategy",
                    "exclusion_reason": "" if attributed else "order_not_linked_to_frozen_strategy",
                })
        normalized.sort(
            key=lambda fill: (
                _aware_timestamp(fill["filled_at"], "fill filled_at"),
                str(fill["fill_id"]),
            )
        )
        normalized = [
            fill for fill in normalized
            if start_date <= (
                _aware_timestamp(fill["filled_at"], "fill filled_at")
                .astimezone(_MARKET_TIMEZONES[str(fill["market"])])
                .date()
            ) <= end_date
        ]
        return {
            "source_id": f"actual:tiger:{self.config.account}",
            "account_id": self.config.account,
            "market": "US",
            "orders_seen": len(order_ids),
            "fills": normalized,
        }


def _tiger_api_call(operation: str, call: Any) -> object:
    try:
        return call()
    except TigerAccountError:
        raise
    except Exception as exc:
        raise TigerAccountError(
            f"failed to query Tiger {operation}",
            error_type=f"{operation.replace(' ', '_')}_query_failed",
        ) from exc


def _tiger_transaction_record(transaction: object) -> dict[str, object]:
    contract = getattr(transaction, "contract", None)
    market = str(getattr(contract, "market", "") or "").strip().upper()
    if market not in _MARKET_TIMEZONES:
        raise ValueError("Tiger fill market is invalid")
    side = str(getattr(transaction, "action", "") or "").strip().upper()
    side = {"BUY": "buy", "SELL": "sell"}.get(side, "")
    if not side:
        raise ValueError("Tiger fill side is invalid")
    return {
        "fill_id": str(getattr(transaction, "id", "") or "").strip(),
        "order_id": str(getattr(transaction, "order_id", "") or "").strip(),
        "market": market,
        "symbol": str(getattr(contract, "symbol", "") or "").strip().upper(),
        "currency": str(getattr(contract, "currency", "") or _market_currency(market)).strip().upper(),
        "side": side,
        "quantity": str(getattr(transaction, "filled_quantity", "") or "").strip(),
        "price": str(getattr(transaction, "filled_price", "") or "").strip(),
        "filled_at": _tiger_timestamp(
            getattr(transaction, "transacted_at", None), market=market
        ),
    }


def _tiger_timestamp(value: object, *, market: str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value) / 1000, UTC).isoformat()
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("Tiger fill timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_MARKET_TIMEZONES[market])
    return parsed.isoformat()


def _tiger_order_fee(order: object) -> Decimal | None:
    gst = _optional_nonnegative_decimal(getattr(order, "gst", None)) or Decimal("0")
    commission = getattr(order, "commission", None)
    if commission is not None and str(commission).strip() != "":
        # Tiger defines commission as the aggregate commission/tax/regulatory fee;
        # charges is its optional breakdown, while GST is a separate amount.
        fee = _required_decimal(commission, "Tiger order commission")
        if fee < 0:
            raise ValueError("Tiger order commission must be non-negative")
        with localcontext(_CALCULATION_CONTEXT):
            return fee + gst
    charges = getattr(order, "charges", None)
    if not isinstance(charges, list) or not charges:
        return None
    with localcontext(_CALCULATION_CONTEXT):
        fee = sum(
            (_required_decimal(getattr(charge, "total", None), "Tiger order charge") for charge in charges),
            Decimal("0"),
        )
        return fee + gst


def _allocate_order_fee(
    fills: Sequence[Mapping[str, object]], fee: Decimal | None
) -> list[Decimal | None]:
    if fee is None:
        return [None] * len(fills)
    with localcontext(_CALCULATION_CONTEXT):
        notionals = [
            _required_decimal(fill["quantity"], "Tiger fill quantity")
            * _required_decimal(fill["price"], "Tiger fill price")
            for fill in fills
        ]
        total = sum(notionals, Decimal("0"))
        if total <= 0:
            raise ValueError("Tiger order fill notional must be positive")
        allocated = [fee * notional / total for notional in notionals[:-1]]
        return [*allocated, fee - sum(allocated, Decimal("0"))]


def _deduplicate_records(
    records: Sequence[Mapping[str, object]], identity_field: str
) -> list[dict[str, object]]:
    unique: dict[str, dict[str, object]] = {}
    for raw in records:
        record = dict(raw)
        identity = str(record.get(identity_field) or "").strip()
        if not identity:
            raise ValueError(f"broker record {identity_field} is required")
        if identity in unique and unique[identity] != record:
            raise ValueError(f"conflicting duplicate broker {identity_field}: {identity}")
        unique[identity] = record
    return [unique[key] for key in sorted(unique)]


def _normalized_futu_fill(
    deal: Mapping[str, object],
    *,
    order: Mapping[str, object],
    account_id: str,
    market: str,
    attribution: Mapping[str, object] | None,
) -> dict[str, object]:
    code = str(deal.get("code") or order.get("code") or "").strip().upper()
    normalized_market = market.strip().upper()
    prefix, separator, symbol = code.partition(".")
    market_prefixes = {
        "CN": {"SH", "SZ", "BJ"},
        "HK": {"HK"},
        "US": {"US"},
    }
    if (
        not separator
        or prefix not in market_prefixes.get(normalized_market, set())
        or not symbol
    ):
        raise ValueError(f"Futu fill code {code!r} does not belong to {normalized_market}")
    side = str(deal.get("trd_side") or order.get("trd_side") or "").strip().upper()
    side = {"BUY": "buy", "SELL": "sell"}.get(side, "")
    if not side:
        raise ValueError("Futu fill side is invalid")
    fact = dict(attribution or {})
    attributed = bool(fact.get("strategy_id") and fact.get("strategy_version"))
    attribution_status = str(fact.get("attribution_status") or "").strip()
    if attribution_status not in {"attributed", "ambiguous", "outside_strategy"}:
        attribution_status = "attributed" if attributed else "outside_strategy"
    return {
        "fill_id": str(deal.get("deal_id") or "").strip(),
        "broker_fill_id": deal.get("broker_fill_id"),
        "execution_granularity": str(
            deal.get("execution_granularity") or "broker_fill"
        ),
        "order_id": str(deal.get("order_id") or "").strip(),
        "source": "simulation",
        "source_id": f"simulation:futu:{account_id}",
        "broker": "futu",
        "account_id": account_id,
        "market": normalized_market,
        "symbol": symbol,
        "currency": str(order.get("currency") or _market_currency(normalized_market)).strip().upper(),
        "side": side,
        "quantity": str(deal.get("qty") or "").strip(),
        "price": str(deal.get("price") or "").strip(),
        "fee": "0",
        "costs_complete": False,
        "broker_fee_used": False,
        "filled_at": _broker_timestamp(deal.get("create_time"), normalized_market),
        "strategy_id": str(fact.get("strategy_id") or "").strip(),
        "strategy_version": str(fact.get("strategy_version") or "").strip(),
        "report_sha256": str(fact.get("report_sha256") or "").strip(),
        "normal_cost_rate": str(fact.get("normal_cost_rate") or "").strip(),
        "normal_cost_model": str(fact.get("normal_cost_model") or "").strip(),
        "attribution_status": attribution_status,
        "exclusion_reason": (
            "" if attribution_status == "attributed"
            else str(fact.get("exclusion_reason") or "order_not_linked_to_frozen_strategy")
        ),
    }


def _broker_timestamp(value: object, market: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("broker fill timestamp is invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_MARKET_TIMEZONES[market])
    return parsed.isoformat()


def _market_currency(market: str) -> str:
    return {"CN": "CNY", "HK": "HKD", "US": "USD"}[market]


def strategy_payoff_ratio(
    profitable_returns: Sequence[object],
    losing_returns: Sequence[object],
) -> tuple[str | None, str]:
    wins = [_required_decimal(value, "profitable round return") for value in profitable_returns]
    losses = [_required_decimal(value, "losing round return") for value in losing_returns]
    if any(value <= 0 for value in wins):
        raise ValueError("profitable round return must be positive")
    if any(value > 0 for value in losses):
        raise ValueError("losing round return must be non-positive")
    if not wins:
        return None, "no_wins"
    if not losses:
        return None, "no_losses"
    average_loss = abs(_average(losses))
    if average_loss == 0:
        return None, "zero_denominator"
    return _decimal_text(_divide(_average(wins), average_loss)), "available"


def eligible_simulation_rounds(
    payload: object,
    *,
    market: str,
    strategy_id: str,
    opening_strategy_version: str | None = None,
) -> list[dict[str, object]]:
    validated = _validated_payload(payload)
    normalized_market = _required_text(market, "market").upper()
    normalized_strategy_id = _required_text(strategy_id, "strategy_id")
    return [
        dict(round_)
        for round_ in validated["rounds"]
        if round_["source"] == "simulation"
        and round_["kelly_eligible"] is True
        and round_["costs_complete"] is True
        and round_["attribution_status"] == "attributed"
        and round_["market"] == normalized_market
        and round_["strategy_id"] == normalized_strategy_id
        and (
            opening_strategy_version is None
            or round_["opening_strategy_version"] == opening_strategy_version
        )
    ]


def sync_trend_api_stats(
    *,
    data_dir: Any,
    reports_dir: Any,
    futu_clients: Mapping[str, object],
    tiger_client: object,
    start: str,
    end: str,
    generated_at: str,
    statistics_cutoff_at: str,
) -> dict[str, object]:
    data_dir = _path(data_dir)
    reports_dir = _path(reports_dir)
    facts = _load_frozen_strategy_facts(reports_dir)
    futu_attributions = _futu_order_attributions(data_dir, facts)
    fetched_fills: list[Mapping[str, object]] = []
    sources: list[dict[str, object]] = []
    for market, client in sorted(futu_clients.items()):
        result = client.fetch_fills(
            start=start,
            end=end,
            attributions_by_order=futu_attributions.get(market.upper(), {}),
        )
        fills = _result_fills(result)
        fetched_fills.extend(fills)
        account_id = str(
            result.get("account_id")
            or (fills[0].get("account_id") if fills else "")
        )
        source_id = str(result.get("source_id") or f"simulation:futu:{account_id}")
        sources.append({
            "source": "simulation",
            "source_id": source_id,
            "broker": "futu",
            "account_id": account_id,
            "market": market.upper(),
            "orders_seen": int(result.get("orders_seen", 0)),
            "fill_count": len(fills),
            "statistics_cutoff_at": statistics_cutoff_at,
            "status": "available",
        })
    tiger_result = tiger_client.fetch_fills(
        start=start,
        end=end,
        attributions_by_order={},
    )
    tiger_fills = _attribute_actual_fills(_result_fills(tiger_result), facts)
    fetched_fills.extend(tiger_fills)
    tiger_account = str(
        tiger_result.get("account_id")
        or (tiger_fills[0].get("account_id") if tiger_fills else "")
    )
    tiger_source_id = str(
        tiger_result.get("source_id") or f"actual:tiger:{tiger_account}"
    )
    sources.append({
        "source": "actual",
        "source_id": tiger_source_id,
        "broker": "tiger",
        "account_id": tiger_account,
        "market": "US",
        "orders_seen": int(tiger_result.get("orders_seen", 0)),
        "fill_count": len(tiger_fills),
        "statistics_cutoff_at": statistics_cutoff_at,
        "status": "available",
    })
    path = data_dir / "latest" / "trend_api_stats.json"
    existing_fills: list[Mapping[str, object]] = []
    existing_sources: list[Mapping[str, object]] = []
    if path.exists():
        existing = load_trend_api_stats(data_dir)
        existing_fills = list(existing["fills"])
        existing_sources = list(existing["sources"])
    strategy_versions = sorted({
        (str(fact["market"]), str(fact["strategy_id"]), str(fact["strategy_version"]))
        for fact in facts
    })
    payload = build_trend_api_stats_payload(
        _merge_synced_fills(existing_fills, fetched_fills),
        strategy_versions=[
            {"market": market, "strategy_id": strategy_id, "strategy_version": version}
            for market, strategy_id, version in strategy_versions
        ],
        generated_at=generated_at,
        statistics_cutoff_at=statistics_cutoff_at,
    )
    payload["sources"] = _merge_source_audits(existing_sources, sources)
    write_trend_api_stats(data_dir, payload)
    return payload


def build_statement_actual_stats_payload(
    *,
    data_dir: Any,
    reports_dir: Any,
    broker: str,
    statement_period: str,
    fills: Sequence[Mapping[str, object]],
    generated_at: str,
    statistics_cutoff_at: str,
) -> dict[str, object]:
    normalized_broker = broker.strip().lower()
    if normalized_broker not in _STATEMENT_ACCOUNTS:
        raise ValueError(f"unsupported statement stats broker: {broker}")
    if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", statement_period) is None:
        raise ValueError("statement_period is invalid")
    account_id, market = _STATEMENT_ACCOUNTS[normalized_broker]
    incoming = [dict(fill) for fill in fills]
    if any(
        fill.get("source") != "actual"
        or fill.get("broker") != normalized_broker
        or fill.get("account_id") != account_id
        or fill.get("market") != market
        or fill.get("statement_period") != statement_period
        for fill in incoming
    ):
        raise ValueError("statement fill source facts are inconsistent")

    path = _path(data_dir) / "latest" / "trend_api_stats.json"
    existing: dict[str, object] | None = None
    if path.exists():
        existing = load_trend_api_stats(data_dir)
    incoming_identities = {
        (str(fill.get("source_id")), str(fill.get("fill_id"))) for fill in incoming
    }
    retained = [
        dict(fill)
        for fill in (existing["fills"] if existing is not None else [])
        if not (
            fill["broker"] == normalized_broker
            and fill.get("statement_period") == statement_period
        )
        and (str(fill["source_id"]), str(fill["fill_id"])) not in incoming_identities
    ]
    combined = [*retained, *incoming]
    facts = _load_frozen_strategy_facts(reports_dir)
    combined = _reattribute_statement_fills(combined, facts)

    cutoff_values = [statistics_cutoff_at]
    if existing is not None:
        cutoff_values.append(str(existing["statistics_cutoff_at"]))
    artifact_cutoff = max(
        cutoff_values,
        key=lambda value: _aware_timestamp(value, "statistics_cutoff_at"),
    )
    existing_versions = {
        (
            str(stat["market"]),
            str(stat["strategy_id"]),
            str(stat["opening_strategy_version"]),
        )
        for stat in (existing["stats"] if existing is not None else [])
    }
    strategy_versions = existing_versions | {
        (str(fact["market"]), str(fact["strategy_id"]), str(fact["strategy_version"]))
        for fact in facts
    }
    payload = build_trend_api_stats_payload(
        combined,
        strategy_versions=[
            {"market": item[0], "strategy_id": item[1], "strategy_version": item[2]}
            for item in sorted(strategy_versions)
        ],
        generated_at=generated_at,
        statistics_cutoff_at=artifact_cutoff,
    )
    statement_source_id = f"actual:{normalized_broker}:{account_id}"
    broker_fills = [
        fill
        for fill in payload["fills"]
        if fill["source_id"] == statement_source_id and fill["market"] == market
    ]
    source = {
        "source": "actual",
        "source_id": statement_source_id,
        "broker": normalized_broker,
        "account_id": account_id,
        "market": market,
        "orders_seen": len(broker_fills),
        "fill_count": len(broker_fills),
        "statistics_cutoff_at": statistics_cutoff_at,
        "status": "available",
    }
    payload["sources"] = _merge_source_audits(
        existing["sources"] if existing is not None else [],
        [source],
    )
    return _validated_payload(payload)


def _reattribute_statement_fills(
    fills: Sequence[Mapping[str, object]],
    facts: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    statement_brokers = set(_STATEMENT_ACCOUNTS)
    other: list[dict[str, object]] = []
    statement: list[dict[str, object]] = []
    for raw in fills:
        fill = dict(raw)
        if fill.get("broker") not in statement_brokers:
            other.append(fill)
            continue
        fill.update(
            strategy_id="",
            strategy_version="",
            report_sha256="",
            attribution_status="outside_strategy",
            exclusion_reason="no_matching_opening_strategy_action",
        )
        statement.append(fill)
    attributed = _attribute_actual_fills(statement, facts)
    same_day_sides: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for fill in attributed:
        market = str(fill["market"])
        local_date = _aware_timestamp(
            fill["filled_at"], "fill filled_at"
        ).astimezone(_MARKET_TIMEZONES[market]).date().isoformat()
        same_day_sides[(
            str(fill["source_id"]), market, str(fill["symbol"]), local_date
        )].add(str(fill["side"]))
    for fill in attributed:
        market = str(fill["market"])
        local_date = _aware_timestamp(
            fill["filled_at"], "fill filled_at"
        ).astimezone(_MARKET_TIMEZONES[market]).date().isoformat()
        key = (str(fill["source_id"]), market, str(fill["symbol"]), local_date)
        if len(same_day_sides[key]) > 1:
            fill.update(
                strategy_id="",
                strategy_version="",
                report_sha256="",
                attribution_status="ambiguous",
                exclusion_reason="statement_trade_time_unavailable",
            )
    return [*other, *attributed]


def _merge_source_audits(
    existing: Sequence[Mapping[str, object]],
    incoming: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    merged = {
        (str(source["source_id"]), str(source["market"])): dict(source)
        for source in existing
    }
    for source in incoming:
        merged[(str(source["source_id"]), str(source["market"]))] = dict(source)
    return [merged[key] for key in sorted(merged)]


def _merge_synced_fills(
    existing_fills: Sequence[Mapping[str, object]],
    fetched_fills: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    merged = {
        (str(fill["source_id"]), str(fill["fill_id"])): fill
        for fill in _deduplicate_fills(existing_fills)
    }
    for incoming in _deduplicate_fills(fetched_fills):
        identity = (str(incoming["source_id"]), str(incoming["fill_id"]))
        existing = merged.get(identity)
        if existing is None or existing == incoming:
            merged[identity] = incoming
            continue
        aggregate = (
            existing.get("source") == incoming.get("source") == "simulation"
            and existing.get("broker") == incoming.get("broker") == "futu"
            and existing.get("execution_granularity")
            == incoming.get("execution_granularity")
            == "order_aggregate"
        )
        if not aggregate:
            raise ValueError(
                f"conflicting duplicate fill: {identity[0]}:{identity[1]}"
            )
        immutable_fields = (
            "order_id", "source", "source_id", "broker", "account_id",
            "market", "symbol", "currency", "side",
        )
        if any(existing[field] != incoming[field] for field in immutable_fields):
            raise ValueError("Futu aggregate snapshot source facts changed")
        old_quantity = _required_decimal(
            existing["quantity"], "existing aggregate quantity"
        )
        new_quantity = _required_decimal(
            incoming["quantity"], "incoming aggregate quantity"
        )
        old_price = _required_decimal(
            existing["price"], "existing aggregate average price"
        )
        new_price = _required_decimal(
            incoming["price"], "incoming aggregate average price"
        )
        old_time = _aware_timestamp(existing["filled_at"], "existing aggregate time")
        new_time = _aware_timestamp(incoming["filled_at"], "incoming aggregate time")
        if new_quantity < old_quantity or new_time < old_time:
            raise ValueError("Futu aggregate snapshot regressed")
        if new_quantity == old_quantity and new_price != old_price:
            raise ValueError("Futu aggregate average price changed without new fills")
        if new_time == old_time and (
            new_quantity != old_quantity
            or new_price != old_price
        ):
            raise ValueError("conflicting duplicate fill: Futu aggregate snapshot")
        if (
            existing["attribution_status"] == "attributed"
            and (
                incoming["attribution_status"] != "attributed"
                or any(
                    existing.get(field) != incoming.get(field)
                    for field in (
                        "strategy_id", "strategy_version", "report_sha256",
                        "normal_cost_rate", "normal_cost_model",
                    )
                )
            )
        ):
            raise ValueError("Futu aggregate snapshot attribution changed")
        merged[identity] = incoming
    return _deduplicate_fills(list(merged.values()))


def write_trend_api_stats(data_dir: Any, payload: object) -> Any:
    data_dir = _path(data_dir)
    validated = _validated_payload(payload)
    path = data_dir / "latest" / "trend_api_stats.json"
    _write_json_atomic(path, validated)
    return path


def load_trend_api_stats(data_dir: Any) -> dict[str, object]:
    path = _path(data_dir) / "latest" / "trend_api_stats.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("trend_api_stats.json is missing") from None
    except (OSError, UnicodeError):
        raise ValueError("trend_api_stats.json is unreadable") from None
    except json.JSONDecodeError:
        raise ValueError("trend_api_stats.json is invalid JSON") from None
    return _validated_payload(payload)


def _load_frozen_strategy_facts(reports_dir: Any) -> list[dict[str, object]]:
    reports_dir = _path(reports_dir)
    facts: list[dict[str, object]] = []
    for market, directory in _REPORT_DIRECTORIES.items():
        for path in sorted((reports_dir / directory).glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ValueError(f"frozen trend report is unreadable: {path}") from None
            if not isinstance(payload, Mapping):
                raise ValueError(f"frozen trend report is invalid: {path}")
            metadata = payload.get("metadata")
            snapshot = payload.get("strategy_snapshot")
            judgments = payload.get("strategy_judgments")
            if not isinstance(snapshot, Mapping):
                continue
            strategy_id = str(snapshot.get("strategy_id") or "").strip()
            strategy_version = str(snapshot.get("strategy_version") or "").strip()
            if not strategy_id and not strategy_version:
                continue
            if not (
                isinstance(metadata, Mapping)
                and str(metadata.get("market") or "").strip().upper() == market
                and strategy_id
                and strategy_version
                and isinstance(judgments, Mapping)
                and isinstance(judgments.get("formal_actions"), list)
            ):
                raise ValueError(f"frozen trend report strategy facts are invalid: {path}")
            parameters = snapshot.get("parameters")
            parameters = parameters if isinstance(parameters, Mapping) else {}
            facts.append({
                "market": market,
                "execution_date": _required_text(payload.get("execution_date"), "execution_date"),
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "report_sha256": _report_hash(dict(payload)),
                "normal_cost_rate": str(parameters.get("normal_cost_rate") or "").strip(),
                "normal_cost_model": str(parameters.get("normal_cost_model") or "").strip(),
                "formal_actions": judgments["formal_actions"],
            })
    return facts


def _futu_order_attributions(
    data_dir: Any,
    facts: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, dict[str, object]]]:
    fact_index = {
        (str(fact["report_sha256"]), str(fact["strategy_version"])): fact
        for fact in facts
    }
    by_market: dict[str, dict[str, set[tuple[str, str]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for market in _REPORT_DIRECTORIES:
        for _, _, _, event in _action_events(_path(data_dir), market):
            identity = (
                str(event.get("report_sha256") or "").strip().lower(),
                str(event.get("strategy_version") or "").strip(),
            )
            for order_id in event.get("order_ids", []):
                if str(order_id).strip():
                    by_market[market][str(order_id).strip()].add(identity)
    result: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for market, orders in by_market.items():
        for order_id, identities in orders.items():
            matched = {identity for identity in identities if identity in fact_index}
            if len(matched) == 1 and matched == identities:
                fact = fact_index[next(iter(matched))]
                result[market][order_id] = {
                    key: fact[key]
                    for key in (
                        "strategy_id", "strategy_version", "report_sha256",
                        "normal_cost_rate", "normal_cost_model",
                    )
                } | {"attribution_status": "attributed", "exclusion_reason": ""}
            else:
                result[market][order_id] = {
                    "attribution_status": "ambiguous",
                    "exclusion_reason": "ambiguous_frozen_report_attribution",
                }
    return {market: dict(orders) for market, orders in result.items()}


def _attribute_actual_fills(
    fills: Sequence[Mapping[str, object]],
    facts: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    actions: dict[tuple[str, str, str, str], list[Mapping[str, object]]] = defaultdict(list)
    for fact in facts:
        for action in fact["formal_actions"]:
            if not isinstance(action, Mapping):
                continue
            side = {"BUY": "buy", "SELL_ALL": "sell"}.get(str(action.get("action") or ""))
            symbol = str(action.get("symbol") or "").strip().upper()
            if side and symbol:
                actions[(str(fact["market"]), str(fact["execution_date"]), symbol, side)].append(fact)
    attributed: list[dict[str, object]] = []
    for raw in fills:
        fill = dict(raw)
        market = str(fill.get("market") or "").upper()
        filled_at = _aware_timestamp(fill.get("filled_at"), "fill filled_at")
        local_date = filled_at.astimezone(_MARKET_TIMEZONES[market]).date().isoformat()
        candidates = {
            (
                str(fact["strategy_id"]),
                str(fact["strategy_version"]),
                str(fact["report_sha256"]),
            ): fact
            for fact in actions.get((market, local_date, str(fill.get("symbol") or "").upper(), str(fill.get("side") or "")), [])
        }
        if len(candidates) == 1:
            fact = next(iter(candidates.values()))
            fill.update({
                "strategy_id": fact["strategy_id"],
                "strategy_version": fact["strategy_version"],
                "report_sha256": fact["report_sha256"],
                "attribution_status": "attributed",
                "exclusion_reason": "",
            })
        elif len(candidates) > 1:
            fill.update({
                "strategy_id": "",
                "strategy_version": "",
                "attribution_status": "ambiguous",
                "exclusion_reason": "multiple_opening_strategy_matches",
            })
        attributed.append(fill)
    return attributed


def _result_fills(result: object) -> list[dict[str, object]]:
    if not isinstance(result, Mapping) or not isinstance(result.get("fills"), list):
        raise ValueError("broker fill result is invalid")
    return [dict(fill) for fill in result["fills"] if isinstance(fill, Mapping)]


def _path(value: object) -> Path:
    return value if isinstance(value, Path) else Path(value)


def build_trend_api_stats_payload(
    fills: Sequence[Mapping[str, object]],
    *,
    strategy_versions: Sequence[Mapping[str, object]],
    generated_at: str,
    statistics_cutoff_at: str,
) -> dict[str, object]:
    generated_timestamp = _aware_timestamp(generated_at, "generated_at")
    cutoff_timestamp = _aware_timestamp(
        statistics_cutoff_at, "statistics_cutoff_at"
    )
    if generated_timestamp < cutoff_timestamp:
        raise ValueError("generated_at must not precede statistics_cutoff_at")
    normalized = _deduplicate_fills(fills)
    if any(
        _aware_timestamp(fill["filled_at"], "fill filled_at") > cutoff_timestamp
        for fill in normalized
    ):
        raise ValueError("fill filled_at exceeds statistics_cutoff_at")
    rounds = _closed_rounds(normalized)
    stats = _strategy_stats(
        rounds,
        strategy_versions=strategy_versions,
        statistics_cutoff_at=statistics_cutoff_at,
    )
    return {
        "schema_version": TREND_API_STATS_SCHEMA_VERSION,
        "generated_at": generated_at,
        "statistics_cutoff_at": statistics_cutoff_at,
        "sources": [],
        "fills": normalized,
        "rounds": rounds,
        "stats": stats,
    }


def _validated_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("trend_api_stats must contain a JSON object")
    if payload.get("schema_version") != TREND_API_STATS_SCHEMA_VERSION:
        raise ValueError("trend_api_stats schema_version is invalid")
    for field in ("sources", "fills", "rounds", "stats"):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"trend_api_stats {field} must be a list")
    cutoff = _required_text(
        payload.get("statistics_cutoff_at"), "statistics_cutoff_at"
    )
    _validate_sources(payload["sources"], statistics_cutoff_at=cutoff)
    strategy_versions = sorted({
        (
            _required_text(stat.get("market"), "stats market"),
            _required_text(stat.get("strategy_id"), "stats strategy_id"),
            _required_text(stat.get("opening_strategy_version"), "stats opening version"),
        )
        for stat in payload["stats"]
        if isinstance(stat, Mapping)
    })
    rebuilt = build_trend_api_stats_payload(
        payload["fills"],
        strategy_versions=[
            {"market": market, "strategy_id": strategy_id, "strategy_version": version}
            for market, strategy_id, version in strategy_versions
        ],
        generated_at=_required_text(payload.get("generated_at"), "generated_at"),
        statistics_cutoff_at=cutoff,
    )
    if payload["rounds"] != rebuilt["rounds"]:
        raise ValueError("rounds are not derived from fills")
    if payload["stats"] != rebuilt["stats"]:
        raise ValueError("stats are not derived from rounds")
    return payload


def _validate_sources(
    sources: Sequence[object], *, statistics_cutoff_at: str
) -> None:
    required_fields = {
        "source", "source_id", "broker", "account_id", "market",
        "orders_seen", "fill_count", "statistics_cutoff_at", "status",
    }
    identities: set[tuple[str, str]] = set()
    for raw in sources:
        if not isinstance(raw, Mapping) or set(raw) != required_fields:
            raise ValueError("source audit record fields are invalid")
        source = str(raw["source"])
        broker = str(raw["broker"])
        account_id = _required_text(raw["account_id"], "source account_id")
        market = str(raw["market"])
        allowed_markets = _SOURCE_MARKETS.get((source, broker))
        if allowed_markets is None:
            raise ValueError("source and broker are inconsistent")
        if market not in allowed_markets:
            raise ValueError("source market is invalid")
        expected_source_id = f"{source}:{broker}:{account_id}"
        if raw["source_id"] != expected_source_id:
            raise ValueError("source_id is inconsistent with source facts")
        for field in ("orders_seen", "fill_count"):
            count = raw[field]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"source {field} must be a non-negative integer")
        if raw["status"] != "available":
            raise ValueError("source status is invalid")
        source_cutoff = _aware_timestamp(
            raw["statistics_cutoff_at"], "source statistics_cutoff_at"
        )
        if source_cutoff > _aware_timestamp(
            statistics_cutoff_at, "statistics_cutoff_at"
        ):
            raise ValueError("source statistics_cutoff_at exceeds artifact cutoff")
        identity = (expected_source_id, market)
        if identity in identities:
            raise ValueError("source audit record is duplicated")
        identities.add(identity)


def _deduplicate_fills(
    fills: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    by_identity: dict[tuple[str, str], dict[str, object]] = {}
    for raw in fills:
        fill = _validated_fill(raw)
        identity = (str(fill["source_id"]), str(fill["fill_id"]))
        existing = by_identity.get(identity)
        if existing is not None and existing != fill:
            raise ValueError(f"conflicting duplicate fill: {identity[0]}:{identity[1]}")
        by_identity[identity] = fill
    return sorted(
        by_identity.values(),
        key=_fill_sort_key,
    )


def _fill_sort_key(fill: Mapping[str, object]) -> tuple[object, ...]:
    common = (
        _aware_timestamp(fill["filled_at"], "fill filled_at"),
        str(fill["source_id"]),
    )
    if fill.get("broker") in _STATEMENT_ACCOUNTS:
        return (
            *common,
            0,
            int(fill["statement_sequence"]),
            str(fill["fill_id"]),
        )
    return (*common, 1, 0, str(fill["fill_id"]))


def _validated_fill(raw: Mapping[str, object]) -> dict[str, object]:
    fill = dict(raw)
    for field in (
        "fill_id",
        "order_id",
        "source_id",
        "broker",
        "account_id",
        "market",
        "symbol",
        "currency",
    ):
        if not isinstance(fill.get(field), str) or not str(fill[field]).strip():
            raise ValueError(f"fill {field} is required")
    if fill.get("source") not in {"simulation", "actual"}:
        raise ValueError("fill source must be simulation or actual")
    source = str(fill["source"])
    broker = str(fill["broker"])
    allowed_markets = _SOURCE_MARKETS.get((source, broker))
    if allowed_markets is None:
        raise ValueError("fill source and broker are inconsistent")
    if fill["market"] not in allowed_markets:
        raise ValueError("fill source market is invalid")
    if fill["source_id"] != (
        f"{fill['source']}:{broker}:{fill['account_id']}"
    ):
        raise ValueError("fill source_id is inconsistent with source facts")
    if broker in {"eastmoney", "phillips"}:
        period = fill.get("statement_period")
        if not isinstance(period, str) or re.fullmatch(
            r"\d{4}-\d{2}(?:-\d{2})?", period
        ) is None:
            raise ValueError("statement fill statement_period is invalid")
        if fill.get("execution_granularity") != "statement_trade_date":
            raise ValueError("statement fill execution_granularity is invalid")
        if fill.get("timestamp_semantics") != "market_close_ordering_sentinel":
            raise ValueError("statement fill timestamp_semantics is invalid")
        sequence = fill.get("statement_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("statement fill statement_sequence is invalid")
        local_timestamp = _aware_timestamp(fill.get("filled_at"), "fill filled_at").astimezone(
            _MARKET_TIMEZONES[str(fill["market"])]
        )
        sentinel = time(15 if broker == "eastmoney" else 16)
        if local_timestamp.time().replace(tzinfo=None) != sentinel:
            raise ValueError("statement fill ordering sentinel is invalid")
    if fill.get("side") not in {"buy", "sell"}:
        raise ValueError("fill side must be buy or sell")
    quantity = _required_decimal(fill.get("quantity"), "fill quantity")
    price = _required_decimal(fill.get("price"), "fill price")
    if quantity <= 0 or price <= 0:
        raise ValueError("fill quantity and price must be positive")
    if not isinstance(fill.get("costs_complete"), bool):
        raise ValueError("fill costs_complete must be boolean")
    fee = _required_decimal(fill.get("fee"), "fill fee")
    if fee < 0:
        raise ValueError("fill fee must be non-negative")
    _aware_timestamp(fill.get("filled_at"), "fill filled_at")
    if fill.get("attribution_status") not in {
        "attributed",
        "ambiguous",
        "outside_strategy",
    }:
        raise ValueError("fill attribution_status is invalid")
    if fill["attribution_status"] == "attributed":
        for field in ("strategy_id", "strategy_version"):
            if not isinstance(fill.get(field), str) or not str(fill[field]).strip():
                raise ValueError(f"attributed fill {field} is required")
    elif not isinstance(fill.get("exclusion_reason"), str) or not str(
        fill["exclusion_reason"]
    ).strip():
        raise ValueError("non-attributed fill exclusion_reason is required")
    return fill


def _closed_rounds(fills: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for fill in fills:
        grouped[(
            str(fill["source_id"]),
            str(fill["market"]),
            str(fill["symbol"]),
        )].append(fill)

    rounds: list[dict[str, object]] = []
    for group in grouped.values():
        position = Decimal("0")
        current: list[dict[str, object]] = []
        for fill in group:
            quantity = _required_decimal(fill["quantity"], "fill quantity")
            if fill["side"] == "buy":
                current.append(fill)
                position += quantity
                continue
            if position == 0:
                continue
            if quantity > position:
                raise ValueError("sell fill exceeds the linked open position")
            current.append(fill)
            position -= quantity
            if position == 0:
                rounds.append(_build_round(current))
                current = []
    return sorted(
        rounds,
        key=lambda round_: (
            _aware_timestamp(round_["closed_at"], "round closed_at"),
            str(round_["round_id"]),
        ),
    )


def _build_round(fills: Sequence[dict[str, object]]) -> dict[str, object]:
    opening = fills[0]
    for field in (
        "source",
        "source_id",
        "broker",
        "account_id",
        "market",
        "symbol",
        "currency",
    ):
        if any(fill[field] != opening[field] for fill in fills[1:]):
            raise ValueError(f"round fills disagree on {field}")
    buys = [fill for fill in fills if fill["side"] == "buy"]
    sells = [fill for fill in fills if fill["side"] == "sell"]
    with localcontext(_CALCULATION_CONTEXT):
        buy_quantity = sum((_required_decimal(fill["quantity"], "fill quantity") for fill in buys), Decimal("0"))
        sell_quantity = sum((_required_decimal(fill["quantity"], "fill quantity") for fill in sells), Decimal("0"))
        buy_notional = sum((_required_decimal(fill["quantity"], "fill quantity") * _required_decimal(fill["price"], "fill price") for fill in buys), Decimal("0"))
        sell_notional = sum((_required_decimal(fill["quantity"], "fill quantity") * _required_decimal(fill["price"], "fill price") for fill in sells), Decimal("0"))
        if opening["source"] == "simulation":
            rate = _optional_nonnegative_decimal(opening.get("normal_cost_rate"))
            model = str(opening.get("normal_cost_model") or "").strip()
            report_sha256 = str(opening.get("report_sha256") or "").strip().lower()
            costs_complete = (
                rate is not None
                and bool(model)
                and _is_sha256(report_sha256)
            )
            fees = buy_notional * rate if costs_complete and rate is not None else None
            buy_costs = fees
            cost_source = (
                "opening_strategy_normal_cost_model" if costs_complete else "unavailable"
            )
        else:
            costs_complete = all(fill["costs_complete"] is True for fill in fills)
            fees = (
                sum((_required_decimal(fill["fee"], "fill fee") for fill in fills), Decimal("0"))
                if costs_complete
                else None
            )
            buy_costs = (
                sum((_required_decimal(fill["fee"], "fill fee") for fill in buys), Decimal("0"))
                if costs_complete
                else None
            )
            rate = None
            model = ""
            report_sha256 = ""
            cost_source = "broker_actual" if costs_complete else "unavailable"
        net_pnl = (
            sell_notional - buy_notional - fees
            if fees is not None
            else None
        )
        cost_basis = (
            buy_notional + buy_costs
            if buy_costs is not None
            else None
        )
        net_return = (
            net_pnl / cost_basis
            if net_pnl is not None and cost_basis is not None and cost_basis > 0
            else None
        )
    conflicting_scaled_entry = any(
        fill["attribution_status"] != "attributed"
        or fill.get("strategy_id") != opening.get("strategy_id")
        or fill.get("strategy_version") != opening.get("strategy_version")
        for fill in buys[1:]
    )
    attribution_status = (
        "ambiguous" if conflicting_scaled_entry else str(opening["attribution_status"])
    )
    exclusion_reason = (
        "scaled_entry_attribution_conflict"
        if conflicting_scaled_entry
        else str(opening.get("exclusion_reason") or "")
    )
    eligible = attribution_status == "attributed" and costs_complete
    identity = ":".join((
        str(opening["source_id"]),
        str(opening["market"]),
        str(opening["symbol"]),
        str(opening["fill_id"]),
    ))
    return {
        "round_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "source": opening["source"],
        "source_id": opening["source_id"],
        "broker": opening["broker"],
        "account_id": opening["account_id"],
        "market": opening["market"],
        "symbol": opening["symbol"],
        "currency": opening["currency"],
        "strategy_id": str(opening.get("strategy_id") or ""),
        "opening_strategy_version": str(opening.get("strategy_version") or ""),
        "opened_at": opening["filled_at"],
        "closed_at": fills[-1]["filled_at"],
        "opening_fill_id": opening["fill_id"],
        "fill_ids": [fill["fill_id"] for fill in fills],
        "buy_quantity": _decimal_text(buy_quantity),
        "sell_quantity": _decimal_text(sell_quantity),
        "buy_notional": _decimal_text(buy_notional),
        "sell_notional": _decimal_text(sell_notional),
        "fees": _decimal_text(fees) if fees is not None else None,
        "costs_complete": costs_complete,
        "cost_source": cost_source,
        "normal_cost_rate": _decimal_text(rate) if rate is not None else None,
        "normal_cost_model": model or None,
        "opening_report_sha256": report_sha256 or None,
        "net_pnl": _decimal_text(net_pnl) if net_pnl is not None else None,
        "net_return": _decimal_text(net_return) if net_return is not None else None,
        "result": (
            "win" if net_pnl is not None and net_pnl > 0
            else "loss" if net_pnl is not None and net_pnl < 0
            else "flat" if net_pnl == 0
            else "unavailable"
        ),
        "attribution_status": attribution_status,
        "exclusion_reason": exclusion_reason,
        "kelly_eligible": opening["source"] == "simulation" and eligible,
    }


def _strategy_stats(
    rounds: Sequence[dict[str, object]],
    *,
    strategy_versions: Sequence[Mapping[str, object]],
    statistics_cutoff_at: str,
) -> list[dict[str, object]]:
    strategy_identities: set[tuple[str, str, str]] = {
        (
            str(round_["market"]),
            str(round_["strategy_id"]),
            str(round_["opening_strategy_version"]),
        )
        for round_ in rounds
        if round_["attribution_status"] == "attributed"
    }
    for fact in strategy_versions:
        market = _required_text(fact.get("market"), "strategy market").upper()
        strategy_id = _required_text(fact.get("strategy_id"), "strategy_id")
        version = _required_text(fact.get("strategy_version"), "strategy_version")
        strategy_identities.add((market, strategy_id, version))
    identities = {
        (source, market, strategy_id, version)
        for market, strategy_id, version in strategy_identities
        for source in ("actual", "simulation")
    }
    stats = []
    for source, market, strategy_id, version in sorted(identities):
        eligible = [
            round_ for round_ in rounds
            if (
                round_["source"],
                round_["market"],
                round_["strategy_id"],
                round_["opening_strategy_version"],
            ) == (source, market, strategy_id, version)
            and round_["attribution_status"] == "attributed"
            and round_["costs_complete"] is True
            and round_["net_return"] is not None
        ]
        wins = sum(round_["result"] == "win" for round_ in eligible)
        payoff_ratio, payoff_status = strategy_payoff_ratio(
            [round_["net_return"] for round_ in eligible if round_["result"] == "win"],
            [round_["net_return"] for round_ in eligible if round_["result"] == "loss"],
        )
        stats.append({
            "source": source,
            "market": market,
            "strategy_id": strategy_id,
            "opening_strategy_version": version,
            "win_rate": _decimal_text(_divide(Decimal(wins), Decimal(len(eligible)))) if eligible else None,
            "payoff_ratio": payoff_ratio,
            "payoff_ratio_status": payoff_status,
            "eligible_sample_count": len(eligible),
            "statistics_cutoff_at": statistics_cutoff_at,
        })
    return stats


def _required_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _optional_nonnegative_decimal(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = _required_decimal(value, "normal cost rate")
    if parsed < 0:
        raise ValueError("normal cost rate must be non-negative")
    return parsed


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT):
        return numerator / denominator


def _average(values: Sequence[Decimal]) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT):
        return sum(values, Decimal("0")) / len(values)


def _aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an aware ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError(f"{field} must be an aware ISO timestamp")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
