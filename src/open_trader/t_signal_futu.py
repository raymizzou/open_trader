from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from .futu_quote import (
    SNAPSHOT_FAILED_NEXT_STEP,
    FutuQuoteClient,
    FutuQuoteError,
    _can_connect_to_opend,
    _default_context_factory,
)
from .t_signal import TMarketFacts


class FutuTSignalMarketDataClient(FutuQuoteClient):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        context_factory: Callable[..., Any] = _default_context_factory,
        connectivity_checker: Callable[[str, int], bool] = _can_connect_to_opend,
        kline_type_1m: object | None = None,
        kline_type_5m: object | None = None,
    ) -> None:
        super().__init__(
            host=host,
            port=port,
            context_factory=context_factory,
            connectivity_checker=connectivity_checker,
        )
        self.kline_type_1m = kline_type_1m or _default_kline_type("K_1M")
        self.kline_type_5m = kline_type_5m or _default_kline_type("K_5M")

    def get_market_facts(
        self,
        *,
        run_date: str,
        market: str,
        symbol: str,
        futu_symbol: str,
        name: str,
        session_phase: str,
        updated_at: str,
    ) -> TMarketFacts:
        snapshot = self._snapshot_row(futu_symbol)
        kline_1m = self._kline_rows(futu_symbol, self.kline_type_1m)
        kline_5m = self._kline_rows(futu_symbol, self.kline_type_5m)
        order_book = self._order_book(futu_symbol)

        return TMarketFacts(
            run_date=run_date,
            market=market,
            symbol=symbol,
            futu_symbol=futu_symbol,
            name=name,
            session_phase=session_phase,
            updated_at=updated_at,
            last_price=_decimal(snapshot.get("last_price")),
            day_change_pct=_first_decimal(
                snapshot,
                ("change_rate", "change_rate_pct", "change_ratio"),
            ),
            vwap=_vwap(kline_1m),
            ma_1m=_average_close(kline_1m),
            ma_5m=_average_close(kline_5m),
            day_low=_first_decimal(snapshot, ("low_price", "day_low", "low")),
            day_high=_first_decimal(snapshot, ("high_price", "day_high", "high")),
            bid=order_book.bid,
            ask=order_book.ask,
            bid_depth=order_book.bid_depth,
            ask_depth=order_book.ask_depth,
            rsi_5m=_rsi_from_closes(kline_5m),
            volume_ratio_5m=_volume_ratio(kline_5m),
        )

    def _snapshot_row(self, futu_symbol: str) -> dict[str, object]:
        ret_code, data = self.context.get_market_snapshot([futu_symbol])
        self._raise_on_error(ret_code, data)
        for record in _records(data):
            if str(record.get("code", "")).strip() == futu_symbol:
                return record
        return {}

    def _kline_rows(self, futu_symbol: str, kline_type: object) -> list[dict[str, object]]:
        ret_code, data = self.context.get_cur_kline(futu_symbol, 30, kline_type)
        self._raise_on_error(ret_code, data)
        return _records(data)

    def _order_book(self, futu_symbol: str) -> _OrderBook:
        ret_code, data = self.context.get_order_book(futu_symbol, 1)
        self._raise_on_error(ret_code, data)
        bid = _first_order_book_row(data, "Bid")
        ask = _first_order_book_row(data, "Ask")
        return _OrderBook(
            bid=_decimal(bid[0]) if bid else None,
            bid_depth=_decimal(bid[1]) if bid else None,
            ask=_decimal(ask[0]) if ask else None,
            ask_depth=_decimal(ask[1]) if ask else None,
        )

    def _raise_on_error(self, ret_code: int, data: object) -> None:
        if ret_code == 0:
            return
        raise FutuQuoteError(
            str(data),
            error_type="snapshot_failed",
            next_step=SNAPSHOT_FAILED_NEXT_STEP,
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=False,
        )


class _OrderBook:
    def __init__(
        self,
        *,
        bid: Decimal | None,
        bid_depth: Decimal | None,
        ask: Decimal | None,
        ask_depth: Decimal | None,
    ) -> None:
        self.bid = bid
        self.bid_depth = bid_depth
        self.ask = ask
        self.ask_depth = ask_depth


def _default_kline_type(name: str) -> object:
    try:
        from futu import KLType
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
    return getattr(KLType, name)


def _records(data: object) -> list[dict[str, object]]:
    to_dict = getattr(data, "to_dict", None)
    if not callable(to_dict):
        return []
    rows = to_dict("records")
    return [row for row in rows if isinstance(row, dict)]


def _first_order_book_row(data: object, side: str) -> tuple[object, ...] | None:
    if not isinstance(data, dict):
        return None
    rows = data.get(side)
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if isinstance(row, tuple):
        return row
    if isinstance(row, list):
        return tuple(row)
    return None


def _first_decimal(
    row: dict[str, object],
    names: tuple[str, ...],
) -> Decimal | None:
    for name in names:
        value = _decimal(row.get(name))
        if value is not None:
            return value
    return None


def _decimal(value: object) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite():
        return None
    return decimal


def _average_close(rows: list[dict[str, object]]) -> Decimal | None:
    closes = [value for row in rows if (value := _decimal(row.get("close"))) is not None]
    if not closes:
        return None
    return (sum(closes) / Decimal(len(closes))).quantize(Decimal("0.001"))


def _vwap(rows: list[dict[str, object]]) -> Decimal | None:
    total_turnover = Decimal("0")
    total_volume = Decimal("0")
    for row in rows:
        turnover = _decimal(row.get("turnover"))
        volume = _decimal(row.get("volume"))
        if turnover is None or volume is None or volume <= 0:
            continue
        total_turnover += turnover
        total_volume += volume
    if total_volume <= 0:
        return _average_close(rows)
    return (total_turnover / total_volume).quantize(Decimal("0.001"))


def _volume_ratio(rows: list[dict[str, object]]) -> Decimal | None:
    volumes = [
        value
        for row in rows
        if (value := _decimal(row.get("volume"))) is not None and value > 0
    ]
    if len(volumes) < 2:
        return None
    previous_average = sum(volumes[:-1]) / Decimal(len(volumes) - 1)
    if previous_average <= 0:
        return None
    return (volumes[-1] / previous_average).quantize(Decimal("0.01"))


def _rsi_from_closes(rows: list[dict[str, object]]) -> Decimal | None:
    closes = [value for row in rows if (value := _decimal(row.get("close"))) is not None]
    if len(closes) < 2:
        return None
    gains = Decimal("0")
    losses = Decimal("0")
    for previous, current in zip(closes, closes[1:]):
        change = current - previous
        if change > 0:
            gains += change
        elif change < 0:
            losses += abs(change)
    if gains == 0 and losses == 0:
        return Decimal("50")
    if losses == 0:
        return Decimal("100")
    relative_strength = gains / losses
    return (Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength))).quantize(
        Decimal("0.01")
    )
