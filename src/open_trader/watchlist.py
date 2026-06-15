from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedTrigger:
    trigger_type: str
    operator: str
    trigger_price: str
    trigger_text: str
    status: str
    error: str


PRICE_RE = r"(?P<price>\d+(?:\.\d+)?)"
DOWNSIDE_RE = re.compile(
    rf"(?P<open>open\s+)?(?:(?:breaks\s+)?(?:below|under)|<=|<)\s*\$?{PRICE_RE}",
    re.IGNORECASE,
)
UPSIDE_RE = re.compile(
    rf"(?P<open>open\s+)?(?:(?:breaks\s+)?(?:above|over)|>=|>)\s*\$?{PRICE_RE}",
    re.IGNORECASE,
)


def parse_watch_trigger(text: str) -> ParsedTrigger:
    original = text.strip()
    if not original:
        return ParsedTrigger(
            trigger_type="none",
            operator="",
            trigger_price="",
            trigger_text="",
            status="no_trigger",
            error="",
        )

    downside = DOWNSIDE_RE.search(original)
    if downside:
        return ParsedTrigger(
            trigger_type="open_price" if downside.group("open") else "price",
            operator="<=",
            trigger_price=downside.group("price"),
            trigger_text=original,
            status="active",
            error="",
        )

    upside = UPSIDE_RE.search(original)
    if upside:
        return ParsedTrigger(
            trigger_type="open_price" if upside.group("open") else "price",
            operator=">=",
            trigger_price=upside.group("price"),
            trigger_text=original,
            status="active",
            error="",
        )

    return ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=original,
        status="manual_review",
        error="",
    )
