from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import re

import pdfplumber

from open_trader.models import (
    AssetClass,
    CashBalance,
    Market,
    Position,
    TradeFill,
    WarningRecord,
)
from open_trader.parsers.base import (
    ParseResult,
    StatementParser,
    detect_asset_class,
    detect_market,
    parse_decimal,
    source_id_for_fill,
)


BROKER = "phillips"
ACCOUNT_ALIAS = "phillips_main"
NUMERIC = r"(?:-?[\d,.]+|\([\d,.]+\))"
ISSUE_DATE = re.compile(
    r"(?:日期\s*)?Issue Date\s*[:：]\s*(\d{2})/(\d{2})/(\d{2})",
    re.IGNORECASE,
)
TRANSACTION_LINE = re.compile(
    rf"(?P<trade_date>\d{{2}}/\d{{2}}/\d{{2}})\s+"
    rf"(?P<settlement_date>\d{{2}}/\d{{2}}/\d{{2}})\s+"
    r"Equity\s+(?P<reference>[A-Z0-9@#*.-]+)\s+"
    r"(?P<side>Bought|Sold)\s+"
    r"(?P<description>.+?)\s+"
    rf"(?P<quantity>{NUMERIC})\s+"
    rf"(?P<price>{NUMERIC})\s+"
    rf"(?P<turnover>{NUMERIC})\s+"
    rf"(?P<amount>{NUMERIC})",
    re.IGNORECASE,
)


def parse_phillips_text(text: str, month: str) -> ParseResult:
    statement_id = f"{month}-{BROKER}"
    position_products = _position_products_by_name(text, statement_id)
    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    base_cash: CashBalance | None = None
    fills: list[TradeFill] = []
    warnings: list[WarningRecord] = []
    occurrences: dict[str, int] = {}
    in_transactions = False
    in_positions = False
    in_cash = False
    in_account_details = False

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        if "Transaction Details" in line or "TTrraannssaaccttiioonn DDeettaaiillss" in line:
            in_transactions = True
            continue

        if in_transactions:
            if not _is_transaction_candidate(line):
                fill = None
            else:
                occurrence = occurrences.get(line, 0)
                occurrences[line] = occurrence + 1
                match = TRANSACTION_LINE.fullmatch(line)
                if match is not None and _looks_like_us_execution(
                    match.group("description"), position_products
                ):
                    warnings.append(
                        WarningRecord(
                            statement_id=statement_id,
                            broker=BROKER,
                            page=None,
                            severity="warning",
                            code="unsupported_execution_market",
                            message="辉立成交行不是香港市场成交",
                        )
                    )
                    continue
                fill = _parse_transaction_line(line, occurrence, position_products)
            if fill is not None:
                fills.append(fill)
                continue
            if _is_transaction_candidate(line):
                warnings.append(
                    WarningRecord(
                        statement_id=statement_id,
                        broker=BROKER,
                        page=None,
                        severity="warning",
                        code="invalid_execution_row",
                        message="辉立成交行缺少成交标识、方向、数量、价格或日期",
                    )
                )
                continue

        if _is_account_details_start(line):
            in_transactions = False
            in_account_details = True
            in_positions = False
            in_cash = False
            continue

        if in_account_details:
            account_cash = _parse_account_cash_line(line, statement_id)
            if account_cash is not None:
                if "HKD(Base)" in account_cash.notes:
                    base_cash = account_cash
                elif base_cash is None:
                    _upsert_cash_balance(cash_balances, account_cash)
                continue

        if (
            line == "Securities Portfolio"
            or "證券投資組合" in line
            or "证券投资组合" in line
            or "股票投資組合" in line
            or "股票投资组合" in line
            or "SSeeccuurriittiieess PPoorrttffoolliioo" in line
            or "股股票票投投資資組組合合" in line
        ):
            in_transactions = False
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
            if position is not None and (
                position.quantity != 0 or position.market_value != 0
            ):
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
        cash_balances=[base_cash] if base_cash is not None else cash_balances,
        fills=fills,
        warnings=warnings,
    )


def _parse_transaction_line(
    line: str,
    occurrence: int,
    position_products: dict[str, set[tuple[Market, str]]],
) -> TradeFill | None:
    match = TRANSACTION_LINE.fullmatch(line)
    if match is None:
        return None
    quantity = parse_decimal(match.group("quantity"))
    price = parse_decimal(match.group("price"))
    try:
        executed_at = datetime.strptime(match.group("trade_date"), "%d/%m/%y").date().isoformat()
    except ValueError:
        executed_at = None
    symbol = _resolve_execution_symbol(match.group("description"), position_products)
    side = "BUY" if match.group("side").upper() == "BOUGHT" else "SELL"
    if (
        symbol is None
        or quantity is None
        or quantity <= 0
        or price is None
        or price <= 0
        or executed_at is None
    ):
        return None
    market = Market.HK
    return TradeFill(
        source_id=source_id_for_fill(BROKER, [line, str(occurrence)]),
        source_order_id=match.group("reference"),
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=market,
        symbol=symbol,
        currency=_currency_for_market(market),
        side=side,
        quantity=quantity,
        price=price,
        fees=None,
        executed_at=executed_at,
    )


def _is_transaction_candidate(line: str) -> bool:
    return re.search(r"(?:^|\s)Equity(?:\s|$)", line, re.IGNORECASE) is not None


def _is_hk_execution_symbol(symbol: str) -> bool:
    return re.fullmatch(r"0?\d{1,5}", symbol) is not None


def _position_products_by_name(
    text: str,
    statement_id: str,
) -> dict[str, set[tuple[Market, str]]]:
    products: dict[str, set[tuple[Market, str]]] = {}
    for raw_line in text.splitlines():
        position = _parse_position_line(_normalize_line(raw_line), statement_id)
        if position is not None and position.name:
            products.setdefault(_normalize_line(position.name).upper(), set()).add(
                (position.market, position.symbol)
            )
    return products


def _resolve_execution_symbol(
    description: str,
    position_products: dict[str, set[tuple[Market, str]]],
) -> str | None:
    normalized = _normalize_line(description).upper()
    first = normalized.split()[0] if normalized else ""
    if _is_hk_execution_symbol(first):
        return _normalize_phillips_symbol(first, Market.HK).zfill(5)
    products = position_products.get(normalized, set())
    if len(products) != 1:
        return None
    market, symbol = next(iter(products))
    return symbol if market is Market.HK else None


def _looks_like_us_execution(
    description: str,
    position_products: dict[str, set[tuple[Market, str]]],
) -> bool:
    products = position_products.get(_normalize_line(description).upper(), set())
    return len(products) == 1 and next(iter(products))[0] is Market.US


def _parse_position_line(line: str, statement_id: str) -> Position | None:
    ut_match = _match_unit_trust_position_line(line)
    if ut_match is not None:
        market = _detect_phillips_market(ut_match.group("market"))
        symbol = ut_match.group("symbol").upper()
        return Position(
            statement_id=statement_id,
            broker=BROKER,
            account_alias=ACCOUNT_ALIAS,
            market=market,
            asset_class=AssetClass.MONEY_MARKET_FUND,
            symbol=symbol,
            name=ut_match.group("name").strip(),
            currency=_currency_for_market(market),
            quantity=parse_decimal(ut_match.group("quantity")) or Decimal("0"),
            cost_price=None,
            last_price=parse_decimal(ut_match.group("last_price")),
            market_value=parse_decimal(ut_match.group("market_value")),
            cost_value=None,
            unrealized_pnl=None,
            confidence="medium",
            notes="currency inferred from market in Phillips statement",
        )

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


def _match_unit_trust_position_line(line: str) -> re.Match[str] | None:
    return re.fullmatch(
        r"UT\s+"
        r"(?P<market>OTCU|XHKG|HK)\s+"
        r"(?P<symbol>[A-Z0-9.-]+)\s+"
        r"(?P<name>.+?)\s+"
        rf"(?P<previous_quantity>{NUMERIC})\s+"
        rf"(?P<quantity>{NUMERIC})\s+"
        rf"(?P<last_price>{NUMERIC})\s+"
        rf"(?P<market_value>{NUMERIC})\s+"
        rf"(?P<margin_ratio>{NUMERIC})\s+"
        rf"(?P<margin_value>{NUMERIC})",
        line,
    )


def _detect_phillips_market(value: str) -> Market:
    if value in {"XHKG", "OTCU"}:
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
    if match is None:
        return None

    balance = parse_decimal(match.group("balance")) or Decimal("0")
    return CashBalance(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        currency="HKD" if match.group("base") else match.group("currency"),
        cash_balance=balance,
        available_balance=balance,
        confidence="high",
        notes="statement HKD(Base)" if match.group("base") else "",
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

    def statement_date(self, path: Path) -> str:
        with pdfplumber.open(path) as pdf:
            text = pdf.pages[0].extract_text() if pdf.pages else ""
        match = ISSUE_DATE.search(text or "")
        if match is None:
            raise ValueError("辉立结单缺少 Issue Date")
        day, month, year = match.groups()
        try:
            return date(2000 + int(year), int(month), int(day)).isoformat()
        except ValueError:
            raise ValueError("辉立结单包含无效 Issue Date") from None

    def parse(self, path: Path, month: str) -> ParseResult:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result = parse_phillips_text(text, month)
            return replace(result, page_count=len(pdf.pages))
