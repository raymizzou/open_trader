from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import pdfplumber

from open_trader.models import (
    AssetClass,
    CashBalance,
    Market,
    Position,
    StatementTrade,
)
from open_trader.parsers.base import ParseResult, StatementParser, parse_decimal


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
TRANSACTION_HEADER = (
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
MONEY = r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
PRINT_DATE = re.compile(r"打印日期\s*[:：]\s*(\d{4}-\d{2}-\d{2})")


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
    trades = _parse_trades(tables, statement_id)
    total_assets = _extract_money(first_page_text, "总资产")
    available_balance = _extract_money(first_page_text, "资金可用")
    securities_value = sum(
        (position.market_value or Decimal("0") for position in positions),
        Decimal("0"),
    )
    cash_balance = None if total_assets is None else total_assets - securities_value
    if cash_balance is None or cash_balance < 0 or available_balance is None:
        raise ValueError("东方财富对账单缺少人民币资金汇总")

    return ParseResult(
        statement_id=statement_id,
        broker=BROKER,
        positions=positions,
        trades=trades,
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
    )


def _parse_trades(
    tables: list[list[list[str | None]]], statement_id: str
) -> list[StatementTrade]:
    trades: list[StatementTrade] = []
    occurrences: dict[str, int] = {}
    for table in tables:
        if not table or _normalize_row(table[0]) != TRANSACTION_HEADER:
            continue
        for row in table[1:]:
            normalized = tuple(_normalize_cell(cell) for cell in row)
            if normalized == TRANSACTION_HEADER:
                continue
            if len(normalized) != len(TRANSACTION_HEADER):
                raise ValueError("东方财富对账单包含无效成交行")
            (
                traded_date,
                side_label,
                symbol,
                _name,
                quantity_raw,
                price_raw,
                total_raw,
                commission_raw,
                stamp_duty_raw,
                transfer_fee_raw,
                _cash_balance,
            ) = normalized
            side = {"证券买入": "buy", "证券卖出": "sell"}.get(side_label)
            if side is None:
                continue
            quantity = parse_decimal(quantity_raw)
            price = parse_decimal(price_raw)
            total = parse_decimal(total_raw)
            fee_parts = [
                parse_decimal(commission_raw),
                parse_decimal(stamp_duty_raw),
                parse_decimal(transfer_fee_raw),
            ]
            try:
                parsed_date = datetime.strptime(traded_date, "%Y%m%d").date()
            except ValueError:
                parsed_date = None
            if (
                parsed_date is None
                or re.fullmatch(r"\d{6}", symbol) is None
                or quantity is None
                or quantity <= 0
                or price is None
                or price <= 0
                or total is None
                or any(part is None or part < 0 for part in fee_parts)
            ):
                raise ValueError("东方财富对账单包含无效成交行")
            fee = sum((part for part in fee_parts if part is not None), Decimal("0"))
            notional = quantity * price
            expected_total = -(notional + fee) if side == "buy" else notional - fee
            if abs(total - expected_total) > Decimal("0.02"):
                raise ValueError("东方财富成交金额与数量、价格及费用不一致")
            fingerprint = "|".join(normalized)
            occurrences[fingerprint] = occurrences.get(fingerprint, 0) + 1
            reference = (
                sha256(fingerprint.encode("utf-8")).hexdigest()
                + f":{occurrences[fingerprint]}"
            )
            traded_at = datetime(
                parsed_date.year,
                parsed_date.month,
                parsed_date.day,
                15,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).isoformat()
            trades.append(
                StatementTrade(
                    statement_id=statement_id,
                    broker=BROKER,
                    account_alias=ACCOUNT_ALIAS,
                    market=Market.CN,
                    symbol=symbol,
                    currency="CNY",
                    side=side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    costs_complete=True,
                    traded_at=traded_at,
                    reference=reference,
                    execution_granularity="statement_trade_date",
                    statement_sequence=len(trades) + 1,
                )
            )
    return trades


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
