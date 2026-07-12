from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable

import requests

from .kline_technical_facts import DailyKlineBar


class AkShareDailyKlineProvider:
    def __init__(
        self, stock_history: Callable[..., Any] | None = None,
        index_history: Callable[..., Any] | None = None,
        stock_history_fallback: Callable[..., Any] | None = None,
        index_history_fallback: Callable[..., Any] | None = None,
    ) -> None:
        if None in (stock_history, index_history, stock_history_fallback, index_history_fallback):
            import akshare as ak
            stock_history = stock_history or ak.stock_zh_a_hist
            index_history = index_history or ak.stock_zh_index_daily_em
            stock_history_fallback = stock_history_fallback or ak.stock_zh_a_daily
            index_history_fallback = index_history_fallback or ak.stock_zh_index_daily
        self.stock_history = stock_history
        self.index_history = index_history
        self.stock_history_fallback = stock_history_fallback
        self.index_history_fallback = index_history_fallback

    def get_daily_kline(self, symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        match = re.fullmatch(r"CN\.(\d{6})", symbol)
        if match is None:
            raise ValueError("AKShare 行情仅支持 CN.六位代码")
        code = match.group(1)
        try:
            frame = (
                self.index_history(symbol="sh000300")
                if code == "000300"
                else self.stock_history(
                    symbol=code, period="daily", start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""), adjust="qfq",
                )
            )
        except requests.exceptions.RequestException:
            frame = (
                self.index_history_fallback(symbol="sh000300")
                if code == "000300"
                else self.stock_history_fallback(
                    symbol=f"{'sh' if code[0] in '569' else 'sz'}{code}",
                    start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                    adjust="qfq",
                )
            )
        return _validated_bars_between(frame, start, end)


def _validated_bars_between(frame: Any, start: str, end: str) -> list[DailyKlineBar]:
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    bars: list[DailyKlineBar] = []
    seen: set[date] = set()
    try:
        records = frame.to_dict("records")
    except Exception as exc:
        raise ValueError("AKShare 日线数据无效") from exc
    for row in records:
        try:
            raw_date = row.get("日期", row.get("date"))
            bar_date = date.fromisoformat(str(raw_date)[:10])
            if not start_date <= bar_date <= end_date:
                continue
            values = [Decimal(str(row.get(zh, row.get(en)))) for zh, en in (
                ("开盘", "open"), ("最高", "high"), ("最低", "low"),
                ("收盘", "close"), ("成交量", "volume"),
            )]
        except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("AKShare 日线数据无效") from exc
        if not all(value.is_finite() and value >= 0 for value in values):
            raise ValueError("AKShare 日线数据无效")
        open_, high, low, close, volume = values
        if low > min(open_, close) or high < max(open_, close) or low > high:
            raise ValueError("AKShare 日线数据无效")
        if bar_date in seen:
            raise ValueError(f"AKShare 日线数据包含重复日期：{bar_date.isoformat()}")
        seen.add(bar_date)
        bars.append(DailyKlineBar(
            date=bar_date.isoformat(), open=float(open_), high=float(high),
            low=float(low), close=float(close), volume=float(volume),
        ))
    return sorted(bars, key=lambda bar: bar.date)
