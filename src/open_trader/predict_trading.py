"""Narrow authenticated Predict order boundary; credentials never leave this module."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
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

    def no_submit_buy_preflight(self, market_id: str, token_id: str, quantity_wei: int, *, fee_rate_bps: int | None = None) -> PredictLegResult:
        try:
            market = self._market(market_id)
            if fee_rate_bps is not None:
                market = {**market, "feeRateBps": fee_rate_bps}
            self._order_body(self.quote_market_buy(market_id, token_id, quantity_wei), market)
            return PredictLegResult(True, "preflight")
        except Exception:
            return PredictLegResult(False, "rejected", error_code="rejected")

    def submit_buy_once(self, market_id: str, token_id: str, quantity_wei: int, *, fee_rate_bps: int | None = None) -> PredictLegResult:
        try:
            market = self._market(market_id)
            if fee_rate_bps is not None:
                market = {**market, "feeRateBps": fee_rate_bps}
            response = self._json("/v1/orders", method="POST", data=self._order_body(self.quote_market_buy(market_id, token_id, quantity_wei), market), auth=True)
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
            matches = _data(self._json("/v1/orders/matches", auth=True)).get("matches", ())
            activity = _data(self._json("/v1/account/activity", auth=True)).get("activities", ())
            positions = _data(self._json(f"/v1/positions?marketId={market_id}", auth=True)).get("positions", ())
            identity = order.get("hash") == order_hash and str(order.get("tokenId")) == str(token_id) and str(order.get("marketId")) == str(market_id)
            matched = any(str(_row(item).get("orderHash")) == order_hash for item in matches) and any(str(_row(item).get("orderHash")) == order_hash for item in activity)
            positioned = any(str(_row(item).get("tokenId")) == str(token_id) and str(_row(item).get("amount", "0")) not in {"0", "0.0"} for item in positions)
            return {"verified": identity and matched and positioned, "conclusively_absent": not identity and not matches and not activity and not positions, "status": "verified" if identity and matched and positioned else "unknown"}
        except Exception:
            return {"verified": False, "conclusively_absent": False, "status": "unknown"}

    def redeemable_snapshot(self) -> Mapping[str, object]:
        return {"wallet_address": self._config.wallet_address, "positions": _data(self._json("/v1/positions", auth=True)).get("positions", ()), "checked_at": datetime.now(UTC)}


def _data(payload: Mapping[str, object]) -> Mapping[str, object]:
    value = payload.get("data", payload)
    return value if isinstance(value, Mapping) else {}


def _row(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _book(payload: Mapping[str, object]) -> Book:
    def levels(name: str) -> list[DepthLevel]:
        raw = payload.get(name, ())
        if not isinstance(raw, list):
            raise ValueError("invalid predict book")
        return [DepthLevel((int(float(row[0]) * 10**18), int(float(row[1]) * 10**18))) for row in raw if isinstance(row, list) and len(row) == 2]
    return Book(int(payload.get("marketId", 0)), int(payload.get("updateTimestampMs", 0)), levels("asks"), levels("bids"))


def _plain(value: object) -> object:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return getattr(value, "value", value)
