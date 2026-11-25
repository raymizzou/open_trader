"""Size and validate paper entries across the configured simulation accounts."""

import json
from collections import defaultdict
from decimal import Decimal

from .paper_evidence import decimal, instant, text

MAX_INSTRUMENTS = 10
ENTRY_FRACTION = Decimal("0.02")


class PortfolioUnknown(ValueError):
    """A budget cannot be proven from the available executions and account data."""


class Portfolio:
    def __init__(self, accounts):
        if not isinstance(accounts, dict) or not accounts:
            raise PortfolioUnknown("account_or_size_unverified")
        self.accounts = accounts
        self.net = {}
        self.cash = {}
        self.footprints = defaultdict(list)
        self.order_ids = set()
        self.unpriced_orders = {}
        self.gross = defaultdict(Decimal)
        self.currency_gaps = set()
        for name, account in accounts.items():
            net, cash = (
                decimal(account.get("net_value")),
                decimal(account.get("available")),
            )
            currency = account.get("currency")
            if (
                account.get("environment") != "SIMULATE"
                or not text(currency)
                or not text(account.get("identity"))
                or net is None
                or net <= 0
                or cash is None
                or cash < 0
                or not isinstance(account.get("positions"), list)
                or not isinstance(account.get("orders"), list)
            ):
                raise PortfolioUnknown("account_or_size_unverified")
            self.net[name] = net
            self.cash[name] = cash
            for field in ("positions", "orders"):
                for item in account[field]:
                    self.observe(name, item, field)

    def observe(self, name, item, kind):
        value, amount = decimal(item.get("notional")), decimal(item.get("quantity"))
        currency, code = item.get("currency"), item.get("code")
        if amount is None or not text(code):
            raise PortfolioUnknown("portfolio_exposure_unverified")
        if not amount and kind == "positions":
            return
        if amount <= 0 and kind == "orders":
            raise PortfolioUnknown("portfolio_exposure_unverified")
        token, order = item.get("token"), item.get("id")
        if currency != self.accounts[name]["currency"]:
            self.currency_gaps.add(name)
        elif value is not None and value > 0:
            self.gross[name] += value
        elif kind == "orders" and text(token) and text(order):
            key = (name, order, code, token)
            if key in self.unpriced_orders:
                raise PortfolioUnknown("portfolio_exposure_unverified")
            self.unpriced_orders[key] = (currency, amount)
        else:
            raise PortfolioUnknown("portfolio_exposure_unverified")
        self.footprints[code].append((name, abs(amount), kind))
        if kind == "orders" and order:
            self.order_ids.add((name, order, code))

    def reserve(self, row, quantity, kind="reserved", *, debit=True):
        name, code = row["account"], row["code"]
        if (
            name not in self.accounts
            or row["currency"] != self.accounts[name]["currency"]
            or self.accounts[name]["identity"] != row["account_identity"]
        ):
            raise PortfolioUnknown("portfolio_account_unverified")
        value = unit_notional(row) * quantity
        if debit:
            self.cash[name] -= value
        self.gross[name] += value
        self.footprints[code].append((name, quantity, kind))

    def quantity(self, row, *, previous_fills=0):
        name, code = row["account"], row["code"]
        account = self.accounts.get(name, {})
        if (
            row["market"] not in account.get("markets", [])
            or row["product"] not in account.get("products", [])
            or account.get("currency") != row["currency"]
            or row["account_identity"] != account.get("identity")
        ):
            return 0, "account_or_size_unverified"
        if name in self.currency_gaps:
            return 0, "portfolio_currency_unverified"
        holdings = self.footprints.get(code, [])
        position_size = sum(
            amount for _, amount, kind in holdings if kind == "positions"
        )
        if (
            any(
                alias != name or kind not in {"positions", "confirmed_fill"}
                for alias, _, kind in holdings
            )
            or position_size > previous_fills
        ):
            return 0, "instrument_busy"
        if holdings and previous_fills == 0:
            return 0, "instrument_busy"
        if not holdings and len(self.footprints) >= MAX_INSTRUMENTS:
            return 0, "portfolio_slots_full"
        unit, lot = unit_notional(row), row["lot"]
        budget = min(
            self.net[name] * ENTRY_FRACTION - previous_fills * unit,
            self.cash[name],
            self.net[name] - self.gross[name],
        )
        if budget < unit * lot:
            return 0, "account_or_size_unverified"
        return min(int(budget // (unit * lot)) * lot, (2**53 // lot) * lot), None


def unit_notional(row):
    return Decimal(row["reference_price"]) * Decimal(row["multiplier"])


def existing_reservations(store, portfolio, now):
    rows = store.execute(
        """SELECT attempts.*, plans.body FROM attempts
           JOIN plans ON plans.id = attempts.plan
           WHERE attempts.state IN ('active', 'unknown') OR attempts.filled > 0"""
    )
    for attempt in rows:
        plan = json.loads(attempt["body"])
        row = next(line for line in plan["lines"] if line["item"] == attempt["item"])
        pending = attempt["state"] in {"active", "unknown"}
        positions = portfolio.footprints.get(row["code"], [])
        has_position = any(
            alias == row["account"] and kind == "positions"
            for alias, _, kind in positions
        )
        has_order = (
            row["account"],
            attempt["order_id"],
            row["code"],
        ) in portfolio.order_ids
        filled = attempt["filled"] or 0
        key = (row["account"], attempt["order_id"], row["code"], attempt["token"])
        if key in portfolio.unpriced_orders:
            currency, amount = portfolio.unpriced_orders.pop(key)
            if currency != row["currency"] or not 0 < amount <= attempt["quantity"]:
                raise PortfolioUnknown("portfolio_exposure_unverified")
            portfolio.reserve(row, amount, debit=False)
        if pending and not has_order:
            portfolio.reserve(
                row, attempt["quantity"] - (filled if has_position else 0)
            )
        elif filled and not has_position and now < instant(row["closes_at"]):
            portfolio.reserve(row, filled, "confirmed_fill", debit=False)
    if portfolio.unpriced_orders:
        raise PortfolioUnknown("portfolio_exposure_unverified")
