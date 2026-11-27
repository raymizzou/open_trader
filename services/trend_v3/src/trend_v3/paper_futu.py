"""Futu OpenD adapter for the configured simulation accounts only."""

import importlib
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from .paper_evidence import decimal, instant, text

SIMULATE = "SIMULATE"
STOCK_PRODUCTS = {"STOCK": ["stock", "etf"], "STOCK_AND_OPTION": ["stock", "etf"]}
NATIVE_STOCK_CURRENCIES = {"US": "USD", "HK": "HKD", "CN": "CNY"}
SECURITIES_MARKETS = {
    "US": ("US",),
    "HK": ("HK",),
    "CN": ("CN", "SH", "SZ"),
    "JP": ("JP",),
    "SG": ("SG",),
    "AU": ("AU",),
    "CA": ("CA",),
    "MY": ("MY",),
}
FUTURES_MARKETS = {
    f"FUTURES_SIMULATE_{market}": (market,) for market in ("US", "HK", "JP", "SG")
}
ACTIVE = {
    "WAITING_SUBMIT",
    "SUBMITTING",
    "SUBMITTED",
    "FILLED_PART",
    "CANCELLING_PART",
    "CANCELLING_ALL",
}
TERMINAL = {
    "FILLED_ALL": "filled",
    "CANCELLED_PART": "cancelled",
    "CANCELLED_ALL": "cancelled",
    "FAILED": "rejected",
}
OPEN_STATES = {"MORNING", "AFTERNOON"}
POST_CLOSE_STATES = {
    "AFTER_HOURS_BEGIN",
    "AFTER_HOURS_END",
    "CLOSED",
    "STIB_AFTER_HOURS_END",
}
FUTURE_POST_CLOSE_STATES = {"FUTURE_DAY_CLOSE", "FUTURE_CLOSE"}
FUTURE_OPEN_STATES = {
    "FUTURE_DAY_OPEN",
    "FUTURE_AFTERNOON",
    "FUTURE_OPEN",
    "FUTURE_BREAK_OVER",
    "NIGHT_OPEN",
}


class BrokerUnavailable(RuntimeError):
    """A simulation account, market or broker result could not be verified."""


class MissedOpening(RuntimeError):
    """The admission deadline expired before any broker write."""


def integer(value):
    number = decimal(value)
    if number is not None and number == number.to_integral_value():
        return int(number)
    return None


def order_id(value):
    # OpenD order IDs are opaque text. Accept decimal integers from older SDKs.
    if type(value) is int and value > 0:
        return str(value)
    return value if text(value) and value == value.strip() else None


class FutuPaperBroker:
    environment = SIMULATE

    def __init__(self, config, *, sdk=None, clock=None):
        self.settings = {
            name: dict(account) for name, account in config.get("accounts", {}).items()
        }
        self.explicit_accounts = bool(self.settings)
        self.discovery = None
        self.address = {"host": config.get("host", "127.0.0.1"), "port": config["port"]}
        if self.address["host"] not in {"127.0.0.1", "::1"}:
            raise ValueError("paper entry requires local OpenD")
        if (
            type(self.address["port"]) is not int
            or not 0 < self.address["port"] < 65536
        ):
            raise ValueError("invalid OpenD port")
        accounts = [account.get("id") for account in self.settings.values()]
        if any(type(value) is not int or value < 1 for value in accounts) or len(
            set(accounts)
        ) != len(accounts):
            raise ValueError("paper accounts require distinct explicit IDs")
        self.sdk = sdk
        self.clock = clock
        self.contexts = {}
        self.quote = None

    def receipt_reader(self):
        """Use separate SDK contexts and expose no placement interface."""
        from copy import deepcopy

        broker = FutuPaperBroker(
            {**self.address, "accounts": deepcopy(self.settings)},
            sdk=self.sdk,
            clock=self.clock,
        )

        class Reader:
            environment = SIMULATE
            snapshot = broker.snapshot
            restore_scope = broker.restore_scope
            find_order = broker.find_order
            close = broker.close

        return Reader()

    def open_sdk(self):
        if self.sdk is None:
            try:
                self.sdk = importlib.import_module("futu")
            except ImportError as error:
                raise BrokerUnavailable("Futu SDK unavailable") from error
        return self.sdk

    @staticmethod
    def checked(call, **parameters):
        status, result = call(**parameters)
        if status != 0:
            # OpenD error text can contain broker account details.
            raise BrokerUnavailable("OpenD request failed")
        return result if isinstance(result, list) else result.to_dict(orient="records")

    def context(self, account):
        category, market = account["category"], account["trade_market"]
        key = (category, market)
        if key not in self.contexts:
            sdk = self.open_sdk()
            if category == "securities" and market in {*SECURITIES_MARKETS, "N/A"}:
                context = sdk.OpenSecTradeContext(
                    filter_trdmarket=market, **self.address
                )
            elif category == "futures" and market in FUTURES_MARKETS:
                context = sdk.OpenFutureTradeContext(**self.address)
            elif category == "crypto" and market == "CRYPTO":
                context = sdk.OpenCryptoTradeContext(**self.address)
            else:
                raise BrokerUnavailable("unsupported simulation account category")
            self.contexts[key] = context
        return self.contexts[key]

    def planning_quote(self, method, **parameters):
        if self.quote is None:
            self.quote = self.open_sdk().OpenQuoteContext(**self.address)
        return self.checked(getattr(self.quote, method), **parameters)

    @staticmethod
    def discovered(rows, category, market, currency):
        eligible = [
            row
            for row in rows
            if row.get("trd_env") == SIMULATE
            and row.get("acc_status") == "ACTIVE"
            and row.get("trdmarket_auth") == [market]
            and (
                (category == "securities" and row.get("sim_acc_type") in STOCK_PRODUCTS)
                or (category == "futures" and row.get("sim_acc_type") == "FUTURES")
            )
        ]
        if len(eligible) > 1:
            raise BrokerUnavailable("simulation account selection is ambiguous")
        identifier = integer(eligible[0].get("acc_id")) if eligible else None
        if identifier is None or identifier <= 0:
            return None
        return {
            "id": identifier,
            "currency": currency,
            "category": category,
            "trade_market": market,
        }

    def planning_account(self, market, product, currency, code, multiplier):
        category = "futures" if product == "future" else "securities"
        trade_market = (
            f"FUTURES_SIMULATE_{market}"
            if product == "future"
            else ("CN" if market in {"SH", "SZ"} else market)
        )
        if not self.explicit_accounts and self.discovery is None:
            context = self.context({"category": "securities", "trade_market": "N/A"})
            inventory = self.checked(context.get_acc_list)
            discovered = {}
            for exchange, denomination in NATIVE_STOCK_CURRENCIES.items():
                value = self.discovered(inventory, "securities", exchange, denomination)
                if value is not None:
                    discovered[f"paper-{exchange.lower()}"] = value
            identities = {account["id"] for account in discovered.values()}
            if len(identities) != len(discovered):
                raise BrokerUnavailable("simulation account identity is ambiguous")
            self.settings.update(discovered)
            self.discovery = inventory
        matches = [
            name
            for name, account in self.settings.items()
            if (account["category"], account["trade_market"], account["currency"])
            == (category, trade_market, currency)
        ]
        if not matches and not self.explicit_accounts and category == "futures":
            ctx = self.context({"category": category, "trade_market": trade_market})
            value = self.discovered(
                self.checked(ctx.get_acc_list), category, trade_market, currency
            )
            if value and value["id"] not in {
                account["id"] for account in self.settings.values()
            }:
                alias = f"paper-{trade_market.lower()}"
                self.settings[alias] = value
                matches = [alias]
        if len(matches) != 1:
            return None
        (alias,) = matches
        if product == "future":
            values = self.settings[alias].setdefault("contract_multipliers", {})
            if code in values and decimal(values[code]) != decimal(multiplier):
                return None
            values[code] = multiplier
        return alias

    def verified_account(self, name):
        account = self.settings.get(name)
        if account is None:
            raise BrokerUnavailable("no configured simulation account for route")
        ctx = self.context(account)
        # SDK inventory is account-neutral and takes no trd_env parameter.
        matches = [
            row
            for row in self.checked(ctx.get_acc_list)
            if integer(row.get("acc_id")) == account["id"]
            and row.get("trd_env") == SIMULATE
        ]
        if len(matches) != 1 or matches[0].get("acc_status") != "ACTIVE":
            raise BrokerUnavailable("simulation account is unavailable")
        (record,) = matches
        market, category = account["trade_market"], account["category"]
        authorized = record.get("trdmarket_auth")
        if not isinstance(authorized, list) or authorized != [market]:
            raise BrokerUnavailable("simulation account market does not match")
        if category == "securities":
            products = STOCK_PRODUCTS.get(record.get("sim_acc_type"))
            markets = SECURITIES_MARKETS[market]
        elif category == "futures":
            products = ["future"] if record.get("sim_acc_type") == "FUTURES" else []
            markets = FUTURES_MARKETS[market]
        else:
            # OpenD identifies a crypto account by its dedicated context and
            # permission. The SDK has no distinct crypto sim_acc_type enum.
            products, markets = ["crypto"], ("CC", "CRYPTO")
        if not products:
            raise BrokerUnavailable("simulation account product does not match")
        return ctx, account, markets, products, record

    def account_scope(self):
        return {
            name: {
                "identity": self.account_identity(account),
                "category": account["category"],
                "market": account["trade_market"],
                "currency": account["currency"],
                "multipliers": dict(account.get("contract_multipliers") or {}),
            }
            for name, account in self.settings.items()
        }

    def restore_scope(self, expected):
        if not expected:
            raise BrokerUnavailable("persisted simulation account scope is unavailable")
        if not self.explicit_accounts:
            self.settings.clear()
            self.discovery = None
        for account in expected.values():
            market = account["market"]
            if account["category"] == "futures":
                exchange = market.removeprefix("FUTURES_SIMULATE_")
                for code, multiplier in account["multipliers"].items():
                    self.planning_account(
                        exchange, "future", account["currency"], code, multiplier
                    )
            elif not self.explicit_accounts:
                self.planning_account(market, "stock", account["currency"], "", "1")
        if self.account_scope() != expected:
            raise BrokerUnavailable("persisted simulation account scope changed")

    def route(self, line):
        ctx, account, markets, products, record = self.verified_account(line["account"])
        if (
            line["market"] not in markets
            or line["product"] not in products
            or line["currency"] != account["currency"]
            or line["order_type"] != "MARKET"
            or line["account_identity"] != self.account_identity(account)
            or (
                line["product"] == "future"
                and decimal(line["multiplier"])
                != decimal(account.get("contract_multipliers", {}).get(line["code"]))
            )
        ):
            raise BrokerUnavailable("unverified paper order route")
        return ctx, account, record

    @staticmethod
    def parameters(account):
        return {"trd_env": SIMULATE, "acc_id": account["id"]}

    @staticmethod
    def account_identity(account):
        value = f"Futu:{SIMULATE}:{account['id']}:{account['trade_market']}"
        return sha256(value.encode()).hexdigest()

    @staticmethod
    def native_stock_account(account):
        return (
            account["category"] == "securities"
            and account["trade_market"] in NATIVE_STOCK_CURRENCIES
            and NATIVE_STOCK_CURRENCIES.get(account["trade_market"])
            == account["currency"]
        )

    @classmethod
    def available_funds(cls, funds, account):
        currency = funds.get("currency")
        if currency == account["currency"]:
            available = decimal(funds.get("available_funds"))
            if available is None:
                available = decimal(funds.get("net_cash_power"))
            return available
        # Funds for the verified single-market stock sim accounts omit the
        # denomination. Only cash after holds is usable. Power can narrow it.
        if (
            currency != "N/A"
            or not cls.native_stock_account(account)
            or funds.get("available_funds") != "N/A"
            or funds.get("net_cash_power") != "N/A"
        ):
            raise BrokerUnavailable("simulation account balance currency is unverified")
        cash = decimal(funds.get("cash"))
        held = decimal(funds.get("frozen_cash"))
        power = decimal(funds.get("power"))
        if cash is None or held is None or held < 0 or power is None or power < 0:
            raise BrokerUnavailable("simulation account usable cash is unverified")
        return max(Decimal(0), min(cash - held, power))

    @classmethod
    def exposure_currency(cls, row, account):
        currency = row.get("currency")
        if (
            cls.native_stock_account(account)
            and account["trade_market"] == "CN"
            and currency == "CNH"
            and str(row.get("code", "")).startswith(("SH.", "SZ."))
        ):
            # SDK label for this A-share paper ledger. It provides no FX rate.
            return "CNY"
        return currency

    def snapshot(self):
        result = {}
        for name in self.settings:
            ctx, account, markets, products, _ = self.verified_account(name)
            parameters = self.parameters(account)
            currency = account["currency"]
            sdk_currency = (
                "CNH"
                if currency == "CNY" and self.native_stock_account(account)
                else currency
            )
            balances = self.checked(
                ctx.accinfo_query,
                **parameters,
                currency=sdk_currency,
                refresh_cache=True,
            )
            if len(balances) != 1:
                raise BrokerUnavailable("simulation account balance is unverified")
            funds = balances[0]
            available = self.available_funds(funds, account)
            positions = self.checked(
                ctx.position_list_query,
                **parameters,
                currency=sdk_currency,
                refresh_cache=True,
            )
            orders = self.checked(
                ctx.order_list_query, **parameters, refresh_cache=True
            )
            result[name] = {
                "environment": SIMULATE,
                "markets": markets,
                "products": products,
                "identity": self.account_identity(account),
                "currency": currency,
                "net_value": funds.get("total_assets"),
                "available": available,
                "positions": [self.position(row, account) for row in positions],
                "orders": [
                    self.order(row, account)
                    for row in orders
                    if row.get("order_status") not in TERMINAL
                ],
            }
        return result

    @classmethod
    def position(cls, row, account):
        if integer(row.get("acc_id")) != account["id"]:
            raise BrokerUnavailable("position account mismatch")
        notional = decimal(row.get("market_val"))
        amount = decimal(row.get("qty"))
        if account["category"] == "futures" and amount:
            # A futures account's market_val may describe margin rather than
            # economic notional. Use independently configured contract metadata.
            multiplier = decimal(
                account.get("contract_multipliers", {}).get(row.get("code"))
            )
            mark = decimal(row.get("nominal_price"))
            notional = abs(amount) * multiplier * mark if multiplier and mark else None
        return {
            "code": row.get("code"),
            "currency": cls.exposure_currency(row, account),
            "quantity": amount,
            "mark": row.get("nominal_price"),
            "average_entry": row.get("cost_price")
            if row.get("cost_price_valid") is True
            else None,
            "unrealized_pnl": row.get("unrealized_pl"),
            "notional": abs(notional) if notional is not None else None,
        }

    @classmethod
    def order(cls, row, account):
        total, filled = decimal(row.get("qty")), decimal(row.get("dealt_qty"))
        broker_id, status = order_id(row.get("order_id")), row.get("order_status")
        outstanding = (
            total - filled if total is not None and filled is not None else None
        )
        working = (
            broker_id is not None
            and status in ACTIVE
            and outstanding is not None
            and outstanding > 0
            and filled >= 0
            and (status != "FILLED_PART" or filled > 0)
        )
        price = decimal(row.get("price"))
        multiplier = (
            decimal(account.get("contract_multipliers", {}).get(row.get("code")))
            if account["category"] == "futures"
            else Decimal(1)
        )
        return {
            "code": row.get("code"),
            "currency": cls.exposure_currency(row, account),
            "quantity": outstanding,
            "notional": price * outstanding * multiplier
            if price and outstanding and multiplier
            else None,
            "id": broker_id,
            "token": row.get("remark"),
            "state": "active" if working else "unknown",
        }

    def capacity(self, line):
        ctx, account, record = self.route(line)
        short = line["direction"] == "short"
        if (
            short
            and line["product"] != "future"
            and (
                line["market"] not in {"US", "HK"} or record.get("acc_type") != "MARGIN"
            )
        ):
            return None
        rows = self.checked(
            ctx.acctradinginfo_query,
            **self.parameters(account),
            order_type="MARKET",
            code=line["code"],
            price=float(line["reference_price"]),
            session="RTH",
        )
        if len(rows) != 1:
            return None
        field = "max_sell_short" if short else "max_cash_buy"
        if line["product"] == "future" and not short:
            # Futures capacity uses margin. Portfolio limits use gross notional.
            field = "max_cash_and_margin_buy"
        return integer(rows[0].get(field))

    def market_open(self, line, *, after_close=False):
        # Quote state is exchange-wide and takes no trading-environment parameter.
        rows = self.planning_quote("get_market_state", code_list=[line["code"]])
        if line["product"] == "future":
            accepted = FUTURE_POST_CLOSE_STATES if after_close else FUTURE_OPEN_STATES
        else:
            accepted = POST_CLOSE_STATES if after_close else OPEN_STATES
        return (
            len(rows) == 1
            and rows[0].get("code") == line["code"]
            and (rows[0].get("market_state") in accepted)
        )

    def place_market(self, line, quantity, token, *, deadline=None, on_submit=None):
        ctx, account, _ = self.route(line)
        now = self.clock() if self.clock is not None else datetime.now(UTC)
        if deadline is not None and now >= deadline:
            raise MissedOpening
        if on_submit is not None:
            on_submit(now)
        rows = self.checked(
            ctx.place_order,
            **self.parameters(account),
            price=0.0,
            qty=float(quantity),
            code=line["code"],
            trd_side="BUY" if line["direction"] == "long" else "SELL",
            order_type="MARKET",
            remark=token,
            time_in_force="DAY",
            fill_outside_rth=False,
            session="RTH",
        )
        if len(rows) != 1:
            return None
        (row,) = rows
        if row.get("trd_env", SIMULATE) != SIMULATE:
            return None
        return self.normalize(row, line, token, quantity=quantity)

    def find_order(self, line, token, known_id, quantity):
        account = self.settings.get(line["account"])
        if not self.explicit_accounts and (
            account is None
            or (
                line["product"] == "future"
                and line["code"] not in account.get("contract_multipliers", {})
            )
        ):
            quote_market = line["code"].split(".", 1)[0]
            recovered = self.planning_account(
                quote_market,
                line["product"],
                line["currency"],
                line["code"],
                line["multiplier"],
            )
            if recovered != line["account"]:
                raise BrokerUnavailable("original simulation account is unavailable")
        ctx, account, _ = self.route(line)
        params = {**self.parameters(account), "code": line["code"]}
        current = self.checked(ctx.order_list_query, **params, refresh_cache=True)
        history = self.checked(
            ctx.history_order_list_query,
            **params,
            start=line["session_date"],
            end=instant(line["closes_at"]).date().isoformat(),
        )
        matches = []
        for row in current + history:
            result = self.normalize(
                row, line, token, quantity=quantity, known_id=known_id
            )
            if result is not None and result not in matches:
                matches.append(result)
        # Two conflicting returns or a missing marker cannot prove terminal state.
        if len(matches) > 1:
            return {"state": "unknown", "conflict": True}
        return matches[0] if matches else None

    @staticmethod
    def normalize(row, line, token, *, quantity, known_id=None):
        found = order_id(row.get("order_id"))
        sides = {"BUY"} if line["direction"] == "long" else {"SELL", "SELL_SHORT"}
        if (
            not found
            or (known_id and found != known_id)
            or row.get("remark") != token
            or row.get("code") != line["code"]
            or row.get("trd_side") not in sides
            or row.get("order_type") != "MARKET"
            or integer(row.get("qty")) != quantity
        ):
            return None
        status = row.get("order_status")
        state = "active" if status in ACTIVE else TERMINAL.get(status)
        filled = integer(row.get("dealt_qty"))
        if state is None or filled is None:
            return None
        price = decimal(row.get("dealt_avg_price"))
        notional = (
            price * filled * Decimal(line["multiplier"]) if price and filled else None
        )
        return {
            "id": found,
            "state": state,
            "filled": filled,
            "filled_notional": notional,
        }

    def close(self):
        try:
            for context in self.contexts.values():
                context.close()
        finally:
            if self.quote is not None:
                self.quote.close()
