from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import re

from open_trader.models import AssetClass, CashBalance, Market, Position, WarningRecord


@dataclass(frozen=True)
class ParseResult:
    statement_id: str
    broker: str
    positions: list[Position] = field(default_factory=list)
    cash_balances: list[CashBalance] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    page_count: int = 0


class StatementParser:
    broker = ""
    parser_version = "0.1.0"

    def parse(self, path: Path, month: str) -> ParseResult:
        raise NotImplementedError


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None

    normalized = value.strip()
    if normalized in {"", "-", "--"}:
        return None

    normalized = re.sub(r"\b(?:HKD|USD)\b", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.strip()

    negative = normalized.startswith("(") and normalized.endswith(")")
    if negative:
        normalized = normalized[1:-1].strip()

    normalized = normalized.replace(",", "").strip()
    if normalized in {"", "-", "--"}:
        return None

    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None

    if not amount.is_finite():
        return None

    return -amount if negative else amount


def split_symbol_name(value: str) -> tuple[str, str]:
    normalized = _normalize_spaces(value)
    match = re.fullmatch(r"(.+?)(\s*)\((.+)\)", normalized)
    if not match:
        return (normalized.upper(), "")

    before = _normalize_spaces(match.group(1))
    separator = match.group(2)
    inside = _normalize_spaces(match.group(3))
    before_symbol = _normalize_symbol(before)

    if before_symbol != before.upper() and _looks_like_symbol(before_symbol):
        return (before_symbol, inside)
    if separator and _looks_like_symbol(inside):
        return (inside.upper(), before)
    if _looks_like_symbol(before_symbol):
        return (before_symbol, inside)
    return (inside.upper(), before)


def detect_market(value: str) -> Market:
    normalized = value.strip().upper()
    if normalized in {"US", "NASDAQ", "NYSE", "AMEX", "CBOE", "ARCA", "NYSE ARCA"}:
        return Market.US
    if normalized in {"SEHK", "HK", "HKG", "HKEX"}:
        return Market.HK
    return Market.OTHER


def detect_asset_class(symbol: str, name: str) -> AssetClass:
    symbol_upper = symbol.upper()
    name_upper = name.upper()
    combined = f"{symbol_upper} {name_upper}"

    if _has_any(combined, ("货币市场", "貨幣", "货币基金", "MONEY MARKET")):
        return AssetClass.MONEY_MARKET_FUND

    if _looks_like_option_symbol(symbol_upper) or _has_option_words(combined):
        return AssetClass.OPTION

    if _has_any(combined, (" ETF", "ETF ", "EXCHANGE TRADED FUND")) or name_upper.endswith("ETF"):
        return AssetClass.ETF

    if _has_any(combined, ("基金", " FUND", "FUND ")):
        return AssetClass.FUND

    return AssetClass.STOCK


def _normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_symbol(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", value.strip()))


def _normalize_symbol(value: str) -> str:
    normalized = _normalize_spaces(value).upper()

    hk_match = re.fullmatch(r"([A-Z0-9]+)\s+HK", normalized)
    if hk_match:
        return f"{hk_match.group(1)}.HK"

    class_share_match = re.fullmatch(r"([A-Z]{1,5})\s+([A-Z])", normalized)
    if class_share_match:
        return f"{class_share_match.group(1)}.{class_share_match.group(2)}"

    return normalized


def _looks_like_option_symbol(value: str) -> bool:
    return bool(re.search(r"\b[A-Z]{1,6}\s*\d{6}[CP]\d{8}\b", value))


def _has_option_words(value: str) -> bool:
    return bool(re.search(r"\b(?:CALL|PUT|OPTION|OPTIONS)\b|期权|期權", value))


def _has_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)
