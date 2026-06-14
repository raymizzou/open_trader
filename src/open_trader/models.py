from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal


Confidence = Literal["high", "medium", "low"]
RiskFlag = Literal["normal", "overweight", "data_check"]


class Market(StrEnum):
    US = "US"
    HK = "HK"
    OTHER = "OTHER"
    CASH = "CASH"


class AssetClass(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    FUND = "fund"
    MONEY_MARKET_FUND = "money_market_fund"
    OPTION = "option"
    CASH = "cash"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Position:
    statement_id: str
    broker: str
    account_alias: str
    market: Market
    asset_class: AssetClass
    symbol: str
    name: str
    currency: str
    quantity: Decimal
    cost_price: Decimal | None
    last_price: Decimal | None
    market_value: Decimal | None
    cost_value: Decimal | None
    unrealized_pnl: Decimal | None
    confidence: Confidence
    notes: str

    def identity_key(self) -> tuple[Market, AssetClass, str, str]:
        return (self.market, self.asset_class, self.symbol.upper(), self.currency.upper())


@dataclass(frozen=True)
class CashBalance:
    statement_id: str
    broker: str
    account_alias: str
    currency: str
    cash_balance: Decimal
    available_balance: Decimal | None
    confidence: Confidence
    notes: str

    @property
    def market(self) -> Market:
        return Market.CASH

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CASH

    @property
    def symbol(self) -> str:
        return f"{self.currency.upper()}_CASH"


@dataclass(frozen=True)
class WarningRecord:
    statement_id: str
    broker: str
    page: int | None
    severity: str
    code: str
    message: str

    def to_row(self) -> dict[str, str]:
        return {
            "statement_id": self.statement_id,
            "broker": self.broker,
            "page": "" if self.page is None else str(self.page),
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class ManifestRecord:
    month: str
    broker: str
    source_file: str
    source_sha256: str
    parsed_at: str
    page_count: int
    parser_version: str
    status: str
