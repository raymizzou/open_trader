from __future__ import annotations

import csv
from dataclasses import dataclass, replace
import re
from types import MappingProxyType
from typing import Iterable, Mapping
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from tempfile import NamedTemporaryFile

from .futu_watch import QuoteSnapshot
from .market_scope import (
    market_report_path,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)
from .trading_plan import (
    PlanQuoteStatus,
    TradingPlanRow,
    evaluate_plan_quote,
    load_trading_plan_rows,
)


TRADE_ACTION_FIELDNAMES = (
    "run_date",
    "symbol",
    "market",
    "futu_symbol",
    "action",
    "priority",
    "last_price",
    "trigger_status",
    "suggested_quantity",
    "suggested_notional",
    "notional_currency",
    "current_quantity",
    "current_weight",
    "avg_cost_price",
    "target_max_weight",
    "cash_available",
    "limit_price",
    "stop_price",
    "post_trade_quantity",
    "post_trade_weight",
    "post_trade_avg_cost",
    "risk_to_stop",
    "agent_reason",
    "agent_excerpt",
    "trigger_reason",
    "reason",
    "source_plan",
    "status",
    "error",
)

PORTFOLIO_REQUIRED_FIELDNAMES = (
    "market",
    "asset_class",
    "symbol",
    "currency",
    "total_quantity",
    "avg_cost_price",
    "market_value",
    "fx_to_hkd",
    "market_value_hkd",
    "portfolio_weight_hkd",
)


_GROUPED_DECIMAL_WITH_OPTIONAL_SIGN_PATTERN = re.compile(
    r"^[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?$"
)


@dataclass(frozen=True)
class PortfolioActionContext:
    positions: Mapping[tuple[str, str], PortfolioPositionSnapshot]
    cash_by_currency: Mapping[str, Decimal]
    total_market_value_hkd: Decimal


@dataclass(frozen=True)
class PortfolioPositionSnapshot:
    currency: str
    quantity: Decimal
    avg_cost_price: Decimal
    market_value: Decimal
    market_value_hkd: Decimal
    weight: Decimal
    fx_to_hkd: Decimal
    invalid_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeActionsResult:
    run_date: str
    action_count: int
    ready_count: int
    review_count: int
    watch_count: int
    actions_path: Path
    latest_path: Path
    report_path: Path


def generate_trade_actions(
    *,
    plan_path: Path,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    snapshots: dict[str, QuoteSnapshot],
    run_date: str | None,
    update_latest: bool,
    market: str | None = None,
) -> TradeActionsResult:
    market_scope = parse_market_scope(market) if market is not None else None
    active_plans = [
        plan
        for plan in load_trading_plan_rows(plan_path)
        if plan.status == "active"
    ]
    effective_run_date = run_date or _latest_run_date(active_plans)
    plans = [
        _plan_for_run(plan, effective_run_date)
        for plan in active_plans
        if _plan_matches_run_date(plan, effective_run_date)
    ]
    if market_scope is not None:
        plans = [plan for plan in plans if plan.market.upper() == market_scope.value]
    if run_date is not None and not plans:
        raise ValueError(f"no active trading plans match run_date {effective_run_date}")
    portfolio = load_portfolio_action_context(portfolio_path)

    rows = [
        build_trade_action_row(
            plan=plan,
            quote_status=_quote_status_for_plan(plan, snapshots),
            portfolio=portfolio,
            source_plan=str(plan_path),
        )
        for plan in plans
    ]

    if market_scope is not None:
        actions_path = market_run_dir(
            data_dir,
            effective_run_date,
            market_scope,
        ) / "trade_actions.csv"
        latest_path = market_scoped_latest_path(
            data_dir,
            market_scope,
            "trade_actions.csv",
        )
        report_path = market_report_path(
            reports_dir,
            "trade_actions",
            effective_run_date,
            market_scope,
        )
    else:
        actions_path = data_dir / "runs" / effective_run_date / "trade_actions.csv"
        latest_path = data_dir / "latest" / "trade_actions.csv"
        report_path = reports_dir / "trade_actions" / f"{effective_run_date}.md"

    _atomic_write_csv(actions_path, TRADE_ACTION_FIELDNAMES, rows)
    _atomic_write_text(report_path, render_trade_actions_report(effective_run_date, rows))
    if update_latest:
        _atomic_write_csv(latest_path, TRADE_ACTION_FIELDNAMES, rows)

    status_counts = {
        "ready": sum(1 for row in rows if row.get("status") == "ready"),
        "review": sum(1 for row in rows if row.get("status") == "review"),
        "watch": sum(1 for row in rows if row.get("status") == "watch"),
    }
    return TradeActionsResult(
        run_date=effective_run_date,
        action_count=len(rows),
        ready_count=status_counts["ready"],
        review_count=status_counts["review"],
        watch_count=status_counts["watch"],
        actions_path=actions_path,
        latest_path=latest_path,
        report_path=report_path,
    )


def render_trade_actions_report(run_date: str, rows: list[dict[str, str]]) -> str:
    lines = [f"# Trade Actions - {run_date}", ""]
    if not rows:
        lines.append("No active trading actions were generated.")
        return "\n".join(lines) + "\n"

    sorted_rows = sorted(rows, key=_priority_sort_key)
    for index, row in enumerate(sorted_rows):
        if index:
            lines.append("")
        lines.extend([
            f"## {row.get('futu_symbol', '').strip()}",
            f"行动：{row.get('action', '').strip()}",
            f"标的：{row.get('futu_symbol', '').strip()}",
            f"优先级：{row.get('priority', '').strip()}",
            f"价格：{row.get('last_price', '').strip()}",
            f"建议：{_suggestion_text(row)}",
            f"条件：{row.get('reason', '').strip()}",
            f"风控：止损 {row.get('stop_price', '').strip()}",
            (
                "原因：来自 "
                f"{row.get('source_plan', '').strip()}，"
                f"计划仓位上限 {row.get('target_max_weight', '').strip()}"
            ),
            f"状态：{row.get('status', '').strip()}",
        ])
    return "\n".join(lines) + "\n"


def map_quote_status_to_action(trigger_status: str) -> tuple[str, str]:
    mapping = {
        "stop_loss_hit": ("SELL_STOP", "critical"),
        "target_2_hit": ("TAKE_PROFIT", "high"),
        "target_1_hit": ("TRIM", "medium"),
        "entry_zone": ("BUY", "high"),
        "add_zone": ("ADD", "medium"),
        "watch": ("HOLD", "low"),
        "missing_quote": ("REVIEW", "medium"),
    }
    return mapping.get(trigger_status, ("REVIEW", "medium"))


def build_trade_action_row(
    *,
    plan: TradingPlanRow,
    quote_status: PlanQuoteStatus,
    portfolio: PortfolioActionContext,
    source_plan: str,
) -> dict[str, str]:
    position = portfolio.positions.get((plan.market.upper(), plan.symbol.upper()))
    target_max_weight = _optional_percent(plan.max_weight)
    notional_currency = _notional_currency(plan.market, position)
    cash_available = portfolio.cash_by_currency.get(notional_currency, Decimal("0"))
    action, priority = map_quote_status_to_action(quote_status.status)
    trigger_reason = quote_status.message
    reason = plan.agent_reason.strip() or trigger_reason
    if action == "BUY" and _plan_text_implies_trim(plan.plan_text):
        action, priority = "TRIM", "medium"
        trigger_reason = "Plan text indicates trim at current levels."
        reason = plan.agent_reason.strip() or trigger_reason

    row = {
        "run_date": plan.run_date,
        "symbol": plan.symbol.upper(),
        "market": plan.market.upper(),
        "futu_symbol": quote_status.futu_symbol,
        "action": action,
        "priority": priority,
        "last_price": _decimal_to_text(quote_status.last_price),
        "trigger_status": quote_status.status,
        "suggested_quantity": "",
        "suggested_notional": "",
        "notional_currency": notional_currency,
        "current_quantity": _decimal_to_text(position.quantity if position else None),
        "current_weight": _percent_to_text(position.weight if position else None),
        "avg_cost_price": _decimal_to_text(
            position.avg_cost_price if position else None
        ),
        "target_max_weight": (
            _percent_to_text(target_max_weight)
            if target_max_weight is not None
            else plan.max_weight.strip()
        ),
        "cash_available": _decimal_to_text(cash_available),
        "limit_price": "",
        "stop_price": _decimal_to_text(
            plan.stop_loss
            if plan.stop_loss is not None and plan.stop_loss > 0
            else None
        ),
        "post_trade_quantity": "",
        "post_trade_weight": "",
        "post_trade_avg_cost": "",
        "risk_to_stop": "",
        "agent_reason": plan.agent_reason.strip(),
        "agent_excerpt": plan.agent_excerpt.strip(),
        "trigger_reason": trigger_reason,
        "reason": reason,
        "source_plan": source_plan,
        "status": "",
        "error": "",
    }

    if action == "HOLD":
        row["status"] = "watch"
        return row
    if action == "REVIEW":
        return _review_row(row, quote_status.message)
    if action in {"SELL_STOP", "TAKE_PROFIT", "TRIM"}:
        return _size_sell_action_row(row, action, quote_status, position, portfolio)
    return _size_buy_action_row(
        row=row,
        action=action,
        plan=plan,
        quote_status=quote_status,
        portfolio=portfolio,
        position=position,
        cash_available=cash_available,
        target_max_weight=target_max_weight,
    )


def load_portfolio_action_context(portfolio_path: Path) -> PortfolioActionContext:
    with portfolio_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        blank_columns = [name for name in fieldnames if not (name or "").strip()]
        if blank_columns:
            raise ValueError("portfolio column names must not be blank")

        duplicate_columns = sorted(
            {
                name
                for name in fieldnames
                if fieldnames.count(name) > 1
            }
        )
        if duplicate_columns:
            raise ValueError(
                f"duplicate portfolio column(s): {', '.join(duplicate_columns)}"
            )

        missing = sorted(set(PORTFOLIO_REQUIRED_FIELDNAMES) - set(fieldnames))
        if missing:
            raise ValueError(f"missing portfolio column(s): {', '.join(missing)}")
        rows = [row for row in reader]

    positions: dict[tuple[str, str], PortfolioPositionSnapshot] = {}
    cash_by_currency: dict[str, Decimal] = {}
    total_market_value_hkd = Decimal("0")

    for row in rows:
        if None in row:
            continue

        if any(row.get(name) is None for name in PORTFOLIO_REQUIRED_FIELDNAMES):
            continue

        market_value_hkd = _optional_decimal(row.get("market_value_hkd", "") or "")
        if market_value_hkd is not None:
            total_market_value_hkd += market_value_hkd

        market = (row.get("market", "") or "").strip().upper()
        asset_class = (row.get("asset_class", "") or "").strip().lower()
        symbol = (row.get("symbol", "") or "").strip().upper()
        currency = (row.get("currency", "") or "").strip().upper()

        if market == "CASH" or asset_class == "cash":
            cash_value = _optional_decimal(row.get("market_value", "") or "")
            if currency and cash_value is not None:
                cash_by_currency[currency] = cash_by_currency.get(currency, Decimal("0")) + cash_value
            continue

        invalid_fields: list[str] = []
        quantity = _position_decimal(row, "total_quantity", invalid_fields)
        avg_cost_price = _position_positive_decimal(
            row,
            "avg_cost_price",
            invalid_fields,
        )
        market_value = _position_decimal(row, "market_value", invalid_fields)
        weight = _position_percent(row, "portfolio_weight_hkd", invalid_fields)
        fx_to_hkd = _position_decimal(row, "fx_to_hkd", invalid_fields)
        if market_value_hkd is None:
            invalid_fields.append("market_value_hkd")

        if market and symbol:
            key = (market, symbol)
            if key in positions:
                raise ValueError(f"duplicate portfolio position(s): {market}.{symbol}")

            positions[key] = PortfolioPositionSnapshot(
                currency=currency,
                quantity=quantity or Decimal("0"),
                avg_cost_price=avg_cost_price or Decimal("0"),
                market_value=market_value or Decimal("0"),
                market_value_hkd=market_value_hkd or Decimal("0"),
                weight=weight or Decimal("0"),
                fx_to_hkd=fx_to_hkd or Decimal("0"),
                invalid_fields=tuple(invalid_fields),
            )

    return PortfolioActionContext(
        positions=MappingProxyType(positions),
        cash_by_currency=MappingProxyType(cash_by_currency),
        total_market_value_hkd=total_market_value_hkd,
    )


def _optional_decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None

    if "," in value and not _GROUPED_DECIMAL_WITH_OPTIONAL_SIGN_PATTERN.fullmatch(value):
        return None

    parsed_value = value.replace(",", "")
    if not parsed_value:
        return None
    try:
        parsed = Decimal(parsed_value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _optional_percent(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    if value.endswith("%"):
        parsed = _optional_decimal(value[:-1])
        return None if parsed is None else parsed / Decimal("100")
    return None


def _position_decimal(
    row: dict[str, str],
    fieldname: str,
    invalid_fields: list[str],
) -> Decimal | None:
    parsed = _optional_decimal(row.get(fieldname, "") or "")
    if parsed is None:
        invalid_fields.append(fieldname)
    return parsed


def _position_positive_decimal(
    row: dict[str, str],
    fieldname: str,
    invalid_fields: list[str],
) -> Decimal | None:
    parsed = _optional_decimal(row.get(fieldname, "") or "")
    if parsed is None or parsed <= 0:
        invalid_fields.append(fieldname)
        return None
    return parsed


def _position_percent(
    row: dict[str, str],
    fieldname: str,
    invalid_fields: list[str],
) -> Decimal | None:
    raw_value = row.get(fieldname, "") or ""
    parsed = _optional_percent(raw_value)
    if parsed is None and raw_value.strip():
        invalid_fields.append(fieldname)
    return parsed


def _size_sell_action_row(
    row: dict[str, str],
    action: str,
    quote_status: PlanQuoteStatus,
    position: PortfolioPositionSnapshot | None,
    portfolio: PortfolioActionContext,
) -> dict[str, str]:
    if quote_status.last_price <= 0:
        return _review_row(row, "invalid last price")
    if position is None:
        return _review_row(row, "missing portfolio position for sell sizing")
    invalid_fields = _invalid_position_fields(
        position,
        (
            "total_quantity",
            "market_value",
            "market_value_hkd",
            "fx_to_hkd",
        ),
        require_avg_cost_price=False,
    )
    if invalid_fields:
        return _review_row(
            row,
            f"invalid portfolio sizing field(s): {', '.join(invalid_fields)}",
        )
    if position.fx_to_hkd <= 0:
        return _review_row(row, "missing positive fx_to_hkd for sell-side sizing")

    if action == "TRIM":
        quantity = (position.quantity * Decimal("0.5")).to_integral_value(
            rounding=ROUND_DOWN
        )
        if quantity < 1 and position.quantity >= 1:
            quantity = Decimal("1")
    else:
        quantity = position.quantity

    if quantity < 1:
        return _review_row(row, "current quantity below one share for sell sizing")

    if action != "SELL_STOP":
        row["limit_price"] = _decimal_to_text(quote_status.last_price)
    row["suggested_quantity"] = _decimal_to_text(quantity)
    executable_notional = quantity * quote_status.last_price
    row["suggested_notional"] = _decimal_to_text(executable_notional)
    _set_post_trade_fields(
        row=row,
        position=position,
        quantity_delta=-quantity,
        execution_price=quote_status.last_price,
        total_market_value_hkd=portfolio.total_market_value_hkd,
        post_trade_avg_cost=(
            position.avg_cost_price
            if position.quantity > quantity and position.avg_cost_price > 0
            else None
        ),
    )
    row["status"] = "ready"
    return row


def _size_buy_action_row(
    *,
    row: dict[str, str],
    action: str,
    plan: TradingPlanRow,
    quote_status: PlanQuoteStatus,
    portfolio: PortfolioActionContext,
    position: PortfolioPositionSnapshot | None,
    cash_available: Decimal,
    target_max_weight: Decimal | None,
) -> dict[str, str]:
    if target_max_weight is None:
        return _review_row(row, "unparseable target max weight")
    if position is None:
        return _review_row(row, "missing portfolio position for buy-side sizing")
    invalid_fields = _invalid_position_fields(
        position,
        (
            "total_quantity",
            "avg_cost_price",
            "market_value",
            "market_value_hkd",
            "fx_to_hkd",
        ),
        require_avg_cost_price=position.quantity > 0,
    )
    if invalid_fields:
        return _review_row(
            row,
            f"invalid portfolio sizing field(s): {', '.join(invalid_fields)}",
        )
    if position.fx_to_hkd <= 0:
        return _review_row(row, "missing positive fx_to_hkd for buy-side sizing")
    if cash_available <= 0:
        return _review_row(row, "no same-currency cash available")

    portfolio_value_in_symbol_currency = (
        portfolio.total_market_value_hkd / position.fx_to_hkd
    )
    target_budget = portfolio_value_in_symbol_currency * target_max_weight
    remaining_target_budget = target_budget - position.market_value
    if remaining_target_budget <= 0:
        return _review_row(row, "no remaining target budget")
    plan_ratio = _plan_ratio(plan.plan_text, action)
    if action == "BUY":
        entry_tranche_budget = target_budget * plan_ratio
        remaining_entry_budget = entry_tranche_budget - position.market_value
        if remaining_entry_budget <= 0:
            return _review_row(row, "no remaining entry budget")
        plan_budget = remaining_entry_budget
    else:
        plan_budget = target_budget * plan_ratio
    suggested_notional_budget = min(
        plan_budget,
        remaining_target_budget,
        cash_available,
    )
    if quote_status.last_price <= 0:
        return _review_row(row, "invalid last price")
    quantity = (suggested_notional_budget / quote_status.last_price).to_integral_value(
        rounding=ROUND_DOWN
    )
    if quantity < 1:
        return _review_row(row, "suggested quantity below one share")

    executable_notional = quantity * quote_status.last_price
    row["limit_price"] = _decimal_to_text(quote_status.last_price)
    row["suggested_quantity"] = _decimal_to_text(quantity)
    row["suggested_notional"] = _decimal_to_text(executable_notional)
    post_trade_quantity = position.quantity + quantity
    post_trade_avg_cost = (
        (position.quantity * position.avg_cost_price) + executable_notional
    ) / post_trade_quantity
    _set_post_trade_fields(
        row=row,
        position=position,
        quantity_delta=quantity,
        execution_price=quote_status.last_price,
        total_market_value_hkd=portfolio.total_market_value_hkd,
        post_trade_avg_cost=post_trade_avg_cost,
    )
    row["status"] = "ready"
    return row


def _set_post_trade_fields(
    *,
    row: dict[str, str],
    position: PortfolioPositionSnapshot,
    quantity_delta: Decimal,
    execution_price: Decimal,
    total_market_value_hkd: Decimal | None,
    post_trade_avg_cost: Decimal | None,
) -> None:
    post_trade_quantity = position.quantity + quantity_delta
    row["post_trade_quantity"] = _decimal_to_text(post_trade_quantity)
    row["post_trade_avg_cost"] = (
        _decimal_to_text(post_trade_avg_cost) if post_trade_quantity > 0 else ""
    )

    portfolio_value_hkd = total_market_value_hkd
    if portfolio_value_hkd is None:
        portfolio_value_hkd = position.market_value_hkd
    if portfolio_value_hkd > 0 and position.fx_to_hkd > 0 and execution_price > 0:
        post_trade_market_value_hkd = (
            post_trade_quantity * execution_price * position.fx_to_hkd
        )
        row["post_trade_weight"] = _percent_to_text(
            post_trade_market_value_hkd / portfolio_value_hkd
        )

    stop_price = _optional_decimal(row.get("stop_price", ""))
    if (
        stop_price is not None
        and stop_price > 0
        and execution_price > 0
        and post_trade_quantity > 0
    ):
        risk_to_stop = max(
            Decimal("0"),
            (execution_price - stop_price) * post_trade_quantity,
        )
        row["risk_to_stop"] = _decimal_to_text(risk_to_stop)


def _invalid_position_fields(
    position: PortfolioPositionSnapshot,
    fieldnames: tuple[str, ...],
    *,
    require_avg_cost_price: bool = True,
) -> tuple[str, ...]:
    invalid = set(position.invalid_fields)
    if not require_avg_cost_price:
        invalid.discard("avg_cost_price")
    elif "avg_cost_price" in fieldnames and position.avg_cost_price <= 0:
        invalid.add("avg_cost_price")
    return tuple(fieldname for fieldname in fieldnames if fieldname in invalid)


def _review_row(row: dict[str, str], error: str) -> dict[str, str]:
    row["action"] = "REVIEW"
    row["reason"] = error
    row["status"] = "review"
    row["error"] = error
    return row


def _plan_ratio(plan_text: str, action: str) -> Decimal:
    if action == "ADD":
        return _ratio_after_keywords(
            plan_text,
            keywords=("加仓", "加碼", "加码"),
            fallback=Decimal("0.4"),
        )
    return _ratio_after_keywords(
        plan_text,
        keywords=("买入", "買入", "建仓", "建倉"),
        fallback=Decimal("0.6"),
    )


def _plan_text_implies_trim(plan_text: str) -> bool:
    normalized = plan_text.lower()
    return any(
        keyword in normalized
        for keyword in (
            "trim",
            "reduce",
            "减仓",
            "減倉",
            "降低仓位",
            "降低倉位",
        )
    )


def _ratio_after_keywords(
    text: str,
    *,
    keywords: tuple[str, ...],
    fallback: Decimal,
) -> Decimal:
    keyword_pattern = "|".join(re.escape(keyword) for keyword in keywords)
    pattern = re.compile(
        rf"(?:{keyword_pattern})[^%％。！？!?]*?(\d+(?:\.\d+)?)\s*[%％]"
    )
    match = pattern.search(text)
    if not match:
        return fallback
    return Decimal(match.group(1)) / Decimal("100")


def _notional_currency(
    market: str,
    position: PortfolioPositionSnapshot | None,
) -> str:
    if position and position.currency:
        return position.currency
    market = market.upper()
    if market == "US":
        return "USD"
    if market == "HK":
        return "HKD"
    return ""


def _decimal_to_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _percent_to_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{_decimal_to_text(value * Decimal('100'))}%"


def _latest_run_date(plans: list[TradingPlanRow]) -> str:
    dates = sorted({plan.run_date.strip() for plan in plans if plan.run_date.strip()})
    if not dates:
        raise ValueError("--date is required when trading plan has no active run_date rows")
    return dates[-1]


def _quote_status_for_plan(
    plan: TradingPlanRow,
    snapshots: Mapping[str, QuoteSnapshot],
) -> PlanQuoteStatus:
    quote = snapshots.get(plan.futu_symbol)
    if quote is None:
        return PlanQuoteStatus(
            symbol=plan.symbol,
            futu_symbol=plan.futu_symbol,
            last_price=Decimal("0"),
            status="missing_quote",
            message="Futu did not return a quote.",
        )
    return evaluate_plan_quote(plan, quote.last_price)


def _plan_matches_run_date(plan: TradingPlanRow, run_date: str) -> bool:
    return not plan.run_date.strip() or plan.run_date == run_date


def _plan_for_run(plan: TradingPlanRow, run_date: str) -> TradingPlanRow:
    if plan.run_date.strip():
        return plan
    return replace(plan, run_date=run_date)


def _priority_sort_key(row: Mapping[str, str]) -> tuple[int, str]:
    priority_order = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }
    priority = row.get("priority", "").strip().lower()
    futu_symbol = row.get("futu_symbol", "").strip()
    return priority_order.get(priority, len(priority_order)), futu_symbol


def _suggestion_text(row: Mapping[str, str]) -> str:
    status = row.get("status", "").strip()
    if status == "watch":
        return "继续观察，不建议交易"
    if status == "review":
        error = row.get("error", "").strip() or row.get("reason", "").strip()
        return f"需要人工复核：{error}"

    action = row.get("action", "").strip()
    verb = {
        "BUY": "买入",
        "ADD": "加仓",
        "TRIM": "减仓",
        "TAKE_PROFIT": "止盈卖出",
        "SELL_STOP": "止损卖出",
        "HOLD": "继续观察",
        "REVIEW": "人工复核",
    }.get(action, "人工复核")
    quantity = row.get("suggested_quantity", "").strip()
    currency = row.get("notional_currency", "").strip()
    notional = row.get("suggested_notional", "").strip()
    if quantity and currency and notional:
        return f"{verb} {quantity} 股，预算约 {currency} {notional}"
    return verb


def _atomic_write_csv(
    path: Path,
    fieldnames: tuple[str, ...],
    rows: Iterable[Mapping[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
