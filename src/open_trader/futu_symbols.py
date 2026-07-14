from __future__ import annotations

import re


KNOWN_PREFIXES = {"HK", "US", "CN", "SH", "SZ", "BJ"}
US_SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*")


def to_futu_symbol(market: str, symbol: str) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if normalized_market not in {"HK", "US", "CN"}:
        raise ValueError(f"unsupported Futu market: {market}")
    if "." in normalized_symbol:
        prefix, remainder = normalized_symbol.split(".", 1)
        if prefix == normalized_market:
            normalized_symbol = remainder
        elif normalized_market == "CN" and prefix in {"SH", "SZ", "BJ"}:
            if prefix != _cn_exchange(remainder):
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
