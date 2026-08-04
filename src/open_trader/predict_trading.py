"""Narrow authenticated Predict order boundary; credentials never leave this module."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from eth_account import Account
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
PREDICT_DECIMALS = 18
PREDICT_BASE_UNITS = 10**PREDICT_DECIMALS
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
        gas_signer: str,
        urlopen_fn: Callable[..., object] = urlopen,
    ) -> None:
        self._config = config
        self._builder = builder
        self._api_key = api_key
        self._gas_signer = gas_signer
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
                OrderBuilderOptions(
                    precision=PREDICT_DECIMALS,
                    predict_account=config.predict.wallet_address,
                ),
            )
            gas_signer = Account.from_key(private_key).address
        except Exception as exc:
            del exc
            raise RuntimeError("predict trading error: signing") from None
        return cls(config.predict, builder, load_api_key(), gas_signer=gas_signer, urlopen_fn=urlopen_fn)

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
            body = self._order_body(
                self.quote_market_buy(market_id, token_id, quantity_wei), market
            )
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")
        return self._post_order_once(body)

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
        requested_units = _scaled_integer(requested, PREDICT_BASE_UNITS)
        net_units = _scaled_integer(net, PREDICT_BASE_UNITS)
        price_units = _scaled_integer(max_price, PREDICT_BASE_UNITS)
        cost_units = _scaled_integer(max_cost, PREDICT_BASE_UNITS)
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
            or calculable_gas <= 0
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
            price = Decimal(quote.price_per_share_wei) / Decimal(PREDICT_BASE_UNITS)
            debit = Decimal(quote.max_collateral_debit) / Decimal(PREDICT_BASE_UNITS)
            fee = debit - (Decimal(net_units) / Decimal(PREDICT_BASE_UNITS)) * price
            if (
                price <= 0
                or price > max_price
                or debit <= 0
                or fee < 0
                or fee > maximum_fee
                or debit > max_cost
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
            body = self._order_body(quote, market)
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")
        return self._post_order_once(body)

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

        quantity_wei = _scaled_integer(quantity, PREDICT_BASE_UNITS)
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
            price = Decimal(quote.price_per_share_wei) / Decimal(PREDICT_BASE_UNITS)
            max_spend = Decimal(quote.max_collateral_debit) / Decimal(PREDICT_BASE_UNITS)
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
        units = _scaled_integer(quantity, PREDICT_BASE_UNITS)
        price_wei = _scaled_integer(price, PREDICT_BASE_UNITS)
        debit = _scaled_integer(max_spend, PREDICT_BASE_UNITS)
        if units is None or price_wei is None or debit is None:
            return PredictLegResult(False, "rejected", error_code="rejected")
        try:
            quote = PredictBuyQuote(
                str(order["market_id"]), str(order["token_id"]), price_wei, debit, units
            )
            market = self._market(quote.market_id)
            body = self._order_body(quote, market)
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")
        return self._post_order_once(body)

    def _post_order_once(self, body: Mapping[str, object]) -> PredictLegResult:
        try:
            token = self._authenticate()
            request = Request(
                f"{PREDICT_REST_URL}/v1/orders",
                data=json.dumps(body, separators=(",", ":")).encode(),
                headers={
                    "x-api-key": self._api_key,
                    "User-Agent": "open-trader/0.1",
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")
        try:
            with self._urlopen_fn(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
            payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            if not isinstance(payload, Mapping):
                raise ValueError("invalid predict response")
            data = _data(payload)
            order_id = data.get("orderId")
            order_hash = data.get("orderHash")
            if (
                payload.get("success") is not True
                or not isinstance(order_id, str)
                or not order_id
                or not isinstance(order_hash, str)
                or not order_hash
            ):
                raise ValueError("invalid predict order response")
            return PredictLegResult(True, "accepted", order_hash)
        except Exception:
            return PredictLegResult(False, "ambiguous", error_code="ambiguous")

    def approval_facts(self, market_id: str, exact_debit_wei: int = 0) -> Mapping[str, object]:
        scope = self._approval_scope_for_market(market_id)
        return self._approval_facts_for_scope(scope, exact_debit_wei=exact_debit_wei)

    def set_exact_buy_allowance(self, market_id: str, exact_debit_wei: int) -> Mapping[str, object]:
        amount = _non_negative_int(exact_debit_wei, "approval")
        if amount <= 0:
            return {
                "success": False,
                "status": "rejected",
                "error_code": "invalid_amount",
                "possible_mutation": False,
            }
        scope = self._approval_scope_for_market(market_id)
        facts = dict(self._approval_facts_for_scope(scope, exact_debit_wei=amount))
        step = self._approval_step(scope)
        try:
            result = self._builder.set_approval(step, approved=True, amount=amount)
        except Exception:
            return self._allowance_result(
                False, "receipt_ambiguous", facts, possible_mutation=True
            )
        allowance = self._post_allowance(result, step, expected=amount)
        if allowance is None:
            possible_mutation = True
            initial_allowance = _number(facts.get("allowance_raw"))
            if _receipt_status(getattr(result, "receipt", None)) == 0:
                try:
                    post_allowance = self._raw_allowance(step)
                except Exception:
                    pass
                else:
                    facts["allowance"] = _usdt_string(post_allowance)
                    facts["allowance_raw"] = str(post_allowance)
                    facts["allowance_breaker"] = post_allowance > 0
                    possible_mutation = not (
                        initial_allowance == 0 and post_allowance == 0
                    )
            return self._allowance_result(
                False,
                _receipt_error_code(result),
                facts,
                result,
                possible_mutation=possible_mutation,
            )
        if allowance != amount:
            facts["allowance"] = _usdt_string(allowance)
            facts["allowance_raw"] = str(allowance)
            facts["allowance_breaker"] = allowance > 0
            return self._allowance_result(
                False, "allowance_mismatch", facts, result, possible_mutation=True
            )
        facts["allowance"] = _usdt_string(allowance)
        facts["allowance_raw"] = str(allowance)
        facts["allowance_breaker"] = allowance > 0
        return self._allowance_result(
            True, "none", facts, result, possible_mutation=True
        )

    def clear_buy_allowance(self, market_id: str) -> Mapping[str, object]:
        scope = self._approval_scope_for_market(market_id)
        facts = dict(self._approval_facts_for_scope(scope, exact_debit_wei=0))
        step = self._approval_step(scope)
        try:
            result = self._builder.set_approval(step, approved=False)
        except Exception:
            return self._allowance_result(False, "rejected", facts)
        allowance = self._post_allowance(result, step, expected=0)
        if allowance is None:
            return self._allowance_result(False, _receipt_error_code(result), facts)
        if allowance != 0:
            facts["allowance"] = _usdt_string(allowance)
            facts["allowance_raw"] = str(allowance)
            facts["allowance_breaker"] = allowance > 0
            return self._allowance_result(False, "allowance_mismatch", facts, result)
        facts["allowance"] = "0"
        facts["allowance_raw"] = "0"
        facts["allowance_breaker"] = False
        return self._allowance_result(True, "none", facts, result)

    def account_snapshot(self) -> Mapping[str, object]:
        facts = self._approval_facts_for_scope(
            ApprovalScope("TRADE", False, False, Side.BUY),
            exact_debit_wei=0,
        )
        open_orders = self._paged_rows("/v1/orders", "orders")
        positions = _canonical_positions(self._paged_rows("/v1/positions", "positions"))
        allowance = _non_negative_int(facts["allowance_raw"], "allowance")
        minimum_top_up = _number(facts["minimum_top_up_bnb"]) or Decimal("0")
        return {
            "wallet_address": self._config.wallet_address,
            "predict_account": facts["predict_account"],
            "gas_signer": facts["gas_signer"],
            "available_usdt": facts["available_usdt"],
            "available_usdt_raw": facts["available_usdt_raw"],
            "allowance": facts["allowance"],
            "allowance_raw": facts["allowance_raw"],
            "scope_ready": facts["scope_ready"],
            "gas_ready": minimum_top_up == 0,
            "approval_scope": facts["approval_scope"],
            "bnb_balance": facts["bnb_balance"],
            "required_bnb": facts["required_bnb"],
            "minimum_top_up_bnb": facts["minimum_top_up_bnb"],
            "allowance_breaker": allowance > 0 and not _has_active_execution(open_orders),
            "open_orders": open_orders,
            "positions": positions,
            "checked_at": datetime.now(UTC),
        }

    def reconcile_buy(self, market_id: str, token_id: str, order_hash: str) -> Mapping[str, object]:
        try:
            order_missing = False
            try:
                order = _order_data(self._json(f"/v1/orders/{order_hash}", auth=True))
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                order = {}
                order_missing = True
            order_list = self._paged_rows("/v1/orders", "orders")
            matches = self._paged_rows(
                f"/v1/orders/matches?{urlencode({'orderHashes': order_hash})}",
                "matches",
            )
            activity = self._paged_rows("/v1/account/activity", "activities")
            positions = _canonical_positions(
                self._paged_rows(
                    f"/v1/positions?{urlencode({'marketId': market_id})}",
                    "positions",
                )
            )
            identity = (
                order.get("hash") == order_hash
                and str(order.get("tokenId")) == str(token_id)
                and str(order.get("marketId")) == str(market_id)
                and str(order.get("signer", order.get("signerAddress", ""))).lower() == self._config.wallet_address.lower()
            )
            final = str(order.get("status", "")).upper() in {"FILLED", "MATCHED", "COMPLETED"}
            order_amount = _number(order.get("amountFilled"))
            matched = [
                fact
                for row in matches
                if (
                    fact := _match_facts(
                        row,
                        order_hash=order_hash,
                        market_id=market_id,
                        token_id=token_id,
                        signer=self._config.wallet_address,
                    )
                )
                is not None
            ]
            events = [
                fact
                for row in activity
                if (
                    fact := _activity_facts(
                        row,
                        order_hash=order_hash,
                        market_id=market_id,
                        token_id=token_id,
                    )
                )
                is not None
            ]
            facts = [(match, event) for match in matched for event in events]
            agreed = next((match for match, event in facts if match is not None and match == event), None)
            trade_ids = list(
                dict.fromkeys(
                    match[0]
                    for match, event in facts
                    if match is not None and match == event
                )
            )
            matching_positions = [
                row
                for row in positions
                if _row_identity(row, market_id=market_id, token_id=token_id)
            ]
            position_amounts = [_number(row.get("quantity")) for row in matching_positions]
            if any(amount is None for amount in position_amounts):
                raise ValueError("invalid Predict position amount")
            position_quantity = sum(
                (amount for amount in position_amounts if amount is not None), Decimal("0")
            )
            positioned = agreed is not None and position_quantity >= agreed[1]
            verified = identity and final and agreed is not None and order_amount == agreed[1] and positioned
            absent = (
                order_missing
                and not any(
                    _order_data(row).get("hash") == order_hash for row in order_list
                )
                and not matched
                and not events
                and not matching_positions
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
                        "actual_fee": agreed[2],
                        "execution_proof": {
                            "verified": True,
                            "venue": "predict.fun",
                            "order_ids": [order_hash],
                            "trade_ids": trade_ids,
                            "fee": agreed[2],
                        },
                    }
                )
                result["minimum_order_size"] = Decimal("0.01")
            return result
        except Exception:
            return {"verified": False, "conclusively_absent": False, "status": "unknown"}

    def _paged_rows(
        self, path: str, name: str
    ) -> tuple[Mapping[str, object], ...]:
        rows: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page_path = path
            if cursor is not None:
                page_path += ("&" if "?" in path else "?") + urlencode({"after": cursor})
            payload = self._json(page_path, auth=True)
            rows.extend(_rows_exact(payload, name))
            next_cursor = payload.get("cursor")
            if next_cursor is None:
                return tuple(rows)
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen
            ):
                raise ValueError("invalid Predict cursor")
            seen.add(next_cursor)
            cursor = next_cursor

    def redeemable_snapshot(self) -> Mapping[str, object]:
        return {
            "wallet_address": self._config.wallet_address,
            "positions": _canonical_positions(
                self._paged_rows("/v1/positions", "positions")
            ),
            "checked_at": datetime.now(UTC),
        }

    def _approval_scope_for_market(self, market_id: str) -> ApprovalScope:
        market = self._market(market_id)
        is_neg_risk = market.get("isNegRisk")
        is_yield_bearing = market.get("isYieldBearing")
        if not isinstance(is_neg_risk, bool) or not isinstance(is_yield_bearing, bool):
            raise RuntimeError("predict trading error: market")
        if is_neg_risk or is_yield_bearing:
            raise RuntimeError("predict trading error: market")
        return ApprovalScope("TRADE", is_neg_risk, is_yield_bearing, Side.BUY)

    def _approval_facts_for_scope(
        self,
        scope: ApprovalScope,
        *,
        exact_debit_wei: int,
    ) -> Mapping[str, object]:
        amount = _non_negative_int(exact_debit_wei, "approval")
        step = self._approval_step(scope)
        allowance_raw = self._raw_allowance(step)
        available_usdt_raw = _non_negative_int(
            self._builder.balance_of("USDT", self._config.wallet_address),
            "allowance",
        )
        set_cost = self._approval_cost_wei(step, amount)
        clear_cost = self._approval_cost_wei(step, 0)
        bnb_balance_wei = self._signer_bnb_balance_wei()
        required_bnb_wei = set_cost + clear_cost
        minimum_top_up_wei = max(required_bnb_wei - bnb_balance_wei, 0)
        return {
            "predict_account": self._config.wallet_address,
            "gas_signer": self._gas_signer,
            "available_usdt": _usdt_string(available_usdt_raw),
            "available_usdt_raw": str(available_usdt_raw),
            "allowance": _usdt_string(allowance_raw),
            "allowance_raw": str(allowance_raw),
            "allowance_breaker": allowance_raw > 0,
            "exact_debit_wei": amount,
            "approval_scope": {
                "operation": str(scope.operation),
                "side": "BUY",
                "is_neg_risk": scope.is_neg_risk,
                "is_yield_bearing": scope.is_yield_bearing,
            },
            "scope_ready": True,
            "approval_step_id": str(getattr(step, "id", "")),
            "approval_spender": str(getattr(step, "spender", "")),
            "bnb_balance": _bnb_wei_string(bnb_balance_wei),
            "required_bnb": _bnb_wei_string(required_bnb_wei),
            "minimum_top_up_bnb": _bnb_wei_string(minimum_top_up_wei),
        }

    def _approval_step(self, scope: ApprovalScope) -> object:
        steps = tuple(self._builder.get_approval_steps(scope))
        if len(steps) != 1:
            raise RuntimeError("predict trading error: approval")
        step = steps[0]
        if getattr(step, "type", None) != "ERC20_ALLOWANCE":
            raise RuntimeError("predict trading error: approval")
        addresses = getattr(self._builder, "_addresses", None)
        exchange_key = getattr(self._builder, "_exchange_key", None)
        if addresses is None or not callable(exchange_key):
            raise RuntimeError("predict trading error: approval")
        expected_spender = getattr(addresses, exchange_key(scope.is_neg_risk, scope.is_yield_bearing), None)
        expected_token = getattr(addresses, "USDT", None)
        if (
            not isinstance(expected_spender, str)
            or not isinstance(expected_token, str)
            or _normalize_address(getattr(step, "spender", None)) != _normalize_address(expected_spender)
            or _normalize_address(getattr(step, "token", None)) != _normalize_address(expected_token)
        ):
            raise RuntimeError("predict trading error: approval")
        return step

    def _raw_allowance(self, step: object) -> int:
        contracts = getattr(self._builder, "contracts", None)
        usdt = getattr(contracts, "usdt", None)
        functions = getattr(usdt, "functions", None)
        allowance_fn = getattr(functions, "allowance", None)
        if not callable(allowance_fn):
            raise RuntimeError("predict trading error: approval")
        return _non_negative_int(
            allowance_fn(self._config.wallet_address, getattr(step, "spender", "")).call(),
            "allowance",
        )

    def _approval_cost_wei(self, step: object, amount: int) -> int:
        method = self._approval_method(step, amount)
        estimate_gas = getattr(method, "estimate_gas", None)
        gas_price = getattr(getattr(getattr(self._builder, "_web3", None), "eth", None), "gas_price", None)
        if not callable(estimate_gas):
            raise RuntimeError("predict trading error: approval")
        estimated_gas = _non_negative_int(estimate_gas({"from": self._gas_signer}), "approval")
        gas_price_wei = _non_negative_int(gas_price, "approval")
        return ((estimated_gas * gas_price_wei) * 125) // 100

    def _approval_method(self, step: object, amount: int) -> object:
        contracts = getattr(self._builder, "contracts", None)
        usdt = getattr(contracts, "usdt", None)
        functions = getattr(usdt, "functions", None)
        approve = getattr(functions, "approve", None)
        predict_account = getattr(self._builder, "_predict_account", None)
        if predict_account:
            web3 = getattr(self._builder, "_web3", None)
            encode_abi = getattr(usdt, "encode_abi", None)
            encode_execution = getattr(self._builder, "_encode_execution_calldata", None)
            execution_mode = getattr(self._builder, "_execution_mode", None)
            kernel = getattr(contracts, "kernel", None)
            if (
                web3 is None
                or getattr(web3, "eth", None) is None
                or not callable(encode_abi)
                or not callable(encode_execution)
                or kernel is None
                or execution_mode is None
            ):
                raise RuntimeError("predict trading error: approval")
            encoded = encode_abi("approve", [getattr(step, "spender", ""), amount])
            calldata = encode_execution(usdt.address, encoded, value=0)
            kernel_contract = web3.eth.contract(address=predict_account, abi=getattr(kernel, "abi", None))
            return kernel_contract.functions.execute(execution_mode, calldata)
        if not callable(approve):
            raise RuntimeError("predict trading error: approval")
        return approve(getattr(step, "spender", ""), amount)

    def _signer_bnb_balance_wei(self) -> int:
        web3 = getattr(self._builder, "_web3", None)
        eth = getattr(web3, "eth", None)
        get_balance = getattr(eth, "get_balance", None)
        if not callable(get_balance):
            raise RuntimeError("predict trading error: bnb")
        return _non_negative_int(get_balance(self._gas_signer), "bnb")

    def _post_allowance(self, result: object, step: object, *, expected: int) -> int | None:
        receipt = getattr(result, "receipt", None)
        status = _receipt_status(receipt)
        success = getattr(result, "success", False) is True
        if not success or status != 1:
            return None
        try:
            return self._raw_allowance(step)
        except Exception:
            return None

    def _allowance_result(
        self,
        success: bool,
        error_code: str,
        facts: Mapping[str, object],
        result: object | None = None,
        *,
        possible_mutation: bool | None = None,
    ) -> Mapping[str, object]:
        receipt = getattr(result, "receipt", None) if result is not None else None
        payload = {
            "success": success,
            "status": "confirmed" if success else "failed",
            "error_code": error_code,
            "predict_account": facts.get("predict_account"),
            "gas_signer": facts.get("gas_signer"),
            "approval_step_id": facts.get("approval_step_id"),
            "approval_spender": facts.get("approval_spender"),
            "allowance": facts.get("allowance"),
            "allowance_raw": facts.get("allowance_raw"),
            "allowance_breaker": facts.get("allowance_breaker"),
            "exact_debit_wei": facts.get("exact_debit_wei"),
            "checked_at": datetime.now(UTC),
        }
        if possible_mutation is not None:
            payload["possible_mutation"] = possible_mutation
        tx_hash = _receipt_hash(receipt)
        if tx_hash:
            payload["transaction_hash"] = tx_hash
        status = _receipt_status(receipt)
        if status is not None:
            payload["transaction_status"] = status
        return payload


def _data(payload: Mapping[str, object]) -> Mapping[str, object]:
    value = payload.get("data", payload)
    return value if isinstance(value, Mapping) else {}


def _order_data(payload: Mapping[str, object]) -> Mapping[str, object]:
    data = _data(payload)
    nested = data.get("order")
    if nested is None:
        return data
    return {**data, **nested} if isinstance(nested, Mapping) else {}


def _row(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _rows(payload: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]:
    value = payload.get("data", payload)
    if isinstance(value, Mapping):
        value = value.get(name, ())
    return tuple(item for item in value if isinstance(item, Mapping)) if isinstance(value, list) else ()


def _rows_exact(
    payload: Mapping[str, object], name: str
) -> tuple[Mapping[str, object], ...]:
    value = payload.get("data", payload)
    if isinstance(value, Mapping):
        value = value.get(name)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("invalid Predict list response")
    return tuple(value)


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


def _row_identity(
    row: Mapping[str, object], *, market_id: str, token_id: str
) -> bool:
    market = _row(row.get("market"))
    outcome = _row(row.get("outcome"))
    row_market_id = market.get("id", row.get("marketId", row.get("market_id")))
    row_token_id = outcome.get("onChainId", row.get("tokenId", row.get("token_id")))
    return str(row_market_id) == str(market_id) and str(row_token_id) == str(token_id)


def _match_facts(
    row: Mapping[str, object],
    *,
    order_hash: str,
    market_id: str,
    token_id: str,
    signer: str,
) -> tuple[str, Decimal, Decimal] | None:
    legacy_hash = row.get("orderHash")
    if legacy_hash is not None:
        return _facts(row) if str(legacy_hash) == order_hash else None
    if str(_row(row.get("market")).get("id")) != str(market_id):
        return None
    participants = [row.get("taker"), *(
        row.get("makers") if isinstance(row.get("makers"), list) else ()
    )]
    owned = [
        value
        for value in participants
        if isinstance(value, Mapping)
        and str(value.get("hash")) == order_hash
        and str(value.get("signer", "")).casefold() == signer.casefold()
        and str(_row(value.get("outcome")).get("onChainId")) == str(token_id)
    ]
    if len(owned) != 1:
        return None
    fee = _row(owned[0].get("fee"))
    if fee.get("type") != "COLLATERAL":
        return None
    return _execution_facts(row, fee.get("amount"))


def _activity_facts(
    row: Mapping[str, object], *, order_hash: str, market_id: str, token_id: str
) -> tuple[str, Decimal, Decimal] | None:
    legacy_hash = row.get("orderHash")
    if legacy_hash is not None:
        return _facts(row) if str(legacy_hash) == order_hash else None
    if not _row_identity(row, market_id=market_id, token_id=token_id):
        return None
    order = _row(row.get("order"))
    fee = _row(order.get("fee"))
    if fee.get("type") != "COLLATERAL":
        return None
    return _execution_facts(row, fee.get("amount"))


def _execution_facts(
    row: Mapping[str, object], fee_value: object
) -> tuple[str, Decimal, Decimal] | None:
    transaction = row.get("transactionHash")
    amount = _number(row.get("amountFilled"))
    fee = _raw_units(fee_value)
    if (
        not isinstance(transaction, str)
        or not transaction
        or amount is None
        or amount <= 0
        or fee is None
        or fee < 0
    ):
        return None
    return transaction, amount, fee


def _raw_units(value: object) -> Decimal | None:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    return Decimal(int(value)) / Decimal(PREDICT_BASE_UNITS)


def _canonical_positions(
    rows: tuple[Mapping[str, object], ...]
) -> tuple[Mapping[str, object], ...]:
    positions: list[Mapping[str, object]] = []
    for row in rows:
        market = _row(row.get("market"))
        outcome = _row(row.get("outcome"))
        market_id = market.get("id")
        condition_id = market.get("conditionId")
        token_id = outcome.get("onChainId")
        outcome_name = outcome.get("name")
        outcome_status = outcome.get("status")
        quantity = _raw_units(row.get("amount"))
        if (
            market_id in (None, "")
            or not isinstance(condition_id, str)
            or not condition_id
            or not isinstance(token_id, str)
            or not token_id
            or not isinstance(outcome_name, str)
            or not outcome_name
            or quantity is None
        ):
            raise ValueError("invalid Predict position")
        positions.append(
            {
                "market_id": str(market_id),
                "condition_id": condition_id,
                "token_id": token_id,
                "outcome": outcome_name.upper(),
                "quantity": format(quantity, "f"),
                "redeemable": (
                    isinstance(outcome_status, str)
                    and outcome_status.upper() == "WON"
                ),
            }
        )
    return tuple(positions)


def _book(payload: Mapping[str, object]) -> Book:
    def levels(name: str) -> list[DepthLevel]:
        raw = payload.get(name, ())
        if not isinstance(raw, list):
            raise ValueError("invalid predict book")
        return [DepthLevel((int(Decimal(str(row[0])) * PREDICT_BASE_UNITS), int(Decimal(str(row[1])) * PREDICT_BASE_UNITS))) for row in raw if isinstance(row, list) and len(row) == 2]

    return Book(int(payload.get("marketId", 0)), int(payload.get("updateTimestampMs", 0)), levels("asks"), levels("bids"))


def _normalize_address(value: object) -> str:
    return str(value).strip().lower()


def _non_negative_int(value: object, code: str) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        raise RuntimeError(f"predict trading error: {code}") from None
    if number < 0:
        raise RuntimeError(f"predict trading error: {code}")
    return number


def _bnb_wei_string(value: int) -> str:
    return _decimal_string(Decimal(value) / Decimal(10**18))


def _usdt_string(value: int) -> str:
    return _decimal_string(Decimal(value) / Decimal(PREDICT_BASE_UNITS))


def _decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _receipt_status(receipt: object) -> int | None:
    if isinstance(receipt, Mapping):
        status = receipt.get("status")
    else:
        status = getattr(receipt, "status", None)
    return status if type(status) is int and status in (0, 1) else None


def _receipt_hash(receipt: object) -> str | None:
    if isinstance(receipt, Mapping):
        value = receipt.get("transactionHash")
    else:
        value = getattr(receipt, "transactionHash", None)
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, str) and value:
        return value
    return None


def _receipt_error_code(result: object) -> str:
    receipt = getattr(result, "receipt", None)
    status = _receipt_status(receipt)
    return "receipt_failed" if status == 0 else "receipt_ambiguous"


def _has_active_execution(open_orders: object) -> bool:
    return isinstance(open_orders, (list, tuple)) and len(open_orders) > 0


def _plain(value: object) -> object:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return getattr(value, "value", value)
