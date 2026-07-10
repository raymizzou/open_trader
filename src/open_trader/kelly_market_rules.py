from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KellyMarketCapitalPool:
    market: str
    amount: str
    currency: str
    enabled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "amount": self.amount,
            "currency": self.currency,
            "enabled": self.enabled,
        }


KELLY_MARKET_CAPITAL_POOLS: dict[str, KellyMarketCapitalPool] = {
    "US": KellyMarketCapitalPool(
        market="US",
        amount="30000",
        currency="USD",
        enabled=True,
    ),
    "HK": KellyMarketCapitalPool(
        market="HK",
        amount="200000",
        currency="HKD",
        enabled=True,
    ),
    "CN": KellyMarketCapitalPool(
        market="CN",
        amount="150000",
        currency="CNY",
        enabled=False,
    ),
}


def normalize_kelly_market(value: object) -> str:
    market = str(value or "").strip().upper()
    if market not in KELLY_MARKET_CAPITAL_POOLS:
        raise ValueError("market must be one of: US, HK, CN")
    return market


def kelly_market_currency(market: str) -> str:
    return KELLY_MARKET_CAPITAL_POOLS[normalize_kelly_market(market)].currency


def kelly_market_capital_pool(market: str) -> dict[str, object]:
    return KELLY_MARKET_CAPITAL_POOLS[normalize_kelly_market(market)].to_dict()
