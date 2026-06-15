from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import re

import pdfplumber

from open_trader.models import CashBalance, Position
from open_trader.parsers.base import (
    ParseResult,
    StatementParser,
    detect_asset_class,
    detect_market,
    parse_decimal,
    split_symbol_name,
)


BROKER = "futu"
ACCOUNT_ALIAS = "futu_main"
NUMERIC = r"(?:-?[\d,.]+|\([\d,.]+\))"
MULTIPLIER = rf"(?:-|{NUMERIC})"


def parse_futu_text(text: str, month: str) -> ParseResult:
    statement_id = f"{month}-{BROKER}"
    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    in_positions = False
    in_cash = False
    wrapped_position_line: str | None = None

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        if wrapped_position_line is not None:
            combined_line = _combine_wrapped_position_line(wrapped_position_line, line)
            wrapped_position_line = None
            position = _parse_position_line(combined_line, statement_id)
            if position is not None:
                positions.append(position)
            continue

        summary_cash = _parse_summary_cash_line(line, statement_id)
        if summary_cash is not None:
            _upsert_cash_balance(cash_balances, summary_cash)
            in_positions = False
            in_cash = False
            continue

        if "期末概覽-股票" in line or "期末概览-股票" in line:
            in_positions = True
            in_cash = False
            continue
        if "現金結餘" in line or "现金结余" in line:
            in_positions = False
            in_cash = True
            continue
        if line.startswith(("代碼名稱", "代码名称")):
            continue

        if in_positions:
            if _looks_like_wrapped_position_start(line):
                wrapped_position_line = line
                continue
            position = _parse_position_line(line, statement_id)
            if position is not None:
                positions.append(position)
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
    match = _match_position_line(line)
    if match is None or _has_unclosed_parenthesis(match.group("display")):
        return None

    symbol, name = split_symbol_name(match.group("display"))
    return Position(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=detect_market(match.group("market")),
        asset_class=detect_asset_class(symbol, name),
        symbol=symbol,
        name=name,
        currency=match.group("currency"),
        quantity=parse_decimal(match.group("quantity")) or Decimal("0"),
        cost_price=None,
        last_price=parse_decimal(match.group("last_price")),
        market_value=parse_decimal(match.group("market_value")),
        cost_value=None,
        unrealized_pnl=None,
        confidence="high",
        notes="",
    )


def _match_position_line(line: str) -> re.Match[str] | None:
    return re.fullmatch(
        r"(?P<display>.+?)\s+"
        r"(?P<market>US|SEHK|HK|HKEX|NASDAQ|NYSE)\s+"
        r"(?P<currency>[A-Z]{3})\s+"
        rf"(?P<quantity>{NUMERIC})\s+"
        rf"(?P<last_price>{NUMERIC})\s+"
        rf"(?P<multiplier>{MULTIPLIER})\s+"
        rf"(?P<market_value>{NUMERIC})\s+"
        rf"(?P<initial_margin>{NUMERIC})\s+"
        rf"(?P<maintenance_margin>{NUMERIC})\s+"
        rf"(?P<maintenance_rate>{NUMERIC})",
        line,
    )


def _looks_like_wrapped_position_start(line: str) -> bool:
    match = _match_position_line(line)
    return match is not None and _has_unclosed_parenthesis(match.group("display"))


def _has_unclosed_parenthesis(value: str) -> bool:
    return value.count("(") > value.count(")")


def _combine_wrapped_position_line(first_line: str, continuation: str) -> str:
    return re.sub(
        r"\s+(US|SEHK|HK|HKEX|NASDAQ|NYSE)\s+",
        f" {continuation} " + r"\1 ",
        first_line,
        count=1,
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


def _parse_summary_cash_line(line: str, statement_id: str) -> CashBalance | None:
    if not line.startswith(("現金結餘 ", "现金结余 ")):
        return None

    values = re.findall(NUMERIC, line)
    if len(values) < 2:
        return None

    balance_index = 1 if len(values) == 3 else 0
    balance = parse_decimal(values[balance_index]) or Decimal("0")
    return CashBalance(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        currency="HKD",
        cash_balance=balance,
        available_balance=balance,
        confidence="medium",
        notes="cash parsed from HKD statement summary",
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


class FutuStatementParser(StatementParser):
    broker = BROKER

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result = parse_futu_text(text, month)
            return replace(result, page_count=len(pdf.pages))
