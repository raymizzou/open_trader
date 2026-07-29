from __future__ import annotations

import re


KNOWN_PREFIXES = {"HK", "US", "CN", "SH", "SZ", "BJ"}
US_SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*")
CN_EXCHANGES = {"SH", "SZ", "BJ"}


def to_futu_symbol(market: str, symbol: str) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if normalized_market not in {"HK", "US", "CN"}:
        raise ValueError(f"unsupported Futu market: {market}")
    if "." in normalized_symbol:
        prefix, remainder = normalized_symbol.split(".", 1)
        if prefix == normalized_market:
            if (
                normalized_market == "HK"
                and len(remainder) == 6
                and remainder.isdigit()
            ):
                return f"HK.{remainder}"
            normalized_symbol = remainder
        elif normalized_market == "CN" and prefix in {"SH", "SZ", "BJ"}:
            if len(remainder) != 6 or not remainder.isdigit():
                raise ValueError(f"invalid CN symbol: {symbol}")
            inferred_prefix = _cn_exchange(remainder)
            ambiguous_000_code = (
                remainder.startswith("000")
                and {prefix, inferred_prefix} == {"SH", "SZ"}
            )
            if prefix != inferred_prefix and not ambiguous_000_code:
                raise ValueError(f"symbol prefix {prefix} does not match {symbol}")
            return f"{prefix}.{remainder}"
        elif not (normalized_market == "US" and prefix not in KNOWN_PREFIXES):
            raise ValueError(
                f"symbol prefix {prefix} does not match market {normalized_market}"
            )
    if not normalized_symbol:
        raise ValueError(f"empty symbol for market {normalized_market}")
    if normalized_market == "US":
        if US_SYMBOL_PATTERN.fullmatch(normalized_symbol) is None:
            raise ValueError(f"invalid US symbol: {symbol}")
        return f"US.{normalized_symbol}"
    if (
        normalized_market == "HK"
        and normalized_symbol.isdigit()
        and len(normalized_symbol) <= 5
    ):
        return f"HK.{normalized_symbol.zfill(5)}"
    if normalized_market == "CN":
        return f"{_cn_exchange(normalized_symbol)}.{normalized_symbol}"
    raise ValueError(f"invalid symbol for market {normalized_market}: {symbol}")


def to_trend_animals_symbol(market: str, symbol: str) -> str:
    futu_symbol = to_futu_symbol(market, symbol)
    exchange, code = futu_symbol.split(".", 1)
    if exchange in CN_EXCHANGES:
        return f"{code}.{exchange}"
    if exchange == "HK":
        if len(code) != 5 or not code.isdigit() or not code.startswith("0"):
            raise ValueError(f"invalid HK symbol: {symbol}")
        return f"{code[1:]}.HK"
    return code


def from_trend_animals_symbol(market: str, symbol: str) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if normalized_market == "CN":
        parts = normalized_symbol.rsplit(".", 1)
        if len(parts) == 1:
            return to_futu_symbol("CN", normalized_symbol)
        code, exchange = parts
        if exchange not in CN_EXCHANGES:
            raise ValueError(f"invalid CN Trend Animals symbol: {symbol}")
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"invalid CN Trend Animals symbol: {symbol}")
        return f"{exchange}.{code}"
    if normalized_market == "HK":
        try:
            code, exchange = normalized_symbol.rsplit(".", 1)
        except ValueError:
            raise ValueError(f"invalid HK Trend Animals symbol: {symbol}") from None
        if exchange != "HK" or len(code) != 4 or not code.isdigit():
            raise ValueError(f"invalid HK Trend Animals symbol: {symbol}")
        return to_futu_symbol("HK", code)
    if normalized_market == "US":
        suffix = normalized_symbol.rsplit(".", 1)[-1]
        if suffix == "US":
            normalized_symbol = normalized_symbol[:-3]
        elif suffix in KNOWN_PREFIXES:
            raise ValueError(f"invalid US Trend Animals symbol: {symbol}")
        return to_futu_symbol("US", normalized_symbol)
    raise ValueError(f"unsupported Futu market: {market}")


def _cn_exchange(symbol: str) -> str:
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError(f"invalid CN symbol: {symbol}")
    if symbol.startswith("92"):
        return "BJ"
    if symbol == "000300" or symbol[0] in "569":
        return "SH"
    if symbol[0] in "0123":
        return "SZ"
    raise ValueError(f"unsupported CN symbol: {symbol}")
