from __future__ import annotations

from dataclasses import replace
from datetime import date
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
    parse_decimal,
    source_id_for_fill,
)


BROKER = "eastmoney"
ACCOUNT_ALIAS = "eastmoney_main"
POSITION_HEADER = (
    "交易市场",
    "证券代码",
    "证券名称",
    "持仓数量",
    "市价",
    "成本价",
    "证券市值",
)
EXECUTION_HEADER = (
    "发生日期",
    "买卖类别",
    "证券代码",
    "证券名称",
    "成交数量",
    "成交价格",
    "总发生金额",
    "手续费",
    "印花税",
    "过户费",
    "资金余额",
)
SUPPORTED_MARKETS = {"沪市A股", "深市A股"}
NON_TRADE_EXECUTION_CATEGORIES = {
    "证券红利",
    "证券转入",
    "证券转出",
    "红利入账",
    "天天宝申购",
    "天天宝赎回",
    "银行转证券",
    "证券转银行",
    "利息归本",
}
MONEY = r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
PRINT_DATE = re.compile(r"打印日期\s*[:：]\s*(\d{4}-\d{2}-\d{2})")
QUERY_INTERVAL = re.compile(
    r"查询区间\s*[:：]\s*(\d{4}/\d{2}/\d{2})\s*-\s*(\d{4}/\d{2}/\d{2})"
)


def parse_eastmoney_page(
    first_page_text: str,
    tables: list[list[list[str | None]]],
    month: str,
) -> ParseResult:
    table = next(
        (
            candidate
            for candidate in tables
            if candidate and _normalize_row(candidate[0]) == POSITION_HEADER
        ),
        None,
    )
    if table is None:
        raise ValueError("东方财富对账单缺少汇总股票资料表")

    statement_id = f"{month}-{BROKER}"
    positions = [
        position
        for row in table[1:]
        if (position := _parse_position(row, statement_id)) is not None
    ]
    total_assets = _extract_money(first_page_text, "总资产")
    available_balance = _extract_money(first_page_text, "资金可用")
    securities_value = sum(
        (position.market_value or Decimal("0") for position in positions),
        Decimal("0"),
    )
    cash_balance = None if total_assets is None else total_assets - securities_value
    if cash_balance is None or cash_balance < 0 or available_balance is None:
        raise ValueError("东方财富对账单缺少人民币资金汇总")

    fills: list[TradeFill] = []
    warnings: list[WarningRecord] = []
    occurrences: dict[tuple[str, ...], int] = {}
    sequence_by_group: dict[tuple[str, str], int] = {}
    execution_tables = tuple(
        candidate
        for candidate in tables
        if candidate and _normalize_row(candidate[0]) == EXECUTION_HEADER
    )
    fills_complete = bool(execution_tables)
    for execution_table in execution_tables:
        for row in execution_table[1:]:
            values = _normalize_row(row)
            category = values[1] if len(values) > 1 else ""
            occurrence = occurrences.get(values, 0)
            occurrences[values] = occurrence + 1
            fill = _parse_fill(row, occurrence)
            if fill is not None:
                group = (fill.symbol, fill.executed_at)
                source_sequence = sequence_by_group.get(group, 0)
                sequence_by_group[group] = source_sequence + 1
                fill = replace(fill, source_sequence=source_sequence)
                fills.append(fill)
            elif (
                any(values)
                and category not in NON_TRADE_EXECUTION_CATEGORIES
            ):
                fills_complete = False
                warnings.append(
                    WarningRecord(
                        statement_id=statement_id,
                        broker=BROKER,
                        page=None,
                        severity="warning",
                        code="invalid_execution_row",
                        message="东方财富成交行缺少成交标识、方向、数量、价格或日期",
                    )
                )

    fills_coverage_start, fills_coverage_end = _fill_coverage_interval(
        first_page_text
    )
    return ParseResult(
        statement_id=statement_id,
        broker=BROKER,
        positions=positions,
        cash_balances=[
            CashBalance(
                statement_id=statement_id,
                broker=BROKER,
                account_alias=ACCOUNT_ALIAS,
                currency="CNY",
                cash_balance=cash_balance,
                available_balance=available_balance,
                confidence="high",
                notes="cash derived from statement total assets less securities value",
            )
        ],
        fills=fills,
        fills_complete=fills_complete,
        fills_coverage_start=fills_coverage_start,
        fills_coverage_end=fills_coverage_end,
        warnings=warnings,
    )


def _parse_fill(row: list[str | None], occurrence: int) -> TradeFill | None:
    if len(row) != len(EXECUTION_HEADER):
        return None
    values = [_normalize_cell(cell) for cell in row]
    executed_at = _execution_date(values[0])
    side = {"证券买入": "BUY", "证券卖出": "SELL"}.get(values[1])
    quantity = parse_decimal(values[4])
    price = parse_decimal(values[5])
    if (
        executed_at is None
        or side is None
        or re.fullmatch(r"\d{6}", values[2]) is None
        or quantity is None
        or quantity <= 0
        or price is None
        or price <= 0
    ):
        return None
    fee_parts = [parse_decimal(value) for value in values[7:10]]
    fees = (
        sum(fee_parts, Decimal("0"))
        if all(fee is not None for fee in fee_parts)
        else None
    )
    return TradeFill(
        source_id=source_id_for_fill(BROKER, [*values, str(occurrence)]),
        source_order_id=None,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=Market.CN,
        symbol=values[2],
        currency="CNY",
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        executed_at=executed_at,
    )


def _execution_date(value: str) -> str | None:
    try:
        return date.fromisoformat(
            f"{value[:4]}-{value[4:6]}-{value[6:]}" if re.fullmatch(r"\d{8}", value) else value
        ).isoformat()
    except ValueError:
        return None


def _fill_coverage_interval(text: str) -> tuple[str | None, str | None]:
    match = QUERY_INTERVAL.search(text)
    if match is None:
        return None, None
    try:
        start = date.fromisoformat(match.group(1).replace("/", "-"))
        end = date.fromisoformat(match.group(2).replace("/", "-"))
    except ValueError:
        raise ValueError("东方财富对账单包含无效查询区间") from None
    if start > end:
        raise ValueError("东方财富对账单包含无效查询区间")
    return start.isoformat(), end.isoformat()


def _parse_position(row: list[str | None], statement_id: str) -> Position | None:
    if len(row) != len(POSITION_HEADER):
        raise ValueError("东方财富汇总股票资料包含无效持仓行")

    market_label, symbol, name, quantity_raw, price_raw, cost_raw, value_raw = (
        _normalize_cell(cell) for cell in row
    )
    quantity = parse_decimal(quantity_raw)
    last_price = parse_decimal(price_raw)
    cost_price = parse_decimal(cost_raw)
    market_value = parse_decimal(value_raw)
    if (
        market_label not in SUPPORTED_MARKETS
        or re.fullmatch(r"\d{6}", symbol) is None
        or quantity is None
        or quantity < 0
        or last_price is None
        or cost_price is None
        or market_value is None
    ):
        raise ValueError("东方财富汇总股票资料包含无效持仓行")
    if quantity == 0:
        if market_value != 0:
            raise ValueError("东方财富汇总股票资料包含无效持仓行")
        return None

    cost_value = quantity * cost_price
    return Position(
        statement_id=statement_id,
        broker=BROKER,
        account_alias=ACCOUNT_ALIAS,
        market=Market.CN,
        asset_class=AssetClass.STOCK,
        symbol=symbol,
        name=name,
        currency="CNY",
        quantity=quantity,
        cost_price=cost_price,
        last_price=last_price,
        market_value=market_value,
        cost_value=cost_value,
        unrealized_pnl=market_value - cost_value,
        confidence="high",
        notes="",
    )


def _extract_money(text: str, label: str) -> Decimal | None:
    match = re.search(
        rf"{re.escape(label)}\s*\(RMB\)\s*[:：]\s*({MONEY})",
        text,
    )
    return parse_decimal(match.group(1)) if match else None


def _normalize_row(row: list[str | None]) -> tuple[str, ...]:
    return tuple(_normalize_cell(cell) for cell in row)


def _normalize_cell(cell: str | None) -> str:
    return re.sub(r"\s+", "", cell or "")


class _EmptyStatementError(Exception):
    pass


class EastmoneyStatementParser(StatementParser):
    broker = BROKER

    def __init__(self, password: str):
        self._password = password

    def statement_date(self, path: Path) -> str:
        try:
            with pdfplumber.open(path, password=self._password) as pdf:
                if not pdf.pages:
                    raise _EmptyStatementError
                text = pdf.pages[0].extract_text() or ""
        except _EmptyStatementError:
            raise ValueError("东方财富对账单没有页面") from None
        except Exception:
            raise ValueError("无法打开或解密东方财富对账单") from None
        match = PRINT_DATE.search(text)
        if match is None:
            raise ValueError("东方财富对账单缺少打印日期")
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            raise ValueError("东方财富对账单包含无效打印日期") from None

    def parse(self, path: Path, month: str) -> ParseResult:
        try:
            with pdfplumber.open(path, password=self._password) as pdf:
                if not pdf.pages:
                    raise _EmptyStatementError
                page_count = len(pdf.pages)
                first_page_text = pdf.pages[0].extract_text() or ""
                tables = [
                    table
                    for page in pdf.pages
                    for table in page.extract_tables()
                ]
        except _EmptyStatementError:
            raise ValueError("东方财富对账单没有页面") from None
        except Exception:
            raise ValueError("无法打开或解密东方财富对账单") from None

        return replace(
            parse_eastmoney_page(first_page_text, tables, month),
            page_count=page_count,
        )
