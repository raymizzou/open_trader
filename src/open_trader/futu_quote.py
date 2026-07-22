from __future__ import annotations

import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import math
from time import monotonic, sleep
from typing import Any

from .futu_watch import QuoteSnapshot
from .futu_symbols import to_futu_symbol
from .kline_technical_facts import DailyKlineBar


OPEND_UNREACHABLE_NEXT_STEP = (
    "请启动或重启 Futu OpenD，确认已登录，并检查配置的 host/port 后重新运行每日盘前流程。"
)
CONTEXT_FAILED_NEXT_STEP = (
    "请确认 futu-api 可用、OpenD 已启动且登录正常，然后重新运行每日盘前流程。"
)
QUOTE_INTERRUPTED_NEXT_STEP = (
    "请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。"
)
SNAPSHOT_FAILED_NEXT_STEP = (
    "请检查 OpenD 行情服务状态和网络连接，然后重新运行每日盘前流程。"
)
_TRADING_DAYS_CACHE_SECONDS = 300.0
_TRADING_DAYS_CACHE: dict[
    tuple[object, str, int, str, str, str], tuple[float, tuple[str, ...]]
] = {}


def _clear_trading_days_cache() -> None:
    _TRADING_DAYS_CACHE.clear()


@dataclass(frozen=True)
class DashboardQuoteSnapshot:
    futu_symbol: str
    last_price: Decimal | None
    pre_price: Decimal | None
    after_price: Decimal | None
    overnight_price: Decimal | None
    update_time: str


def _positive_decimal(value: object) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


class FutuQuoteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "snapshot_failed",
        next_step: str = SNAPSHOT_FAILED_NEXT_STEP,
        opend_reachable: bool | None = None,
        context_ok: bool | None = None,
        snapshot_ok: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.next_step = next_step
        self.opend_reachable = opend_reachable
        self.context_ok = context_ok
        self.snapshot_ok = snapshot_ok


def _default_context_factory(*, host: str, port: int) -> Any:
    try:
        from futu import OpenQuoteContext
    except ImportError as exc:
        raise FutuQuoteError(
            "futu-api is not installed. Install it with: "
            ".venv/bin/python -m pip install futu-api",
            error_type="context_failed",
            next_step="请在当前虚拟环境安装 futu-api 后重新运行每日盘前流程。",
            opend_reachable=None,
            context_ok=False,
            snapshot_ok=False,
        ) from exc
    context = OpenQuoteContext(
        host=host,
        port=port,
        is_async_connect=True,
    )
    context.set_sync_query_connect_timeout(3.0)
    return context


def _can_connect_to_opend(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


class FutuQuoteClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        context_factory: Callable[..., Any] = _default_context_factory,
        connectivity_checker: Callable[[str, int], bool] = _can_connect_to_opend,
        sleep_fn: Callable[[float], None] = sleep,
        monotonic_fn: Callable[[], float] = monotonic,
    ) -> None:
        if not connectivity_checker(host, port):
            raise FutuQuoteError(
                f"Futu OpenD is not reachable at {host}:{port}. "
                "Start OpenD, log in, and check the configured host and port.",
                error_type="opend_unreachable",
                next_step=OPEND_UNREACHABLE_NEXT_STEP,
                opend_reachable=False,
                context_ok=False,
                snapshot_ok=False,
            )
        try:
            self.context = context_factory(host=host, port=port)
        except FutuQuoteError:
            raise
        except Exception as exc:
            raise FutuQuoteError(
                f"failed to connect to Futu OpenD at {host}:{port}: {exc}",
                error_type="context_failed",
                next_step=CONTEXT_FAILED_NEXT_STEP,
                opend_reachable=True,
                context_ok=False,
                snapshot_ok=False,
            ) from exc
        self.host = host
        self.port = port
        self._sleep_fn = sleep_fn
        self._monotonic_fn = monotonic_fn
        self._calendar_cache_scope = context_factory

    def get_snapshots(self, futu_symbols: Sequence[str]) -> dict[str, QuoteSnapshot]:
        requested = set(futu_symbols)
        ret_code, data = self.context.get_market_snapshot(list(futu_symbols))
        if ret_code != 0:
            self._raise_quote_error(str(data), error_type="snapshot_failed")
        snapshots: dict[str, QuoteSnapshot] = {}
        for record in data.to_dict("records"):
            code = str(record.get("code", "")).strip()
            price = _positive_decimal(record.get("last_price"))
            if code not in requested or price is None:
                continue
            snapshots[code] = QuoteSnapshot(futu_symbol=code, last_price=price)
        return snapshots

    def get_dashboard_snapshots(
        self, futu_symbols: Sequence[str]
    ) -> dict[str, DashboardQuoteSnapshot]:
        requested = set(futu_symbols)
        ret_code, data = self.context.get_market_snapshot(list(futu_symbols))
        if ret_code != 0:
            self._raise_quote_error(str(data), error_type="snapshot_failed")
        snapshots: dict[str, DashboardQuoteSnapshot] = {}
        for record in data.to_dict("records"):
            code = str(record.get("code", "")).strip()
            if code not in requested:
                continue
            snapshots[code] = DashboardQuoteSnapshot(
                futu_symbol=code,
                last_price=_positive_decimal(record.get("last_price")),
                pre_price=_positive_decimal(record.get("pre_price")),
                after_price=_positive_decimal(record.get("after_price")),
                overnight_price=_positive_decimal(record.get("overnight_price")),
                update_time=str(record.get("update_time", "")).strip(),
            )
        return snapshots

    def get_market_states(self, futu_symbols: Sequence[str]) -> dict[str, str]:
        requested = set(futu_symbols)
        ret_code, data = self.context.get_market_state(list(futu_symbols))
        if ret_code != 0:
            self._raise_quote_error(str(data), error_type="market_state_failed")
        return {
            code: str(record.get("market_state", "")).strip()
            for record in data.to_dict("records")
            if (code := str(record.get("code", "")).strip()) in requested
        }

    def _raise_quote_error(self, message: str, *, error_type: str) -> None:
        interrupted = "网络中断" in message
        raise FutuQuoteError(
            message,
            error_type="quote_server_interrupted" if interrupted else error_type,
            next_step=(
                QUOTE_INTERRUPTED_NEXT_STEP if interrupted else SNAPSHOT_FAILED_NEXT_STEP
            ),
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=error_type == "market_state_failed",
        )

    def get_cn_trading_days(self, *, start: str, end: str) -> list[str]:
        return self.get_trading_days(market="CN", start=start, end=end)

    def get_trading_days(self, *, market: str, start: str, end: str) -> list[str]:
        normalized_market = market.strip().upper()
        if normalized_market not in {"CN", "HK", "US"}:
            raise ValueError(f"unsupported Futu market: {market}")
        cache_key = (
            self._calendar_cache_scope,
            self.host,
            self.port,
            normalized_market,
            start,
            end,
        )
        now = self._monotonic_fn()
        cached = _TRADING_DAYS_CACHE.get(cache_key)
        if cached is not None:
            expires_at, trading_days = cached
            if expires_at > now:
                return list(trading_days)
            del _TRADING_DAYS_CACHE[cache_key]
        try:
            from futu import TradeDateMarket

            wire_market = getattr(TradeDateMarket, normalized_market)
        except ImportError:
            wire_market = normalized_market
        ret_code, data = self.context.request_trading_days(
            market=wire_market, start=start, end=end
        )
        if ret_code != 0:
            message = str(data)
            raise FutuQuoteError(
                message,
                error_type=(
                    "quote_server_interrupted" if "网络中断" in message
                    else "snapshot_failed"
                ),
                next_step=(
                    QUOTE_INTERRUPTED_NEXT_STEP if "网络中断" in message
                    else SNAPSHOT_FAILED_NEXT_STEP
                ),
                opend_reachable=True,
                context_ok=True,
                snapshot_ok=False,
            )
        trading_days: list[str] = []
        try:
            for item in data:
                if not isinstance(item, Mapping):
                    raise TypeError("trading calendar row is not a mapping")
                time = item.get("time", "")
                if not isinstance(time, str):
                    raise TypeError("trading calendar time is not a string")
                if time := time.strip():
                    date.fromisoformat(time)
                    trading_days.append(time)
        except (AttributeError, TypeError, ValueError) as exc:
            raise FutuQuoteError(
                "Futu trading calendar returned malformed data",
                error_type="snapshot_failed",
                next_step=SNAPSHOT_FAILED_NEXT_STEP,
                opend_reachable=True,
                context_ok=True,
                snapshot_ok=False,
            ) from exc
        _TRADING_DAYS_CACHE[cache_key] = (
            self._monotonic_fn() + _TRADING_DAYS_CACHE_SECONDS,
            tuple(trading_days),
        )
        return trading_days

    def get_lot_sizes(self, futu_symbols: Sequence[str]) -> dict[str, int]:
        requested = set(futu_symbols)
        ret_code, data = self.context.get_market_snapshot(list(futu_symbols))
        if ret_code != 0:
            raise FutuQuoteError(str(data))
        lot_sizes: dict[str, int] = {}
        for record in data.to_dict("records"):
            code = str(record.get("code", "")).strip()
            try:
                lot_size = int(str(record.get("lot_size", "")).strip())
            except (TypeError, ValueError):
                continue
            if code in requested and lot_size > 0:
                lot_sizes[code] = lot_size
        return lot_sizes

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[DailyKlineBar]:
        try:
            from futu import AuType, KLType

            ktype = KLType.K_DAY
            autype = AuType.QFQ
        except ImportError:
            ktype = "K_DAY"
            autype = "QFQ"
        market, symbol = futu_symbol.split(".", 1)
        wire_symbol = (
            to_futu_symbol("CN", futu_symbol)
            if market in {"SH", "SZ", "BJ"}
            else to_futu_symbol(market, futu_symbol)
        )
        bars: list[DailyKlineBar] = []
        page_req_key: object = None
        seen_page_keys: set[object] = set()
        rate_limit_retried = False
        while True:
            response = self.context.request_history_kline(
                wire_symbol,
                start=start,
                end=end,
                ktype=ktype,
                autype=autype,
                max_count=1000,
                page_req_key=page_req_key,
            )
            ret_code, data = response[0], response[1]
            if ret_code != 0:
                message = str(data)
                if "获取历史K线频率太高" in message and not rate_limit_retried:
                    rate_limit_retried = True
                    self._sleep_fn(30.0)
                    continue
                if "网络中断" in message:
                    raise FutuQuoteError(
                        message,
                        error_type="quote_server_interrupted",
                        next_step=QUOTE_INTERRUPTED_NEXT_STEP,
                        opend_reachable=True,
                        context_ok=True,
                        snapshot_ok=False,
                    )
                raise FutuQuoteError(
                    message,
                    error_type="snapshot_failed",
                    next_step=SNAPSHOT_FAILED_NEXT_STEP,
                    opend_reachable=True,
                    context_ok=True,
                    snapshot_ok=False,
                )
            for record in data.to_dict("records"):
                date_text = str(record.get("time_key") or record.get("date") or "").strip()
                close_text = record.get("close")
                volume_text = record.get("volume")
                if not date_text or close_text in {None, ""} or volume_text in {None, ""}:
                    continue
                try:
                    close = float(str(close_text))
                    volume = float(str(volume_text))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(close) and math.isfinite(volume) and volume >= 0:
                    open_ = _optional_float(record.get("open"))
                    high = _optional_float(record.get("high"))
                    low = _optional_float(record.get("low"))
                    comparable_open = close if open_ is None else open_
                    comparable_high = close if high is None else high
                    comparable_low = close if low is None else low
                    if (
                        comparable_low > min(comparable_open, close)
                        or comparable_high < max(comparable_open, close)
                        or comparable_low > comparable_high
                    ):
                        continue
                    bars.append(
                        DailyKlineBar(
                            date=date_text[:10],
                            open=open_,
                            high=high,
                            low=low,
                            close=close,
                            volume=volume,
                        )
                    )
            next_key = response[2] if len(response) > 2 else None
            if next_key is None:
                break
            if next_key in seen_page_keys:
                raise FutuQuoteError(
                    "富途历史 K 线分页游标重复",
                    error_type="snapshot_failed",
                    next_step=SNAPSHOT_FAILED_NEXT_STEP,
                    opend_reachable=True,
                    context_ok=True,
                    snapshot_ok=False,
                )
            seen_page_keys.add(next_key)
            page_req_key = next_key
        return bars

    def get_rehab_rows(self, futu_symbol: str) -> list[dict[str, str]]:
        ret_code, data = self.context.get_rehab(futu_symbol)
        if ret_code != 0:
            raise FutuQuoteError(
                str(data),
                error_type="snapshot_failed",
                next_step=SNAPSHOT_FAILED_NEXT_STEP,
                opend_reachable=True,
                context_ok=True,
                snapshot_ok=False,
            )
        return [
            {
                str(key): "" if value is None else str(value)
                for key, value in sorted(row.items(), key=lambda item: str(item[0]))
            }
            for row in data.to_dict("records")
        ]

    def close(self) -> None:
        self.context.close()


def _optional_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(str(value))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None
