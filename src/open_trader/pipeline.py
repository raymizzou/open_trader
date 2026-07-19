from __future__ import annotations

from dataclasses import dataclass
import csv
from datetime import UTC, date, datetime
from decimal import Decimal
from os import close
from pathlib import Path
import re
from shutil import copyfile, rmtree
from tempfile import mkdtemp, mkstemp
from typing import Iterable, Mapping
from uuid import uuid4

from .csv_io import write_rows
from .fx import StaticMonthEndFxProvider
from .models import (
    AssetClass,
    CashBalance,
    ManifestRecord,
    Market,
    Position,
    WarningRecord,
)
from .parsers.base import ParseResult, StatementParser, sha256_file
from .portfolio import (
    PORTFOLIO_FIELDNAMES,
    build_portfolio_rows,
    merge_eastmoney_portfolio_rows,
    replace_broker_portfolio_rows,
)


MANIFEST_FIELDNAMES = [
    "month",
    "broker",
    "source_file",
    "source_sha256",
    "parsed_at",
    "page_count",
    "parser_version",
    "status",
]

POSITION_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "quantity",
    "cost_price",
    "last_price",
    "market_value",
    "cost_value",
    "unrealized_pnl",
    "confidence",
    "notes",
]

CASH_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "currency",
    "cash_balance",
    "available_balance",
    "confidence",
    "notes",
]

WARNING_FIELDNAMES = [
    "statement_id",
    "broker",
    "page",
    "severity",
    "code",
    "message",
]

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class ImportResult:
    run_dir: Path
    portfolio_path: Path
    latest_path: Path
    positions_count: int
    cash_count: int
    warnings_count: int


def run_import(
    month: str,
    statement_paths: Mapping[str, Path],
    parsers: Iterable[StatementParser],
    data_dir: Path,
    fx_provider: StaticMonthEndFxProvider,
    update_latest: bool = True,
) -> ImportResult:
    validate_month(month)
    return _run_import(
        month=month,
        statement_period=month,
        run_name=month,
        statement_paths=statement_paths,
        parsers=parsers,
        data_dir=data_dir,
        latest_path=data_dir / "latest" / "portfolio.csv",
        fx_provider=fx_provider,
        update_latest=update_latest,
        replace_latest_broker=None,
    )


def run_uploaded_statement(
    *,
    statement_date: str,
    statement_path: Path,
    parser: StatementParser,
    data_dir: Path,
    portfolio_path: Path,
    fx_provider: StaticMonthEndFxProvider,
) -> ImportResult:
    try:
        parsed_date = date.fromisoformat(statement_date)
    except ValueError:
        raise ValueError(f"invalid statement date: {statement_date}") from None
    if parsed_date.isoformat() != statement_date:
        raise ValueError(f"invalid statement date: {statement_date}")
    return _run_import(
        month=statement_date[:7],
        statement_period=statement_date,
        run_name=statement_date[:7],
        statement_paths={parser.broker: statement_path},
        parsers=[parser],
        data_dir=data_dir,
        latest_path=portfolio_path,
        fx_provider=fx_provider,
        update_latest=True,
        replace_latest_broker=parser.broker,
    )


def _run_import(
    *,
    month: str,
    statement_period: str,
    run_name: str,
    statement_paths: Mapping[str, Path],
    parsers: Iterable[StatementParser],
    data_dir: Path,
    latest_path: Path,
    fx_provider: StaticMonthEndFxProvider,
    update_latest: bool,
    replace_latest_broker: str | None,
) -> ImportResult:
    parser_list = list(parsers)
    _validate_statement_paths(statement_paths, parser_list)

    run_dir = data_dir / "runs" / run_name
    latest_dir = latest_path.parent

    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    warnings: list[WarningRecord] = []
    manifest: list[ManifestRecord] = []
    if replace_latest_broker is not None:
        positions, cash_balances, warnings, manifest = _preserved_run_records(
            run_dir, replace_latest_broker
        )
    uploaded_positions: list[Position] = []
    uploaded_cash: list[CashBalance] = []

    for parser in parser_list:
        source_path = statement_paths[parser.broker]
        parsed_at = datetime.now(UTC).isoformat()
        parse_result = parser.parse(source_path, statement_period)
        _validate_parse_result_brokers(parser.broker, parse_result)

        positions.extend(parse_result.positions)
        cash_balances.extend(parse_result.cash_balances)
        uploaded_positions.extend(parse_result.positions)
        uploaded_cash.extend(parse_result.cash_balances)
        warnings.extend(parse_result.warnings)
        manifest.append(
            ManifestRecord(
                month=month,
                broker=parse_result.broker,
                source_file=str(source_path),
                source_sha256=sha256_file(source_path),
                parsed_at=parsed_at,
                page_count=parse_result.page_count,
                parser_version=parser.parser_version,
                status="parsed",
            )
        )

    portfolio_rows = build_portfolio_rows(month, positions, cash_balances, fx_provider)
    uploaded_portfolio_rows = build_portfolio_rows(
        month, uploaded_positions, uploaded_cash, fx_provider
    )
    latest_portfolio_rows = portfolio_rows

    eastmoney_mode = {parser.broker for parser in parser_list} == {"eastmoney"}
    if replace_latest_broker is not None and latest_path.exists():
        detail_dir = _latest_daily_detail_dir(data_dir)
        if detail_dir is not None:
            latest_positions, latest_cash, _, _ = _preserved_run_records(
                detail_dir, replace_latest_broker
            )
            latest_portfolio_rows = build_portfolio_rows(
                month,
                [*latest_positions, *uploaded_positions],
                [*latest_cash, *uploaded_cash],
                fx_provider,
            )
        else:
            with latest_path.open(newline="", encoding="utf-8") as handle:
                latest_portfolio_rows = replace_broker_portfolio_rows(
                    list(csv.DictReader(handle)),
                    uploaded_portfolio_rows,
                    replace_latest_broker,
                )
    elif eastmoney_mode and latest_path.exists():
        with latest_path.open(newline="", encoding="utf-8") as handle:
            portfolio_rows = merge_eastmoney_portfolio_rows(
                list(csv.DictReader(handle)), portfolio_rows
            )
    temp_run_dir = _make_temp_run_dir(run_dir)
    temp_latest_path: Path | None = None
    backup_latest_path: Path | None = None
    backup_run_dir: Path | None = None
    temp_run_promoted = False
    latest_replaced = False
    try:
        if update_latest:
            latest_dir.mkdir(parents=True, exist_ok=True)
            temp_latest_path = _make_temp_latest_path(latest_path)
        write_rows(
            temp_run_dir / "manifest.csv",
            MANIFEST_FIELDNAMES,
            (_manifest_to_row(record) for record in manifest),
        )
        write_rows(
            temp_run_dir / "extracted_positions.csv",
            POSITION_FIELDNAMES,
            (_position_to_row(position) for position in positions),
        )
        write_rows(
            temp_run_dir / "extracted_cash.csv",
            CASH_FIELDNAMES,
            (_cash_to_row(cash) for cash in cash_balances),
        )
        write_rows(
            temp_run_dir / "parse_warnings.csv",
            WARNING_FIELDNAMES,
            (warning.to_row() for warning in warnings),
        )
        write_rows(temp_run_dir / "portfolio.csv", PORTFOLIO_FIELDNAMES, portfolio_rows)

        if update_latest:
            assert temp_latest_path is not None
            if latest_portfolio_rows is portfolio_rows:
                copyfile(temp_run_dir / "portfolio.csv", temp_latest_path)
            else:
                write_rows(
                    temp_latest_path,
                    PORTFOLIO_FIELDNAMES,
                    latest_portfolio_rows,
                )
            if latest_path.exists():
                backup_latest_path = _make_backup_latest_path(latest_path)
                latest_path.rename(backup_latest_path)
            temp_latest_path.replace(latest_path)
            latest_replaced = True

        if run_dir.exists():
            backup_run_dir = _make_backup_run_dir(run_dir)
            run_dir.rename(backup_run_dir)
        temp_run_dir.rename(run_dir)
        temp_run_promoted = True
        if backup_run_dir is not None and backup_run_dir.exists():
            rmtree(backup_run_dir)
        if backup_latest_path is not None and backup_latest_path.exists():
            _best_effort_unlink(backup_latest_path)
    except Exception:
        _rollback_failed_promotion(
            run_dir=run_dir,
            temp_run_dir=temp_run_dir,
            temp_latest_path=temp_latest_path,
            latest_path=latest_path,
            backup_latest_path=backup_latest_path,
            backup_run_dir=backup_run_dir,
            temp_run_promoted=temp_run_promoted,
            latest_replaced=latest_replaced,
        )
        raise

    portfolio_path = run_dir / "portfolio.csv"

    return ImportResult(
        run_dir=run_dir,
        portfolio_path=portfolio_path,
        latest_path=latest_path,
        positions_count=(
            sum(row["asset_class"] != "cash" for row in portfolio_rows)
            if eastmoney_mode and replace_latest_broker is None
            else len(uploaded_positions)
        ),
        cash_count=len(uploaded_cash),
        warnings_count=len(warnings),
    )


def _latest_daily_detail_dir(data_dir: Path) -> Path | None:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return None
    candidates = [
        path
        for path in runs_dir.iterdir()
        if path.is_dir()
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)
        and (
            (path / "extracted_positions.csv").is_file()
            or (path / "extracted_cash.csv").is_file()
        )
    ]
    return max(candidates, key=lambda path: path.name) if candidates else None


def _preserved_run_records(
    run_dir: Path,
    target_broker: str,
) -> tuple[
    list[Position],
    list[CashBalance],
    list[WarningRecord],
    list[ManifestRecord],
]:
    target = target_broker.strip().lower()
    positions = [
        _position_from_row(row)
        for row in _read_rows(run_dir / "extracted_positions.csv")
        if _detail_broker(row) != target
    ]
    cash = [
        _cash_from_row(row)
        for row in _read_rows(run_dir / "extracted_cash.csv")
        if _detail_broker(row) != target
    ]
    warnings = [
        WarningRecord(
            statement_id=row.get("statement_id", ""),
            broker=row.get("broker", ""),
            page=int(row["page"]) if row.get("page", "").strip() else None,
            severity=row.get("severity", ""),
            code=row.get("code", ""),
            message=row.get("message", ""),
        )
        for row in _read_rows(run_dir / "parse_warnings.csv")
        if row.get("broker", "").strip().lower() != target
    ]
    manifest = [
        ManifestRecord(
            month=row.get("month", ""),
            broker=row.get("broker", ""),
            source_file=row.get("source_file", ""),
            source_sha256=row.get("source_sha256", ""),
            parsed_at=row.get("parsed_at", ""),
            page_count=int(row.get("page_count", "0")),
            parser_version=row.get("parser_version", ""),
            status=row.get("status", ""),
        )
        for row in _read_rows(run_dir / "manifest.csv")
        if row.get("broker", "").strip().lower() != target
    ]
    return positions, cash, warnings, manifest


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _detail_broker(row: dict[str, str]) -> str:
    broker = row.get("broker", "").strip().lower()
    if not broker or ";" in broker or "," in broker:
        raise ValueError(f"invalid detailed broker: {broker or 'missing'}")
    return broker


def _position_from_row(row: dict[str, str]) -> Position:
    return Position(
        statement_id=row.get("statement_id", ""),
        broker=_detail_broker(row),
        account_alias=row.get("account_alias", ""),
        market=_enum_or_default(Market, row.get("market", ""), Market.OTHER),
        asset_class=_enum_or_default(
            AssetClass, row.get("asset_class", ""), AssetClass.UNKNOWN
        ),
        symbol=row.get("symbol", ""),
        name=row.get("name", ""),
        currency=row.get("currency", "").upper(),
        quantity=Decimal(row["quantity"]),
        cost_price=_optional_decimal(row.get("cost_price", "")),
        last_price=_optional_decimal(row.get("last_price", "")),
        market_value=_optional_decimal(row.get("market_value", "")),
        cost_value=_optional_decimal(row.get("cost_value", "")),
        unrealized_pnl=_optional_decimal(row.get("unrealized_pnl", "")),
        confidence=_confidence(row.get("confidence", "")),
        notes=row.get("notes", ""),
    )


def _cash_from_row(row: dict[str, str]) -> CashBalance:
    return CashBalance(
        statement_id=row.get("statement_id", ""),
        broker=_detail_broker(row),
        account_alias=row.get("account_alias", ""),
        currency=row.get("currency", "").upper(),
        cash_balance=Decimal(row["cash_balance"]),
        available_balance=_optional_decimal(row.get("available_balance", "")),
        confidence=_confidence(row.get("confidence", "")),
        notes=row.get("notes", ""),
    )


def _optional_decimal(value: str) -> Decimal | None:
    return Decimal(value) if value.strip() else None


def _confidence(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if normalized in {"high", "medium", "low"} else "low"


def _enum_or_default(enum_type, value: str, default):
    try:
        return enum_type(value.strip())
    except ValueError:
        return default


def validate_month(month: str) -> str:
    if not MONTH_PATTERN.fullmatch(month):
        raise ValueError(f"invalid month {month!r}; expected YYYY-MM")
    return month


def _make_backup_run_dir(run_dir: Path) -> Path:
    return _unique_sibling_path(run_dir, "backup")


def _make_backup_latest_path(latest_path: Path) -> Path:
    return _unique_sibling_path(latest_path, "backup")


def _make_failed_run_dir(run_dir: Path) -> Path:
    return _unique_sibling_path(run_dir, "failed")


def _unique_sibling_path(path: Path, suffix: str) -> Path:
    return path.parent / f".{path.name}.{uuid4().hex}.{suffix}"


def _make_temp_latest_path(latest_path: Path) -> Path:
    file_descriptor, name = mkstemp(
        prefix=".portfolio.",
        suffix=".tmp",
        dir=latest_path.parent,
    )
    close(file_descriptor)
    return Path(name)


def _make_temp_run_dir(run_dir: Path) -> Path:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        mkdtemp(
            prefix=f".{run_dir.name}.",
            suffix=".tmp",
            dir=run_dir.parent,
        )
    )


def _rollback_failed_promotion(
    *,
    run_dir: Path,
    temp_run_dir: Path,
    temp_latest_path: Path | None,
    latest_path: Path,
    backup_latest_path: Path | None,
    backup_run_dir: Path | None,
    temp_run_promoted: bool,
    latest_replaced: bool,
) -> None:
    _restore_latest_after_failure(
        latest_path=latest_path,
        backup_latest_path=backup_latest_path,
        latest_replaced=latest_replaced,
    )

    failed_run_dir: Path | None = None
    if temp_run_promoted and run_dir.exists():
        failed_run_dir = _make_failed_run_dir(run_dir)
        try:
            run_dir.rename(failed_run_dir)
        except Exception:
            failed_run_dir = None
            if backup_run_dir is None:
                _best_effort_rmtree(run_dir)

    if backup_run_dir is not None and backup_run_dir.exists():
        if run_dir.exists():
            _best_effort_rmtree(run_dir)
        if not run_dir.exists():
            try:
                backup_run_dir.rename(run_dir)
            except Exception:
                pass

    if failed_run_dir is not None and failed_run_dir.exists():
        _best_effort_rmtree(failed_run_dir)
    if temp_run_dir.exists():
        _best_effort_rmtree(temp_run_dir)
    if temp_latest_path is not None and temp_latest_path.exists():
        _best_effort_unlink(temp_latest_path)
    if backup_latest_path is not None and backup_latest_path.exists():
        _best_effort_unlink(backup_latest_path)


def _restore_latest_after_failure(
    *,
    latest_path: Path,
    backup_latest_path: Path | None,
    latest_replaced: bool,
) -> None:
    if backup_latest_path is not None and backup_latest_path.exists():
        if latest_path.exists():
            _best_effort_unlink(latest_path)
        try:
            backup_latest_path.rename(latest_path)
        except Exception:
            pass
    elif latest_replaced and latest_path.exists():
        _best_effort_unlink(latest_path)


def _best_effort_rmtree(path: Path) -> None:
    try:
        rmtree(path)
    except Exception:
        pass


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


def _validate_statement_paths(
    statement_paths: Mapping[str, Path],
    parsers: list[StatementParser],
) -> None:
    parser_broker_list = [parser.broker for parser in parsers]
    duplicate_brokers = sorted(
        broker
        for broker in set(parser_broker_list)
        if parser_broker_list.count(broker) > 1
    )
    if duplicate_brokers:
        raise ValueError(
            f"duplicate parser broker(s): {', '.join(duplicate_brokers)}"
        )

    parser_brokers = set(parser_broker_list)
    path_brokers = set(statement_paths)
    missing = sorted(parser_brokers - path_brokers)
    if missing:
        raise ValueError(f"missing statement path for broker(s): {', '.join(missing)}")

    unknown = sorted(path_brokers - parser_brokers)
    if unknown:
        raise ValueError(f"unknown statement path broker(s): {', '.join(unknown)}")


def _validate_parse_result_brokers(
    expected_broker: str,
    parse_result: ParseResult,
) -> None:
    result_broker = parse_result.broker
    if result_broker != expected_broker:
        raise ValueError(
            f"parser broker {expected_broker} returned result broker {result_broker}"
        )

    for collection_name in ("positions", "cash_balances", "warnings"):
        for record in getattr(parse_result, collection_name):
            if record.broker != expected_broker:
                raise ValueError(
                    f"parser broker {expected_broker} emitted {collection_name} "
                    f"record for broker {record.broker}"
                )


def _manifest_to_row(record: ManifestRecord) -> dict[str, str]:
    return {
        "month": record.month,
        "broker": record.broker,
        "source_file": record.source_file,
        "source_sha256": record.source_sha256,
        "parsed_at": record.parsed_at,
        "page_count": str(record.page_count),
        "parser_version": record.parser_version,
        "status": record.status,
    }


def _position_to_row(position: Position) -> dict[str, str]:
    return {
        "statement_id": position.statement_id,
        "broker": position.broker,
        "account_alias": position.account_alias,
        "market": position.market.value,
        "asset_class": position.asset_class.value,
        "symbol": position.symbol,
        "name": position.name,
        "currency": position.currency,
        "quantity": _decimal_to_str(position.quantity),
        "cost_price": _decimal_to_str(position.cost_price),
        "last_price": _decimal_to_str(position.last_price),
        "market_value": _decimal_to_str(position.market_value),
        "cost_value": _decimal_to_str(position.cost_value),
        "unrealized_pnl": _decimal_to_str(position.unrealized_pnl),
        "confidence": position.confidence,
        "notes": position.notes,
    }


def _cash_to_row(cash: CashBalance) -> dict[str, str]:
    return {
        "statement_id": cash.statement_id,
        "broker": cash.broker,
        "account_alias": cash.account_alias,
        "currency": cash.currency,
        "cash_balance": _decimal_to_str(cash.cash_balance),
        "available_balance": _decimal_to_str(cash.available_balance),
        "confidence": cash.confidence,
        "notes": cash.notes,
    }


def _decimal_to_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")
