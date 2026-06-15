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
    cash_currency: str | None = None
    position_section: str | None = None
    pending_display: str | None = None
    pending_values: str | None = None

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        currency_match = re.fullmatch(r"按货币分类:\s*(?P<currency>[A-Z]{3})", line)
        if currency_match is not None:
            cash_currency = currency_match.group("currency")
            in_cash = False
            continue
        currency_cash = _parse_currency_cash_line(line, statement_id, cash_currency)
        if currency_cash is not None:
            _upsert_cash_balance(cash_balances, currency_cash)
            continue

        if "期末持仓" in line or "期末持倉" in line:
            in_positions = True
            in_cash = False
            pending_display = None
            pending_values = None
            continue
        if in_positions and line in {"基金", "股票"}:
            position_section = line
            pending_display = None
            pending_values = None
            continue
        if line.startswith("代码 数量 "):
            continue
        if line.startswith(("现金", "現金")):
            in_positions = False
            in_cash = True
            continue

        if in_positions:
            position = _parse_position_line(line, statement_id)
            if position is not None:
                positions.append(position)
                pending_display = None
                pending_values = None
                continue

            multiline_position = _parse_multiline_position_block(
                display=pending_display,
                values=pending_values,
                symbol_line=line,
                section=position_section,
                statement_id=statement_id,
            )
            if multiline_position is not None:
                positions.append(multiline_position)
                pending_display = None
                pending_values = None
                continue

            if pending_display is not None and _looks_like_values_line(line):
                pending_values = line
                continue

            if (
                not line.startswith("合计")
                and not line.startswith("注：")
                and not line.startswith("代码 ")
                and not _is_parenthesized_symbol(line)
            ):
                pending_display = line
                pending_values = None
        elif in_cash:
            cash_balance = _parse_cash_line(line, statement_id)
            if cash_balance is not None:
                _upsert_cash_balance(cash_balances, cash_balance)
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
    currency = match.group("currency")
    quantity = parse_decimal(match.group("quantity")) or Decimal("0")
    multiplier = parse_decimal(match.group("multiplier"))
    cost_price = parse_decimal(match.group("cost_price"))
    cost_value = (
        cost_price * quantity * multiplier
        if cost_price is not None and multiplier is not None
        else None
    )

    return Position(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=_market_for_currency(currency),
        asset_class=detect_asset_class(symbol, name),
        symbol=symbol,
        name=name,
        currency=currency,
        quantity=quantity,
        cost_price=cost_price,
        last_price=parse_decimal(match.group("last_price")),
        market_value=parse_decimal(match.group("market_value")),
        cost_value=cost_value,
        unrealized_pnl=parse_decimal(match.group("unrealized_pnl")),
        confidence="high",
        notes="",
    )


def _market_for_currency(currency: str) -> Market:
    if currency == "HKD":
        return Market.HK
    return Market.US


def _parse_multiline_position_block(
    *,
    display: str | None,
    values: str | None,
    symbol_line: str,
    section: str | None,
    statement_id: str,
) -> Position | None:
    if display is None or values is None:
        return None
    symbol_match = re.fullmatch(r"\((?P<symbol>[A-Z0-9.]+)\)", symbol_line)
    if symbol_match is None:
        return None

    match = _match_multiline_values(values, section)
    if match is None:
        return None

    symbol = symbol_match.group("symbol").upper()
    currency = match.group("currency")
    quantity = parse_decimal(match.group("quantity")) or Decimal("0")
    multiplier = parse_decimal(match.groupdict().get("multiplier")) or Decimal("1")
    cost_price = parse_decimal(match.group("cost_price"))
    cost_value = cost_price * quantity * multiplier if cost_price is not None else None

    return Position(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=_market_for_currency(currency),
        asset_class=detect_asset_class(symbol, display),
        symbol=symbol,
        name=display,
        currency=currency,
        quantity=quantity,
        cost_price=cost_price,
        last_price=parse_decimal(match.group("last_price")),
        market_value=parse_decimal(match.group("market_value")),
        cost_value=cost_value,
        unrealized_pnl=parse_decimal(match.group("unrealized_pnl")),
        confidence="high",
        notes="",
    )


def _match_multiline_values(line: str, section: str | None) -> re.Match[str] | None:
    if section == "基金":
        return re.fullmatch(
            rf"(?P<quantity>{NUMERIC})\s+"
            rf"(?P<cost_price>{NUMERIC})\s+"
            rf"(?P<last_price>{NUMERIC})\s+"
            rf"(?P<market_value>{NUMERIC})\s+"
            rf"(?P<unrealized_pnl>{NUMERIC})\s+"
            rf"(?P<initial_margin>{NUMERIC})\s+"
            rf"(?P<maintenance_margin>{NUMERIC})\s+"
            r"(?P<currency>[A-Z]{3})",
            line,
        )
    return re.fullmatch(
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


def _looks_like_values_line(line: str) -> bool:
    return bool(re.search(r"\b[A-Z]{3}$", line)) and bool(re.search(r"\d", line))


def _is_parenthesized_symbol(line: str) -> bool:
    return bool(re.fullmatch(r"\([A-Z0-9.]+\)", line))


def _parse_currency_cash_line(
    line: str,
    statement_id: str,
    currency: str | None,
) -> CashBalance | None:
    if currency is None:
        return None
    match = re.fullmatch(rf"期末现金\s+(?P<balance>{NUMERIC}).*", line)
    if match is None:
        return None
    balance = parse_decimal(match.group("balance")) or Decimal("0")
    return CashBalance(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        currency=currency,
        cash_balance=balance,
        available_balance=balance,
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


def _upsert_cash_balance(
    cash_balances: list[CashBalance],
    cash_balance: CashBalance,
) -> None:
    for index, existing in enumerate(cash_balances):
        if existing.currency == cash_balance.currency:
            cash_balances[index] = cash_balance
            return
    cash_balances.append(cash_balance)


def _normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


class TigerStatementParser(StatementParser):
    broker = BROKER

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result = parse_tiger_text(text, month)
            return replace(result, page_count=len(pdf.pages))
