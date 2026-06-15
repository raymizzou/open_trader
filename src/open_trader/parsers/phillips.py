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
    in_account_details = False

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        if _is_account_details_start(line):
            in_account_details = True
            in_positions = False
            in_cash = False
            continue

        if in_account_details:
            account_cash = _parse_account_cash_line(line, statement_id)
            if account_cash is not None:
                _upsert_cash_balance(cash_balances, account_cash)
                continue

        if (
            line == "Securities Portfolio"
            or "證券投資組合" in line
            or "证券投资组合" in line
            or "SSeeccuurriittiieess PPoorrttffoolliioo" in line
            or "股股票票投投資資組組合合" in line
        ):
            in_positions = True
            in_account_details = False
            in_cash = False
            continue
        if line.startswith(("產品 市場", "Product Market")):
            continue
        if line == "Cash Balance":
            in_positions = False
            in_account_details = False
            in_cash = True
            continue

        if in_positions:
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
    match = _match_stock_position_line(line) or _match_equity_position_line(line)
    if match is None:
        return None

    market = _detect_phillips_market(match.group("market"))
    symbol = _normalize_phillips_symbol(match.group("symbol"), market)
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
        notes="currency inferred from market in Phillips statement",
    )


def _match_stock_position_line(line: str) -> re.Match[str] | None:
    return re.fullmatch(
        r"(?:股票|Stock)\s+"
        r"(?P<market>HK|US|SEHK|NASDAQ|NYSE)\s+"
        r"(?P<symbol>[A-Z0-9.-]+)\s+"
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


def _match_equity_position_line(line: str) -> re.Match[str] | None:
    return re.fullmatch(
        r"Equity\s+"
        r"(?P<market>XHKG|XNAS|XNYS|US|HK)\s+"
        r"(?P<symbol>[A-Z0-9.-]+)\s+"
        r"(?P<name>.+?)\s+"
        rf"(?P<previous_quantity>{NUMERIC})\s+"
        r"(?P<last_buy_date>(?:\d{2}/\d{2}/\d{2}|\d{4}/\d{2}/\d{2}))\s+"
        rf"(?P<quantity>{NUMERIC})\s+"
        rf"(?P<last_price>{NUMERIC})\s+"
        rf"(?P<market_value>{NUMERIC})\s+"
        rf"(?P<margin_ratio>{NUMERIC})\s+"
        rf"(?P<margin_value>{NUMERIC})",
        line,
    )


def _detect_phillips_market(value: str) -> Market:
    if value == "XHKG":
        return Market.HK
    if value in {"XNAS", "XNYS"}:
        return Market.US
    return detect_market(value)


def _normalize_phillips_symbol(symbol: str, market: Market) -> str:
    normalized = symbol.upper()
    if market == Market.HK and re.fullmatch(r"0\d{5}", normalized):
        return normalized[-5:]
    return normalized


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


def _parse_account_cash_line(line: str, statement_id: str) -> CashBalance | None:
    match = re.fullmatch(
        rf"(?P<currency>[A-Z]{{3}})(?P<base>\(Base\))?\s+"
        rf"(?P<balance>{NUMERIC})\s+.*",
        line,
    )
    if match is None or match.group("base"):
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


def _is_account_details_start(line: str) -> bool:
    return (
        "Account Details" in line
        or "戶口資料" in line
        or "户口资料" in line
        or line.startswith("Currency Balance C/F")
        or line.startswith("貨幣 轉下結餘")
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


class PhillipsStatementParser(StatementParser):
    broker = BROKER

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result = parse_phillips_text(text, month)
            return replace(result, page_count=len(pdf.pages))
