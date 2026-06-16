from __future__ import annotations

import csv
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Iterable, Mapping
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from tempfile import NamedTemporaryFile

from .futu_watch import QuoteSnapshot
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
    "target_max_weight",
    "cash_available",
    "limit_price",
    "stop_price",
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
    market_value: Decimal
    market_value_hkd: Decimal
    weight: Decimal
    fx_to_hkd: Decimal


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
) -> TradeActionsResult:
    active_plans = [
        plan
        for plan in load_trading_plan_rows(plan_path)
        if plan.status == "active"
    ]
    effective_run_date = run_date or _latest_run_date(active_plans)
    plans = [
        plan
        for plan in active_plans
        if plan.run_date == effective_run_date
    ]
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
            f"建议：{_suggestion_text(row.get('action', '').strip())}",
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
        "target_max_weight": (
            _percent_to_text(target_max_weight)
            if target_max_weight is not None
            else plan.max_weight.strip()
        ),
        "cash_available": _decimal_to_text(cash_available),
        "limit_price": "",
        "stop_price": _decimal_to_text(plan.stop_loss),
        "reason": quote_status.message,
        "source_plan": source_plan,
        "status": "",
        "error": "",
    }

    if action == "HOLD":
        row["status"] = "watch"
        return row
    if action == "REVIEW":
        row["status"] = "review"
        row["error"] = quote_status.message
        return row
    if action in {"SELL_STOP", "TAKE_PROFIT", "TRIM"}:
        return _size_sell_action_row(row, action, quote_status, position)
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

        quantity = _optional_decimal(row.get("total_quantity", "") or "")
        market_value = _optional_decimal(row.get("market_value", "") or "")
        weight = _optional_percent(row.get("portfolio_weight_hkd", "") or "")
        fx_to_hkd = _optional_decimal(row.get("fx_to_hkd", "") or "")

        if market and symbol:
            key = (market, symbol)
            if key in positions:
                raise ValueError(f"duplicate portfolio position(s): {market}.{symbol}")

            positions[key] = PortfolioPositionSnapshot(
                currency=currency,
                quantity=quantity or Decimal("0"),
                market_value=market_value or Decimal("0"),
                market_value_hkd=market_value_hkd or Decimal("0"),
                weight=weight or Decimal("0"),
                fx_to_hkd=fx_to_hkd or Decimal("0"),
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


def _size_sell_action_row(
    row: dict[str, str],
    action: str,
    quote_status: PlanQuoteStatus,
    position: PortfolioPositionSnapshot | None,
) -> dict[str, str]:
    if quote_status.last_price <= 0:
        return _review_row(row, "invalid last price")
    if position is None:
        return _review_row(row, "missing portfolio position for sell sizing")

    if action == "TRIM":
        quantity = (position.quantity * Decimal("0.5")).to_integral_value(
            rounding=ROUND_DOWN
        )
    else:
        quantity = position.quantity

    if quantity < 1:
        return _review_row(row, "current quantity below one share for sell sizing")

    if action != "SELL_STOP":
        row["limit_price"] = _decimal_to_text(quote_status.last_price)
    row["suggested_quantity"] = _decimal_to_text(quantity)
    row["suggested_notional"] = _decimal_to_text(quantity * quote_status.last_price)
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
    row["status"] = "ready"
    return row


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


def _suggestion_text(action: str) -> str:
    return {
        "BUY": "买入",
        "ADD": "加仓",
        "TRIM": "减仓",
        "TAKE_PROFIT": "止盈卖出",
        "SELL_STOP": "止损卖出",
        "HOLD": "继续观察",
        "REVIEW": "人工复核",
    }.get(action, "人工复核")


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
