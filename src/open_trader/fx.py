from __future__ import annotations

import calendar
from dataclasses import dataclass
from decimal import Decimal


DEFAULT_RATES_TO_HKD = {"USD": Decimal("7.85")}


@dataclass(frozen=True)
class FxQuote:
    currency: str
    fx_date: str
    rate: Decimal
    source: str


def month_end_date(month: str) -> str:
    year_s, month_s = month.split("-", 1)
    year = int(year_s)
    month_i = int(month_s)
    last_day = calendar.monthrange(year, month_i)[1]
    return f"{year:04d}-{month_i:02d}-{last_day:02d}"


class StaticMonthEndFxProvider:
    """Deterministic month-end FX provider."""

    source = "external_month_end_static"

    def __init__(self, month: str, rates_to_hkd: dict[str, Decimal]):
        self.month = month
        self.fx_date = month_end_date(month)
        self.rates_to_hkd = {
            currency.upper(): rate for currency, rate in rates_to_hkd.items()
        }

    def get_rate_to_hkd(self, currency: str) -> FxQuote:
        normalized = currency.upper()
        if normalized == "HKD":
            return FxQuote(
                currency="HKD",
                fx_date=self.fx_date,
                rate=Decimal("1"),
                source=self.source,
            )
        if normalized not in self.rates_to_hkd:
            raise KeyError(f"Missing HKD FX rate for {normalized}")
        return FxQuote(
            currency=normalized,
            fx_date=self.fx_date,
            rate=self.rates_to_hkd[normalized],
            source=self.source,
        )


def convert_to_hkd(amount: Decimal, currency: str, provider: StaticMonthEndFxProvider) -> Decimal:
    quote = provider.get_rate_to_hkd(currency)
    return amount * quote.rate
