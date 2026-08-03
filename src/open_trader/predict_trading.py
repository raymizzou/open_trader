"""Narrow authenticated Predict order boundary; credentials never leave this module."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from predict_sdk import (
    ApprovalScope,
    Book,
    BuildOrderInput,
    ChainId,
    DepthLevel,
    MarketHelperInput,
    OrderBuilder,
    OrderBuilderOptions,
    Side,
)

from .polymarket_trading import (
    PredictConfig,
    TradingConfig,
    load_predict_api_key,
    load_predict_private_key,
)


PREDICT_REST_URL = "https://api.predict.fun"
_TIMEOUT_SECONDS = 10.0


def _preflight_error_code(exc: BaseException) -> str:
    status = getattr(exc, "code", None)
    if isinstance(status, int):
        if status in {401, 403}:
            return "auth"
        if status == 429 or status >= 500:
            return "unavailable"
        return "rejected"
    if isinstance(exc, (ConnectionError, OSError, TimeoutError, URLError)):
        return "unavailable"
    text = str(exc).lower()
    if "auth" in text or "unauthor" in text or "forbidden" in text:
        return "auth"
    if "sign" in text:
        return "signing"
    return "rejected"


@dataclass(frozen=True, slots=True)
class PredictBuyQuote:
    market_id: str
    token_id: str
    price_per_share_wei: int
    max_collateral_debit: int
    minimum_redeemable_units: int


@dataclass(frozen=True, slots=True)
class PredictLegResult:
    accepted: bool
    status: str
    order_id: str = ""
    error_code: str = "none"


class PredictTradingClient:
    """Synchronous mainnet Predict adapter with injected transport for tests."""

    def __init__(
        self,
        config: PredictConfig,
        builder: object,
        api_key: str,
        *,
        urlopen_fn: Callable[..., object] = urlopen,
    ) -> None:
        self._config = config
        self._builder = builder
        self._api_key = api_key
        self._jwt: str | None = None
        self._urlopen_fn = urlopen_fn
        self._cross_entry_ready: tuple[tuple[object, ...], PredictBuyQuote, Mapping[str, object]] | None = None

    @classmethod
    def from_keychain(
        cls,
        config: TradingConfig,
        *,
        sdk_builder: Callable[..., object] = OrderBuilder.make,
        load_private_key: Callable[[], str] = load_predict_private_key,
        load_api_key: Callable[[], str] = load_predict_api_key,
        urlopen_fn: Callable[..., object] = urlopen,
    ) -> "PredictTradingClient":
        if config.predict is None:
            raise ValueError("predict config required")
        private_key = load_private_key()
        try:
            builder = sdk_builder(
                ChainId.BNB_MAINNET,
                private_key,
                OrderBuilderOptions(predict_account=config.predict.wallet_address),
            )
        except Exception as exc:
            del exc
            raise RuntimeError("predict trading error: signing") from None
        return cls(config.predict, builder, load_api_key(), urlopen_fn=urlopen_fn)

    def _json(self, path: str, *, method: str = "GET", data: object | None = None, auth: bool = False, retry_auth: bool = True) -> Mapping[str, object]:
        headers = {"x-api-key": self._api_key, "User-Agent": "open-trader/0.1"}
        if auth:
            headers["Authorization"] = f"Bearer {self._authenticate()}"
        encoded = json.dumps(data, separators=(",", ":")).encode() if data is not None else None
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{PREDICT_REST_URL}{path}", data=encoded, headers=headers, method=method)
        try:
            with self._urlopen_fn(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except HTTPError as exc:
            if auth and method == "GET" and retry_auth and exc.code == 401:
                self._jwt = None
                return self._json(path, method=method, data=data, auth=True, retry_auth=False)
            raise
        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        if not isinstance(payload, Mapping):
            raise ValueError("invalid predict response")
        return payload

    def _authenticate(self) -> str:
        if self._jwt:
            return self._jwt
        message = self._json("/v1/auth/message")
        raw_message = message.get("message") or _data(message).get("message")
        if not isinstance(raw_message, str) or not raw_message:
            raise RuntimeError("predict trading error: auth")
        try:
            signature = self._builder.sign_predict_account_message(raw_message)
        except Exception as exc:
            del exc
            raise RuntimeError("predict trading error: signing") from None
        response = self._json(
            "/v1/auth",
            method="POST",
            data={"signer": self._config.wallet_address, "signature": signature, "message": raw_message},
        )
        token = response.get("token") or _data(response).get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("predict trading error: auth")
        self._jwt = token
        return token

    def _market(self, market_id: str) -> Mapping[str, object]:
        return _data(self._json(f"/v1/markets/{market_id}", auth=True))

    def quote_market_buy(self, market_id: str, token_id: str, quantity_wei: int) -> PredictBuyQuote:
        market = self._market(market_id)
        book_payload = _data(self._json(f"/v1/markets/{market_id}/orderbook", auth=True))
        book = _book(book_payload)
        amounts = self._builder.get_market_order_amounts(
            MarketHelperInput(Side.BUY, quantity_wei, slippage_bps=0, is_min_amount_out=True), book
        )
        return PredictBuyQuote(
            str(market_id), str(token_id), int(amounts.price_per_share), int(amounts.maker_amount), int(amounts.taker_amount)
        )

    def _signed_order(self, quote: PredictBuyQuote, market: Mapping[str, object]) -> object:
        fee_rate_bps = market.get("feeRateBps")
        order = self._builder.build_order(
            "MARKET",
            BuildOrderInput(Side.BUY, quote.token_id, quote.max_collateral_debit, quote.minimum_redeemable_units, fee_rate_bps),
        )
        typed = self._builder.build_typed_data(
            order,
            is_neg_risk=market.get("isNegRisk") is True,
            is_yield_bearing=market.get("isYieldBearing") is True,
        )
        return self._builder.sign_typed_data_order(typed)

    def _order_body(self, quote: PredictBuyQuote, market: Mapping[str, object]) -> Mapping[str, object]:
        return {"data": {"pricePerShare": quote.price_per_share_wei, "strategy": "MARKET", "slippageBps": "0", "isFillOrKill": True, "isPostOnly": False, "reservedBalancePolicy": "REJECT_MARKET_ORDER", "isMinAmountOut": True, "selfTradePrevention": "CANCEL_MAKER", "order": _plain(self._signed_order(quote, market))}}

    def no_submit_buy_preflight(self, market_id: str, token_id: str, quantity_wei: int) -> PredictLegResult:
        try:
            market = self._market(market_id)
            self._order_body(self.quote_market_buy(market_id, token_id, quantity_wei), market)
            return PredictLegResult(True, "preflight")
        except Exception as exc:
            error_code = _preflight_error_code(exc)
            return PredictLegResult(False, "rejected", error_code=error_code)

    def submit_buy_once(self, market_id: str, token_id: str, quantity_wei: int) -> PredictLegResult:
        try:
            market = self._market(market_id)
            response = self._json("/v1/orders", method="POST", data=self._order_body(self.quote_market_buy(market_id, token_id, quantity_wei), market), auth=True)
            row = _data(response)
            order_id = row.get("hash") or row.get("id")
            return PredictLegResult(True, "accepted", str(order_id or ""))
        except (HTTPError, URLError, OSError):
            return PredictLegResult(False, "ambiguous", error_code="ambiguous")
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")

    def _cross_entry_bound(self, order: Mapping[str, object]) -> tuple[tuple[object, ...], str, str, int, int, Decimal, Decimal, Decimal, Decimal] | None:
        """Validate one persisted cross-leg bound without accepting replacement terms."""

        execution_id = order.get("execution_id")
        idempotency_key = order.get("idempotency_key")
        market_id = order.get("market_id")
        condition_id = order.get("condition_id")
        token_id = order.get("token_id")
        outcome = order.get("outcome")
        if (
            order.get("venue") != "predict.fun"
            or not all(isinstance(value, str) and value.strip() for value in (
                execution_id, idempotency_key, market_id, condition_id, token_id
            ))
            or execution_id != idempotency_key
            or outcome not in {"YES", "NO"}
        ):
            return None
        requested = _number(order.get("requested_quantity"))
        net = _number(order.get("net_quantity"))
        max_price = _number(order.get("max_price"))
        max_cost = _number(order.get("max_cost"))
        maximum_fee = _number(order.get("maximum_fee"))
        calculable_gas = _number(order.get("calculable_gas"))
        requested_units = _scaled_integer(requested, 10**18)
        net_units = _scaled_integer(net, 10**18)
        price_units = _scaled_integer(max_price, 10**6)
        cost_units = _scaled_integer(max_cost, 10**6)
        if (
            requested is None
            or net is None
            or max_price is None
            or max_cost is None
            or maximum_fee is None
            or calculable_gas is None
            or requested_units is None
            or net_units is None
            or price_units is None
            or cost_units is None
            or net > requested
            or maximum_fee < 0
            or calculable_gas < 0
        ):
            return None
        key = (
            execution_id, idempotency_key, market_id, condition_id, token_id, outcome,
            requested_units, net_units, price_units, cost_units,
            format(maximum_fee, "f"), format(calculable_gas, "f"),
        )
        return (
            key, market_id, token_id, requested_units, net_units,
            max_price, max_cost, maximum_fee, calculable_gas,
        )

    def no_submit_cross_buy_preflight(self, order: Mapping[str, object]) -> PredictLegResult:
        """Bind one fresh Predict FOK quote to the approved cross-leg ceiling."""

        bound = self._cross_entry_bound(order)
        self._cross_entry_ready = None
        if bound is None:
            return PredictLegResult(False, "rejected", error_code="rejected")
        (
            key, market_id, token_id, requested_units, net_units,
            max_price, max_cost, maximum_fee, calculable_gas,
        ) = bound
        try:
            market = self._market(market_id)
            book_payload = _data(self._json(f"/v1/markets/{market_id}/orderbook", auth=True))
            stamp = _book_timestamp(book_payload.get("updateTimestampMs"))
            age = None if stamp is None else (datetime.now(UTC) - stamp).total_seconds()
            if stamp is None or age is None or age < 0 or age > 10:
                return PredictLegResult(False, "rejected", error_code="rejected")
            amounts = self._builder.get_market_order_amounts(
                MarketHelperInput(Side.BUY, requested_units, slippage_bps=0, is_min_amount_out=True),
                _book(book_payload),
            )
            quote = PredictBuyQuote(
                market_id, token_id, int(amounts.price_per_share),
                int(amounts.maker_amount), int(amounts.taker_amount),
            )
            if quote.minimum_redeemable_units != net_units:
                return PredictLegResult(False, "rejected", error_code="rejected")
            price = Decimal(quote.price_per_share_wei) / Decimal(10**6)
            debit = Decimal(quote.max_collateral_debit) / Decimal(10**6)
            fee = debit - (Decimal(net_units) / Decimal(10**18)) * price
            if (
                price <= 0
                or price > max_price
                or debit <= 0
                or fee < 0
                or fee > maximum_fee
                or debit + calculable_gas > max_cost
            ):
                return PredictLegResult(False, "rejected", error_code="rejected")
            self._order_body(quote, market)
            self._cross_entry_ready = (key, quote, market)
            return PredictLegResult(True, "preflight")
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")

    def submit_cross_buy_once(self, order: Mapping[str, object]) -> PredictLegResult:
        """Submit only the exact preflight-bound FOK order once."""

        bound = self._cross_entry_bound(order)
        ready = self._cross_entry_ready
        if bound is None or ready is None or ready[0] != bound[0]:
            return PredictLegResult(False, "rejected", error_code="rejected")
        self._cross_entry_ready = None
        try:
            _key, quote, market = ready
            response = self._json(
                "/v1/orders", method="POST", data=self._order_body(quote, market), auth=True
            )
            row = _data(response)
            order_id = row.get("hash") or row.get("id")
            return PredictLegResult(True, "accepted", str(order_id or ""))
        except (HTTPError, URLError, OSError):
            return PredictLegResult(False, "ambiguous", error_code="ambiguous")
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")

    def cross_remediation_option(
        self,
        *,
        venue: str,
        market_id: str,
        condition_id: str,
        token_id: str,
        outcome: str,
        side: str,
        quantity: Decimal,
        maximum_fee: Decimal,
    ) -> dict[str, object]:
        """Refresh one exact Predict BUY completion option without submitting."""

        quantity_wei = _scaled_integer(quantity, 10**18)
        if (
            venue != "predict.fun"
            or side != "BUY"
            or not all(isinstance(value, str) and value.strip() for value in (market_id, condition_id, token_id, outcome))
            or quantity_wei is None
            or not isinstance(maximum_fee, Decimal)
            or not maximum_fee.is_finite()
            or maximum_fee < 0
        ):
            return {"fresh": False}
        try:
            # Read the actual book once and bind the exact resulting builder
            # quote; a submit never silently re-quotes the approved option.
            book_payload = _data(self._json(f"/v1/markets/{market_id}/orderbook", auth=True))
            stamp_ms = book_payload.get("updateTimestampMs")
            stamp = _book_timestamp(stamp_ms)
            if stamp is None or (datetime.now(UTC) - stamp).total_seconds() < 0 or (datetime.now(UTC) - stamp).total_seconds() > 10:
                return {"fresh": False}
            amounts = self._builder.get_market_order_amounts(
                MarketHelperInput(Side.BUY, quantity_wei, slippage_bps=0, is_min_amount_out=True),
                _book(book_payload),
            )
            quote = PredictBuyQuote(
                str(market_id), str(token_id), int(amounts.price_per_share),
                int(amounts.maker_amount), int(amounts.taker_amount),
            )
            if quote.minimum_redeemable_units != quantity_wei:
                return {"fresh": False}
            price = Decimal(quote.price_per_share_wei) / Decimal(10**6)
            max_spend = Decimal(quote.max_collateral_debit) / Decimal(10**6)
            fee = max_spend - quantity * price
            if price <= 0 or price > 1 or max_spend <= 0 or fee < 0 or fee > maximum_fee:
                return {"fresh": False}
            return {
                "fresh": True,
                "checked_at": stamp,
                "option": {
                    "venue": venue,
                    "market_id": market_id,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "outcome": outcome,
                    "side": "BUY",
                    "quantity": quantity,
                    "executable_price": price,
                    "fee": fee,
                    "slippage": Decimal("0"),
                    "residual_dust": Decimal("0"),
                    "max_spend": max_spend,
                },
            }
        except Exception:
            return {"fresh": False}

    def submit_cross_remediation_once(self, order: Mapping[str, object]) -> PredictLegResult:
        """Submit exactly the bound Predict BUY option, never a replacement quote."""

        if (
            order.get("venue") != "predict.fun"
            or order.get("side") != "BUY"
            or not isinstance(order.get("market_id"), str)
            or not isinstance(order.get("token_id"), str)
        ):
            return PredictLegResult(False, "rejected", error_code="rejected")
        quantity = _number(order.get("quantity"))
        price = _number(order.get("executable_price"))
        max_spend = _number(order.get("max_spend"))
        if quantity is None or price is None or max_spend is None or quantity <= 0 or not (Decimal("0") < price <= Decimal("1")) or max_spend <= 0:
            return PredictLegResult(False, "rejected", error_code="rejected")
        units = _scaled_integer(quantity, 10**18)
        price_wei = _scaled_integer(price, 10**6)
        debit = _scaled_integer(max_spend, 10**6)
        if units is None or price_wei is None or debit is None:
            return PredictLegResult(False, "rejected", error_code="rejected")
        try:
            quote = PredictBuyQuote(
                str(order["market_id"]), str(order["token_id"]), price_wei, debit, units
            )
            market = self._market(quote.market_id)
            response = self._json(
                "/v1/orders", method="POST", data=self._order_body(quote, market), auth=True
            )
            row = _data(response)
            order_id = row.get("hash") or row.get("id")
            return PredictLegResult(True, "accepted", str(order_id or ""))
        except (HTTPError, URLError, OSError):
            return PredictLegResult(False, "ambiguous", error_code="ambiguous")
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")

    def account_snapshot(self) -> Mapping[str, object]:
        scope = ApprovalScope("TRADE", False, False, Side.BUY)
        checks = self._builder.check_approvals(self._builder.get_approval_steps(scope))
        return {"wallet_address": self._config.wallet_address, "available_usdt": str(self._builder.balance_of("USDT")), "allowance_ready": all(check.satisfied for check in checks), "open_orders": _data(self._json("/v1/orders", auth=True)).get("orders", ()), "positions": _data(self._json("/v1/positions", auth=True)).get("positions", ()), "checked_at": datetime.now(UTC)}

    def reconcile_buy(self, market_id: str, token_id: str, order_hash: str) -> Mapping[str, object]:
        try:
            order = _data(self._json(f"/v1/orders/{order_hash}", auth=True))
            matches = _rows(self._json("/v1/orders/matches", auth=True), "matches")
            activity = _rows(self._json("/v1/account/activity", auth=True), "activities")
            positions = _rows(self._json(f"/v1/positions?marketId={market_id}", auth=True), "positions")
            identity = (
                order.get("hash") == order_hash
                and str(order.get("tokenId")) == str(token_id)
                and str(order.get("marketId")) == str(market_id)
                and str(order.get("signer", order.get("signerAddress", ""))).lower() == self._config.wallet_address.lower()
            )
            final = str(order.get("status", "")).upper() in {"FILLED", "MATCHED", "COMPLETED"}
            order_amount = _number(order.get("amountFilled"))
            matched = [row for row in matches if str(row.get("orderHash")) == order_hash]
            events = [row for row in activity if str(row.get("orderHash")) == order_hash]
            facts = [(_facts(match), _facts(event)) for match in matched for event in events]
            agreed = next((match for match, event in facts if match is not None and match == event), None)
            position_quantity = sum(
                (
                    _number(row.get("amount")) or Decimal("0")
                    for row in positions
                    if str(row.get("tokenId")) == str(token_id)
                ),
                Decimal("0"),
            )
            positioned = agreed is not None and position_quantity >= agreed[1] and any(
                str(row.get("tokenId")) == str(token_id)
                and _number(row.get("amountDelta", row.get("delta"))) == agreed[1]
                for row in positions
            )
            verified = identity and final and agreed is not None and order_amount == agreed[1] and positioned
            absent = (
                str(order.get("status", "")).upper() in {"NOT_FOUND", "ABSENT"}
                and not matched
                and not events
                and not any(str(row.get("tokenId")) == str(token_id) for row in positions)
            )
            result = {
                "verified": verified,
                "conclusively_absent": absent,
                "status": "verified" if verified else "absent" if absent else "unknown",
            }
            if verified:
                result.update(
                    {
                        "filled_quantity": agreed[1],
                        "position_quantity": position_quantity,
                    }
                )
                try:
                    minimum_order_size = _number(
                        self._market(market_id).get("minimumOrderSize")
                    )
                except Exception:
                    minimum_order_size = None
                if minimum_order_size is not None and minimum_order_size > 0:
                    result["minimum_order_size"] = minimum_order_size
            return result
        except Exception:
            return {"verified": False, "conclusively_absent": False, "status": "unknown"}

    def redeemable_snapshot(self) -> Mapping[str, object]:
        return {"wallet_address": self._config.wallet_address, "positions": _data(self._json("/v1/positions", auth=True)).get("positions", ()), "checked_at": datetime.now(UTC)}


def _data(payload: Mapping[str, object]) -> Mapping[str, object]:
    value = payload.get("data", payload)
    return value if isinstance(value, Mapping) else {}


def _row(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _rows(payload: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]:
    value = _data(payload).get(name, ())
    return tuple(_row(item) for item in value) if isinstance(value, list) else ()


def _number(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _scaled_integer(value: object, scale: int) -> int | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    scaled = number * Decimal(scale)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _book_timestamp(value: object) -> datetime | None:
    try:
        milliseconds = int(value)
        if milliseconds <= 0:
            return None
        return datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _facts(row: Mapping[str, object]) -> tuple[str, Decimal, Decimal] | None:
    transaction = row.get("transactionHash")
    amount = _number(row.get("executedAmount"))
    fee = _number(row.get("fee"))
    return (str(transaction), amount, fee) if isinstance(transaction, str) and transaction and amount is not None and amount > 0 and fee is not None and fee >= 0 else None


def _book(payload: Mapping[str, object]) -> Book:
    def levels(name: str) -> list[DepthLevel]:
        raw = payload.get(name, ())
        if not isinstance(raw, list):
            raise ValueError("invalid predict book")
        return [DepthLevel((int(Decimal(str(row[0])) * 10**18), int(Decimal(str(row[1])) * 10**18))) for row in raw if isinstance(row, list) and len(row) == 2]
    return Book(int(payload.get("marketId", 0)), int(payload.get("updateTimestampMs", 0)), levels("asks"), levels("bids"))


def _plain(value: object) -> object:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return getattr(value, "value", value)
