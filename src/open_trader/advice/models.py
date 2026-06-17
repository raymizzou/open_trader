from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AdviceStatus = Literal["ok", "fallback", "error"]
WatchlistStatus = Literal["active", "manual_review", "no_trigger", "error"]
TriggerType = Literal["price", "open_price", "manual_review", "none"]
ChangeType = Literal[
    "new_signal",
    "action_changed",
    "risk_changed",
    "trigger_changed",
    "no_material_change",
]
Severity = Literal["low", "medium", "high"]


TRADING_ADVICE_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "asset_class",
    "last_price",
    "price_currency",
    "portfolio_weight_hkd",
    "market_value_hkd",
    "risk_flag",
    "source",
    "advice_action",
    "advice_summary",
    "raw_decision",
    "status",
    "error",
    "source_status",
    "fallback_reason",
    "fallback_from_date",
]

CHANGE_CLASSIFICATION_FIELDNAMES = [
    "run_date",
    "symbol",
    "include_in_report",
    "change_type",
    "severity",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
    "status",
    "error",
]

PREMARKET_ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "change_type",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
]

WATCHLIST_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "suggested_action",
    "severity",
    "portfolio_weight_hkd",
    "trigger_type",
    "operator",
    "trigger_price",
    "trigger_text",
    "status",
    "error",
]


@dataclass(frozen=True)
class PortfolioInputRow:
    symbol: str
    market: str
    asset_class: str
    name: str
    portfolio_weight_hkd: str
    risk_flag: str
    analysis_symbol: str
    market_value_hkd: str = ""
    last_price: str = ""
    price_currency: str = ""


@dataclass(frozen=True)
class TradingAdvice:
    run_date: str
    symbol: str
    market: str
    asset_class: str
    portfolio_weight_hkd: str
    risk_flag: str
    source: str
    advice_action: str
    advice_summary: str
    raw_decision: str
    status: AdviceStatus
    error: str
    source_status: str = "ok"
    fallback_reason: str = ""
    fallback_from_date: str = ""
    market_value_hkd: str = ""
    last_price: str = ""
    price_currency: str = ""

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in TRADING_ADVICE_FIELDNAMES}


@dataclass(frozen=True)
class ChangeClassification:
    run_date: str
    symbol: str
    include_in_report: bool
    change_type: ChangeType
    severity: Severity
    suggested_action: str
    summary: str
    rationale: str
    watch_trigger: str
    status: AdviceStatus
    error: str

    def to_row(self) -> dict[str, str]:
        row = {
            field: str(getattr(self, field))
            for field in CHANGE_CLASSIFICATION_FIELDNAMES
        }
        row["include_in_report"] = "true" if self.include_in_report else "false"
        return row


@dataclass(frozen=True)
class PremarketAction:
    run_date: str
    symbol: str
    market: str
    portfolio_weight_hkd: str
    severity: Severity
    change_type: ChangeType
    suggested_action: str
    summary: str
    rationale: str
    watch_trigger: str

    @classmethod
    def from_classification(
        cls,
        portfolio_row: PortfolioInputRow,
        classification: ChangeClassification,
    ) -> PremarketAction:
        return cls(
            run_date=classification.run_date,
            symbol=classification.symbol,
            market=portfolio_row.market,
            portfolio_weight_hkd=portfolio_row.portfolio_weight_hkd,
            severity=classification.severity,
            change_type=classification.change_type,
            suggested_action=classification.suggested_action,
            summary=classification.summary,
            rationale=classification.rationale,
            watch_trigger=classification.watch_trigger,
        )

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in PREMARKET_ACTION_FIELDNAMES}


@dataclass(frozen=True)
class WatchlistRow:
    run_date: str
    symbol: str
    market: str
    suggested_action: str
    severity: Severity
    portfolio_weight_hkd: str
    trigger_type: TriggerType
    operator: str
    trigger_price: str
    trigger_text: str
    status: WatchlistStatus
    error: str

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in WATCHLIST_FIELDNAMES}
