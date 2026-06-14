from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import re

import pdfplumber

from open_trader.models import CashBalance, Market, Position
from open_trader.parsers.base import (
    ParseResult,
    StatementParser,
    detect_asset_class,
    detect_market,
    parse_decimal,
)


BROKER = "phillips"
ACCOUNT_ALIAS = "phillips_main"
NUMERIC = r"(?:-?[\d,.]+|\([\d,.]+\))"


def parse_phillips_text(text: str, month: str) -> ParseResult:
    statement_id = f"{month}-{BROKER}"
    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    in_positions = False
    in_cash = False

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        if line == "Securities Portfolio" or "證券投資組合" in line or "证券投资组合" in line:
            in_positions = True
            in_cash = False
            continue
        if line.startswith(("產品 市場", "Product Market")):
            continue
        if line == "Cash Balance":
            in_positions = False
            in_cash = True
            continue

        if in_positions:
            position = _parse_position_line(line, statement_id)
            if position is not None:
                positions.append(position)
        elif in_cash:
            cash_balance = _parse_cash_line(line, statement_id)
            if cash_balance is not None:
                cash_balances.append(cash_balance)
            else:
                in_cash = False

    return ParseResult(
        statement_id=statement_id,
        broker=BROKER,
        positions=positions,
        cash_balances=cash_balances,
    )


def _parse_position_line(line: str, statement_id: str) -> Position | None:
    match = re.fullmatch(
        r"(?:股票|Stock)\s+"
        r"(?P<market>HK|US|SEHK|NASDAQ|NYSE)\s+"
        r"(?P<symbol>[A-Z0-9.]+)\s+"
        r"(?P<name>.+?)\s+"
        rf"(?P<previous_quantity>{NUMERIC})\s+"
        r"(?P<last_buy_date>\d{4}/\d{2}/\d{2})\s+"
        rf"(?P<quantity>{NUMERIC})\s+"
        rf"(?P<last_price>{NUMERIC})\s+"
        rf"(?P<market_value>{NUMERIC})\s+"
        rf"(?P<margin_ratio>{NUMERIC})\s+"
        rf"(?P<margin_value>{NUMERIC})",
        line,
    )
    if match is None:
        return None

    market = detect_market(match.group("market"))
    symbol = match.group("symbol").upper()
    name = match.group("name").strip()

    return Position(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=market,
        asset_class=detect_asset_class(symbol, name),
        symbol=symbol,
        name=name,
        currency=_currency_for_market(market),
        quantity=parse_decimal(match.group("quantity")) or Decimal("0"),
        cost_price=None,
        last_price=parse_decimal(match.group("last_price")),
        market_value=parse_decimal(match.group("market_value")),
        cost_value=None,
        unrealized_pnl=None,
        confidence="medium",
        notes="currency inferred from market in Phillips text fixture",
    )


def _currency_for_market(market: Market) -> str:
    if market == Market.HK:
        return "HKD"
    if market == Market.US:
        return "USD"
    return ""


def _parse_cash_line(line: str, statement_id: str) -> CashBalance | None:
    match = re.fullmatch(rf"(?P<currency>[A-Z]{{3}})\s+(?P<balance>{NUMERIC})", line)
    if match is None:
        return None

    balance = parse_decimal(match.group("balance")) or Decimal("0")
    return CashBalance(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        currency=match.group("currency"),
        cash_balance=balance,
        available_balance=balance,
        confidence="high",
        notes="",
    )


def _normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


class PhillipsStatementParser(StatementParser):
    broker = BROKER

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result = parse_phillips_text(text, month)
            return replace(result, page_count=len(pdf.pages))
