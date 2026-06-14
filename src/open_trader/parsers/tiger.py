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
    parse_decimal,
    split_symbol_name,
)


BROKER = "tiger"
ACCOUNT_ALIAS = "tiger_main"
NUMERIC = r"(?:-?[\d,.]+|\([\d,.]+\))"


def parse_tiger_text(text: str, month: str) -> ParseResult:
    statement_id = f"{month}-{BROKER}"
    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    in_positions = False
    in_cash = False

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        if "期末持仓" in line or "期末持倉" in line:
            in_positions = True
            in_cash = False
            continue
        if line == "股票" or line.startswith("代码 数量 "):
            continue
        if line.startswith(("现金", "現金")):
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
        r"(?P<display>.+?)\s+"
        rf"(?P<quantity>{NUMERIC})\s+"
        rf"(?P<multiplier>{NUMERIC})\s+"
        rf"(?P<cost_price>{NUMERIC})\s+"
        rf"(?P<last_price>{NUMERIC})\s+"
        rf"(?P<market_value>{NUMERIC})\s+"
        rf"(?P<unrealized_pnl>{NUMERIC})\s+"
        rf"(?P<initial_margin>{NUMERIC})\s+"
        rf"(?P<maintenance_margin>{NUMERIC})\s+"
        r"(?P<currency>[A-Z]{3})",
        line,
    )
    if match is None:
        return None

    symbol, name = split_symbol_name(match.group("display"))
    quantity = parse_decimal(match.group("quantity")) or Decimal("0")
    cost_price = parse_decimal(match.group("cost_price"))
    cost_value = cost_price * quantity if cost_price is not None else None

    return Position(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=Market.US,
        asset_class=detect_asset_class(symbol, name),
        symbol=symbol,
        name=name,
        currency=match.group("currency"),
        quantity=quantity,
        cost_price=cost_price,
        last_price=parse_decimal(match.group("last_price")),
        market_value=parse_decimal(match.group("market_value")),
        cost_value=cost_value,
        unrealized_pnl=parse_decimal(match.group("unrealized_pnl")),
        confidence="high",
        notes="",
    )


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


class TigerStatementParser(StatementParser):
    broker = BROKER

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result = parse_tiger_text(text, month)
            return replace(result, page_count=len(pdf.pages))
